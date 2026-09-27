#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import queue
import re
import shutil
import signal
import socket
import struct
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from dataclasses import dataclass
from typing import BinaryIO, Callable
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, QSettings, QSocketNotifier, QStandardPaths, QThread, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QActionGroup,
    QColor,
    QIcon,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPalette,
    QPen,
    QPixmap,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket

from okam_native.account import AccountDevice, AccountError, Eye4AccountClient
from okam_native.cs2 import (
    LIVE_STREAM_RESPONSE_COMMANDS,
    LOGIN_RESPONSE_COMMAND,
    MAX_FRAME_BYTES,
    CS2Error,
    CS2Session,
    CS2Timeout,
    CameraLoginRejected,
    authenticate_camera,
    inspect_h264,
    make_cgi_request,
    parse_result,
    read_command,
    read_command_result,
    write_command,
)
from okam_native.p2p import (
    P2PError,
    get_service_parameter,
    resolve_client_id,
    select_camera_password,
)
from okam_native.wakeup import WakeError, load_wake_credentials, wake_camera
from Xlib import X as X11, Xutil, display as xdisplay
from Xlib.protocol import event as xevent


APPLICATION_NAME = "O-KAM Linux"
LOG = logging.getLogger("okam-linux")
LOG_DIRECTORY = Path.home() / ".cache/okam-linux"
LOG_FILE_NAME = "okam-linux.log"
LOG_MAX_BYTES = 1024 * 1024
LOG_BACKUPS = 2
TRAY_ARGUMENT = "--tray"
NOTICE_MS = 4000
CAMERA_PASSWORD_ARGUMENT = "--camera-password"
SIGNAL_POLL_MS = 500
INSTANCE_SERVER_PREFIX = "okam-linux"
INSTANCE_CONNECT_TIMEOUT_MS = 500
SHOW_WINDOW_REQUEST = b"show"
ACCOUNT_REJECTED_MESSAGE = "O-KAM account login was rejected."
WAKE_SOURCE = Path.home() / ".local/share/okam-linux/vendor/device_wakeup_server.dart"
ICON_COLOR = "#f5f5f5"
ICON_NAME_PROPERTY = "iconName"
ICON_DIRECTORY = Path(__file__).resolve().parent / "assets/icons"
MAX_ACCOUNT_RESPONSE_BYTES = 1024 * 1024
SECRET_ATTRIBUTES = ("application", "okam-linux", "account")
MOTOR_COMMANDS = {"Left": (4, 5), "Right": (6, 7), "Up": (0, 1), "Down": (2, 3)}
PRESET_COMMANDS = {f"Preset {index}": 29 + index * 2 for index in range(1, 6)}
PTZ_RESPONSE_COMMAND = 0x6019
MOTOR_PULSE_SECONDS = 0.12
DRAG_PIXELS_PER_STEP = 90
MAX_DRAG_STEPS = 4
VIDEO_READ_TIMEOUT_SECONDS = 2
VIDEO_STALL_SECONDS = 12
AUDIO_RESPONSE_COMMAND = 0x6031
RECONNECT_MAX_SECONDS = 30
OVERLAY_TIMEOUT_MS = 5000
MAX_ZOOM_LEVEL = 4
DOUBLE_CLICK_ZOOM_STEPS = 2
X11_WHEEL_UP = 4
X11_WHEEL_DOWN = 5
MAX_MPV_RESPONSE_BYTES = 65536
OVERLAY_COLOR = QColor(24, 24, 24, 170)
OVERLAY_MAX_RADIUS = 32
OVERLAY_MARGIN = 16
VIDEO_ASPECT_WIDTH = 16
VIDEO_ASPECT_HEIGHT = 9
WM_NORMAL_HINTS_FIELDS = (
    "flags",
    "min_width",
    "min_height",
    "max_width",
    "max_height",
    "width_inc",
    "height_inc",
    "min_aspect",
    "max_aspect",
    "base_width",
    "base_height",
    "win_gravity",
)
NET_WM_STATE_ADD = 1
NET_WM_SOURCE_APPLICATION = 1
CAMERA_STATUS_PATH = "get_status.cgi?"
TRANSPARENT_RESPONSE_COMMAND = 0x60D1
CAMERA_CONTROL_RESPONSE_COMMAND = 0x6012
WHITE_LIGHT_COMMAND = "2109"
WHITE_LIGHT_SET_COMMAND = "0"
WHITE_LIGHT_STATUS_COMMAND = "2"
WHITE_LIGHT_OFF_STATUS = "0"
WHITE_LIGHT_SET_PATH = "trans_cmd_string.cgi?cmd=2109&command=0&light={light}&"
WHITE_LIGHT_STATUS_PATH = "trans_cmd_string.cgi?cmd=2109&command=2&"
VIDEO_QUALITY_PATH = "camera_control.cgi?param=16&value={value}&"
VIDEO_QUALITIES = {"Super HD": 100, "HD": 1, "SD": 2, "Low": 4}
SUPER_HD_QUALITY = "Super HD"
RESTART_REQUIRED_PIXELS = ("200", "300")
DEFAULT_QUALITY_LABEL = "Auto"
QUALITY_SETTING = "camera/quality"
SETTING_LIGHT = "light"
SETTING_QUALITY = "quality"
SETTING_RESPONSE_SECONDS = 5
RESPONSE_FIELD_PATTERN = re.compile(r'(?:var\s+)?([\w\[\]]+)\s*=\s*"?([^";\r\n]*)"?\s*;')
RECORD_LIST_PATH = "get_record_file.cgi?GetType=file&dirname={day}&"
RECORD_LIST_RESPONSE_COMMAND = 0x6007
RECORD_LIST_SECONDS = 20
RECORD_NAME_FIELD = "record_name["
RECORD_DURATION_FIELD = "record_duration["
RECORD_SIZE_FIELD = "record_size["
RECORDING_NAME_PATTERN = re.compile(r"(\d{14})_(\d{3})\.mp4")
CONTINUOUS_RECORDING_TYPE = "100"
RECORDING_TIME_LENGTH = 14
RECORDING_TIME_FORMAT = "%Y%m%d%H%M%S"
RECORD_DAY_FORMAT = "%Y%m%d"
CARD_PLAY_PATH = "livestream.cgi?streamid=4&filename={name}&offset=0&download=1&"
CARD_STOP_PATH = "livestream.cgi?streamid=17&"
CARD_CHANNEL = 4
CARD_PLAY_RESPONSE_COMMAND = 0x6037
CARD_RESPONSE_SECONDS = 10
CARD_FRAME_SECONDS = 15
CARD_QUIET_SECONDS = 0.3
CARD_DRAIN_SECONDS = 3.0
CARD_TIME_MARGIN_SECONDS = 15
CARD_START_FRAME_TYPE = 0x63
CARD_END_FRAME_TYPE = 0x64
CARD_VIDEO_FRAME_TYPES = (0x00, 0x01)
CARD_AUDIO_FRAME_TYPE = 0x0D
SEQUENCE_HALF_RANGE = 0x8000
MAX_OUT_OF_ORDER_PACKETS = 4096
KEY_FRAME_TYPE = 0x00
MATROSKA_SUFFIX = ".mkv"
PACER_IDLE_SECONDS = 0.1
PACER_RESYNC_SECONDS = 1.0
PACER_POSITION_SECONDS = 0.25
REPLAY_SPEEDS = (1, 2, 4, 8)
REPLAY_DEFAULT_REWIND_SECONDS = 60
REPLAY_SPEED_LABEL = "{speed}x"
LIVE_BUTTON_LABEL = "LIVE"
LOADING_STATUS = "Loading {moment:%d/%m/%Y %H:%M:%S} {percent}%"
DETECTION_JUMP_MARGIN_SECONDS = 5
DETECTION_RECENT_SECONDS = 30
DETECTION_MERGE_SECONDS = 10
RECORDING_CHAIN_TOLERANCE_SECONDS = 5
MAX_RECORDING_RELOADS = 2
MAX_REPLAY_DAYS = 31
LOADING_MIN_FRACTION = 0.02
REPLAY_WORKER_WAIT_MS = 20000
TIMELINE_HEIGHT = 84
TIMELINE_HEADER_HEIGHT = 28
TIMELINE_TICK_HEIGHT = 10
TIMELINE_DRAG_PIXELS = 4
TIMELINE_MERGE_SECONDS = 2
TIMELINE_MAX_LABELS = 8
TIMELINE_DEFAULT_ZOOM = 4
TIMELINE_SPANS = (600, 1800, 3600, 3 * 3600, 6 * 3600, 12 * 3600, 24 * 3600, 3 * 24 * 3600)
TIMELINE_LABEL_STEPS = (60, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)
TIMELINE_PREFERRED_WIDTH = 4096
TIMELINE_HEADER_COLOR = QColor(0, 0, 0, 0)
TIMELINE_EMPTY_COLOR = QColor(255, 255, 255, 40)
TIMELINE_RECORDING_COLOR = QColor(122, 168, 245)
TIMELINE_DETECTION_COLOR = QColor(244, 122, 111)
TIMELINE_LABEL_COLOR = QColor(220, 220, 220)
TIMELINE_TICK_COLOR = QColor(150, 150, 150)
TIMELINE_CURSOR_COLOR = QColor(34, 184, 216)
TIMELINE_CURSOR_TEXT_COLOR = QColor(255, 255, 255)
TIMELINE_CURSOR_LABEL_WIDTH = 150
TIMELINE_LABEL_WIDTH = 80
REPLAY_ATTEMPTS = 2
REPLAY_LIST_REQUEST = "list"
REPLAY_DOWNLOAD_REQUEST = "download"
FRAME_MAGIC = b"\x55\xaa\x15\xa8"
AAC_SUFFIX = ".aac"
ADTS_HEADER_BYTES = 7
ADTS_AAC_LC_PROFILE = 1
ADTS_16000_HZ_INDEX = 8
ADTS_MONO_CHANNELS = 1
DETECTION_POLL_MS = 15 * 60 * 1000
DETECTION_SETTING = "detections/last_seen"
DETECTION_MESSAGE_MS = 15000
RAW_RECORDING_SUFFIX = ".h264"
MIN_RECORDING_FRAMES = 2
RECORDING_REMUX_TIMEOUT_SECONDS = 600
RECORDING_TICK_MS = 1000
RECORDING_DOT_COLOR = "#ff4d4d"


def configure_logging() -> None:
    LOG_DIRECTORY.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        LOG_DIRECTORY / LOG_FILE_NAME, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"
    )
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    LOG.addHandler(handler)
    LOG.setLevel(logging.INFO)


def menu_icon(name: str, color: QColor) -> QIcon:
    try:
        svg = (ICON_DIRECTORY / f"{name}.svg").read_text(encoding="utf-8")
    except OSError:
        return QIcon()
    pixmap = QPixmap()
    pixmap.loadFromData(svg.replace(ICON_COLOR, color.name()).encode("utf-8"), "SVG")
    return QIcon(pixmap)


def set_button_icon(button: QPushButton, name: str, label: str, size: int = 44) -> None:
    button.setText("")
    button.setProperty(ICON_NAME_PROPERTY, name)
    button.setIcon(QIcon(str(ICON_DIRECTORY / f"{name}.svg")))
    button.setIconSize(QSize(28, 28))
    button.setFixedSize(size, size)
    button.setToolTip(label)
    button.setAccessibleName(label)


class X11WindowHints:
    def __init__(self, window: QWidget, skip_taskbar: bool) -> None:
        self.skip_taskbar = skip_taskbar
        self.x_display = xdisplay.Display()
        self.window = self.x_display.create_resource_object("window", int(window.winId()))
        self.state_atom = self.x_display.intern_atom("_NET_WM_STATE")
        self.skip_taskbar_atom = self.x_display.intern_atom("_NET_WM_STATE_SKIP_TASKBAR")
        self.normal_hints_atom = self.x_display.intern_atom("WM_NORMAL_HINTS")
        self.window.change_attributes(event_mask=X11.StructureNotifyMask | X11.PropertyChangeMask)
        self.notifier = QSocketNotifier(self.x_display.fileno(), QSocketNotifier.Type.Read, window)
        self.notifier.activated.connect(self.process_events)
        self.apply_aspect_ratio()

    def process_events(self) -> None:
        while self.x_display.pending_events():
            event = self.x_display.next_event()
            if event.type == X11.MapNotify and self.skip_taskbar:
                self.hide_taskbar_entry()
            elif event.type == X11.PropertyNotify and event.atom == self.normal_hints_atom:
                self.apply_aspect_ratio()

    def hide_taskbar_entry(self) -> None:
        self.x_display.screen().root.send_event(
            xevent.ClientMessage(
                window=self.window,
                client_type=self.state_atom,
                data=(32, [NET_WM_STATE_ADD, self.skip_taskbar_atom, 0, NET_WM_SOURCE_APPLICATION, 0]),
            ),
            event_mask=X11.SubstructureRedirectMask | X11.SubstructureNotifyMask,
        )
        self.x_display.flush()

    def apply_aspect_ratio(self) -> None:
        hints = self.window.get_wm_normal_hints()
        aspect = {"num": VIDEO_ASPECT_WIDTH, "denum": VIDEO_ASPECT_HEIGHT}
        if hints is None:
            fields = {"flags": 0}
        else:
            fields = {field: getattr(hints, field) for field in WM_NORMAL_HINTS_FIELDS}
            if (
                hints.flags & Xutil.PAspect
                and hints.min_aspect == aspect
                and hints.max_aspect == aspect
            ):
                return
        fields["flags"] |= Xutil.PAspect
        fields["min_aspect"] = aspect
        fields["max_aspect"] = aspect
        self.window.set_wm_normal_hints(fields)
        self.x_display.flush()

    def close(self) -> None:
        self.notifier.setEnabled(False)
        self.x_display.close()


def response_fields(payload: bytes) -> dict[str, str]:
    return dict(RESPONSE_FIELD_PATTERN.findall(payload.decode("utf-8", "replace")))


def read_response_fields(
    session: CS2Session, command_id: int, expected: dict[str, str], timeout: float
) -> dict[str, str] | None:
    deadline = time.monotonic() + timeout
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            command, payload = read_command(session, timeout=remaining)
        except CS2Timeout:
            return None
        if command != command_id:
            continue
        fields = response_fields(payload)
        if parse_result(payload) == 0 and all(fields.get(key) == value for key, value in expected.items()):
            return fields
    return None


def available_qualities(status: dict[str, str]) -> list[str]:
    if not status or status.get("pixel") in RESTART_REQUIRED_PIXELS:
        return []
    qualities = list(VIDEO_QUALITIES)
    if status.get("support_pixel_shift") != "1":
        qualities.remove(SUPER_HD_QUALITY)
    return qualities


def white_light_path(enabled: bool) -> str:
    return WHITE_LIGHT_SET_PATH.format(light=int(enabled))


def video_quality_path(quality: str) -> str:
    return VIDEO_QUALITY_PATH.format(value=VIDEO_QUALITIES[quality])


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
            raise AccountError(ACCOUNT_REJECTED_MESSAGE) from None
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


