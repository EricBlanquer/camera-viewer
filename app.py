#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import os
import signal
import subprocess
import sys
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import BinaryIO

from PyQt6.QtCore import QSettings, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from okam_native.account import AccountDevice, AccountError, Eye4AccountClient
from okam_native.cs2 import CS2Error, CS2Session, make_cgi_request, read_command_result, write_command
from okam_native.p2p import (
    P2PError,
    get_service_parameter,
    open_stream_process,
    resolve_client_id,
    select_camera_password,
)
from okam_native.wakeup import WakeError, load_wake_credentials, wake_camera


ROOT = Path(__file__).resolve().parent
HELPER = ROOT / "bin" / "okam-amd64-connect"
WAKE_SOURCE = Path.home() / ".local/share/okam-linux/vendor/device_wakeup_server.dart"
MAX_ACCOUNT_RESPONSE_BYTES = 1024 * 1024
SECRET_ATTRIBUTES = ("application", "okam-linux", "account")
CAMERA_COMMANDS = {
    "Left": (4, 1),
    "Right": (6, 1),
    "Up": (0, 1),
    "Down": (2, 1),
    "Preset 1": (31, 0),
    "Preset 2": (33, 0),
    "Preset 3": (35, 0),
    "Preset 4": (37, 0),
    "Preset 5": (39, 0),
}
PTZ_RESPONSE_COMMAND = 0x6019


def stored_secret(category: str, identifier: str) -> str | None:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", "okam-linux", category, identifier],
            capture_output=True,
            check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return result.stdout.decode("utf-8").removesuffix("\n")
    except UnicodeError:
        return None


def stored_account_password(username: str) -> str | None:
    return stored_secret("account", username)


def stored_camera_password(uid: str) -> str | None:
    return stored_secret("camera", uid)