class CameraFrameReader:
    def __init__(self) -> None:
        self.pending_frame: tuple[int, int] | None = None

    def read(self, session: CS2Session) -> tuple[bytes, int]:
        if self.pending_frame is None:
            header = session.read_exact(1, 32, timeout=VIDEO_READ_TIMEOUT_SECONDS)
            if header[:4] != b"\x55\xaa\x15\xa8":
                raise CS2Error("camera video framing is invalid")
            length = int.from_bytes(header[16:20], "little")
            if not 0 < length <= MAX_FRAME_BYTES:
                raise CS2Error("camera video frame is invalid")
            self.pending_frame = (length, header[4])
        frame = session.read_exact(1, self.pending_frame[0], timeout=VIDEO_READ_TIMEOUT_SECONDS)
        frame_type = self.pending_frame[1]
        self.pending_frame = None
        return frame, frame_type


def remux_to_matroska(
    video_path: Path,
    frame_rate: float,
    output_path: Path,
    audio_path: Path | None = None,
    audio_offset: float = 0.0,
) -> None:
    command = [
        "ffmpeg",
        "-nostdin",
        "-loglevel",
        "error",
        "-y",
        "-f",
        "h264",
        "-i",
        str(video_path),
    ]
    if audio_path is not None:
        command += ["-itsoffset", f"{audio_offset:.3f}", "-f", "aac", "-i", str(audio_path), "-map", "0:v", "-map", "1:a"]
    command += [
        "-c",
        "copy",
        "-bsf:v",
        f"setts=ts=N/({frame_rate:.6f}*TB)",
        "-f",
        "matroska",
        str(output_path),
    ]
    try:
        result = subprocess.run(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=RECORDING_REMUX_TIMEOUT_SECONDS,
            check=False,
        )
    except subprocess.TimeoutExpired:
        output_path.unlink(missing_ok=True)
        raise OSError("ffmpeg did not finish the recording.") from None
    if result.returncode != 0 or not output_path.is_file() or output_path.stat().st_size == 0:
        output_path.unlink(missing_ok=True)
        raise OSError(result.stderr.decode("utf-8", "replace").strip() or "ffmpeg failed.")


def mpv_stream_command(socket_path: Path, window_id: int, fill: bool) -> list[str]:
    command = [
        "mpv",
        "--no-config",
        "--no-terminal",
        "--really-quiet",
        "--vo=x11",
        "--osc=no",
        "--input-cursor=no",
        "--input-default-bindings=no",
        "--input-vo-keyboard=no",
        "--force-window=yes",
        "--profile=low-latency",
        "--cache=no",
        "--untimed",
        "--no-audio",
        "--demuxer=lavf",
        "--demuxer-lavf-format=h264",
        f"--input-ipc-server={socket_path}",
        f"--wid={window_id}",
    ]
    if fill:
        command.append("--panscan=1.0")
    return command + ["-"]


def media_directory() -> Path:
    pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    directory = (Path(pictures) if pictures else Path.home() / "Pictures") / APPLICATION_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def camera_time(timestamp: float) -> datetime:
    return datetime.fromtimestamp(timestamp, timezone.utc).replace(tzinfo=None)


def camera_timestamp(moment: datetime) -> float:
    return moment.replace(tzinfo=timezone.utc).timestamp()


def mpv_request(socket_path: Path | None, command: list[object]) -> tuple[bool, object]:
    if socket_path is None:
        return False, None
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
            connection.settimeout(2)
            connection.connect(str(socket_path))
            connection.sendall(json.dumps({"command": command}).encode("utf-8") + b"\n")
            response = bytearray()
            while b"\n" not in response and len(response) < MAX_MPV_RESPONSE_BYTES:
                chunk = connection.recv(4096)
                if not chunk:
                    break
                response.extend(chunk)
        answer = json.loads(response.split(b"\n", 1)[0])
        return answer.get("error") == "success", answer.get("data")
    except (OSError, ValueError, IndexError, AttributeError):
        return False, None


class VideoRecorder:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.raw_path = path.with_name(path.name + RAW_RECORDING_SUFFIX)
        self.raw_file: BinaryIO | None = None
        self.frames = 0
        self.first_frame_time = 0.0
        self.last_frame_time = 0.0

    @property
    def started(self) -> bool:
        return self.raw_file is not None

    def write(self, frame: bytes, keyframe: bool, now: float) -> bool:
        if self.raw_file is None:
            if not keyframe:
                return False
            self.raw_file = self.raw_path.open("xb")
            self.first_frame_time = now
        self.raw_file.write(frame)
        self.frames += 1
        self.last_frame_time = now
        return True

    def frame_rate(self) -> float:
        duration = self.last_frame_time - self.first_frame_time
        return (self.frames - 1) / duration

    def finish(self) -> Path:
        if self.raw_file is None:
            raise OSError("No video was recorded.")
        self.raw_file.close()
        try:
            if self.frames < MIN_RECORDING_FRAMES or self.last_frame_time <= self.first_frame_time:
                raise OSError("The recording is too short.")
            remux_to_matroska(self.raw_path, self.frame_rate(), self.path)
        finally:
            self.raw_path.unlink(missing_ok=True)
        return self.path


class ControlsOverlay(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(
            parent,
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(OVERLAY_COLOR)
        radius = min(self.height() / 2, OVERLAY_MAX_RADIUS)
        painter.drawRoundedRect(self.rect(), radius, radius)
        painter.end()


class ReliableCS2Session(CS2Session):
    def discard_channel(self, channel: int, quiet_seconds: float, limit_seconds: float) -> None:
        deadline = time.monotonic() + limit_seconds
        last_size = -1
        quiet_since = time.monotonic()
        while time.monotonic() < deadline:
            size = len(self._channel_buffers[channel]) + len(self._out_of_order[channel])
            if size != last_size:
                last_size = size
                quiet_since = time.monotonic()
            elif time.monotonic() - quiet_since >= quiet_seconds:
                break
            self._pump()
        self._channel_buffers[channel].clear()
        self._out_of_order[channel].clear()

    def _handle_data(self, packet: bytes) -> None:
        declared = int.from_bytes(packet[2:4], "big") if len(packet) >= 4 else -1
        body = packet[4:]
        if declared != len(body) or len(body) < 4 or body[0] != 0xD1 or body[1] >= 8:
            self._count("data_packets_invalid")
            return
        channel = body[1]
        sequence = int.from_bytes(body[2:4], "big")
        expected = self._incoming_sequence[channel]
        waiting = self._out_of_order[channel]
        distance = (sequence - expected) & 0xFFFF
        if 0 < distance < SEQUENCE_HALF_RANGE and sequence not in waiting:
            if len(waiting) >= MAX_OUT_OF_ORDER_PACKETS:
                self._count("data_packets_deferred")
                return
            waiting[sequence] = body[4:]
        self._count(f"channel{channel}_packets")
        self._count(f"channel{channel}_bytes", len(body) - 4)
        assert self._peer is not None
        self._send_clear(b"\xf1\xd1\x00\x06\xd1" + bytes([channel]) + b"\x00\x01" + body[2:4], self._peer)
        if distance != 0:
            return
        self._channel_buffers[channel].extend(body[4:])
        expected = (expected + 1) & 0xFFFF
        while expected in waiting:
            self._channel_buffers[channel].extend(waiting.pop(expected))
            expected = (expected + 1) & 0xFFFF
        self._incoming_sequence[channel] = expected


def prepare_camera_connection(
    device: AccountDevice, report: Callable[[str], None], stop_requested: threading.Event
) -> tuple[str, object] | None:
    credentials = load_wake_credentials(WAKE_SOURCE)
    if credentials is None:
        raise WakeError("Verified O-KAM wake configuration is unavailable. Run install.sh.")
    report("Resolving camera connection...")
    client_id = resolve_client_id(device.uid)
    service_parameter = get_service_parameter(client_id)
    if stop_requested.is_set():
        return None
    report("Waking camera...")
    try:
        asyncio.run(wake_camera(device.uid, credentials))
    except WakeError:
        report("Wake service did not respond; trying the camera connection...")
    if stop_requested.is_set():
        return None
    report("Connecting to camera...")
    return client_id, service_parameter


@dataclass(frozen=True)
class CardRecording:
    name: str
    start: datetime
    duration: int
    size: int

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def detection(self) -> bool:
        return is_detection_recording(self.name)


def list_recordings(session: CS2Session, user: str, password: str, day: str) -> list[CardRecording]:
    write_command(session, make_cgi_request(RECORD_LIST_PATH.format(day=day), user, password))
    deadline = time.monotonic() + RECORD_LIST_SECONDS
    recordings: list[CardRecording] = []
    while (remaining := deadline - time.monotonic()) > 0:
        try:
            command, payload = read_command(session, timeout=remaining)
        except CS2Timeout:
            raise CS2Error("The camera did not list its recordings.") from None
        if command != RECORD_LIST_RESPONSE_COMMAND:
            continue
        if parse_result(payload) != 0:
            raise CS2Error("The camera rejected the recording list request.")
        fields = response_fields(payload)
        for key, name in fields.items():
            if not key.startswith(RECORD_NAME_FIELD) or RECORDING_NAME_PATTERN.fullmatch(name) is None:
                continue
            index = key[len(RECORD_NAME_FIELD):]
            try:
                duration = int(fields.get(f"{RECORD_DURATION_FIELD}{index}", "0"))
                size = int(fields.get(f"{RECORD_SIZE_FIELD}{index}", "0"))
            except ValueError:
                continue
            recordings.append(CardRecording(name, recording_time(name), duration, size))
        if fields.get("current_page", "0") == fields.get("totol_page", "0"):
            return sorted(recordings, key=lambda recording: recording.name)
    raise CS2Error("The camera did not finish listing its recordings.")


def list_detections(session: CS2Session, user: str, password: str, day: str) -> list[str]:
    return [recording.name for recording in list_recordings(session, user, password, day) if recording.detection]


def adts_header(length: int) -> bytes:
    size = length + ADTS_HEADER_BYTES
    return bytes(
        [
            0xFF,
            0xF1,
            (ADTS_AAC_LC_PROFILE << 6) | (ADTS_16000_HZ_INDEX << 2),
            (ADTS_MONO_CHANNELS << 6) | (size >> 11),
            (size >> 3) & 0xFF,
            ((size & 0x07) << 5) | 0x1F,
            0xFC,
        ]
    )


class CardClipWriter:
    def __init__(self, path: Path, directory: Path) -> None:
        self.path = path
        self.video_path = directory / f"{path.stem}{RAW_RECORDING_SUFFIX}"
        self.audio_path = directory / f"{path.stem}{AAC_SUFFIX}"
        self.video_file = self.video_path.open("wb")
        self.audio_file = self.audio_path.open("wb")
        self.video_frames = 0
        self.first_video_time: float | None = None
        self.last_video_time = 0.0
        self.first_audio_time: float | None = None

    def add(self, frame_type: int, timestamp: float, body: bytes) -> None:
        if frame_type in CARD_VIDEO_FRAME_TYPES:
            if self.first_video_time is None:
                self.first_video_time = timestamp
            self.last_video_time = timestamp
            self.video_frames += 1
            self.video_file.write(body)
        elif frame_type == CARD_AUDIO_FRAME_TYPE and body:
            if self.first_audio_time is None:
                self.first_audio_time = timestamp
            self.audio_file.write(adts_header(len(body)) + body)

    def finish(self) -> Path:
        self.close()
        try:
            if (
                self.first_video_time is None
                or self.video_frames < MIN_RECORDING_FRAMES
                or self.last_video_time <= self.first_video_time
            ):
                raise OSError(
                    f"The camera sent no playable video ({self.video_frames} video frames,"
                    f" first {self.first_video_time}, last {self.last_video_time})."
                )
            frame_rate = (self.video_frames - 1) / (self.last_video_time - self.first_video_time)
            audio_path = self.audio_path if self.first_audio_time is not None else None
            audio_offset = (self.first_audio_time or 0.0) - self.first_video_time
            remux_to_matroska(self.video_path, frame_rate, self.path, audio_path, audio_offset)
        finally:
            self.discard()
        return self.path

    def close(self) -> None:
        self.video_file.close()
        self.audio_file.close()

    def discard(self) -> None:
        self.close()
        self.video_path.unlink(missing_ok=True)
        self.audio_path.unlink(missing_ok=True)


def download_card_recording(
    session: CS2Session,
    user: str,
    password: str,
    recording: CardRecording,
    on_frame: Callable[[int, float, bytes], None],
    progress: Callable[[float], None],
    cancelled: Callable[[], bool],
    idle: Callable[[], None] = lambda: None,
) -> bool:
    if isinstance(session, ReliableCS2Session):
        session.discard_channel(CARD_CHANNEL, CARD_QUIET_SECONDS, CARD_DRAIN_SECONDS)
    write_command(session, make_cgi_request(CARD_PLAY_PATH.format(name=recording.name), user, password))
    earliest = camera_timestamp(recording.start) - CARD_TIME_MARGIN_SECONDS
    latest = camera_timestamp(recording.end) + CARD_TIME_MARGIN_SECONDS
    response = read_command_result(session, (CARD_PLAY_RESPONSE_COMMAND,), timeout=CARD_RESPONSE_SECONDS)
    if response is None or response[1] != 0:
        raise CS2Error("The camera rejected the playback request.")
    started = False
    received = 0
    try:
        while not cancelled():
            header = session.read_exact(CARD_CHANNEL, 32, timeout=CARD_FRAME_SECONDS)
            if header[:4] != FRAME_MAGIC:
                raise CS2Error("camera playback framing is invalid")
            frame_type = header[4]
            milliseconds, seconds, _frame_number, length = struct.unpack_from("<HIII", header, 6)
            if length > MAX_FRAME_BYTES:
                raise CS2Error("camera playback frame is invalid")
            body = session.read_exact(CARD_CHANNEL, length, timeout=CARD_FRAME_SECONDS) if length else b""
            if frame_type == CARD_START_FRAME_TYPE:
                started = True
                continue
            if not started:
                continue
            if frame_type == CARD_END_FRAME_TYPE:
                if received == 0:
                    continue
                return True
            timestamp = seconds + milliseconds / 1000
            if not earliest <= timestamp <= latest:
                continue
            on_frame(frame_type, timestamp, body)
            idle()
            received += len(header) + length
            if recording.size > 0:
                progress(min(1.0, received / recording.size))
    except CS2Timeout:
        raise CS2Error("The camera stopped sending the recording.") from None
    write_command(session, make_cgi_request(CARD_STOP_PATH, user, password))
    return False


def is_detection_recording(name: str) -> bool:
    match = RECORDING_NAME_PATTERN.fullmatch(name)
    return match is not None and match.group(2) != CONTINUOUS_RECORDING_TYPE


def recording_time(name: str) -> datetime:
    return datetime.strptime(name[:RECORDING_TIME_LENGTH], RECORDING_TIME_FORMAT)


class DetectionWorker(QThread):
    detections_listed = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, device: AccountDevice, camera_password: str, days: list[str]) -> None:
        super().__init__()
        self.device = device
        self.camera_password = camera_password
        self.days = days
        self.stop_requested = threading.Event()

    def run(self) -> None:
        try:
            connection = prepare_camera_connection(self.device, lambda message: None, self.stop_requested)
            if connection is None:
                return
            session = ReliableCS2Session(*connection)
            try:
                session.connect(timeout=55)
                login = authenticate_camera(
                    session, select_camera_password(self.device.device_password, self.camera_password)
                )
                names: list[str] = []
                for day in self.days:
                    names += list_detections(session, login.user, login.password, day)
                self.detections_listed.emit(names)
            finally:
                session.close()
        except Exception as ex:
            print(f"Detection check error: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
            self.failed.emit("Unable to check camera detections.")
        finally:
            self.camera_password = ""


class StreamWorker(QThread):
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)
    control_completed = pyqtSignal(str)
    control_failed = pyqtSignal(str)
    sound_changed = pyqtSignal(bool)
    sound_failed = pyqtSignal(str)
    recording_started = pyqtSignal()
    recording_saved = pyqtSignal(str)
    recording_failed = pyqtSignal(str)
    capabilities_found = pyqtSignal(list, object)
    detections_listed = pyqtSignal(list)
    detections_failed = pyqtSignal(str)
    setting_completed = pyqtSignal(str, object)
    setting_failed = pyqtSignal(str, str)

    def __init__(self, device: AccountDevice, camera_password: str, player_input: BinaryIO) -> None:
        super().__init__()
        self.device = device
        self.camera_password = camera_password
        self.player_input = player_input
        self.stop_requested = threading.Event()
        self.controls: queue.Queue[tuple[str, ...]] = queue.Queue(maxsize=1)
        self.settings: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        self.sound_requested = threading.Event()
        self.sound_active = False
        self.audio_player: subprocess.Popen[bytes] | None = None
        self.recording_request: Path | None = None
        self.recorder: VideoRecorder | None = None
        self.detection_days: list[str] | None = None

    def queue_setting(self, name: str, value: object) -> bool:
        try:
            self.settings.put_nowait((name, value))
            return True
        except queue.Full:
            return False

    def request_detections(self, days: list[str]) -> None:
        self.detection_days = days

    def set_recording(self, path: Path | None) -> None:
        self.recording_request = path

    def stop(self) -> None:
        self.stop_requested.set()

    def queue_control(self, commands: tuple[str, ...]) -> bool:
        try:
            self.controls.put_nowait(commands)
            return True
        except queue.Full:
            return False

    def set_sound(self, enabled: bool) -> None:
        if enabled:
            self.sound_requested.set()
        else:
            self.sound_requested.clear()

    def run(self) -> None:
        try:
            self._stream()
        except CameraLoginRejected:
            if not self.stop_requested.is_set():
                self.failed.emit("The camera rejected the available credentials.")
        except (OSError, CS2Error, P2PError, WakeError) as ex:
            if not self.stop_requested.is_set():
                print(f"Camera stream error: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
                message = str(ex) if isinstance(ex, (CS2Error, P2PError, WakeError)) else "Camera connection failed."
                self.failed.emit(message)
        except Exception as ex:
            if not self.stop_requested.is_set():
                print(f"Camera stream error: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
                self.failed.emit("Unable to start the camera stream.")
        finally:
            self.camera_password = ""

    def _stream(self) -> None:
        connection = prepare_camera_connection(self.device, self.status_changed.emit, self.stop_requested)
        if connection is None:
            return
        client_id, service_parameter = connection
        password = select_camera_password(self.device.device_password, self.camera_password)
        session = ReliableCS2Session(client_id, service_parameter)
        stream_started = False
        try:
            session.connect(timeout=55)
            if self.stop_requested.is_set():
                return
            login = authenticate_camera(session, password)
            self._read_capabilities(session, login.user, login.password)
            write_command(
                session,
                make_cgi_request(
                    "livestream.cgi?streamid=10&substream=2&", login.user, login.password
                ),
            )
            stream_started = True
            response = read_command_result(session, LIVE_STREAM_RESPONSE_COMMANDS, timeout=10)
            if response is not None and response[1] != 0:
                raise P2PError("The camera rejected the live stream request.")
            received_video = False
            last_video = time.monotonic()
            frame_reader = CameraFrameReader()
            while not self.stop_requested.is_set():
                self._process_control(session, login.user, login.password)
                self._process_setting(session, login.user, login.password)
                self._process_detections(session, login.user, login.password)
                self._update_sound(session, login.user, login.password)
                try:
                    frame, frame_type = frame_reader.read(session)
                except CS2Timeout:
                    if time.monotonic() - last_video >= VIDEO_STALL_SECONDS:
                        raise P2PError("The camera stopped sending video.") from None
                    continue
                if frame_type == 12:
                    self._play_audio(frame)
                    continue
                if frame_type in (0x10, 0x11):
                    continue
                valid, keyframe = inspect_h264(frame)
                if not valid:
                    continue
                last_video = time.monotonic()
                self._record_frame(frame, keyframe, last_video)
                if not received_video:
                    received_video = True
                    self.status_changed.emit("Live video")
                try:
                    self.player_input.write(frame)
                except BrokenPipeError:
                    raise P2PError("The video player stopped unexpectedly.") from None
        finally:
            if self.sound_active:
                try:
                    self._send_sound_command(session, login.user, login.password, False)
                except CS2Error:
                    pass
            self._close_audio_player()
            self.recording_request = None
            self._finish_recording()
            if stream_started:
                try:
                    write_command(
                        session,
                        make_cgi_request(
                            "livestream.cgi?streamid=16&substream=0&", login.user, login.password
                        ),
                    )
                except CS2Error:
                    pass
            session.close()

    def _process_detections(self, session: CS2Session, user: str, password: str) -> None:
        days = self.detection_days
        if days is None:
            return
        self.detection_days = None
        try:
            names: list[str] = []
            for day in days:
                names += list_detections(session, user, password, day)
        except CS2Error as ex:
            print(f"Detection check error: {ex}", file=sys.stderr, flush=True)
            self.detections_failed.emit("Unable to check camera detections.")
            return
        self.detections_listed.emit(names)

    def _read_capabilities(self, session: CS2Session, user: str, password: str) -> None:
        write_command(session, make_cgi_request(CAMERA_STATUS_PATH, user, password))
        status = read_response_fields(session, LOGIN_RESPONSE_COMMAND, {}, SETTING_RESPONSE_SECONDS) or {}
        write_command(session, make_cgi_request(WHITE_LIGHT_STATUS_PATH, user, password))
        light = read_response_fields(
            session,
            TRANSPARENT_RESPONSE_COMMAND,
            {"cmd": WHITE_LIGHT_COMMAND, "command": WHITE_LIGHT_STATUS_COMMAND},
            SETTING_RESPONSE_SECONDS,
        )
        light_on = None
        if light is not None and "lightStatus" in light and status.get("support_manual_light", "1") == "1":
            light_on = light["lightStatus"] != WHITE_LIGHT_OFF_STATUS
        self.capabilities_found.emit(available_qualities(status), light_on)

    def _process_setting(self, session: CS2Session, user: str, password: str) -> None:
        try:
            name, value = self.settings.get_nowait()
        except queue.Empty:
            return
        try:
            if name == SETTING_LIGHT:
                write_command(session, make_cgi_request(white_light_path(bool(value)), user, password))
                accepted = (
                    read_response_fields(
                        session,
                        TRANSPARENT_RESPONSE_COMMAND,
                        {"cmd": WHITE_LIGHT_COMMAND, "command": WHITE_LIGHT_SET_COMMAND},
                        SETTING_RESPONSE_SECONDS,
                    )
                    is not None
                )
            else:
                write_command(session, make_cgi_request(video_quality_path(str(value)), user, password))
                response = read_command_result(
                    session, (CAMERA_CONTROL_RESPONSE_COMMAND,), timeout=SETTING_RESPONSE_SECONDS
                )
                accepted = response is not None and response[1] == 0
        except (CS2Error, OSError):
            accepted = False
        if accepted:
            self.setting_completed.emit(name, value)
        elif not self.stop_requested.is_set():
            self.setting_failed.emit(name, "The camera rejected the setting.")

    def _record_frame(self, frame: bytes, keyframe: bool, now: float) -> None:
        if self.recorder is not None and self.recorder.path != self.recording_request:
            self._finish_recording()
        if self.recorder is None and self.recording_request is not None:
            self.recorder = VideoRecorder(self.recording_request)
        if self.recorder is None:
            return
        was_started = self.recorder.started
        try:
            if self.recorder.write(frame, keyframe, now) and not was_started:
                self.recording_started.emit()
        except OSError:
            self.recording_request = None
            if self.recorder.started:
                self._finish_recording()
            else:
                self.recorder = None
                self.recording_failed.emit("Unable to write the recording.")

    def _finish_recording(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is None:
            return
        if not recorder.started:
            self.recording_failed.emit("Recording stopped before any video arrived.")
            return
        threading.Thread(target=self._save_recording, args=(recorder,)).start()

    def _save_recording(self, recorder: VideoRecorder) -> None:
        try:
            self.recording_saved.emit(str(recorder.finish()))
        except OSError as ex:
            print(f"Recording error: {ex}", file=sys.stderr, flush=True)
            self.recording_failed.emit("Unable to save the recording.")

    def _send_sound_command(
        self, session: CS2Session, user: str, password: str, enabled: bool
    ) -> None:
        stream_id = 7 if enabled else 16
        path = f"audiostream.cgi?streamid={stream_id}&"
        write_command(session, make_cgi_request(path, user, password))
        response = read_command_result(session, (AUDIO_RESPONSE_COMMAND,), timeout=5)
        if response is None or response[1] != 0:
            raise CS2Error("The camera rejected the sound request.")

    def _update_sound(self, session: CS2Session, user: str, password: str) -> None:
        enabled = self.sound_requested.is_set()
        if enabled == self.sound_active:
            return
        if enabled:
            sound_started = False
            try:
                self._send_sound_command(session, user, password, True)
                sound_started = True
                self.audio_player = subprocess.Popen(
                    [
                        "ffplay",
                        "-nodisp",
                        "-loglevel",
                        "error",
                        "-f",
                        "alaw",
                        "-ar",
                        "8000",
                        "-ac",
                        "1",
                        "-i",
                        "pipe:0",
                    ],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
                self.sound_active = True
                self.sound_changed.emit(True)
            except (CS2Error, OSError):
                self.sound_requested.clear()
                if sound_started:
                    try:
                        self._send_sound_command(session, user, password, False)
                    except CS2Error:
                        pass
                self._close_audio_player()
                self.sound_failed.emit("Camera sound is unavailable.")
        else:
            try:
                self._send_sound_command(session, user, password, False)
            except CS2Error:
                self.sound_failed.emit("The camera did not acknowledge sound stop.")
            self.sound_active = False
            self._close_audio_player()
            self.sound_changed.emit(False)

    def _play_audio(self, frame: bytes) -> None:
        if not self.sound_active or self.audio_player is None or self.audio_player.stdin is None:
            return
        try:
            self.audio_player.stdin.write(frame)
        except (BrokenPipeError, OSError):
            self.sound_requested.clear()
            self.sound_failed.emit("The audio player stopped unexpectedly.")

    def _close_audio_player(self) -> None:
        if self.audio_player is None:
            return
        if self.audio_player.stdin is not None:
            try:
                self.audio_player.stdin.close()
            except OSError:
                pass
        if self.audio_player.poll() is None:
            self.audio_player.terminate()
            try:
                self.audio_player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.audio_player.kill()
                self.audio_player.wait(timeout=2)
        self.audio_player = None

    def _process_control(self, session: CS2Session, user: str, password: str) -> None:
        try:
            commands = self.controls.get_nowait()
        except queue.Empty:
            return
        try:
            for command in commands:
                if self.stop_requested.is_set():
                    break
                if command in MOTOR_COMMANDS:
                    self._move_one_step(session, user, password, command)
                else:
                    self._send_command(session, user, password, PRESET_COMMANDS[command])
                    self._require_control_response(session)
            if not self.stop_requested.is_set():
                self.control_completed.emit(commands[0] if len(commands) == 1 else "Drag")
        except (CS2Error, OSError):
            if not self.stop_requested.is_set():
                self.control_failed.emit("Camera movement failed.")

    def _send_command(self, session: CS2Session, user: str, password: str, value: int) -> None:
        path = f"decoder_control.cgi?command={value}&onestep=0&"
        write_command(session, make_cgi_request(path, user, password))

    def _require_control_response(self, session: CS2Session) -> None:
        response = read_command_result(session, (PTZ_RESPONSE_COMMAND,), timeout=5)
        if response is None or response[1] != 0:
            raise CS2Error("The camera rejected the movement command.")

    def _move_one_step(
        self, session: CS2Session, user: str, password: str, direction: str
    ) -> None:
        start, stop = MOTOR_COMMANDS[direction]
        self._send_command(session, user, password, start)
        try:
            time.sleep(MOTOR_PULSE_SECONDS)
        finally:
            self._send_command(session, user, password, stop)
        self._require_control_response(session)
        self._require_control_response(session)


@dataclass(frozen=True)
class RecordingSpan:
    start: datetime
    end: datetime
    first: CardRecording


def merge_recordings(recordings: list[CardRecording], gap: timedelta) -> list[RecordingSpan]:
    spans: list[RecordingSpan] = []
    for recording in sorted(recordings, key=lambda item: item.start):
        if spans and recording.start <= spans[-1].end + gap:
            last = spans[-1]
            spans[-1] = RecordingSpan(last.start, max(last.end, recording.end), last.first)
        else:
            spans.append(RecordingSpan(recording.start, recording.end, recording))
    return spans


@dataclass(frozen=True)
class CardFrame:
    timestamp: float
    frame_type: int
    body: bytes


class ReplayBuffer:
    def __init__(self, recording: CardRecording) -> None:
        self.recording = recording
        self.frames: list[CardFrame] = []
        self.ended = False
        self.condition = threading.Condition()

    def add(self, frame_type: int, timestamp: float, body: bytes) -> None:
        with self.condition:
            self.frames.append(CardFrame(timestamp, frame_type, body))
            self.condition.notify_all()

    def end(self) -> None:
        with self.condition:
            self.ended = True
            self.condition.notify_all()

    def frame(self, index: int, timeout: float) -> CardFrame | None:
        with self.condition:
            if index >= len(self.frames) and not self.ended:
                self.condition.wait(timeout)
            return self.frames[index] if index < len(self.frames) else None

    def exhausted(self, index: int) -> bool:
        with self.condition:
            return self.ended and index >= len(self.frames)

    def start_index(self, timestamp: float, timeout: float) -> int | None:
        deadline = time.monotonic() + timeout
        with self.condition:
            while True:
                reached = bool(self.frames) and self.frames[-1].timestamp >= timestamp
                if reached or self.ended:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.condition.wait(remaining)
            start = None
            for index, frame in enumerate(self.frames):
                if frame.timestamp > timestamp and start is not None:
                    break
                if frame.frame_type == KEY_FRAME_TYPE:
                    start = index
            return start


class ReplayPacer(threading.Thread):
    def __init__(
        self,
        video_output: BinaryIO,
        position: Callable[[float], None],
        finished: Callable[[str], None],
    ) -> None:
        super().__init__(daemon=True)
        self.video_output = video_output
        self.position = position
        self.finished = finished
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.stop_requested = threading.Event()
        self.buffer: ReplayBuffer | None = None
        self.target: float | None = None
        self.index = 0
        self.playing = False
        self.speed = 1.0
        self.sound = False
        self.audio_player: subprocess.Popen[bytes] | None = None
        self.anchor: tuple[float, float] | None = None
        self.last_position = 0.0

    def load(self, buffer: ReplayBuffer, timestamp: float) -> None:
        with self.lock:
            self.buffer = buffer
            self.target = timestamp
            self.anchor = None
            self.playing = True
        self.wake.set()

    def seek(self, timestamp: float) -> None:
        with self.lock:
            self.target = timestamp
            self.anchor = None
            self.playing = True
        self.wake.set()

    def set_playing(self, playing: bool) -> None:
        with self.lock:
            self.playing = playing
            self.anchor = None
        self.wake.set()

    def set_speed(self, speed: float) -> None:
        with self.lock:
            self.speed = speed
            self.anchor = None
        self.wake.set()

    def set_sound(self, enabled: bool) -> None:
        with self.lock:
            self.sound = enabled
        if not enabled:
            self._close_audio()

    def stop(self) -> None:
        self.stop_requested.set()
        self.wake.set()

    def run(self) -> None:
        try:
            while not self.stop_requested.is_set():
                self._step()
        finally:
            self._close_audio()

    def _step(self) -> None:
        with self.lock:
            buffer = self.buffer
            target = self.target
            playing = self.playing
        if buffer is None or not playing:
            self.wake.wait(PACER_IDLE_SECONDS)
            self.wake.clear()
            return
        if target is not None:
            start = buffer.start_index(target, PACER_IDLE_SECONDS)
            if start is None:
                return
            with self.lock:
                if self.target == target and self.buffer is buffer:
                    self.index = start
                    self.target = None
                    self.anchor = None
            return
        with self.lock:
            index = self.index
            speed = self.speed
            anchor = self.anchor
        frame = buffer.frame(index, PACER_IDLE_SECONDS)
        if frame is None:
            if buffer.exhausted(index):
                with self.lock:
                    self.playing = False
                self.finished(buffer.recording.name)
            return
        now = time.monotonic()
        if anchor is None or now - (anchor[0] + (frame.timestamp - anchor[1]) / speed) > PACER_RESYNC_SECONDS:
            anchor = (now, frame.timestamp)
            with self.lock:
                self.anchor = anchor
        delay = anchor[0] + (frame.timestamp - anchor[1]) / speed - now
        if delay > 0:
            self.wake.wait(min(delay, PACER_IDLE_SECONDS))
            self.wake.clear()
            return
        with self.lock:
            if self.buffer is not buffer or self.index != index or self.target is not None:
                return
            self.index = index + 1
            sound = self.sound and speed == 1.0
        self._output(frame, sound)
        if now - self.last_position >= PACER_POSITION_SECONDS:
            self.last_position = now
            self.position(frame.timestamp)

    def _output(self, frame: CardFrame, sound: bool) -> None:
        try:
            if frame.frame_type in CARD_VIDEO_FRAME_TYPES:
                self.video_output.write(frame.body)
            elif frame.frame_type == CARD_AUDIO_FRAME_TYPE and sound and frame.body:
                audio = self._audio_input()
                if audio is not None:
                    audio.write(adts_header(len(frame.body)) + frame.body)
        except (BrokenPipeError, OSError):
            if frame.frame_type in CARD_VIDEO_FRAME_TYPES:
                self.stop_requested.set()
            else:
                self._close_audio()

    def _audio_input(self) -> BinaryIO | None:
        if self.audio_player is None or self.audio_player.poll() is not None:
            try:
                self.audio_player = subprocess.Popen(
                    ["ffplay", "-nodisp", "-loglevel", "error", "-f", "aac", "-i", "pipe:0"],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except OSError:
                self.audio_player = None
                return None
        return self.audio_player.stdin

    def _close_audio(self) -> None:
        player = self.audio_player
        self.audio_player = None
        if player is None:
            return
        if player.stdin is not None:
            try:
                player.stdin.close()
            except OSError:
                pass
        if player.poll() is None:
            player.terminate()
            try:
                player.wait(timeout=2)
            except subprocess.TimeoutExpired:
                player.kill()
                player.wait(timeout=2)


class ReplayWorker(QThread):
    day_listed = pyqtSignal(str, list)
    progress = pyqtSignal(str, float)
    downloaded = pyqtSignal(str, str)
    failed = pyqtSignal(str)

    def __init__(self, device: AccountDevice, camera_password: str, directory: Path) -> None:
        super().__init__()
        self.device = device
        self.camera_password = camera_password
        self.directory = directory
        self.listings: queue.Queue[str] = queue.Queue()
        self.downloads: queue.Queue[ReplayBuffer] = queue.Queue()
        self.wake = threading.Event()
        self.stop_requested = threading.Event()
        self.cancel_download = threading.Event()
        self.session: CS2Session | None = None
        self.login_user = ""
        self.login_password = ""

    def list_day(self, day: str) -> None:
        self.listings.put(day)
        self.wake.set()

    def download(self, buffer: ReplayBuffer) -> None:
        self.cancel_download.set()
        self.downloads.put(buffer)
        self.wake.set()

    def stop(self) -> None:
        self.stop_requested.set()
        self.cancel_download.set()
        self.wake.set()

    def run(self) -> None:
        try:
            while not self.stop_requested.is_set():
                request = self._next_request()
                if request is None:
                    continue
                for attempt in range(REPLAY_ATTEMPTS):
                    try:
                        self._handle(*request)
                        break
                    except Exception as ex:
                        print(f"Replay error: {type(ex).__name__}: {ex}", file=sys.stderr, flush=True)
                        self._close_session()
                        retry = (
                            attempt + 1 < REPLAY_ATTEMPTS
                            and not self.stop_requested.is_set()
                            and not (
                                isinstance(request[1], ReplayBuffer)
                                and (request[1].frames or self.cancel_download.is_set())
                            )
                        )
                        if retry:
                            continue
                        if isinstance(request[1], ReplayBuffer):
                            request[1].end()
                        if not self.stop_requested.is_set():
                            self.failed.emit("The camera recording could not be read.")
                        break
        finally:
            self._close_session()
            self.camera_password = ""

    def _next_request(self) -> tuple[str, object] | None:
        self.wake.wait(PACER_IDLE_SECONDS)
        self.wake.clear()
        try:
            return REPLAY_LIST_REQUEST, self.listings.get_nowait()
        except queue.Empty:
            pass
        latest: ReplayBuffer | None = None
        while True:
            try:
                buffer = self.downloads.get_nowait()
            except queue.Empty:
                break
            if latest is not None:
                latest.end()
            latest = buffer
        if latest is None:
            return None
        if not self.listings.empty() or not self.downloads.empty():
            self.wake.set()
        return REPLAY_DOWNLOAD_REQUEST, latest

    def _serve_listings(self, session: CS2Session) -> None:
        while True:
            try:
                day = self.listings.get_nowait()
            except queue.Empty:
                return
            LOG.info("Replay request list %s during a download", day)
            self.day_listed.emit(day, list_recordings(session, self.login_user, self.login_password, day))

    def _open_session(self) -> CS2Session:
        if self.session is not None:
            return self.session
        connection = prepare_camera_connection(self.device, lambda message: None, self.stop_requested)
        if connection is None:
            raise CS2Error("Replay stopped.")
        session = ReliableCS2Session(*connection)
        try:
            session.connect(timeout=55)
            login = authenticate_camera(
                session, select_camera_password(self.device.device_password, self.camera_password)
            )
        except Exception:
            session.close()
            raise
        self.session = session
        self.login_user = login.user
        self.login_password = login.password
        return session

    def _close_session(self) -> None:
        if self.session is not None:
            self.session.close()
            self.session = None

    def _handle(self, kind: str, value: object) -> None:
        session = self._open_session()
        LOG.info("Replay request %s %s", kind, value.recording.name if isinstance(value, ReplayBuffer) else value)
        if kind == REPLAY_LIST_REQUEST:
            day = str(value)
            self.day_listed.emit(day, list_recordings(session, self.login_user, self.login_password, day))
            if not self.listings.empty() or not self.downloads.empty():
                self.wake.set()
            return
        buffer = value
        assert isinstance(buffer, ReplayBuffer)
        self.cancel_download.clear()
        recording = buffer.recording
        writer = CardClipWriter(self.directory / f"{Path(recording.name).stem}{MATROSKA_SUFFIX}", self.directory)

        def keep(frame_type: int, timestamp: float, body: bytes) -> None:
            buffer.add(frame_type, timestamp, body)
            writer.add(frame_type, timestamp, body)

        try:
            complete = download_card_recording(
                session,
                self.login_user,
                self.login_password,
                recording,
                keep,
                lambda fraction: self.progress.emit(recording.name, fraction),
                lambda: self.cancel_download.is_set() or self.stop_requested.is_set(),
                lambda: self._serve_listings(session),
            )
        except Exception:
            writer.discard()
            raise
        buffer.end()
        LOG.info("Download of %s %s with %d frames", recording.name, "finished" if complete else "stopped", len(buffer.frames))
        if not complete:
            writer.discard()
            return
        try:
            clip = str(writer.finish())
        except OSError as ex:
            print(f"Replay clip error: {recording.name}: {ex}", file=sys.stderr, flush=True)
            clip = ""
        self.downloaded.emit(recording.name, clip)


class VideoWidget(QWidget):
    clicked = pyqtSignal()
    dragged = pyqtSignal(int, int)
    drag_moved = pyqtSignal(int, int)
    wheel_zoomed = pyqtSignal(int, int, int)
    double_clicked = pyqtSignal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self.drag_start: tuple[int, int] | None = None
        self.drag_last: tuple[int, int] | None = None
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.clicked.emit)
        self.controls_overlay: QWidget | None = None
        self.recording_badge: QWidget | None = None
        self.x_display = xdisplay.Display() if QApplication.platformName() == "xcb" else None
        self.input_window = None
        self.input_timer = QTimer(self)
        self.input_timer.timeout.connect(self.read_mouse_events)
        if self.x_display is not None:
            self.input_timer.start(20)

    def set_recording_badge(self, badge: QWidget) -> None:
        self.recording_badge = badge

    def set_controls_overlay(self, overlay: QWidget) -> None:
        self.controls_overlay = overlay
        if self.x_display is None:
            self.place_overlay()
            return
        parent = self.x_display.create_resource_object("window", int(self.winId()))
        parent.change_attributes(event_mask=X11.SubstructureNotifyMask)
        self.input_window = parent.create_window(
            0,
            0,
            self.width(),
            self.height(),
            0,
            0,
            X11.InputOnly,
            X11.CopyFromParent,
            event_mask=X11.ButtonPressMask | X11.ButtonReleaseMask | X11.PointerMotionMask,
        )
        self.input_window.map()
        self.place_overlay()

    def raise_interaction_layer(self) -> None:
        if self.input_window is not None:
            self.input_window.configure(stack_mode=X11.Above)
        for overlay in (self.controls_overlay, self.recording_badge):
            if overlay is not None and overlay.isVisible():
                overlay.raise_()
        if self.x_display is not None:
            self.x_display.flush()

    def place_overlay(self) -> None:
        if self.recording_badge is not None:
            badge_size = self.recording_badge.sizeHint()
            self.recording_badge.setGeometry(
                QRect(
                    self.mapToGlobal(QPoint((self.width() - badge_size.width()) // 2, OVERLAY_MARGIN)),
                    badge_size,
                )
            )
        if self.controls_overlay is None:
            return
        overlay_layout = self.controls_overlay.layout()
        if overlay_layout is not None:
            overlay_layout.invalidate()
            overlay_layout.activate()
        height = self.controls_overlay.sizeHint().height()
        width = min(max(0, self.width() - 2 * OVERLAY_MARGIN), self.controls_overlay.sizeHint().width())
        position = self.mapToGlobal(
            QPoint(
                (self.width() - width) // 2,
                max(OVERLAY_MARGIN, self.height() - height - OVERLAY_MARGIN),
            )
        )
        self.controls_overlay.setGeometry(QRect(position, QSize(width, height)))
        self.raise_interaction_layer()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if self.input_window is not None:
            self.input_window.configure(width=self.width(), height=self.height())
            self.x_display.flush()
        self.place_overlay()

    def _start_drag(self, x: int, y: int) -> None:
        self.drag_start = (x, y)
        self.drag_last = (x, y)

    def _move_drag(self, x: int, y: int) -> None:
        if self.drag_last is None:
            return
        dx = x - self.drag_last[0]
        dy = y - self.drag_last[1]
        self.drag_last = (x, y)
        if dx or dy:
            self.drag_moved.emit(dx, dy)

    def _finish_drag(self, x: int, y: int) -> None:
        self.drag_last = None
        if self.drag_start is None:
            return
        dx = x - self.drag_start[0]
        dy = y - self.drag_start[1]
        self.drag_start = None
        if abs(dx) < DRAG_PIXELS_PER_STEP // 2 and abs(dy) < DRAG_PIXELS_PER_STEP // 2:
            if self.click_timer.isActive():
                self.click_timer.stop()
                self.double_clicked.emit(x, y)
            else:
                self.click_timer.start(QApplication.doubleClickInterval())
        else:
            self.dragged.emit(dx, dy)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_drag(round(event.position().x()), round(event.position().y()))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        self._move_drag(round(event.position().x()), round(event.position().y()))

    def wheelEvent(self, event: QWheelEvent) -> None:
        if event.angleDelta().y():
            self.wheel_zoomed.emit(
                1 if event.angleDelta().y() > 0 else -1, round(event.position().x()), round(event.position().y())
            )

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._finish_drag(round(event.position().x()), round(event.position().y()))
            event.accept()
        else:
            super().mouseReleaseEvent(event)

    def read_mouse_events(self) -> None:
        if self.x_display is None:
            return
        motion: tuple[int, int] | None = None
        while self.x_display.pending_events():
            event = self.x_display.next_event()
            if self.input_window is None:
                continue
            if event.type == X11.MapNotify and event.window != self.input_window:
                self.raise_interaction_layer()
                continue
            if event.type == X11.MotionNotify:
                motion = (event.event_x, event.event_y)
                continue
            if event.type not in (X11.ButtonPress, X11.ButtonRelease):
                continue
            if motion is not None:
                self._move_drag(*motion)
                motion = None
            if event.type == X11.ButtonPress and event.detail in (X11_WHEEL_UP, X11_WHEEL_DOWN):
                self.wheel_zoomed.emit(1 if event.detail == X11_WHEEL_UP else -1, event.event_x, event.event_y)
            elif event.type == X11.ButtonPress and event.detail == 1:
                self._start_drag(event.event_x, event.event_y)
            elif event.type == X11.ButtonRelease and event.detail == 1 and self.drag_start is not None:
                self._finish_drag(event.event_x, event.event_y)
        if motion is not None:
            self._move_drag(*motion)

    def closeEvent(self, event: object) -> None:
        self.input_timer.stop()
        if self.x_display is not None:
            self.x_display.close()
        super().closeEvent(event)


class TimelineWidget(QWidget):
    seek_requested = pyqtSignal(object)
    range_changed = pyqtSignal(object, object)

    def __init__(self) -> None:
        super().__init__()
        self.setMinimumHeight(TIMELINE_HEIGHT)
        self.setMaximumHeight(TIMELINE_HEIGHT)
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.center = datetime.now()
        self.zoom_index = TIMELINE_DEFAULT_ZOOM
        self.recordings: list[CardRecording] = []
        self.drag_x: float | None = None
        self.drag_center = self.center
        self.dragged = False

    def sizeHint(self) -> QSize:
        return QSize(TIMELINE_PREFERRED_WIDTH, TIMELINE_HEIGHT)

    @property
    def span(self) -> float:
        return TIMELINE_SPANS[self.zoom_index]

    def visible_range(self) -> tuple[datetime, datetime]:
        half = timedelta(seconds=self.span / 2)
        return self.center - half, self.center + half

    def set_recordings(self, recordings: list[CardRecording]) -> None:
        self.recordings = recordings
        self.update()

    def set_center(self, moment: datetime) -> None:
        if self.drag_x is not None:
            return
        self.center = moment
        self.update()
        self.range_changed.emit(*self.visible_range())

    def zoom(self, step: int) -> None:
        index = min(len(TIMELINE_SPANS) - 1, max(0, self.zoom_index + step))
        if index == self.zoom_index:
            return
        self.zoom_index = index
        self.update()
        self.range_changed.emit(*self.visible_range())

    def time_at(self, x: float) -> datetime:
        return self.center + timedelta(seconds=(x - self.width() / 2) * self.span / max(1, self.width()))

    def x_at(self, moment: datetime) -> float:
        return self.width() / 2 + (moment - self.center).total_seconds() * self.width() / self.span

    def label_step(self) -> int:
        return next(
            (step for step in TIMELINE_LABEL_STEPS if self.span / step <= TIMELINE_MAX_LABELS),
            TIMELINE_LABEL_STEPS[-1],
        )

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        width = self.width()
        painter.fillRect(0, 0, width, TIMELINE_HEADER_HEIGHT, TIMELINE_HEADER_COLOR)
        painter.fillRect(0, TIMELINE_HEADER_HEIGHT, width, self.height() - TIMELINE_HEADER_HEIGHT, TIMELINE_EMPTY_COLOR)
        start, end = self.visible_range()
        bar_top = TIMELINE_HEADER_HEIGHT + TIMELINE_TICK_HEIGHT + 4
        bar_height = self.height() - bar_top - 6
        gap = timedelta(seconds=TIMELINE_MERGE_SECONDS)
        continuous = merge_recordings([recording for recording in self.recordings if not recording.detection], gap)
        detections = merge_recordings([recording for recording in self.recordings if recording.detection], gap)
        for spans, color in ((continuous, TIMELINE_RECORDING_COLOR), (detections, TIMELINE_DETECTION_COLOR)):
            for span in spans:
                if span.end < start or span.start > end:
                    continue
                left = max(0.0, self.x_at(span.start))
                right = min(float(width), self.x_at(span.end))
                painter.fillRect(QRectF(left, bar_top, max(2.0, right - left), bar_height), color)
        step = self.label_step()
        first = datetime.fromtimestamp((camera_timestamp(start) // step + 1) * step, timezone.utc).replace(tzinfo=None)
        painter.setPen(TIMELINE_LABEL_COLOR)
        moment = first
        middle = width / 2
        while moment <= end:
            x = self.x_at(moment)
            label = f"{moment:%d/%m}" if moment.hour == 0 and moment.minute == 0 else f"{moment:%H:%M}"
            if abs(x - middle) > TIMELINE_CURSOR_LABEL_WIDTH / 2 + TIMELINE_LABEL_WIDTH / 2:
                painter.drawText(
                    QRectF(x - TIMELINE_LABEL_WIDTH / 2, 0, TIMELINE_LABEL_WIDTH, TIMELINE_HEADER_HEIGHT),
                    Qt.AlignmentFlag.AlignCenter,
                    label,
                )
            painter.setPen(TIMELINE_TICK_COLOR)
            painter.drawLine(QPointF(x, TIMELINE_HEADER_HEIGHT), QPointF(x, TIMELINE_HEADER_HEIGHT + TIMELINE_TICK_HEIGHT))
            painter.setPen(TIMELINE_LABEL_COLOR)
            moment += timedelta(seconds=step)
        cursor_label = QRectF(middle - TIMELINE_CURSOR_LABEL_WIDTH / 2, 3, TIMELINE_CURSOR_LABEL_WIDTH, TIMELINE_HEADER_HEIGHT - 6)
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(TIMELINE_CURSOR_COLOR)
        painter.drawRoundedRect(cursor_label, cursor_label.height() / 2, cursor_label.height() / 2)
        painter.setPen(TIMELINE_CURSOR_TEXT_COLOR)
        painter.drawText(cursor_label, Qt.AlignmentFlag.AlignCenter, f"{self.center:%a %d/%m %H:%M:%S}")
        painter.setPen(QPen(TIMELINE_CURSOR_COLOR, 2))
        painter.drawLine(QPointF(middle, TIMELINE_HEADER_HEIGHT), QPointF(middle, self.height()))
        painter.end()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton:
            return
        self.drag_x = event.position().x()
        self.drag_center = self.center
        self.dragged = False
        self.setCursor(Qt.CursorShape.ClosedHandCursor)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self.drag_x is None:
            return
        offset = event.position().x() - self.drag_x
        if abs(offset) >= TIMELINE_DRAG_PIXELS:
            self.dragged = True
        if self.dragged:
            self.center = self.drag_center - timedelta(seconds=offset * self.span / max(1, self.width()))
            self.update()

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() != Qt.MouseButton.LeftButton or self.drag_x is None:
            return
        if not self.dragged:
            self.center = self.time_at(event.position().x())
        self.drag_x = None
        self.setCursor(Qt.CursorShape.OpenHandCursor)
        self.update()
        self.range_changed.emit(*self.visible_range())
        self.seek_requested.emit(self.center)

    def wheelEvent(self, event: QWheelEvent) -> None:
        self.zoom(-1 if event.angleDelta().y() > 0 else 1)


class ReplayController(QObject):
    status_changed = pyqtSignal(str)
    position_changed = pyqtSignal(float)
    playback_finished = pyqtSignal(str)
    playing_changed = pyqtSignal(bool)
    clip_available = pyqtSignal(bool)

    def __init__(self, device: AccountDevice, camera_password: str, timeline: TimelineWidget) -> None:
        super().__init__()
        self.device = device
        self.timeline = timeline
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_replay_")
        self.recordings: dict[str, list[CardRecording]] = {}
        self.requested_days: set[str] = set()
        self.saved_clips: dict[str, Path | None] = {}
        self.start_request: datetime | None = None
        self.current: ReplayBuffer | None = None
        self.prefetched: ReplayBuffer | None = None
        self.target_fraction = 0.0
        self.loading_target = datetime.now()
        self.waiting_for_target = False
        self.pending_jump = 0
        self.jump_origin = datetime.now()
        self.last_position: datetime | None = None
        self.reloads = 0
        self.playing = False
        self.sound = True
        self.speed_index = 0
        self.pacer: ReplayPacer | None = None
        self.worker = ReplayWorker(device, camera_password, Path(self.directory.name))
        self.worker.day_listed.connect(self.on_day_listed)
        self.worker.progress.connect(self.on_progress)
        self.worker.downloaded.connect(self.on_downloaded)
        self.worker.failed.connect(self.status_changed.emit)
        self.worker.failed.connect(self.cancel_jump)
        self.position_changed.connect(self.on_position)
        self.playback_finished.connect(self.on_finished)
        self.timeline.seek_requested.connect(self.seek)
        self.timeline.range_changed.connect(self.load_visible_days)
        self.worker.start()

    @property
    def speed(self) -> int:
        return REPLAY_SPEEDS[self.speed_index]

    def start(self, video_output: BinaryIO, start: datetime | None) -> None:
        self.start_request = start
        self.pacer = ReplayPacer(video_output, self.position_changed.emit, self.playback_finished.emit)
        self.pacer.set_sound(self.sound)
        self.pacer.start()
        self.timeline.set_recordings(self.all_recordings())
        self.timeline.set_center(start or datetime.now())
        self.status_changed.emit("Loading recordings...")
        visible_start, visible_end = self.timeline.visible_range()
        self.load_visible_days(visible_start - timedelta(days=1), visible_end)

    def stop(self) -> None:
        self.timeline.seek_requested.disconnect(self.seek)
        self.timeline.range_changed.disconnect(self.load_visible_days)
        self.worker.stop()
        if self.pacer is not None:
            self.pacer.stop()
            self.pacer.join(timeout=3)
            self.pacer = None
        self.worker.wait(REPLAY_WORKER_WAIT_MS)
        self.directory.cleanup()

    def load_visible_days(self, start: datetime, end: datetime) -> None:
        day = start.date()
        today = datetime.now().date()
        while day <= min(end.date(), today):
            key = day.strftime(RECORD_DAY_FORMAT)
            if key not in self.requested_days:
                self.requested_days.add(key)
                self.worker.list_day(key)
            day += timedelta(days=1)

    def all_recordings(self) -> list[CardRecording]:
        return sorted(
            (recording for recordings in self.recordings.values() for recording in recordings),
            key=lambda recording: recording.name,
        )

    def on_day_listed(self, day: str, recordings: list[CardRecording]) -> None:
        self.recordings[day] = recordings
        self.timeline.set_recordings(self.all_recordings())
        if self.pending_jump:
            self.continue_jump()
            return
        if self.current is not None:
            return
        if self.start_request is not None:
            if any(recording.start <= self.start_request < recording.end for recording in recordings):
                self.seek(self.start_request)
            return
        if day == datetime.now().strftime(RECORD_DAY_FORMAT):
            latest = self.all_recordings()
            if latest:
                self.seek(max(latest[-1].start, latest[-1].end - timedelta(seconds=REPLAY_DEFAULT_REWIND_SECONDS)))
            else:
                self.status_changed.emit("No recording today.")

    def jump_to_detection(self, direction: int) -> None:
        self.jump_origin = self.timeline.center
        self.pending_jump = direction
        self.continue_jump()

    def detection_events(self) -> list[RecordingSpan]:
        detections = [recording for recording in self.all_recordings() if recording.detection]
        return merge_recordings(detections, timedelta(seconds=DETECTION_MERGE_SECONDS))

    def continue_jump(self) -> None:
        direction = self.pending_jump
        events = self.detection_events()
        recent = timedelta(seconds=DETECTION_RECENT_SECONDS)
        containing = next(
            (event for event in events if event.start <= self.jump_origin <= event.end + recent),
            None,
        )
        margin = timedelta(seconds=DETECTION_JUMP_MARGIN_SECONDS)
        if direction < 0:
            limit = containing.start if containing is not None else self.jump_origin
            found = next((event for event in reversed(events) if event.start < limit - margin), None)
        else:
            limit = containing.end if containing is not None else self.jump_origin + margin
            found = next((event for event in events if event.start > limit), None)
        if found is not None:
            self.pending_jump = 0
            LOG.info("Jump %s from %s to detection %s", "back" if direction < 0 else "forward", self.jump_origin, found.first.name)
            self.seek(found.start, found.first)
            return
        loaded = sorted(self.recordings)
        today = datetime.now().strftime(RECORD_DAY_FORMAT)
        if loaded and (direction < 0 or loaded[-1] < today):
            step = timedelta(days=-1 if direction < 0 else 1)
            edge = loaded[0] if direction < 0 else loaded[-1]
            neighbour = (datetime.strptime(edge, RECORD_DAY_FORMAT) + step).strftime(RECORD_DAY_FORMAT)
            exhausted = direction < 0 and (not self.recordings[edge] or len(loaded) >= MAX_REPLAY_DAYS)
            if not exhausted:
                if neighbour not in self.requested_days:
                    self.requested_days.add(neighbour)
                    self.worker.list_day(neighbour)
                self.status_changed.emit(
                    "Looking for an earlier detection..." if direction < 0 else "Looking for a later detection..."
                )
                return
        LOG.info("No %s detection from %s with days %s", "earlier" if direction < 0 else "later", self.jump_origin, loaded)
        self.pending_jump = 0
        self.status_changed.emit("No earlier detection." if direction < 0 else "No later detection.")

    def cancel_jump(self) -> None:
        self.pending_jump = 0

    def recording_at(self, moment: datetime) -> CardRecording | None:
        recordings = self.all_recordings()
        for recording in recordings:
            if recording.start <= moment < recording.end:
                return recording
        return next((recording for recording in recordings if recording.start > moment), None)

    def seek(self, moment: datetime, preferred: CardRecording | None = None) -> None:
        recording = preferred or self.recording_at(moment)
        LOG.info("Seek to %s in %s", moment, recording.name if recording is not None else "no recording")
        if recording is None:
            self.status_changed.emit("No recording at this time.")
            return
        target = max(moment, recording.start)
        self.timeline.set_center(target)
        self.set_playing(True)
        self.target_fraction = (target - recording.start).total_seconds() / max(1, recording.duration)
        self.waiting_for_target = True
        if self.current is not None and self.current.recording == recording and self.current.frames:
            if self.pacer is not None:
                self.pacer.seek(camera_timestamp(target))
            return
        self.clip_available.emit(self.saved_clips.get(recording.name) is not None)
        self.loading_target = target
        self.status_changed.emit(LOADING_STATUS.format(moment=target, percent=0))
        if self.prefetched is not None and self.prefetched.recording == recording:
            self.current = self.prefetched
            self.prefetched = None
            if recording.name in self.saved_clips:
                self.prefetch_following()
        else:
            self.prefetched = None
            self.current = ReplayBuffer(recording)
            self.worker.download(self.current)
        if self.pacer is not None:
            self.pacer.load(self.current, camera_timestamp(target))

    def on_position(self, timestamp: float) -> None:
        self.last_position = camera_time(timestamp)
        self.reloads = 0
        self.waiting_for_target = False
        moment = camera_time(timestamp)
        self.timeline.set_center(moment)
        self.status_changed.emit(f"Playback {moment:%d/%m/%Y %H:%M:%S}")

    def next_recording(self, recording: CardRecording) -> CardRecording | None:
        moment = recording.end
        others = [candidate for candidate in self.all_recordings() if candidate != recording]
        covering = [
            candidate
            for candidate in others
            if candidate.start <= moment < candidate.end - timedelta(seconds=RECORDING_CHAIN_TOLERANCE_SECONDS)
        ]
        if covering:
            return max(covering, key=lambda candidate: (candidate.detection, candidate.end))
        later = [candidate for candidate in others if candidate.start >= moment - timedelta(seconds=RECORDING_CHAIN_TOLERANCE_SECONDS)]
        return min(later, key=lambda candidate: candidate.start) if later else None

    def on_finished(self, name: str) -> None:
        if self.current is None or self.current.recording.name != name:
            return
        recording = self.current.recording
        resume = self.last_position
        if name not in self.saved_clips and resume is not None and resume < recording.end and self.reloads < MAX_RECORDING_RELOADS:
            self.reloads += 1
            self.current = None
            self.seek(resume, recording)
            return
        following = self.next_recording(recording)
        if following is None:
            self.set_playing(False)
            self.status_changed.emit("End of the recordings.")
            return
        self.seek(max(following.start, recording.end), following)

    def on_progress(self, name: str, fraction: float) -> None:
        if self.current is None or self.current.recording.name != name or not self.waiting_for_target:
            return
        percent = min(99, round(100 * fraction / max(self.target_fraction, LOADING_MIN_FRACTION)))
        self.status_changed.emit(LOADING_STATUS.format(moment=self.loading_target, percent=percent))

    def on_downloaded(self, name: str, path: str) -> None:
        self.saved_clips[name] = Path(path) if path else None
        if self.current is None or self.current.recording.name != name:
            return
        self.clip_available.emit(bool(path))
        self.prefetch_following()

    def prefetch_following(self) -> None:
        if self.current is None or self.prefetched is not None:
            return
        following = self.next_recording(self.current.recording)
        if following is not None:
            self.prefetched = ReplayBuffer(following)
            self.worker.download(self.prefetched)

    def set_playing(self, playing: bool) -> None:
        self.playing = playing
        if self.pacer is not None:
            self.pacer.set_playing(playing)
        self.playing_changed.emit(playing)

    def toggle_playing(self) -> None:
        if self.current is None:
            self.seek(self.timeline.center)
        else:
            self.set_playing(not self.playing)

    def toggle_sound(self) -> bool:
        self.sound = not self.sound
        if self.pacer is not None:
            self.pacer.set_sound(self.sound)
        return self.sound

    def change_speed(self) -> int:
        self.speed_index = (self.speed_index + 1) % len(REPLAY_SPEEDS)
        if self.pacer is not None:
            self.pacer.set_speed(float(self.speed))
        return self.speed

    def save_clip(self) -> Path | None:
        source = self.saved_clips.get(self.current.recording.name) if self.current is not None else None
        if source is None:
            return None
        target = media_directory() / f"{self.device.name}_{Path(self.current.recording.name).stem}{MATROSKA_SUFFIX}"
        shutil.copyfile(source, target)
        return target


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APPLICATION_NAME)
        self.setWindowIcon(QIcon(str(ICON_DIRECTORY / "app.svg")))
        self.resize(980, 590)
        self.quit_requested = False
        self.status_text = ""
        self.base_status = ""
        self.notice_timer = QTimer(self)
        self.notice_timer.setSingleShot(True)
        self.notice_timer.timeout.connect(self.restore_status)
        self.aspect_fitted = False
        self.tray: QSystemTrayIcon | None = None
        self.tray_actions: list[tuple[QAction, QPushButton]] = []
        self.window_hints: X11WindowHints | None = None
        self.normal_geometry: QRect | None = None
        self.devices: list[AccountDevice] = []
        self.account_worker: AccountWorker | None = None
        self.stream_worker: StreamWorker | None = None
        self.control_pending = False
        self.player: subprocess.Popen[bytes] | None = None
        self.mpv_directory: tempfile.TemporaryDirectory[str] | None = None
        self.mpv_socket: Path | None = None
        self.recording_path: Path | None = None
        self.light_on: bool | None = None
        self.setting_pending = False
        self.recording_started_at: float | None = None
        self.recording_timer = QTimer(self)
        self.recording_timer.timeout.connect(self.update_recording_badge)
        self.zoom_level = 0
        self.video_pan = (0.0, 0.0)
        self.close_pending = False
        self.stream_error = False
        self.stream_live = False
        self.sound_enabled = False
        self.retry_pending = False
        self.detection_worker: DetectionWorker | None = None
        self.replay: ReplayController | None = None
        self.pending_replay: tuple[datetime | None] | None = None
        self.keep_player = False
        self.latest_detection: datetime | None = None
        self.detection_timer = QTimer(self)
        self.detection_timer.timeout.connect(self.check_detections)
        self.detection_timer.start(DETECTION_POLL_MS)
        self.reconnect_attempts = 0
        self.reconnect_timer = QTimer(self)
        self.reconnect_timer.setSingleShot(True)
        self.reconnect_timer.timeout.connect(self.reconnect)
        self.overlay_timer = QTimer(self)
        self.overlay_timer.setSingleShot(True)
        self.overlay_timer.timeout.connect(self.hide_overlay)
        self.settings = QSettings(APPLICATION_NAME, APPLICATION_NAME)
        self.account_username = self.settings.value("account/username", "", str)
        self.account_secret = (
            stored_account_password(self.account_username) if self.account_username else None
        )
        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.video = VideoWidget()
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.setMinimumSize(640, 360)
        self.video.setStyleSheet("background-color: #171717;")
        self.video.clicked.connect(self.toggle_overlay)
        self.video.dragged.connect(self.move_by_drag)
        self.video.drag_moved.connect(self.pan_zoomed_video)
        self.video.wheel_zoomed.connect(self.change_zoom)
        self.video.double_clicked.connect(self.toggle_zoom_at)
        layout.addWidget(self.video, 1)
        self.overlay = ControlsOverlay(self.video)
        self.overlay.setObjectName("cameraControls")
        self.overlay.setStyleSheet(
            "#cameraControls QPushButton { color: white; background-color: transparent;"
            " border: none; border-radius: 6px; padding: 4px; }"
            "#cameraControls QPushButton:hover { background-color: rgba(255, 255, 255, 35); }"
            "#cameraControls #qualityButton { border: 2px solid white; border-radius: 8px;"
            " font-weight: 600; margin: 6px 2px; }"
            "#ptzPanel, #liveBar, #replayBar { background: transparent; }"
            "#cameraControls #pillButton { border: 2px solid white; border-radius: 8px;"
            " font-weight: 600; margin: 6px 2px; }"
            "QToolTip { color: white; background-color: rgb(32, 32, 32);"
            " border: 1px solid rgba(255, 255, 255, 60); border-radius: 6px; padding: 4px 8px; }"
            "#cameraControls QPushButton:disabled { color: rgba(255, 255, 255, 90);"
            " border-color: rgba(255, 255, 255, 90); }"
        )
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(24, 10, 24, 10)
        controls = QHBoxLayout()
        controls.setSpacing(12)
        controls.addStretch(1)
        self.quality_menu = QMenu(self)
        self.quality_group = QActionGroup(self)
        self.quality_actions: dict[str, QAction] = {}
        for quality in VIDEO_QUALITIES:
            action = self.quality_menu.addAction(quality)
            action.setCheckable(True)
            action.setChecked(quality == self.settings.value(QUALITY_SETTING, "", str))
            action.setVisible(False)
            action.triggered.connect(lambda checked=False, value=quality: self.choose_quality(value))
            self.quality_group.addAction(action)
            self.quality_actions[quality] = action
        self.quality_button = QPushButton(self.quality_label())
        self.quality_button.setObjectName("qualityButton")
        self.quality_button.setFixedSize(56, 44)
        self.quality_button.setEnabled(False)
        self.quality_button.hide()
        self.quality_button.clicked.connect(self.show_quality_menu)
        controls.addWidget(self.quality_button)
        self.replay_button = QPushButton("Playback")
        self.replay_button.setEnabled(False)
        self.replay_button.clicked.connect(lambda: self.enter_replay(None))
        controls.addWidget(self.replay_button)
        self.snapshot_button = QPushButton("Photo")
        self.snapshot_button.setToolTip("Save a picture of the live video")
        self.snapshot_button.setEnabled(False)
        self.snapshot_button.clicked.connect(self.take_snapshot)
        controls.addWidget(self.snapshot_button)
        self.record_button = QPushButton("Record")
        self.record_button.setToolTip("Record the live video locally")
        self.record_button.setEnabled(False)
        self.record_button.clicked.connect(self.toggle_recording)
        controls.addWidget(self.record_button)
        self.sound_button = QPushButton("Sound")
        self.sound_button.setEnabled(False)
        self.sound_button.clicked.connect(self.toggle_sound)
        controls.addWidget(self.sound_button)
        self.light_button = QPushButton("Light")
        self.light_button.setEnabled(False)
        self.light_button.hide()
        self.light_button.clicked.connect(self.toggle_light)
        controls.addWidget(self.light_button)
        self.zoom_out_button = QPushButton("−")
        self.zoom_out_button.setToolTip("Zoom out")
        self.zoom_out_button.setEnabled(False)
        self.zoom_out_button.clicked.connect(lambda: self.change_zoom(-1))
        controls.addWidget(self.zoom_out_button)
        self.zoom_in_button = QPushButton("+")
        self.zoom_in_button.setToolTip("Zoom in")
        self.zoom_in_button.setEnabled(False)
        self.zoom_in_button.clicked.connect(lambda: self.change_zoom(1))
        controls.addWidget(self.zoom_in_button)
        self.ptz_button = QPushButton("PTZ")
        self.ptz_button.clicked.connect(self.toggle_ptz_panel)
        controls.addWidget(self.ptz_button)
        self.fullscreen_button = QPushButton("Full screen")
        self.fullscreen_button.clicked.connect(self.toggle_fullscreen)
        controls.addWidget(self.fullscreen_button)
        self.fullscreen_replay_button = QPushButton()
        self.fullscreen_replay_button.clicked.connect(self.toggle_fullscreen)
        controls.addStretch(1)
        for button, icon_name, label in (
            (self.replay_button, "replay", "Play back recordings"),
            (self.snapshot_button, "photo", "Save picture"),
            (self.record_button, "record", "Record video"),
            (self.sound_button, "sound", "Listen to camera"),
            (self.light_button, "light", "Turn white light on"),
            (self.zoom_out_button, "zoom_out", "Zoom out"),
            (self.zoom_in_button, "zoom_in", "Zoom in"),
            (self.ptz_button, "ptz", "Pan and tilt controls"),
            (self.fullscreen_button, "fullscreen", "Full screen"),
            (self.fullscreen_replay_button, "fullscreen", "Full screen"),
        ):
            set_button_icon(button, icon_name, label)
        self.live_bar = QWidget(self.overlay)
        self.live_bar.setObjectName("liveBar")
        self.live_bar.setLayout(controls)
        overlay_layout.addWidget(self.live_bar)
        self.replay_bar = QWidget(self.overlay)
        self.replay_bar.setObjectName("replayBar")
        replay_controls = QHBoxLayout(self.replay_bar)
        replay_controls.setContentsMargins(0, 0, 0, 0)
        replay_controls.setSpacing(12)
        self.live_button = QPushButton(LIVE_BUTTON_LABEL)
        self.live_button.setObjectName("pillButton")
        self.live_button.setFixedSize(64, 44)
        self.live_button.setToolTip("Back to live video")
        self.live_button.setAccessibleName("Back to live video")
        self.live_button.clicked.connect(lambda: self.exit_replay())
        self.replay_play_button = QPushButton()
        self.replay_sound_button = QPushButton()
        self.replay_speed_button = QPushButton(REPLAY_SPEED_LABEL.format(speed=REPLAY_SPEEDS[0]))
        self.replay_speed_button.setObjectName("pillButton")
        self.replay_speed_button.setFixedSize(56, 44)
        self.replay_speed_button.setToolTip("Playback speed")
        self.replay_speed_button.setAccessibleName("Playback speed")
        self.previous_detection_button = QPushButton()
        self.next_detection_button = QPushButton()
        self.replay_snapshot_button = QPushButton()
        self.replay_save_button = QPushButton()
        self.timeline_zoom_out_button = QPushButton()
        self.timeline_zoom_in_button = QPushButton()
        for button, icon_name, label in (
            (self.previous_detection_button, "previous_detection", "Previous detection"),
            (self.replay_play_button, "pause", "Pause"),
            (self.next_detection_button, "next_detection", "Next detection"),
            (self.replay_sound_button, "sound_on", "Mute playback"),
            (self.replay_snapshot_button, "photo", "Save picture"),
            (self.replay_save_button, "download", "Save this recording"),
            (self.timeline_zoom_out_button, "zoom_out", "Show a longer period"),
            (self.timeline_zoom_in_button, "zoom_in", "Show a shorter period"),
        ):
            set_button_icon(button, icon_name, label)
        replay_controls.addStretch(1)
        for button in (
            self.live_button,
            self.previous_detection_button,
            self.replay_play_button,
            self.next_detection_button,
            self.replay_sound_button,
            self.replay_speed_button,
            self.replay_snapshot_button,
            self.replay_save_button,
            self.timeline_zoom_out_button,
            self.timeline_zoom_in_button,
            self.fullscreen_replay_button,
        ):
            replay_controls.addWidget(button)
        replay_controls.addStretch(1)
        self.replay_save_button.setEnabled(False)
        self.replay_play_button.clicked.connect(lambda: self.replay is not None and self.replay.toggle_playing())
        self.replay_sound_button.clicked.connect(self.toggle_replay_sound)
        self.previous_detection_button.clicked.connect(lambda: self.replay is not None and self.replay.jump_to_detection(-1))
        self.next_detection_button.clicked.connect(lambda: self.replay is not None and self.replay.jump_to_detection(1))
        self.replay_speed_button.clicked.connect(self.change_replay_speed)
        self.replay_snapshot_button.clicked.connect(self.take_snapshot)
        self.replay_save_button.clicked.connect(self.save_replay_clip)
        self.timeline = TimelineWidget()
        self.timeline_zoom_out_button.clicked.connect(lambda: self.timeline.zoom(1))
        self.timeline_zoom_in_button.clicked.connect(lambda: self.timeline.zoom(-1))
        overlay_layout.addWidget(self.replay_bar)
        overlay_layout.addWidget(self.timeline)
        self.replay_bar.hide()
        self.timeline.hide()
        self.ptz_panel = QWidget(self.overlay)
        self.ptz_panel.setObjectName("ptzPanel")
        ptz_layout = QVBoxLayout(self.ptz_panel)
        ptz_layout.setContentsMargins(0, 0, 0, 0)
        movement = QHBoxLayout()
        self.camera_buttons: list[QPushButton] = []
        directions = ("Left", "Right", "Up", "Down")
        for direction in directions:
            button = QPushButton()
            set_button_icon(button, direction.lower(), f"Move camera {direction.lower()}", 40)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=direction: self.control_camera((value,)))
            movement.addWidget(button)
            self.camera_buttons.append(button)
        ptz_layout.addLayout(movement)
        presets = QHBoxLayout()
        for index in range(1, 6):
            label = f"Preset {index}"
            button = QPushButton()
            set_button_icon(button, f"preset_{index}", f"Go to {label.lower()}", 40)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=label: self.control_camera((value,)))
            presets.addWidget(button)
            self.camera_buttons.append(button)
        ptz_layout.addLayout(presets)
        overlay_layout.addWidget(self.ptz_panel)
        self.ptz_panel.hide()
        self.recording_badge = ControlsOverlay(self.video)
        badge_layout = QHBoxLayout(self.recording_badge)
        badge_layout.setContentsMargins(16, 6, 16, 6)
        self.recording_label = QLabel()
        self.recording_label.setStyleSheet("color: white; background: transparent; font-weight: 600;")
        self.recording_label.setAccessibleName("Recording duration")
        badge_layout.addWidget(self.recording_label)
        self.video.set_recording_badge(self.recording_badge)
        self.video.set_controls_overlay(self.overlay)
        for button in self.overlay.findChildren(QPushButton):
            button.clicked.connect(self.show_overlay)
        self.setCentralWidget(body)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.create_tray()
        if QApplication.platformName() == "xcb":
            self.window_hints = X11WindowHints(self, self.tray is not None)
        self.set_status("Connecting to camera...")
        if self.account_username and self.account_secret:
            QTimer.singleShot(0, self.find_cameras)
        else:
            QTimer.singleShot(0, self.change_account)

    def set_status(self, text: str) -> None:
        self.base_status = text
        if not self.notice_timer.isActive():
            self.display_status(text)

    def show_notice(self, text: str) -> None:
        self.display_status(text)
        self.notice_timer.start(NOTICE_MS)

    def restore_status(self) -> None:
        self.display_status(self.base_status)

    def display_status(self, text: str) -> None:
        self.status_text = text
        self.setWindowTitle(f"{APPLICATION_NAME} \u00b7 {text}")
        if self.tray is not None:
            self.tray.setToolTip(f"{APPLICATION_NAME}\n{text}")

    def create_tray(self) -> None:
        QApplication.setQuitOnLastWindowClosed(False)
        menu = QMenu(self)
        self.window_action = menu.addAction("Hide window")
        self.window_action.triggered.connect(self.toggle_window)
        menu.addSeparator()
        for button in (
            self.live_button,
            self.replay_button,
            self.replay_play_button,
            self.snapshot_button,
            self.record_button,
            self.sound_button,
            self.light_button,
            self.zoom_in_button,
            self.zoom_out_button,
            self.fullscreen_button,
        ):
            if button is None:
                menu.addSeparator()
            else:
                self.add_tray_action(menu, button)
        self.quality_menu.setTitle("Video quality")
        menu.addMenu(self.quality_menu)
        self.quality_menu.menuAction().setVisible(False)
        movement = menu.addMenu("Move camera")
        for button in self.camera_buttons:
            self.add_tray_action(movement, button)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit_application)
        menu.aboutToShow.connect(self.update_tray_menu)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(f"{APPLICATION_NAME}\n{self.status_text}")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.messageClicked.connect(lambda: self.enter_replay(self.latest_detection))
        self.tray.show()

    def add_tray_action(self, menu: QMenu, button: QPushButton) -> None:
        action = menu.addAction(button.toolTip())
        action.triggered.connect(button.click)
        self.tray_actions.append((action, button))

    def update_tray_menu(self) -> None:
        self.window_action.setText("Hide window" if self.isVisible() else "Show window")
        text_color = self.tray.contextMenu().palette().color(QPalette.ColorRole.WindowText)
        for action, button in self.tray_actions:
            action.setIcon(menu_icon(button.property(ICON_NAME_PROPERTY), text_color))
            action.setText(button.toolTip())
            action.setEnabled(button.isEnabled() and self.isVisible())
            action.setVisible(button.isVisibleTo(self.overlay))
        self.quality_menu.setEnabled(self.quality_button.isEnabled() and self.isVisible())

    def detection_days(self) -> list[str]:
        today = datetime.now()
        return [(today - timedelta(days=1)).strftime(RECORD_DAY_FORMAT), today.strftime(RECORD_DAY_FORMAT)]

    def check_detections(self) -> None:
        if not self.devices or self.detection_worker is not None or self.close_pending or self.replay is not None:
            return
        if self.stream_live and self.stream_worker is not None:
            self.stream_worker.request_detections(self.detection_days())
            return
        if self.stream_worker is not None or self.account_worker is not None:
            return
        self.detection_worker = DetectionWorker(
            self.selected_device,
            stored_camera_password(self.selected_device.uid) or "",
            self.detection_days(),
        )
        self.detection_worker.detections_listed.connect(self.on_detections_listed)
        self.detection_worker.failed.connect(self.on_detections_failed)
        self.detection_worker.finished.connect(self.on_detection_worker_finished)
        self.detection_worker.start()

    def on_detection_worker_finished(self) -> None:
        self.detection_worker = None
        if self.close_pending:
            self.close()

    def on_detections_listed(self, names: list[str]) -> None:
        last_seen = self.settings.value(DETECTION_SETTING, "", str)
        new_names = sorted(name for name in names if name > last_seen)
        if not new_names:
            return
        self.settings.setValue(DETECTION_SETTING, new_names[-1])
        self.settings.sync()
        if not last_seen:
            return
        latest = recording_time(new_names[-1])
        self.latest_detection = latest
        message = f"{self.selected_device.name} \u00b7 {latest:%d/%m %H:%M:%S}"
        if len(new_names) > 1:
            message += f" ({len(new_names)} new detections)"
        if self.tray is not None:
            self.tray.showMessage(
                "Camera detection", message, self.windowIcon(), DETECTION_MESSAGE_MS
            )
        self.set_status(f"Detection at {latest:%d/%m %H:%M:%S}.")

    def on_detections_failed(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized():
            self.hide_to_tray()
            return
        self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.stream_worker is None and self.account_worker is None:
            self.reconnect()

    def hide_to_tray(self) -> None:
        self.exit_replay(False)
        self.stop_stream()
        self.overlay_timer.stop()
        self.hide_overlay()
        self.hide()
        self.update_recording_badge()

    def quit_application(self) -> None:
        self.quit_requested = True
        self.exit_replay(False)
        self.close()

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
            self.set_status("Enter your O-KAM account and password.")
            return
        self.account_username = username.text().strip()
        self.account_secret = password.text()
        self.find_cameras()

    def find_cameras(self) -> None:
        if not self.account_username or not self.account_secret:
            self.change_account()
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        self.devices = []
        self.set_status("Finding cameras...")
        self.account_worker = AccountWorker(self.account_username, self.account_secret)
        self.account_worker.devices_found.connect(self.on_devices_found)
        self.account_worker.failed.connect(self.on_account_failed)
        self.account_worker.finished.connect(self.on_account_finished)
        self.account_worker.start()

    def on_devices_found(self, devices: list[AccountDevice]) -> None:
        self.devices = devices
        saved_username = self.settings.value("account/username", "", str)
        if not save_account_password(self.account_username, self.account_secret):
            self.set_status("The keyring could not save the account.")
        else:
            if saved_username and saved_username != self.account_username:
                clear_account_password(saved_username)
            self.settings.setValue("account/username", self.account_username)
            self.settings.sync()
        if devices:
            self.selected_device = next((device for device in devices if device.name == "Jardin"), devices[0])
            if not self.status_text.startswith("The keyring could not"):
                credential_status = (
                    "O-KAM supplied a camera credential."
                    if self.selected_device.device_password
                    else "O-KAM supplied no camera credential."
                )
                self.set_status(f"Found {len(devices)} camera(s). {credential_status}")
        else:
            self.set_status("No cameras are visible to this account.")

    def on_account_failed(self, message: str) -> None:
        self.retry_pending = message != ACCOUNT_REJECTED_MESSAGE
        self.set_status(message)

    def enter_replay(self, start: datetime | None) -> None:
        if not self.devices or self.close_pending:
            return
        self.show_window_without_stream()
        if self.replay is not None:
            if start is not None:
                self.replay.seek(start)
            return
        if self.stream_worker is not None:
            self.pending_replay = (start,)
            self.keep_player = True
            self.stop_stream()
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        if not self.start_player():
            return
        assert self.player is not None and self.player.stdin is not None
        self.replay = ReplayController(
            self.selected_device, stored_camera_password(self.selected_device.uid) or "", self.timeline
        )
        self.replay.status_changed.connect(self.set_status)
        self.replay.playing_changed.connect(self.on_replay_playing_changed)
        self.replay.clip_available.connect(self.replay_save_button.setEnabled)
        self.live_bar.hide()
        self.ptz_panel.hide()
        self.replay_bar.show()
        self.timeline.show()
        self.replay_save_button.setEnabled(False)
        self.replay.start(self.player.stdin, start)
        self.show_overlay()

    def exit_replay(self, resume: bool = True) -> None:
        if self.replay is None:
            return
        replay = self.replay
        self.replay = None
        replay.stop()
        self.replay_bar.hide()
        self.timeline.hide()
        self.live_bar.show()
        self.show_overlay()
        if resume and self.isVisible() and self.stream_worker is None and self.account_worker is None:
            self.reconnect()

    def show_window_without_stream(self) -> None:
        if not self.isVisible() or self.isMinimized():
            self.showNormal()
        self.raise_()
        self.activateWindow()

    def on_replay_playing_changed(self, playing: bool) -> None:
        set_button_icon(self.replay_play_button, "pause" if playing else "play", "Pause" if playing else "Play")

    def toggle_replay_sound(self) -> None:
        if self.replay is None:
            return
        sound = self.replay.toggle_sound()
        set_button_icon(self.replay_sound_button, "sound_on" if sound else "sound", "Mute playback" if sound else "Listen")

    def change_replay_speed(self) -> None:
        if self.replay is not None:
            self.replay_speed_button.setText(REPLAY_SPEED_LABEL.format(speed=self.replay.change_speed()))

    def save_replay_clip(self) -> None:
        if self.replay is None:
            return
        try:
            path = self.replay.save_clip()
        except OSError:
            self.set_status("Unable to save the recording.")
            return
        if path is not None:
            self.show_notice(f"Recording saved: {path}")

    def on_account_finished(self) -> None:
        self.replay_button.setEnabled(bool(self.devices))
        self.account_worker = None
        if self.close_pending:
            self.close()
        elif self.devices and self.isVisible():
            self.watch_live()
        elif self.retry_pending and self.isVisible():
            self.schedule_reconnect(self.status_text)

    def schedule_reconnect(self, reason: str) -> None:
        delay = min(RECONNECT_MAX_SECONDS, 2 ** (self.reconnect_attempts + 1))
        self.reconnect_attempts += 1
        self.reconnect_timer.start(delay * 1000)
        self.set_status(f"{reason} Reconnecting in {delay}s.")

    def reconnect(self) -> None:
        if self.devices:
            self.watch_live()
        else:
            self.find_cameras()

    def watch_live(self) -> None:
        if not self.devices or self.stream_worker is not None:
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        if not self.start_player():
            return
        assert self.player.stdin is not None
        self.stream_error = False
        self.stream_live = False
        self.control_pending = False
        self.sound_enabled = False
        self.reset_recording_state()
        self.zoom_level = 0
        set_button_icon(self.sound_button, "sound", "Listen to camera")
        self.stream_worker = StreamWorker(
            self.selected_device, stored_camera_password(self.selected_device.uid) or "", self.player.stdin
        )
        self.stream_worker.status_changed.connect(self.on_stream_status)
        self.stream_worker.failed.connect(self.on_stream_error)
        self.stream_worker.control_completed.connect(self.on_control_completed)
        self.stream_worker.control_failed.connect(self.on_control_failed)
        self.stream_worker.sound_changed.connect(self.on_sound_changed)
        self.stream_worker.sound_failed.connect(self.on_sound_failed)
        self.stream_worker.recording_started.connect(self.on_recording_started)
        self.stream_worker.recording_saved.connect(self.on_recording_saved)
        self.stream_worker.recording_failed.connect(self.on_recording_failed)
        self.stream_worker.capabilities_found.connect(self.on_capabilities_found)
        self.stream_worker.detections_listed.connect(self.on_detections_listed)
        self.stream_worker.detections_failed.connect(self.on_detections_failed)
        self.stream_worker.setting_completed.connect(self.on_setting_completed)
        self.stream_worker.setting_failed.connect(self.on_setting_failed)
        self.stream_worker.finished.connect(self.on_stream_finished)
        self.stream_worker.start()
        QTimer.singleShot(200, self.show_overlay)
        QTimer.singleShot(500, self.video.raise_interaction_layer)

    def start_player(self) -> bool:
        if self.player is not None and self.player.poll() is None:
            self._mpv_command(["set_property", "video-zoom", 0])
            self.zoom_level = 0
            self.video_pan = (0.0, 0.0)
            self.apply_video_pan()
            return True
        self.stop_player()
        self.mpv_directory = tempfile.TemporaryDirectory(prefix="okam-linux-mpv-")
        self.mpv_socket = Path(self.mpv_directory.name) / "control.sock"
        try:
            self.player = subprocess.Popen(
                mpv_stream_command(self.mpv_socket, int(self.video.winId()), True),
                stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
            )
        except OSError:
            self.mpv_socket = None
            self.mpv_directory.cleanup()
            self.mpv_directory = None
            self.set_status("The mpv video player is unavailable.")
            return False
        return True

    def stop_player(self) -> None:
        if self.player is not None:
            if self.player.stdin is not None:
                try:
                    self.player.stdin.close()
                except OSError:
                    pass
            if self.player.poll() is None:
                self.player.terminate()
                try:
                    self.player.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.player.kill()
                    self.player.wait(timeout=3)
            self.player = None
        if self.mpv_directory is not None:
            self.mpv_directory.cleanup()
            self.mpv_directory = None
            self.mpv_socket = None

    def on_stream_status(self, message: str) -> None:
        self.set_status(message)
        if message == "Live video":
            self.stream_live = True
            self.reconnect_attempts = 0
            self.set_controls_enabled(not self.control_pending)
            self.sound_button.setEnabled(True)
            self.snapshot_button.setEnabled(True)
            self.record_button.setEnabled(True)
            self.zoom_out_button.setEnabled(False)
            self.zoom_in_button.setEnabled(True)
            self.light_button.setEnabled(True)
            self.quality_button.setEnabled(True)

    def disable_live_controls(self) -> None:
        self.set_controls_enabled(False)
        self.sound_button.setEnabled(False)
        set_button_icon(self.sound_button, "sound", "Listen to camera")
        self.snapshot_button.setEnabled(False)
        self.record_button.setEnabled(False)
        self.zoom_out_button.setEnabled(False)
        self.zoom_in_button.setEnabled(False)
        self.light_button.setEnabled(False)
        self.quality_button.setEnabled(False)
        self.setting_pending = False

    def set_controls_enabled(self, enabled: bool) -> None:
        for button in self.camera_buttons:
            button.setEnabled(enabled)

    def show_overlay(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self.video.place_overlay()
        self.overlay.show()
        self.video.raise_interaction_layer()
        if self.replay is None:
            self.overlay_timer.start(OVERLAY_TIMEOUT_MS)
        else:
            self.overlay_timer.stop()

    def hide_overlay(self) -> None:
        self.overlay.hide()

    def toggle_overlay(self) -> None:
        if self.overlay.isVisible():
            self.overlay_timer.stop()
            self.hide_overlay()
        else:
            self.show_overlay()

    def toggle_ptz_panel(self) -> None:
        self.ptz_panel.setVisible(not self.ptz_panel.isVisible())
        self.ptz_button.setToolTip(
            "Hide pan and tilt controls" if self.ptz_panel.isVisible() else "Pan and tilt controls"
        )
        self.video.place_overlay()
        self.show_overlay()

    def _mpv_command(self, command: list[object]) -> bool:
        return mpv_request(self.mpv_socket, command)[0]

    def take_snapshot(self) -> None:
        if not self.stream_live and self.replay is None:
            return
        try:
            path = media_directory() / f"Jardin_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        except OSError:
            self.set_status("Unable to create the picture folder.")
            return
        if self._mpv_command(["screenshot-to-file", str(path), "video"]):
            self.show_notice(f"Picture saved: {path}")
        else:
            self.set_status("Unable to save a picture.")

    def toggle_recording(self) -> None:
        if not self.stream_live or self.stream_worker is None:
            return
        if self.recording_path is None:
            try:
                path = media_directory() / f"Jardin_{datetime.now():%Y%m%d_%H%M%S_%f}.mkv"
            except OSError:
                self.set_status("Unable to create the recording folder.")
                return
            self.stream_worker.set_recording(path)
            self.recording_path = path
            set_button_icon(self.record_button, "recording", "Stop recording")
            self.set_status("Waiting for a key frame to start recording...")
        else:
            self.stream_worker.set_recording(None)
            self.reset_recording_state()
            self.set_status("Saving recording...")

    def reset_recording_state(self) -> None:
        self.recording_path = None
        self.recording_started_at = None
        self.recording_timer.stop()
        self.recording_badge.hide()
        set_button_icon(self.record_button, "record", "Record video")

    def on_recording_started(self) -> None:
        if self.recording_path is None:
            return
        self.recording_started_at = time.monotonic()
        self.recording_timer.start(RECORDING_TICK_MS)
        self.set_status("Recording video...")
        self.update_recording_badge()

    def update_recording_badge(self) -> None:
        if self.recording_started_at is None or not self.isVisible() or self.isMinimized():
            self.recording_badge.hide()
            return
        elapsed = int(time.monotonic() - self.recording_started_at)
        hours, remainder = divmod(elapsed, 3600)
        self.recording_label.setText(
            f'<span style="color: {RECORDING_DOT_COLOR};">\u25cf</span>'
            f" {hours:02d}:{remainder // 60:02d}:{remainder % 60:02d}"
        )
        self.video.place_overlay()
        self.recording_badge.show()
        self.video.raise_interaction_layer()

    def on_recording_saved(self, path: str) -> None:
        self.show_notice(f"Recording saved: {path}")

    def on_recording_failed(self, message: str) -> None:
        if self.recording_path is not None:
            self.reset_recording_state()
        self.set_status(message)

    def toggle_zoom_at(self, x: int, y: int) -> None:
        if self.zoom_level > 0:
            self.change_zoom(-self.zoom_level)
        else:
            self.change_zoom(DOUBLE_CLICK_ZOOM_STEPS, x, y)

    def change_zoom(self, step: int, x: int | None = None, y: int | None = None) -> None:
        if not self.stream_live and self.replay is None:
            return
        level = min(MAX_ZOOM_LEVEL, max(0, self.zoom_level + step))
        if level == self.zoom_level:
            return
        if not self._mpv_command(["set_property", "video-zoom", level / 2]):
            self.set_status("Unable to change zoom.")
            return
        old_scale = 2 ** (self.zoom_level / 2)
        new_scale = 2 ** (level / 2)
        if x is not None and y is not None:
            offset_x = (x - self.video.width() / 2) / max(1, self.video.width())
            offset_y = (y - self.video.height() / 2) / max(1, self.video.height())
            self.video_pan = (
                offset_x / new_scale - (offset_x / old_scale - self.video_pan[0]),
                offset_y / new_scale - (offset_y / old_scale - self.video_pan[1]),
            )
        self.zoom_level = level
        self.apply_video_pan()
        self.zoom_out_button.setEnabled(level > 0)
        self.zoom_in_button.setEnabled(level < MAX_ZOOM_LEVEL)
        self.show_notice(f"Zoom {2 ** (level / 2):.1f}×.")

    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            if self.normal_geometry is not None:
                self.setGeometry(self.normal_geometry)
            set_button_icon(self.fullscreen_button, "fullscreen", "Full screen")
        else:
            self.normal_geometry = self.geometry()
            self.showFullScreen()
            set_button_icon(self.fullscreen_button, "exit_fullscreen", "Exit full screen")

    def fit_video_aspect(self) -> None:
        if self.isFullScreen() or self.video.width() <= 0 or self.video.height() <= 0:
            return
        video_height = round(self.video.width() * VIDEO_ASPECT_HEIGHT / VIDEO_ASPECT_WIDTH)
        self.resize(self.width(), self.height() - self.video.height() + video_height)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self.aspect_fitted:
            self.aspect_fitted = True
            QTimer.singleShot(0, self.fit_video_aspect)
        QTimer.singleShot(0, self.show_overlay)
        QTimer.singleShot(0, self.update_recording_badge)

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self.video.place_overlay()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            if self.isMinimized():
                self.overlay_timer.stop()
                self.hide_overlay()
            self.update_recording_badge()

    def quality_label(self) -> str:
        quality = self.settings.value(QUALITY_SETTING, "", str)
        return quality if quality in VIDEO_QUALITIES else DEFAULT_QUALITY_LABEL

    def show_quality_menu(self) -> None:
        self.quality_menu.popup(
            self.quality_button.mapToGlobal(QPoint(0, -self.quality_menu.sizeHint().height()))
        )

    def on_capabilities_found(self, qualities: list[str], light_on: bool | None) -> None:
        for quality, action in self.quality_actions.items():
            action.setVisible(quality in qualities)
        self.quality_button.setVisible(bool(qualities))
        self.quality_menu.menuAction().setVisible(bool(qualities))
        self.light_on = light_on
        self.light_button.setVisible(light_on is not None)
        self.update_light_button()
        self.video.place_overlay()

    def update_light_button(self) -> None:
        set_button_icon(
            self.light_button,
            "light_on" if self.light_on else "light",
            "Turn white light off" if self.light_on else "Turn white light on",
        )

    def queue_setting(self, name: str, value: object, message: str) -> bool:
        if not self.stream_live or self.stream_worker is None or self.setting_pending:
            return False
        if not self.stream_worker.queue_setting(name, value):
            return False
        self.setting_pending = True
        self.light_button.setEnabled(False)
        self.quality_button.setEnabled(False)
        self.set_status(message)
        return True

    def toggle_light(self) -> None:
        if self.light_on is None:
            return
        self.queue_setting(
            SETTING_LIGHT,
            not self.light_on,
            "Turning white light off..." if self.light_on else "Turning white light on...",
        )

    def choose_quality(self, quality: str) -> None:
        if self.recording_path is not None:
            self.sync_quality_actions()
            self.set_status("Stop recording before changing video quality.")
            return
        if not self.queue_setting(SETTING_QUALITY, quality, f"Changing video quality to {quality}..."):
            self.sync_quality_actions()

    def sync_quality_actions(self) -> None:
        current = self.settings.value(QUALITY_SETTING, "", str)
        for quality, action in self.quality_actions.items():
            action.setChecked(quality == current)
        self.quality_button.setText(self.quality_label())

    def on_setting_completed(self, name: str, value: object) -> None:
        self.setting_pending = False
        self.light_button.setEnabled(self.stream_live)
        self.quality_button.setEnabled(self.stream_live)
        if name == SETTING_LIGHT:
            self.light_on = bool(value)
            self.update_light_button()
            self.show_notice("White light on." if self.light_on else "White light off.")
        else:
            self.settings.setValue(QUALITY_SETTING, value)
            self.settings.sync()
            self.sync_quality_actions()
            self.show_notice(f"Video quality set to {value}.")

    def on_setting_failed(self, name: str, message: str) -> None:
        self.setting_pending = False
        self.light_button.setEnabled(self.stream_live)
        self.quality_button.setEnabled(self.stream_live)
        if name == SETTING_QUALITY:
            self.sync_quality_actions()
        self.set_status(message)

    def toggle_sound(self) -> None:
        if self.stream_worker is None:
            return
        self.sound_button.setEnabled(False)
        self.set_status("Changing camera sound...")
        self.stream_worker.set_sound(not self.sound_enabled)

    def on_sound_changed(self, enabled: bool) -> None:
        self.sound_enabled = enabled
        set_button_icon(
            self.sound_button,
            "sound_on" if enabled else "sound",
            "Mute camera" if enabled else "Listen to camera",
        )
        self.sound_button.setEnabled(self.stream_live)
        self.show_notice("Camera sound on." if enabled else "Camera sound off.")

    def on_sound_failed(self, message: str) -> None:
        self.sound_button.setEnabled(self.stream_live)
        self.set_status(message)

    def pan_zoomed_video(self, dx: int, dy: int) -> None:
        if self.zoom_level == 0 or (not self.stream_live and self.replay is None):
            return
        scale = 2 ** (self.zoom_level / 2)
        self.video_pan = (
            self.video_pan[0] + dx / max(1, self.video.width() * scale),
            self.video_pan[1] + dy / max(1, self.video.height() * scale),
        )
        self.apply_video_pan()

    def apply_video_pan(self) -> None:
        limit = (1 - 1 / 2 ** (self.zoom_level / 2)) / 2
        self.video_pan = tuple(min(limit, max(-limit, value)) for value in self.video_pan)
        self._mpv_command(["set_property", "video-pan-x", self.video_pan[0]])
        self._mpv_command(["set_property", "video-pan-y", self.video_pan[1]])

    def move_by_drag(self, dx: int, dy: int) -> None:
        if self.zoom_level > 0 or self.replay is not None:
            return
        if abs(dx) < DRAG_PIXELS_PER_STEP // 2 and abs(dy) < DRAG_PIXELS_PER_STEP // 2:
            return
        if abs(dx) >= abs(dy):
            direction, distance = ("Left" if dx > 0 else "Right"), abs(dx)
        else:
            direction, distance = ("Up" if dy > 0 else "Down"), abs(dy)
        count = min(MAX_DRAG_STEPS, max(1, round(distance / DRAG_PIXELS_PER_STEP)))
        self.control_camera((direction,) * count)

    def control_camera(self, commands: tuple[str, ...]) -> None:
        if not self.stream_live or self.stream_worker is None or self.control_pending:
            return
        if not self.stream_worker.queue_control(commands):
            return
        self.control_pending = True
        self.set_controls_enabled(False)
        command = commands[0]
        if command.startswith("Preset "):
            self.set_status(f"Moving camera to {command.lower()}...")
        else:
            self.set_status(f"Moving camera {command.lower()}...")

    def on_control_completed(self, command: str) -> None:
        self.control_pending = False
        self.set_controls_enabled(self.stream_live)
        if command == "Drag":
            self.show_notice("Camera moved with mouse.")
        elif command.startswith("Preset "):
            self.show_notice(f"Camera moved to {command.lower()}.")
        else:
            self.show_notice(f"Camera moved {command.lower()}.")

    def on_control_failed(self, message: str) -> None:
        self.control_pending = False
        self.set_controls_enabled(self.stream_live)
        self.set_status(message)

    def on_stream_error(self, message: str) -> None:
        self.stream_error = True
        self.retry_pending = message != "The camera rejected the available credentials."
        self.stream_live = False
        self.control_pending = False
        self.sound_enabled = False
        self.disable_live_controls()
        self.set_status(message)

    def stop_stream(self) -> None:
        self.reconnect_timer.stop()
        self.retry_pending = False
        if self.stream_worker is not None:
            self.set_status("Stopping camera...")
            self.stream_worker.stop()
        else:
            self.stop_player()
            self.set_status("Camera stopped.")

    def on_stream_finished(self) -> None:
        self.stream_worker = None
        self.stream_live = False
        self.control_pending = False
        self.sound_enabled = False
        if not (self.retry_pending or self.keep_player) or self.close_pending:
            self.stop_player()
        self.keep_player = False
        self.reset_recording_state()
        self.zoom_level = 0
        self.disable_live_controls()
        if self.retry_pending and not self.close_pending:
            self.schedule_reconnect("Camera disconnected.")
        elif not self.stream_error:
            self.set_status("Camera stopped.")
        if self.close_pending:
            self.close()
        elif self.pending_replay is not None:
            start = self.pending_replay[0]
            self.pending_replay = None
            self.enter_replay(start)

    def closeEvent(self, event: object) -> None:
        if self.tray is not None and not self.quit_requested:
            event.ignore()
            self.hide_to_tray()
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        if (
            self.account_worker is not None
            or self.stream_worker is not None
            or self.detection_worker is not None
        ):
            self.close_pending = True
            self.stop_stream()
            event.ignore()
            return
        event.accept()
        if self.window_hints is not None:
            self.window_hints.close()
        if self.tray is not None:
            self.tray.hide()
            QApplication.quit()


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--forget-account":
        settings = QSettings(APPLICATION_NAME, APPLICATION_NAME)
        username = settings.value("account/username", "", str)
        if username and not clear_account_password(username):
            return 1
        settings.remove("account/username")
        settings.sync()
        return 0 if settings.status() == QSettings.Status.NoError else 1
    if sys.argv[1:] == [CAMERA_PASSWORD_ARGUMENT]:
        return print_camera_passwords()
    start_in_tray = sys.argv[1:] == [TRAY_ARGUMENT]
    if len(sys.argv) != 1 and not start_in_tray:
        return 2
    app = QApplication(sys.argv)
    configure_logging()
    server_name = f"{INSTANCE_SERVER_PREFIX}-{os.getuid()}"
    running_instance = QLocalSocket()
    running_instance.connectToServer(server_name)
    if running_instance.waitForConnected(INSTANCE_CONNECT_TIMEOUT_MS):
        if not start_in_tray:
            running_instance.write(SHOW_WINDOW_REQUEST)
            running_instance.waitForBytesWritten(INSTANCE_CONNECT_TIMEOUT_MS)
        running_instance.disconnectFromServer()
        return 0
    QLocalServer.removeServer(server_name)
    instance_server = QLocalServer()
    if not instance_server.listen(server_name):
        return 1
    window = MainWindow()
    signal.signal(signal.SIGTERM, lambda number, frame: QTimer.singleShot(0, window.quit_application))
    signal_timer = QTimer()
    signal_timer.timeout.connect(lambda: None)
    signal_timer.start(SIGNAL_POLL_MS)
    instance_server.newConnection.connect(lambda: accept_instance_request(instance_server, window))
    if not start_in_tray or window.tray is None:
        window.show()
    return app.exec()


def print_camera_passwords() -> int:
    username = QSettings(APPLICATION_NAME, APPLICATION_NAME).value("account/username", "", str)
    password = stored_account_password(username) if username else None
    if not username or not password:
        print("No saved O-KAM account. Start O-KAM Linux and sign in first.", file=sys.stderr)
        return 1
    try:
        devices = Eye4AccountClient(opener=account_request).enumerate(username, password)
    except AccountError as ex:
        print(str(ex), file=sys.stderr)
        return 1
    for device in devices:
        credential = stored_camera_password(device.uid) or device.device_password
        print(f"{device.name} ({device.uid}): {credential or 'not provided by O-KAM; the camera uses its initial password'}")
    return 0


def accept_instance_request(server: QLocalServer, window: MainWindow) -> None:
    connection = server.nextPendingConnection()
    if connection is None:
        return
    connection.readyRead.connect(lambda: read_instance_request(connection, window))
    connection.disconnected.connect(connection.deleteLater)


def read_instance_request(connection: QLocalSocket, window: MainWindow) -> None:
    if bytes(connection.readAll()) == SHOW_WINDOW_REQUEST:
        window.show_window()


if __name__ == "__main__":
    raise SystemExit(main())