def save_account_password(username: str, password: str) -> bool:
    try:
        result = subprocess.run(
            ["secret-tool", "store", "--label=O-KAM Linux account", *SECRET_ATTRIBUTES, username],
            input=password.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def clear_account_password(username: str) -> bool:
    try:
        result = subprocess.run(
            ["secret-tool", "clear", *SECRET_ATTRIBUTES, username],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def account_request(request: urllib.request.Request, timeout: float) -> bytes:
    path = urllib.parse.urlsplit(request.full_url).path
    stage = {
        "/user/summary": "account lookup",
        "/login/token": "account login",
        "/PC/device/show": "camera list",
    }.get(path, "account")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(MAX_ACCOUNT_RESPONSE_BYTES + 1)
            if response.status != 200 or len(payload) > MAX_ACCOUNT_RESPONSE_BYTES:
                raise AccountError(f"O-KAM {stage} returned an invalid response.")
            return payload
    except urllib.error.HTTPError as ex:
        if path == "/login/token" and ex.code in (401, 403):
            raise AccountError("O-KAM account login was rejected.") from None
        raise AccountError(f"O-KAM {stage} failed (HTTP {ex.code}).") from None
    except urllib.error.URLError:
        raise AccountError(f"O-KAM {stage} is unreachable.") from None


class AccountWorker(QThread):
    devices_found = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, username: str, password: str) -> None:
        super().__init__()
        self.username = username
        self.password = password

    def run(self) -> None:
        try:
            client = Eye4AccountClient(opener=account_request)
            self.devices_found.emit(client.enumerate(self.username, self.password))
        except AccountError as ex:
            self.failed.emit(str(ex))
        except Exception:
            self.failed.emit("Unable to reach the O-KAM account service.")
        finally:
            self.username = ""
            self.password = ""


class StreamWorker(QThread):
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, device: AccountDevice, camera_password: str, player_input: BinaryIO) -> None:
        super().__init__()
        self.device = device
        self.camera_password = camera_password
        self.player_input = player_input
        self.stop_requested = threading.Event()
        self.helper: subprocess.Popen[bytes] | None = None

    def stop(self) -> None:
        self.stop_requested.set()
        if self.helper is not None and self.helper.poll() is None:
            self.helper.send_signal(signal.SIGINT)

    def force_stop(self) -> None:
        self.stop_requested.set()
        if self.helper is not None and self.helper.poll() is None:
            self.helper.kill()

    def run(self) -> None:
        try:
            self._stream()
        except (OSError, P2PError, WakeError) as ex:
            if not self.stop_requested.is_set():
                self.failed.emit(str(ex))
        except Exception:
            if not self.stop_requested.is_set():
                self.failed.emit("Unable to start the camera stream.")
        finally:
            self.camera_password = ""
            if self.helper is not None and self.helper.poll() is None:
                self.helper.terminate()
                try:
                    self.helper.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.helper.kill()
                    self.helper.wait(timeout=3)

    def _stream(self) -> None:
        credentials = load_wake_credentials(WAKE_SOURCE)
        if credentials is None:
            raise WakeError("Verified O-KAM wake configuration is unavailable. Run install.sh.")
        self.status_changed.emit("Resolving camera connection...")
        client_id = resolve_client_id(self.device.uid)
        service_parameter = get_service_parameter(client_id)
        if self.stop_requested.is_set():
            return
        self.status_changed.emit("Waking camera...")
        try:
            asyncio.run(wake_camera(self.device.uid, credentials))
        except WakeError:
            self.status_changed.emit("Wake service did not respond; trying the camera connection...")
        if self.stop_requested.is_set():
            return
        self.status_changed.emit("Connecting to camera...")
        password = select_camera_password(self.device.device_password, self.camera_password)
        environment = os.environ.copy()
        environment["PATH"] = str(Path(sys.executable).parent) + os.pathsep + environment.get("PATH", "")
        self.helper = open_stream_process(
            str(HELPER),
            "/dev/null",
            client_id,
            service_parameter,
            password,
            environment=environment,
        )
        assert self.helper.stdout is not None
        assert self.helper.stderr is not None
        received_video = False
        try:
            while not self.stop_requested.is_set():
                chunk = self.helper.stdout.read(32 * 1024)
                if not chunk:
                    break
                if not received_video:
                    received_video = True
                    self.status_changed.emit("Live video")
                self.player_input.write(chunk)
        except BrokenPipeError:
            if not self.stop_requested.is_set():
                raise P2PError("The video player stopped unexpectedly.") from None
        if self.stop_requested.is_set():
            return
        result = self.helper.wait(timeout=5)
        summary = self.helper.stderr.read(64 * 1024)
        try:
            details = json.loads(summary.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            details = {}
        if result == 5:
            attempts = details.get("login_attempts")
            if isinstance(attempts, list) and any(isinstance(item, int) for item in attempts):
                raise P2PError("The camera rejected the available credentials.")
            raise P2PError("The camera did not answer authentication. Retry after it wakes.")
        if not received_video:
            raise P2PError("The camera did not provide live video.")
        if result != 0:
            raise P2PError("The camera stream ended unexpectedly.")


class ControlWorker(QThread):
    completed = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, device: AccountDevice, command: str) -> None:
        super().__init__()
        self.device = device
        self.command = command

    def run(self) -> None:
        session: CS2Session | None = None
        try:
            password = stored_camera_password(self.device.uid)
            if not password:
                raise CS2Error("The camera credential is unavailable.")
            client_id = resolve_client_id(self.device.uid)
            session = CS2Session(client_id, get_service_parameter(client_id))
            session.connect(timeout=30)
            value, one_step = CAMERA_COMMANDS[self.command]
            path = f"decoder_control.cgi?command={value}&onestep={one_step}&"
            write_command(session, make_cgi_request(path, "admin", password))
            response = read_command_result(session, (PTZ_RESPONSE_COMMAND,), timeout=10)
            if response is None or response[1] != 0:
                raise CS2Error("The camera rejected the movement command.")
            self.completed.emit(self.command)
        except (CS2Error, P2PError, OSError):
            self.failed.emit("Camera movement failed.")
        finally:
            if session is not None:
                session.close()


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle("O-KAM Linux")
        self.resize(980, 750)
        self.devices: list[AccountDevice] = []
        self.account_worker: AccountWorker | None = None
        self.stream_worker: StreamWorker | None = None
        self.control_worker: ControlWorker | None = None
        self.player: subprocess.Popen[bytes] | None = None
        self.close_pending = False
        self.stream_error = False
        self.stream_live = False
        self.settings = QSettings("O-KAM Linux", "O-KAM Linux")
        self.account_username = self.settings.value("account/username", "", str)
        self.account_secret = (
            stored_account_password(self.account_username) if self.account_username else None
        )
        body = QWidget()
        layout = QVBoxLayout(body)
        self.video = QWidget()
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.setMinimumSize(640, 400)
        self.video.setStyleSheet("background-color: #171717;")
        layout.addWidget(self.video, 1)
        controls = QHBoxLayout()
        self.find_button = QPushButton("Reconnect")
        self.watch_button = QPushButton("Watch live")
        self.stop_button = QPushButton("Stop")
        self.watch_button.setEnabled(False)
        self.stop_button.setEnabled(False)
        controls.addWidget(self.watch_button)
        controls.addWidget(self.stop_button)
        controls.addWidget(self.find_button)
        self.fullscreen_button = QPushButton("Full screen")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        layout.addLayout(controls)
        movement = QHBoxLayout()
        self.camera_buttons: list[QPushButton] = []
        directions = (("Left", "←"), ("Right", "→"), ("Up", "↑"), ("Down", "↓"))
        for direction, label in directions:
            button = QPushButton(label)
            button.setToolTip(f"Move camera {direction.lower()}")
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=direction: self.control_camera(value))
            movement.addWidget(button)
            self.camera_buttons.append(button)
        layout.addLayout(movement)
        presets = QHBoxLayout()
        for index in range(1, 6):
            label = f"Preset {index}"
            button = QPushButton(label)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=label: self.control_camera(value))
            presets.addWidget(button)
            self.camera_buttons.append(button)
        layout.addLayout(presets)
        self.status = QLabel("Connecting to camera...")
        layout.addWidget(self.status)
        self.setCentralWidget(body)
        self.find_button.clicked.connect(self.find_cameras)
        self.watch_button.clicked.connect(self.watch_live)
        self.stop_button.clicked.connect(self.stop_stream)
        if self.account_username and self.account_secret:
            QTimer.singleShot(0, self.find_cameras)
        else:
            QTimer.singleShot(0, self.change_account)

    def change_account(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("O-KAM account")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        username = QLineEdit(self.account_username)
        username.setPlaceholderText("O-KAM account email")
        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Account", username)
        form.addRow("Password", password)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        if not username.text().strip() or not password.text():
            self.status.setText("Enter your O-KAM account and password.")
            return
        self.account_username = username.text().strip()
        self.account_secret = password.text()
        self.find_cameras()

    def find_cameras(self) -> None:
        if not self.account_username or not self.account_secret:
            self.change_account()
            return
        self.devices = []
        self.watch_button.setEnabled(False)
        self.find_button.setEnabled(False)
        self.status.setText("Finding cameras...")
        self.account_worker = AccountWorker(self.account_username, self.account_secret)
        self.account_worker.devices_found.connect(self.on_devices_found)
        self.account_worker.failed.connect(self.status.setText)
        self.account_worker.finished.connect(self.on_account_finished)
        self.account_worker.start()

    def on_devices_found(self, devices: list[AccountDevice]) -> None:
        self.devices = devices
        saved_username = self.settings.value("account/username", "", str)
        if not save_account_password(self.account_username, self.account_secret):
            self.status.setText("The keyring could not save the account.")
        else:
            if saved_username and saved_username != self.account_username:
                clear_account_password(saved_username)
            self.settings.setValue("account/username", self.account_username)
            self.settings.sync()
        self.watch_button.setEnabled(bool(devices))
        if devices:
            self.selected_device = next((device for device in devices if device.name == "Jardin"), devices[0])
            if not self.status.text().startswith("The keyring could not"):
                credential_status = (
                    "O-KAM supplied a camera credential."
                    if self.selected_device.device_password
                    else "O-KAM supplied no camera credential."
                )
                self.status.setText(f"Found {len(devices)} camera(s). {credential_status}")
        else:
            self.status.setText("No cameras are visible to this account.")

    def on_account_finished(self) -> None:
        self.find_button.setEnabled(True)
        self.account_worker = None
        if self.close_pending:
            self.close()
        elif self.devices:
            self.watch_live()

    def watch_live(self) -> None:
        if not self.devices:
            return
        try:
            self.player = subprocess.Popen(
                [
                    "mpv",
                    "--no-config",
                    "--no-terminal",
                    "--really-quiet",
                    "--vo=x11",
                    "--force-window=yes",
                    "--profile=low-latency",
                    "--cache=no",
                    "--untimed",
                    "--no-audio",
                    "--demuxer=lavf",
                    "--demuxer-lavf-format=h264",
                    f"--wid={int(self.video.winId())}",
                    "-",
                ],
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.status.setText("The mpv video player is unavailable.")
            return
        assert self.player.stdin is not None
        self.stream_error = False
        self.stream_live = False
        self.stream_worker = StreamWorker(
            self.selected_device, stored_camera_password(self.selected_device.uid) or "", self.player.stdin
        )
        self.stream_worker.status_changed.connect(self.on_stream_status)
        self.stream_worker.failed.connect(self.on_stream_error)
        self.stream_worker.finished.connect(self.on_stream_finished)
        self.stream_worker.start()
        self.watch_button.setEnabled(False)
        self.find_button.setEnabled(False)
        self.stop_button.setEnabled(True)

    def on_stream_status(self, message: str) -> None:
        self.status.setText(message)
        if message == "Live video":
            self.stream_live = True
            self.set_controls_enabled(self.control_worker is None)

    def set_controls_enabled(self, enabled: bool) -> None:
        for button in self.camera_buttons:
            button.setEnabled(enabled)

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.fullscreen_button.setText("Full screen")
        else:
            self.showFullScreen()
            self.fullscreen_button.setText("Exit full screen")

    def control_camera(self, command: str) -> None:
        if self.stream_worker is None or self.control_worker is not None:
            return
        self.set_controls_enabled(False)
        if command.startswith("Preset "):
            self.status.setText(f"Moving camera to {command.lower()}...")
        else:
            self.status.setText(f"Moving camera {command.lower()}...")
        self.control_worker = ControlWorker(self.selected_device, command)
        self.control_worker.completed.connect(self.on_control_completed)
        self.control_worker.failed.connect(self.status.setText)
        self.control_worker.finished.connect(self.on_control_finished)
        self.control_worker.start()

    def on_control_completed(self, command: str) -> None:
        if command.startswith("Preset "):
            self.status.setText(f"Camera moved to {command.lower()}.")
        else:
            self.status.setText(f"Camera moved {command.lower()}.")

    def on_control_finished(self) -> None:
        self.control_worker = None
        self.set_controls_enabled(self.stream_live)
        if self.close_pending:
            self.close()

    def on_stream_error(self, message: str) -> None:
        self.stream_error = True
        self.stream_live = False
        self.set_controls_enabled(False)
        self.status.setText(message)

    def stop_stream(self) -> None:
        if self.stream_worker is not None:
            self.status.setText("Stopping camera...")
            self.stream_worker.stop()
            self.stop_button.setEnabled(False)

    def on_stream_finished(self) -> None:
        self.stream_worker = None
        self.stream_live = False
        if self.player is not None:
            if self.player.stdin is not None:
                self.player.stdin.close()
            if self.player.poll() is None:
                self.player.terminate()
                try:
                    self.player.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.player.kill()
                    self.player.wait(timeout=3)
            self.player = None
        self.find_button.setEnabled(True)
        self.watch_button.setEnabled(bool(self.devices))
        self.stop_button.setEnabled(False)
        self.set_controls_enabled(False)
        if not self.stream_error:
            self.status.setText("Camera stopped.")
        if self.close_pending:
            self.close()

    def closeEvent(self, event: object) -> None:
        if (
            self.account_worker is not None
            or self.stream_worker is not None
            or self.control_worker is not None
        ):
            self.close_pending = True
            self.stop_stream()
            event.ignore()
            return
        event.accept()


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--forget-account":
        settings = QSettings("O-KAM Linux", "O-KAM Linux")
        username = settings.value("account/username", "", str)
        if username and not clear_account_password(username):
            return 1
        settings.remove("account/username")
        settings.sync()
        return 0 if settings.status() == QSettings.Status.NoError else 1
    if len(sys.argv) != 1:
        return 2
    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
