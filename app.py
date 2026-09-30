#!/usr/bin/env python3

from __future__ import annotations

import asyncio
import http.client
import ipaddress
import json
import logging
import logging.handlers
import math
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
import uuid
from collections import deque
from pathlib import Path
from dataclasses import asdict, dataclass, replace
from typing import BinaryIO, Callable
from datetime import datetime, timedelta, timezone

from PyQt6.QtCore import QEvent, QObject, QPoint, QPointF, QRect, QRectF, QSize, QSettings, QSocketNotifier, QStandardPaths, QThread, QTimer, QUrl, Qt, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QDesktopServices,
    QActionGroup,
    QColor,
    QIcon,
    QMouseEvent,
    QMoveEvent,
    QPainter,
    QPen,
    QRegion,
    QResizeEvent,
    QShowEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import (
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMenu,
    QPushButton,
    QStackedWidget,
    QSpinBox,
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
from icam365 import PTZ_COMMAND, PTZ_POSITION_COMMAND, close_bridge, get_bridge, load_config as load_icam365_config
from imou import (
    IMOU_ACCOUNT_URL, IMOU_PRIVACY_MESSAGE, IMOU_REGIONS, IMOU_SECRET_MISSING_MESSAGE,
    ImouAccount, ImouClient, ImouDevice, ImouError, ImouRecording,
)
from rtsp_tunnel import ImouReplayTunnel, RtspWebSocketTunnel
from local_detection import (
    DetectionEngine, DetectionEvent, DetectionPipeline, load_events, model_path, organize_events, prune_events,
)
from Xlib import X as X11, Xutil, display as xdisplay
from Xlib.protocol import event as xevent


APPLICATION_NAME = "Camera Viewer"
STORAGE_NAME = "O-KAM Linux"
RTSP_ACCOUNT = "rtsp"
RTSP_CAMERAS_SETTING = "cameras/rtsp"
RTSP_SOUND_SETTING = "audio/rtsp_sound"
IMOU_PROVIDER = "imou"
IMOU_ACCOUNT_PROVIDER = "imou_account"
IMOU_ACCOUNTS_SETTING = "accounts/imou"
IMOU_CAMERAS_SETTING = "cameras/imou"
IMOU_LOADING_MESSAGE = "An Imou account is already loading."
IMOU_INPUT_CLOCK = "use_wallclock_as_timestamps=1"
IMOU_RTSP_PATH = "/cam/realmonitor"
MULTIVIEW_SETTING = "view/show_all_cameras"
CAMERA_VISIBLE_SETTING = "view/camera_visible"
NO_VISIBLE_CAMERAS_MESSAGE = "No cameras selected."
MULTIVIEW_LAYOUT_SETTING = "view/camera_layout"
CAMERA_ORDER_SETTING = "view/camera_order"
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
ACCOUNT_INPUT_MESSAGE = "Enter your O-KAM account and password."
ACCOUNT_LOADING_MESSAGE = "An account is already loading."
CAMERA_SOURCE_LABELS = ("O-KAM account", "RTSP camera", "Imou Life camera (local)", "Imou Life account")
CAMERA_LAYOUTS = (("horizontal", "Side by side"), ("vertical", "Stacked"), ("grid", "Grid 2 × 2"))
CAMERA_LAYOUT_VALUES = tuple(value for value, label in CAMERA_LAYOUTS)
CAMERA_GRID_COLUMNS = 2
CAMERA_GRID_MIN_ROWS = 2
FORM_ERROR_STYLE = "color: #bd4242;"
ACCOUNT_ERROR_STYLE = "color: #ffb4ab; background-color: #3d2020; padding: 8px;"
WAKE_SOURCE = Path.home() / ".local/share/okam-linux/vendor/device_wakeup_server.dart"
ICON_NAME_PROPERTY = "iconName"
ICON_DIRECTORY = Path(__file__).resolve().parent / "assets/icons"
MAX_ACCOUNT_RESPONSE_BYTES = 1024 * 1024
SECRET_APPLICATION_ATTRIBUTES = ("application", "okam-linux")
MOTOR_COMMANDS = {"Left": (4, 5), "Right": (6, 7), "Up": (0, 1), "Down": (2, 3)}
PRESET_COMMANDS = {f"Preset {index}": 29 + index * 2 for index in range(1, 6)}
PTZ_RESPONSE_COMMAND = 0x6019
MOTOR_PULSE_SECONDS = 0.12
DRAG_PIXELS_PER_STEP = 90
MAX_DRAG_STEPS = 4
VIDEO_READ_TIMEOUT_SECONDS = 2
VIDEO_STALL_SECONDS = 12
RTSP_STALL_SECONDS = 10
RTSP_MAX_ACCUMULATED_DELAY_SECONDS = 6
RTSP_DELAY_CONFIRM_SECONDS = 3
RTSP_AUDIO_FILTER = "--af=lavfi=[volume=25dB,alimiter=limit=0.95]"
AUDIO_RESPONSE_COMMAND = 0x6031
RECONNECT_MAX_SECONDS = 30
OVERLAY_TIMEOUT_MS = 5000
ICAM365_LIGHT_PORT = 8001
ICAM365_LIGHT_PATH = "/whitelight"
ICAM365_LIGHT_ON = "on"
ICAM365_LIGHT_AUTO = "2"
ICAM365_PTZ_PATH = "/ptzctrl"
ICAM365_TILT_ACTIONS = {"Up": 1, "Down": 3}
ICAM365_PTZ_STOP = 0
ICAM365_TILT_PULSE_SECONDS = 0.12
RTSP_DENOISE_FILTER = "--vf=lavfi=[hqdn3d=4:3:6:4.5]"
ICAM365_SERVER = "TAS-Tech IPCam"
TOOLTIP_STYLE = (
    "QToolTip { color: white; background-color: rgb(32, 32, 32);"
    " border: 1px solid rgba(255, 255, 255, 60); border-radius: 6px; padding: 4px 8px; }"
)
CAMERA_CONTROLS_STYLE = (
    "#cameraControls QPushButton { color: white; background-color: transparent;"
    " border: none; border-radius: 6px; padding: 4px; }"
    "#cameraControls QPushButton:hover { background-color: rgba(255, 255, 255, 35); }"
    "#cameraControls #qualityButton { border: 2px solid white; border-radius: 8px;"
    " font-weight: 600; margin: 6px 2px; }"
    "#ptzPanel, #presetControls, #liveBar, #replayBar { background: transparent; }"
    "#cameraControls #pillButton { border: 2px solid white; border-radius: 8px;"
    " font-weight: 600; margin: 6px 2px; }"
    "#cameraControls QPushButton:disabled { color: rgba(255, 255, 255, 90);"
    " border-color: rgba(255, 255, 255, 90); }"
    + TOOLTIP_STYLE
)
MAX_ZOOM_LEVEL = 4
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
DIRECT_CONNECT_SECONDS = 10
RELAY_CONNECT_SECONDS = 55
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
DETECTION_POLL_MS = 60 * 1000
CONTINUOUS_FOLDER = "Continuous"
CONTINUOUS_TIME_FORMAT = "%Y%m%d_%H%M%S"
CONTINUOUS_NAME_PATTERN = re.compile(r"[^/]+_(\d{8}_\d{6})\.mkv")
CONTINUOUS_SEGMENT_SECONDS = 10 * 60
CONTINUOUS_RETENTION = timedelta(hours=24)
REPLAY_FILE_IDLE_SECONDS = 5
CONTINUOUS_SETTING = "recording/continuous"
DETECTION_SETTING = "detections/last_seen"
LOCAL_DETECTION_SETTING = "detections/local_enabled"
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


def set_button_icon(button: QPushButton, name: str, label: str, size: int = 44) -> None:
    button.setText("")
    button.setProperty(ICON_NAME_PROPERTY, name)
    button.setIcon(QIcon(str(ICON_DIRECTORY / f"{name}.svg")))
    button.setIconSize(QSize(28, 28))
    button.setFixedSize(size, size)
    button.setToolTip(label)
    button.setAccessibleName(label)


def set_icam365_light_icon(button: QPushButton, mode: str | None) -> None:
    enabled = mode == ICAM365_LIGHT_ON
    set_button_icon(button, "light_on" if enabled else "light",
                    "Use automatic white light" if enabled else "Turn white light on")


class X11WindowHints:
    def __init__(self, window: QWidget, skip_taskbar: bool) -> None:
        self.skip_taskbar = skip_taskbar
        self.aspect_width = VIDEO_ASPECT_WIDTH
        self.aspect_height = VIDEO_ASPECT_HEIGHT
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
        aspect = {"num": self.aspect_width, "denum": self.aspect_height}
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

    def set_aspect_ratio(self, width: int, height: int) -> None:
        if width <= 0 or height <= 0:
            return
        self.aspect_width = width
        self.aspect_height = height
        self.apply_aspect_ratio()

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
    return save_device_secret("account", username, password, "O-KAM Linux account")


def save_device_secret(category: str, identifier: str, secret: str, label: str) -> bool:
    try:
        result = subprocess.run(
            ["secret-tool", "store", f"--label={label}", *SECRET_APPLICATION_ATTRIBUTES, category, identifier],
            input=secret.encode("utf-8"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


def clear_account_password(username: str) -> bool:
    return clear_device_secret("account", username)


def clear_device_secret(category: str, identifier: str) -> bool:
    try:
        result = subprocess.run(
            ["secret-tool", "clear", *SECRET_APPLICATION_ATTRIBUTES, category, identifier],
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


class ImouAccountWorker(QThread):
    devices_found = pyqtSignal(list)
    failed = pyqtSignal(str)

    def __init__(self, account: ImouAccount, secret: str | None = None) -> None:
        super().__init__()
        self.account = account
        self.secret = secret

    def run(self) -> None:
        try:
            secret = self.secret or stored_secret(IMOU_ACCOUNT_PROVIDER, self.account.app_id)
            devices = ImouClient(self.account, secret or "").devices()
            if self.isInterruptionRequested():
                return
            if self.secret and not save_device_secret(
                IMOU_ACCOUNT_PROVIDER, self.account.app_id, self.secret, "Camera Viewer Imou Life account",
            ):
                raise ImouError("Unable to save the Imou application key in the desktop keyring.")
            self.devices_found.emit(devices)
        except ImouError as ex:
            self.failed.emit(str(ex))
        except Exception:
            self.failed.emit("Unable to load the Imou Life account.")
        finally:
            self.secret = None


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


def mpv_rtsp_command(
    socket_path: Path, window_id: int, fill: bool, camera: RtspCamera,
    sound_enabled: bool = False, playlist_fd: int | None = None,
) -> list[str]:
    command = mpv_stream_command(socket_path, window_id, fill)
    command.remove("--demuxer-lavf-format=h264")
    command.remove("--untimed")
    command.remove("--no-audio")
    command.insert(-1, "--mute=no" if sound_enabled else "--mute=yes")
    command.insert(-1, RTSP_AUDIO_FILTER)
    command.insert(-1, RTSP_DENOISE_FILTER)
    if camera.provider == IMOU_ACCOUNT_PROVIDER:
        command[-1] = "--idle=yes"
        command.insert(-1, "--demuxer-lavf-probesize=32768")
        command.insert(-1, "--demuxer-lavf-analyzeduration=0.000001")
        command.insert(-1, "--demuxer-lavf-o-add=fpsprobesize=0")
    elif camera.provider == IMOU_PROVIDER:
        if playlist_fd is None:
            raise ValueError("A private Imou stream playlist is required.")
        command[-1] = f"--playlist=/proc/self/fd/{playlist_fd}"
    else:
        bridge = get_bridge(camera.uid)
        command[-1] = bridge.url if bridge is not None else camera.url
    command.insert(-1, f"--rtsp-transport={camera.transport}")
    return command


def stop_mpv_player(player: subprocess.Popen[bytes] | None) -> None:
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
            player.wait(timeout=3)
        except subprocess.TimeoutExpired:
            player.kill()
            player.wait(timeout=3)


def media_directory() -> Path:
    pictures = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.PicturesLocation)
    directory = (Path(pictures) if pictures else Path.home() / "Pictures") / STORAGE_NAME
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def safe_camera_name(name: str) -> str:
    return re.sub(r"[^\w.-]+", "_", name, flags=re.UNICODE).strip("._") or "Camera"


@dataclass(frozen=True)
class RtspCamera:
    uid: str
    name: str
    url: str
    transport: str = "tcp"
    provider: str = "rtsp"
    username: str = ""
    imou_account: ImouAccount | None = None
    imou_device: ImouDevice | None = None


def imou_account_camera(account: ImouAccount, device: ImouDevice) -> RtspCamera:
    return RtspCamera(device.uid(account.app_id), device.name, "", "tcp", IMOU_ACCOUNT_PROVIDER,
                      account.email, account, device)


def load_imou_accounts(settings: QSettings) -> list[ImouAccount]:
    try:
        records = json.loads(settings.value(IMOU_ACCOUNTS_SETTING, "[]", str))
    except (ValueError, TypeError):
        return []
    accounts = {}
    for record in records if isinstance(records, list) else []:
        try:
            account = ImouAccount(**record)
            accounts[account.app_id] = account
        except (TypeError, ValueError):
            continue
    return list(accounts.values())


def load_imou_cameras(settings: QSettings, accounts: list[ImouAccount]) -> list[RtspCamera]:
    try:
        records = json.loads(settings.value(IMOU_CAMERAS_SETTING, "[]", str))
    except (ValueError, TypeError):
        return []
    cameras = {}
    by_id = {account.app_id: account for account in accounts}
    for record in records if isinstance(records, list) else []:
        try:
            account = by_id[record["app_id"]]
            camera = imou_account_camera(account, ImouDevice(**record["device"]))
            cameras[camera.uid] = camera
        except (TypeError, ValueError, KeyError):
            continue
    return list(cameras.values())


def camera_continuous_allowed(camera: AccountDevice | RtspCamera) -> bool:
    return (not isinstance(camera, RtspCamera) or camera.provider != IMOU_ACCOUNT_PROVIDER
            or camera.imou_account is not None and camera.imou_account.cloud_recording)


def set_replay_menu(button: QPushButton, remote: Callable[[], None], local: Callable[[], None]) -> None:
    previous = button.menu()
    if previous is not None:
        previous.deleteLater()
    menu = QMenu(button)
    menu.addAction("Camera recordings").triggered.connect(remote)
    menu.addAction("Local recordings (24 h)").triggered.connect(local)
    button.setMenu(menu)


class CameraCredentialError(OSError):
    pass


def imou_rtsp_url(address: str, port: int = 554, channel: int = 1) -> str:
    host = ipaddress.ip_address(address)
    if (not host.is_private or host.is_loopback or host.is_multicast or host.is_unspecified
            or not 1 <= port <= 65535 or not 1 <= channel <= 128):
        raise ValueError("Enter a local camera address, port, and channel.")
    authority = f"[{host}]" if host.version == 6 else str(host)
    return f"rtsp://{authority}:{port}{IMOU_RTSP_PATH}?channel={channel}&subtype=0"


def camera_stream_url(camera: RtspCamera) -> str:
    if camera.provider != IMOU_PROVIDER:
        return camera.url
    password = stored_secret(IMOU_PROVIDER, camera.uid)
    if not password:
        raise CameraCredentialError("The Imou camera password is unavailable in the desktop keyring.")
    parsed = urllib.parse.urlsplit(camera.url)
    username = urllib.parse.quote(camera.username, safe="")
    password = urllib.parse.quote(password, safe="")
    url = urllib.parse.urlunsplit((
        parsed.scheme, f"{username}:{password}@{parsed.netloc}", parsed.path, parsed.query, parsed.fragment,
    ))
    if "\n" in url or "\r" in url:
        raise CameraCredentialError("The Imou camera URL is invalid.")
    return url


def private_stream_descriptor(contents: str) -> int:
    descriptor = os.memfd_create("camera-viewer-stream", os.MFD_CLOEXEC)
    try:
        data = contents.encode("utf-8")
        with os.fdopen(descriptor, "wb", closefd=False) as stream:
            stream.write(data)
        os.lseek(descriptor, 0, os.SEEK_SET)
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def imou_mpv_playlist(camera: RtspCamera) -> int:
    return private_stream_descriptor(f"{camera_stream_url(camera)}\n")


def imou_ffmpeg_playlist(camera: RtspCamera) -> int:
    return private_ffmpeg_playlist(camera_stream_url(camera), camera.transport)


def private_ffmpeg_playlist(url: str, transport: str, wallclock: bool = False) -> int:
    url = url.replace("'", "'\\''")
    contents = f"ffconcat version 1.0\nfile '{url}'\noption rtsp_transport {transport}\n"
    if wallclock:
        contents += f"option {IMOU_INPUT_CLOCK.replace('=', ' ')}\n"
    return private_stream_descriptor(contents)


def icam365_control_request(
    camera: RtspCamera, method: str, path: str, status: int, body: bytes | None = None,
) -> bool:
    if camera.provider != "rtsp":
        return False
    connection: http.client.HTTPConnection | None = None
    try:
        host = urllib.parse.urlsplit(camera.url).hostname
        if host is None:
            return False
        connection = http.client.HTTPConnection(host, ICAM365_LIGHT_PORT, timeout=3)
        connection.request(method, path)
        response = connection.getresponse()
        return response.status == status and response.getheader("Server") == ICAM365_SERVER and (
            body is None or response.read() == body
        )
    except (OSError, ValueError, http.client.HTTPException):
        return False
    finally:
        if connection is not None:
            connection.close()


def icam365_light_request(camera: RtspCamera, mode: str | None = None) -> bool:
    if mode is not None and mode not in (ICAM365_LIGHT_ON, ICAM365_LIGHT_AUTO):
        return False
    path = ICAM365_LIGHT_PATH if mode is None else f"{ICAM365_LIGHT_PATH}?mode={mode}"
    return icam365_control_request(camera, "HEAD" if mode is None else "GET", path, 200,
                                   None if mode is None else b"OK")


def icam365_ptz_request(camera: RtspCamera, direction: str | None = None) -> bool:
    if direction is None:
        return icam365_control_request(camera, "HEAD", ICAM365_PTZ_PATH, 400)
    action = ICAM365_TILT_ACTIONS.get(direction)
    if action is None:
        return False
    started = False
    try:
        started = icam365_control_request(camera, "GET", f"{ICAM365_PTZ_PATH}?act={action}", 200, b"OK")
        if started:
            time.sleep(ICAM365_TILT_PULSE_SECONDS)
    finally:
        stopped = icam365_control_request(camera, "GET", f"{ICAM365_PTZ_PATH}?act={ICAM365_PTZ_STOP}", 200, b"OK")
        if not stopped:
            stopped = icam365_control_request(camera, "GET", f"{ICAM365_PTZ_PATH}?act={ICAM365_PTZ_STOP}", 200, b"OK")
    return started and stopped


def valid_rtsp_url(url: str) -> bool:
    try:
        parsed = urllib.parse.urlsplit(url)
        return (parsed.scheme.lower() == "rtsp" and bool(parsed.hostname)
                and not parsed.username and not parsed.password and parsed.port != 0)
    except ValueError:
        return False


def load_rtsp_cameras(settings: QSettings) -> list[RtspCamera]:
    try:
        records = json.loads(settings.value(RTSP_CAMERAS_SETTING, "[]", str))
        if not isinstance(records, list):
            return []
        return [
            RtspCamera(
                record["uid"], record["name"], record["url"], record.get("transport", "tcp"),
                record.get("provider", "rtsp"), record.get("username", ""),
            )
            for record in records
            if isinstance(record, dict)
            and isinstance(record.get("uid"), str)
            and record["uid"].startswith("rtsp:")
            and isinstance(record.get("name"), str)
            and record["name"].strip()
            and isinstance(record.get("url"), str)
            and valid_rtsp_url(record["url"])
            and record.get("transport", "tcp") in ("udp", "tcp")
            and record.get("provider", "rtsp") in ("rtsp", IMOU_PROVIDER)
            and isinstance(record.get("username", ""), str)
            and (record.get("provider", "rtsp") != IMOU_PROVIDER or bool(record.get("username", "").strip()))
        ]
    except (ValueError, TypeError):
        return []


def rtsp_sound_enabled(settings: QSettings | None, camera: RtspCamera) -> bool:
    return settings.value(f"{RTSP_SOUND_SETTING}/{camera.uid}", False, bool) if settings is not None else False


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


def zoomed_video_pan(
    pan: tuple[float, float], old_level: int, new_level: int,
    x: int, y: int, width: int, height: int,
) -> tuple[float, float]:
    old_scale = 2 ** (old_level / 2)
    new_scale = 2 ** (new_level / 2)
    offset_x = (x - width / 2) / max(1, width)
    offset_y = (y - height / 2) / max(1, height)
    return (
        offset_x / new_scale - (offset_x / old_scale - pan[0]),
        offset_y / new_scale - (offset_y / old_scale - pan[1]),
    )


def dragged_video_pan(
    pan: tuple[float, float], level: int, dx: int, dy: int, width: int, height: int,
) -> tuple[float, float]:
    scale = 2 ** (level / 2)
    return (pan[0] + dx / max(1, width * scale), pan[1] + dy / max(1, height * scale))


def apply_mpv_video_pan(
    command: Callable[[list[object]], bool], pan: tuple[float, float], level: int,
) -> tuple[float, float]:
    limit = (1 - 1 / 2 ** (level / 2)) / 2
    clamped = tuple(min(limit, max(-limit, value)) for value in pan)
    command(["set_property", "video-pan-x", clamped[0]])
    command(["set_property", "video-pan-y", clamped[1]])
    return clamped


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


def continuous_directory() -> Path:
    videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    directory = (Path(videos) if videos else Path.home() / "Videos") / STORAGE_NAME / CONTINUOUS_FOLDER
    directory.mkdir(parents=True, exist_ok=True)
    return directory


_LOCAL_DETECTION_ENGINE: DetectionEngine | None = None
_LOCAL_DETECTION_LOCK = threading.Lock()


def local_detection_engine() -> DetectionEngine:
    global _LOCAL_DETECTION_ENGINE
    with _LOCAL_DETECTION_LOCK:
        if _LOCAL_DETECTION_ENGINE is None or _LOCAL_DETECTION_ENGINE.stopped.is_set():
            _LOCAL_DETECTION_ENGINE = DetectionEngine(continuous_directory())
        return _LOCAL_DETECTION_ENGINE


def measured_video_rate(times: deque[float]) -> float | None:
    if len(times) < 4 or times[-1] - times[0] < 2:
        return None
    rate = (len(times) - 1) / (times[-1] - times[0])
    return round(rate, 3) if 1 <= rate <= 60 else None


def update_local_detector(
    worker: StreamWorker | RtspStreamWorker, camera: AccountDevice | RtspCamera,
    source: Callable[[], tuple[list[str], int | None, bool]],
) -> DetectionPipeline | None:
    enabled = worker.local_detection_enabled and worker.continuous_enabled.is_set()
    if isinstance(worker, StreamWorker):
        enabled = enabled and worker.continuous is not None
    elif worker.continuous is None or worker.continuous.poll() is not None:
        enabled = False
    if not enabled:
        if worker.local_detector is not None:
            worker.local_detector.close()
            worker.local_detector = None
        return None
    if worker.local_detector is not None:
        if worker.local_detector.process.poll() is None and not worker.local_detector.engine.stopped.is_set():
            return worker.local_detector
        worker.local_detection_retry_at = time.monotonic() + 10
        worker.local_error_callback(f"Local detection decoder stopped for {camera.name}.")
        worker.local_detector.close()
        worker.local_detector = None
        return None
    if worker.local_event_callback is None or time.monotonic() < worker.local_detection_retry_at:
        return None
    try:
        if not model_path().is_file():
            raise OSError("The local detection model is missing; run ./install.sh.")
        input_args, descriptor, raw_input = source()
        worker.local_detector = DetectionPipeline(
            local_detection_engine(), camera.uid, camera.name, continuous_prefix(camera),
            input_args, worker.local_event_callback, worker.local_error_callback, descriptor, raw_input,
        )
        LOG.info("Local detection started for %s", camera.name)
        return worker.local_detector
    except (OSError, ValueError, ImportError) as ex:
        worker.local_detection_retry_at = time.monotonic() + 60
        LOG.warning("Local detection unavailable for %s: %s", camera.name, ex)
        worker.local_error_callback(f"Local detection unavailable for {camera.name}.")
        return None


def continuous_prefix(camera: AccountDevice | RtspCamera) -> str:
    name = safe_camera_name(camera.name)
    return f"{name}_{camera.uid[5:13]}" if isinstance(camera, RtspCamera) else name


def camera_recordings(camera: AccountDevice | RtspCamera, active: set[Path] | None = None) -> list[Path]:
    cutoff = datetime.now() - CONTINUOUS_RETENTION
    prefix = continuous_prefix(camera) + "_"
    latest_write = time.time() - REPLAY_FILE_IDLE_SECONDS
    active = active or set()
    recordings = []
    for path in continuous_directory().glob(f"{prefix}*{MATROSKA_SUFFIX}"):
        started = segment_time(path)
        if not path.name.startswith(prefix) or started is None or started < cutoff:
            continue
        try:
            details = path.stat()
        except OSError:
            continue
        if details.st_size > 0 and details.st_mtime <= latest_write and path not in active:
            recordings.append(path)
    return sorted(recordings, key=lambda path: path.name)


def segment_time(path: Path) -> datetime | None:
    match = CONTINUOUS_NAME_PATTERN.fullmatch(path.name)
    if match is None:
        return None
    try:
        return datetime.strptime(match.group(1), CONTINUOUS_TIME_FORMAT)
    except ValueError:
        return None


def prune_continuous_recordings(directory: Path, now: datetime, retention: timedelta) -> None:
    for path in directory.iterdir():
        start = segment_time(path)
        if start is not None and start < now - retention:
            path.unlink(missing_ok=True)
            LOG.info("Deleted continuous recording %s", path.name)


def recover_continuous_recordings(directory: Path, raw_paths: tuple[Path, ...] | None = None) -> None:
    for raw_path in raw_paths if raw_paths is not None else directory.glob(f"*{MATROSKA_SUFFIX}{RAW_RECORDING_SUFFIX}"):
        output = raw_path.with_suffix("")
        start = segment_time(output)
        try:
            result = subprocess.run(
                ["ffprobe", "-v", "error", "-count_packets", "-show_entries", "stream=nb_read_packets",
                 "-of", "csv=p=0", "-f", "h264", str(raw_path)],
                capture_output=True,
                text=True,
                timeout=RECORDING_REMUX_TIMEOUT_SECONDS,
                check=False,
            )
            frames = int(result.stdout.strip().split(",")[0])
            duration = datetime.fromtimestamp(raw_path.stat().st_mtime) - start if start is not None else timedelta()
            if frames < MIN_RECORDING_FRAMES or duration.total_seconds() <= 0:
                raise OSError("The interrupted recording is empty.")
            remux_to_matroska(raw_path, frames / duration.total_seconds(), output)
            raw_path.unlink(missing_ok=True)
            LOG.info("Recovered interrupted recording %s", output.name)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as ex:
            LOG.info("Could not recover %s: %s", raw_path.name, ex)


class ContinuousRecorder:
    def __init__(self, directory: Path, prefix: str) -> None:
        self.directory = directory
        self.prefix = prefix
        self.recorder: VideoRecorder | None = None
        self.segment_started = 0.0

    def write(self, frame: bytes, keyframe: bool, now: float) -> None:
        if self.recorder is not None and keyframe and now - self.segment_started >= CONTINUOUS_SEGMENT_SECONDS:
            self.close()
        if self.recorder is None:
            if not keyframe:
                return
            path = self.directory / f"{self.prefix}_{datetime.now():{CONTINUOUS_TIME_FORMAT}}{MATROSKA_SUFFIX}"
            self.recorder = VideoRecorder(path)
            self.segment_started = now
            prune_continuous_recordings(self.directory, datetime.now(), CONTINUOUS_RETENTION)
        try:
            self.recorder.write(frame, keyframe, now)
        except OSError as ex:
            LOG.info("Continuous recording stopped: %s", ex)
            self.close()

    def close(self) -> None:
        recorder = self.recorder
        self.recorder = None
        if recorder is None or not recorder.started:
            return
        threading.Thread(target=self._save, args=(recorder,)).start()

    def _save(self, recorder: VideoRecorder) -> None:
        try:
            LOG.info("Saved continuous recording %s", recorder.finish().name)
        except OSError as ex:
            LOG.info("Could not save continuous recording %s: %s", recorder.path.name, ex)


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


class MovementControls(QWidget):
    command_requested = pyqtSignal(str)

    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("ptzPanel")
        self.buttons: list[QPushButton] = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        directions = QHBoxLayout()
        for direction in ("Left", "Right", "Up", "Down"):
            button = QPushButton()
            set_button_icon(button, direction.lower(), f"Move camera {direction.lower()}", 40)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=direction: self.command_requested.emit(value))
            directions.addWidget(button)
            self.buttons.append(button)
        layout.addLayout(directions)
        self.presets = QWidget(self)
        self.presets.setObjectName("presetControls")
        presets = QHBoxLayout(self.presets)
        presets.setContentsMargins(0, 0, 0, 0)
        for index in range(1, 6):
            command = f"Preset {index}"
            button = QPushButton()
            set_button_icon(button, f"preset_{index}", f"Go to {command.lower()}", 40)
            button.setEnabled(False)
            button.clicked.connect(lambda checked=False, value=command: self.command_requested.emit(value))
            button.setProperty("movementCommand", command)
            presets.addWidget(button)
            self.buttons.append(button)
        layout.addWidget(self.presets)

    def set_tilt_only(self, tilt_only: bool) -> None:
        self.set_capabilities(not tilt_only, {} if tilt_only else None)

    def set_capabilities(self, pan: bool, presets: dict[str, str] | None) -> None:
        for index, button in enumerate(self.buttons[:4]):
            button.setVisible(pan or index in (2, 3))
        available = presets if presets is not None else {f"Preset {index}": f"Preset {index}" for index in range(1, 6)}
        commands = {button.property("movementCommand") for button in self.buttons[4:]}
        for command in available.keys() - commands:
            button = QPushButton(command.removeprefix("Preset "))
            button.setFixedSize(40, 40)
            button.setProperty("movementCommand", command)
            button.clicked.connect(lambda checked=False, value=command: self.command_requested.emit(value))
            self.presets.layout().addWidget(button)
            self.buttons.append(button)
        for button in self.buttons[4:]:
            command = button.property("movementCommand")
            button.setVisible(command in available)
            if command in available:
                label = f"Go to {available[command]}"
                button.setToolTip(label)
                button.setAccessibleName(label)
        self.presets.setVisible(bool(available))


class VideoStatusOverlay(ControlsOverlay):
    def __init__(self, video: VideoWidget) -> None:
        super().__init__(video)
        self.label = QLabel()
        self.label.setStyleSheet("color: white; background: transparent; font-weight: 600;")
        layout = QHBoxLayout(self)
        layout.setContentsMargins(18, 8, 18, 8)
        layout.addWidget(self.label)
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        video.set_status_overlay(self)
        self.hide()

    def display(self, message: str, persistent: bool = False) -> None:
        if message.startswith("Playback "):
            self.timer.stop()
            self.hide()
            return
        self.label.setText(message)
        self.label.setWordWrap(self.parentWidget().width() < 400)
        self.adjustSize()
        self.parentWidget().place_overlay()
        if self.parentWidget().isVisible():
            self.show()
            self.raise_()
        else:
            self.hide()
        if persistent or message.startswith("Loading") or message.startswith("Looking"):
            self.timer.stop()
        else:
            self.timer.start(NOTICE_MS)


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
        waiting = self._out_of_order[channel]
        if waiting:
            expected = self._incoming_sequence[channel]
            furthest = max(waiting, key=lambda sequence: (sequence - expected) & 0xFFFF)
            self._incoming_sequence[channel] = (furthest + 1) & 0xFFFF
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


def open_camera_session(client_id: str, service_parameter: object) -> ReliableCS2Session:
    direct = ReliableCS2Session(client_id, service_parameter, prefer_relay=False)
    try:
        direct.connect(timeout=DIRECT_CONNECT_SECONDS)
        LOG.info("Camera session is direct with %s", direct._peer[0] if direct._peer else "unknown")
        return direct
    except CS2Error as ex:
        LOG.info("Direct camera session failed (%s); using the relay", ex)
    relay = ReliableCS2Session(client_id, service_parameter)
    relay.connect(timeout=RELAY_CONNECT_SECONDS)
    LOG.info("Camera session uses the relay")
    return relay


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
    detection_event: bool | None = None

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def detection(self) -> bool:
        return self.detection_event if self.detection_event is not None else is_detection_recording(self.name)


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
            session = open_camera_session(*connection)
            try:
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

    def __init__(
        self,
        device: AccountDevice,
        camera_password: str,
        player_input: BinaryIO,
        continuous: ContinuousRecorder | None = None,
    ) -> None:
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
        self.continuous = continuous
        self.continuous_enabled = threading.Event()
        self.local_detection_enabled = False
        self.local_detection_retry_at = 0.0
        self.local_detector: DetectionPipeline | None = None
        self.local_frame_times: deque[float] = deque(maxlen=90)
        self.local_input_rate: float | None = None
        self.local_event_callback: Callable[[DetectionEvent], None] | None = None
        self.local_error_callback: Callable[[str], None] = lambda message: LOG.warning("%s", message)
        self.detection_days: list[str] | None = None
        self.display_enabled = threading.Event()
        self.display_enabled.set()
        self.display_needs_keyframe = False

    def queue_setting(self, name: str, value: object) -> bool:
        try:
            self.settings.put_nowait((name, value))
            return True
        except queue.Full:
            return False

    def set_continuous(self, enabled: bool) -> None:
        if enabled:
            self.continuous_enabled.set()
        else:
            self.continuous_enabled.clear()

    def set_display(self, enabled: bool) -> None:
        if enabled:
            self.display_enabled.set()
        else:
            self.display_enabled.clear()

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
        session = open_camera_session(client_id, service_parameter)
        stream_started = False
        try:
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
                self.local_frame_times.append(last_video)
                self._record_frame(frame, keyframe, last_video)
                if self.continuous is not None:
                    if self.continuous_enabled.is_set():
                        self.continuous.write(frame, keyframe, last_video)
                    else:
                        self.continuous.close()
                if self.local_input_rate is None:
                    self.local_input_rate = measured_video_rate(self.local_frame_times)
                detector = update_local_detector(
                    self, self.device,
                    lambda: (["-r", str(self.local_input_rate), "-f", "h264", "-i", "pipe:0"], None, True),
                ) if self.local_input_rate is not None else None
                if detector is not None:
                    detector.feed(frame, keyframe)
                if not received_video:
                    received_video = True
                    self.status_changed.emit("Live video")
                if not self.display_enabled.is_set():
                    self.display_needs_keyframe = True
                    continue
                if self.display_needs_keyframe and not keyframe:
                    continue
                self.display_needs_keyframe = False
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
            if self.continuous is not None:
                self.continuous.close()
            if self.local_detector is not None:
                self.local_detector.close()
                self.local_detector = None
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
        session = open_camera_session(*connection)
        try:
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
    camera_drop_requested = pyqtSignal(str, QPoint)

    def __init__(self) -> None:
        super().__init__()
        self.drag_start: tuple[int, int] | None = None
        self.drag_last: tuple[int, int] | None = None
        self.camera_uid = ""
        self.reorder_enabled = False
        self.drag_camera_uid = ""
        self.drag_exceeded = False
        self.drag_button = Qt.MouseButton.NoButton
        self.click_timer = QTimer(self)
        self.click_timer.setSingleShot(True)
        self.click_timer.timeout.connect(self.clicked.emit)
        self.controls_overlay: QWidget | None = None
        self.recording_badge: QWidget | None = None
        self.status_overlay: QWidget | None = None
        self.x_display = xdisplay.Display() if QApplication.platformName() == "xcb" else None
        self.input_window = None
        self.input_timer = QTimer(self)
        self.input_timer.timeout.connect(self.read_mouse_events)
        if self.x_display is not None:
            self.input_timer.start(20)

    def set_recording_badge(self, badge: QWidget) -> None:
        self.recording_badge = badge

    def set_status_overlay(self, overlay: QWidget) -> None:
        self.status_overlay = overlay

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
        for overlay in (self.controls_overlay, self.recording_badge, self.status_overlay):
            if overlay is not None and overlay.isVisible():
                overlay.raise_()
        if self.x_display is not None:
            self.x_display.flush()

    def place_overlay(self) -> None:
        if self.status_overlay is not None:
            self.status_overlay.setMaximumWidth(max(1, self.width() - 2 * OVERLAY_MARGIN))
            status_size = self.status_overlay.sizeHint()
            self.status_overlay.setGeometry(
                QRect(
                    self.mapToGlobal(QPoint((self.width() - status_size.width()) // 2, OVERLAY_MARGIN)),
                    status_size,
                )
            )
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

    def _start_drag(self, x: int, y: int, button: Qt.MouseButton = Qt.MouseButton.LeftButton) -> None:
        self.drag_start = (x, y)
        self.drag_last = (x, y)
        self.drag_button = button
        self.drag_camera_uid = self.camera_uid if self.reorder_enabled and button == Qt.MouseButton.LeftButton else ""
        self.drag_exceeded = False

    def _move_drag(self, x: int, y: int) -> None:
        if self.drag_last is None:
            return
        dx = x - self.drag_last[0]
        dy = y - self.drag_last[1]
        self.drag_last = (x, y)
        if self.drag_camera_uid:
            if abs(x - self.drag_start[0]) + abs(y - self.drag_start[1]) >= QApplication.startDragDistance():
                self.drag_exceeded = True
                self.click_timer.stop()
                self.setCursor(Qt.CursorShape.ClosedHandCursor)
            return
        if dx or dy:
            self.drag_moved.emit(dx, dy)

    def _finish_drag(self, x: int, y: int) -> None:
        self.drag_last = None
        if self.drag_start is None:
            return
        dx = x - self.drag_start[0]
        dy = y - self.drag_start[1]
        self.drag_start = None
        if self.drag_camera_uid:
            self.unsetCursor()
            if self.drag_exceeded or abs(dx) + abs(dy) >= QApplication.startDragDistance():
                self.click_timer.stop()
                self.camera_drop_requested.emit(self.drag_camera_uid, self.mapToGlobal(QPoint(x, y)))
                return
        if self.drag_button == Qt.MouseButton.RightButton:
            if abs(dx) >= DRAG_PIXELS_PER_STEP // 2 or abs(dy) >= DRAG_PIXELS_PER_STEP // 2:
                self.dragged.emit(dx, dy)
            return
        if abs(dx) < DRAG_PIXELS_PER_STEP // 2 and abs(dy) < DRAG_PIXELS_PER_STEP // 2:
            if self.click_timer.isActive():
                self.click_timer.stop()
                self.double_clicked.emit(x, y)
            else:
                self.click_timer.start(QApplication.doubleClickInterval())
        else:
            self.dragged.emit(dx, dy)

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() in (Qt.MouseButton.LeftButton, Qt.MouseButton.RightButton):
            self._start_drag(round(event.position().x()), round(event.position().y()), event.button())
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
        if event.button() == self.drag_button:
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
            elif event.detail in (1, 3):
                button = Qt.MouseButton.LeftButton if event.detail == 1 else Qt.MouseButton.RightButton
                if event.type == X11.ButtonPress:
                    self._start_drag(event.event_x, event.event_y, button)
                elif button == self.drag_button:
                    self._finish_drag(event.event_x, event.event_y)
        if motion is not None:
            self._move_drag(*motion)

    def closeEvent(self, event: object) -> None:
        self.input_timer.stop()
        if self.x_display is not None:
            self.x_display.close()
        super().closeEvent(event)


class AspectVideoFrame(QWidget):
    def __init__(self, video: QWidget) -> None:
        super().__init__()
        self.video = video
        video.setParent(self)
        video.setMinimumSize(1, 1)
        self.setMinimumSize(320, 180)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("background-color: #171717;")

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        width = min(self.width(), round(self.height() * VIDEO_ASPECT_WIDTH / VIDEO_ASPECT_HEIGHT))
        height = min(self.height(), round(width * VIDEO_ASPECT_HEIGHT / VIDEO_ASPECT_WIDTH))
        self.video.setGeometry((self.width() - width) // 2, (self.height() - height) // 2, width, height)


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


class ReplayControls(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        self.setObjectName("replayBar")
        controls = QHBoxLayout(self)
        controls.setContentsMargins(0, 0, 0, 0)
        controls.setSpacing(12)
        self.live_button = QPushButton(LIVE_BUTTON_LABEL)
        self.live_button.setObjectName("pillButton")
        self.live_button.setFixedSize(64, 44)
        self.live_button.setToolTip("Back to live video")
        self.live_button.setAccessibleName("Back to live video")
        self.previous_button = QPushButton()
        self.play_button = QPushButton()
        self.next_button = QPushButton()
        self.sound_button = QPushButton()
        self.speed_button = QPushButton(REPLAY_SPEED_LABEL.format(speed=REPLAY_SPEEDS[0]))
        self.speed_button.setObjectName("pillButton")
        self.speed_button.setFixedSize(56, 44)
        self.speed_button.setToolTip("Playback speed")
        self.speed_button.setAccessibleName("Playback speed")
        self.snapshot_button = QPushButton()
        self.save_button = QPushButton()
        self.zoom_out_button = QPushButton()
        self.zoom_in_button = QPushButton()
        self.fullscreen_button = QPushButton()
        for button, icon_name, label in (
            (self.previous_button, "previous_detection", "Previous recording"),
            (self.play_button, "pause", "Pause"),
            (self.next_button, "next_detection", "Next recording"),
            (self.sound_button, "sound_on", "Mute playback"),
            (self.snapshot_button, "photo", "Save picture"),
            (self.save_button, "download", "Save this recording"),
            (self.zoom_out_button, "zoom_out", "Show a longer period"),
            (self.zoom_in_button, "zoom_in", "Show a shorter period"),
            (self.fullscreen_button, "fullscreen", "Full screen"),
        ):
            set_button_icon(button, icon_name, label)
        controls.addStretch(1)
        for button in (
            self.live_button,
            self.previous_button,
            self.play_button,
            self.next_button,
            self.sound_button,
            self.speed_button,
            self.snapshot_button,
            self.save_button,
            self.zoom_out_button,
            self.zoom_in_button,
            self.fullscreen_button,
        ):
            controls.addWidget(button)
        controls.addStretch(1)
        self.timeline = TimelineWidget()
        self.zoom_out_button.clicked.connect(lambda: self.timeline.zoom(1))
        self.zoom_in_button.clicked.connect(lambda: self.timeline.zoom(-1))


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

    def seek(self, moment: datetime, preferred: CardRecording | None = None, retry: bool = False) -> None:
        if not retry:
            self.reloads = 0
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
            self.seek(resume, recording, retry=True)
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
        target = media_directory() / f"{safe_camera_name(self.device.name)}_{Path(self.current.recording.name).stem}{MATROSKA_SUFFIX}"
        shutil.copyfile(source, target)
        return target


class LiveLatencyGuard:
    def __init__(self) -> None:
        self.minimum_offset: float | None = None
        self.delayed_since: float | None = None

    def observe(self, position: object, now: float) -> None:
        if not isinstance(position, (int, float)) or isinstance(position, bool) or not math.isfinite(position):
            self.delayed_since = None
            return
        offset = now - position
        self.minimum_offset = offset if self.minimum_offset is None else min(self.minimum_offset, offset)
        delay = offset - self.minimum_offset
        if delay < RTSP_MAX_ACCUMULATED_DELAY_SECONDS:
            self.delayed_since = None
        elif self.delayed_since is None:
            self.delayed_since = now
        elif now - self.delayed_since >= RTSP_DELAY_CONFIRM_SECONDS:
            raise OSError(f"Live playback delay increased by {delay:.1f}s. Reconnecting to live video.")


class RtspStreamWorker(QThread):
    status_changed = pyqtSignal(str)
    failed = pyqtSignal(str)
    recording_started = pyqtSignal()
    recording_saved = pyqtSignal(str)
    recording_failed = pyqtSignal(str)
    continuous_failed = pyqtSignal(str)
    audio_available = pyqtSignal()
    light_available = pyqtSignal()
    light_changed = pyqtSignal(str)
    light_failed = pyqtSignal()
    ptz_available = pyqtSignal()
    control_completed = pyqtSignal(str)
    control_failed = pyqtSignal(str)
    capabilities_found = pyqtSignal(list, object)
    setting_completed = pyqtSignal(str, object)
    setting_failed = pyqtSignal(str, str)

    def __init__(self, camera: RtspCamera, player: subprocess.Popen[bytes], socket_path: Path,
                 quality: str = "HD") -> None:
        super().__init__()
        self.camera = camera
        self.player = player
        self.socket_path = socket_path
        self.stop_requested = threading.Event()
        self.continuous_enabled = threading.Event()
        self.local_detection_enabled = False
        self.local_detection_retry_at = 0.0
        self.local_detector: DetectionPipeline | None = None
        self.local_event_callback: Callable[[DetectionEvent], None] | None = None
        self.local_error_callback: Callable[[str], None] = lambda message: LOG.warning("%s", message)
        self.recording_request: Path | None = None
        self.recording: subprocess.Popen[bytes] | None = None
        self.recording_path: Path | None = None
        self.recording_announced = False
        self.continuous: subprocess.Popen[bytes] | None = None
        self.light_lock = threading.Lock()
        self.light_request: str | None = None
        self.ptz_lock = threading.Lock()
        self.ptz_request: str | None = None
        self.native_bridge = None
        self.imou_stream_url = ""
        self.imou_tunnel: RtspWebSocketTunnel | None = None
        self.recording_tunnels: dict[subprocess.Popen[bytes], RtspWebSocketTunnel] = {}
        self.imou_client: ImouClient | None = None
        self.imou_collections: dict[str, str] = {}
        self.quality = quality if quality in ("HD", "SD") else "HD"
        self.settings_requests: queue.Queue[tuple[str, object]] = queue.Queue()

    def queue_setting(self, name: str, value: object) -> bool:
        if self.camera.provider != IMOU_ACCOUNT_PROVIDER or name != SETTING_QUALITY or value not in ("HD", "SD"):
            return False
        self.settings_requests.put((name, value))
        return True

    def set_continuous(self, enabled: bool) -> None:
        if enabled and camera_continuous_allowed(self.camera):
            self.continuous_enabled.set()
        else:
            self.continuous_enabled.clear()

    def set_recording(self, path: Path | None) -> None:
        self.recording_request = path

    def set_light(self, mode: str) -> None:
        with self.light_lock:
            self.light_request = mode

    def set_ptz(self, direction: str) -> bool:
        if self.native_bridge is not None or self.imou_client is not None:
            pan, presets = self.movement_options()
            if direction not in ICAM365_TILT_ACTIONS and not (pan and direction in ("Left", "Right")) and direction not in presets:
                return False
        elif direction not in ICAM365_TILT_ACTIONS:
            return False
        with self.ptz_lock:
            if self.ptz_request is not None:
                return False
            self.ptz_request = direction
        return True

    def movement_options(self) -> tuple[bool, dict[str, str]]:
        if self.imou_client is not None and self.camera.imou_device is not None:
            return self.camera.imou_device.supports("PT", "PTZ"), self.imou_collections
        if self.native_bridge is None:
            return False, {}
        return self.native_bridge.pan_supported, {command: preset.name for command, preset in self.native_bridge.presets.items()}

    def stop(self) -> None:
        self.stop_requested.set()

    def _local_detection_source(self) -> tuple[list[str], int | None, bool]:
        if self.native_bridge is not None:
            return ["-i", self.native_bridge.url], None, False
        if self.camera.provider in (IMOU_PROVIDER, IMOU_ACCOUNT_PROVIDER):
            descriptor = self._private_playlist()
            return [
                "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe,rtsp,tcp,udp,rtp",
                "-i", f"/proc/self/fd/{descriptor}",
            ], descriptor, False
        return ["-rtsp_transport", self.camera.transport, "-i", self.camera.url], None, False

    def _private_playlist(self) -> int:
        if self.camera.provider == IMOU_PROVIDER:
            return imou_ffmpeg_playlist(self.camera)
        if self.imou_client is None or self.camera.imou_device is None:
            raise ImouError("The private Imou stream is not connected.")
        return private_ffmpeg_playlist(self.imou_client.stream_url(self.camera.imou_device), self.camera.transport, True)

    def _load_imou_stream(self) -> None:
        account = self.camera.imou_account
        device = self.camera.imou_device
        if account is None or device is None:
            raise ImouError("The Imou camera account configuration is invalid.")
        if device.privacy:
            raise ImouError(IMOU_PRIVACY_MESSAGE)
        self.imou_client = ImouClient(account, stored_secret(IMOU_ACCOUNT_PROVIDER, account.app_id) or "")
        self.imou_tunnel = self._open_imou_tunnel(self.quality)
        self.imou_stream_url = self.imou_tunnel.url
        deadline = time.monotonic() + 10
        while not self.stop_requested.is_set():
            if self.player.poll() is not None:
                raise ImouError("The camera video player stopped.")
            if self.socket_path.exists():
                if not mpv_request(self.socket_path, ["loadfile", self.imou_stream_url, "replace"])[0]:
                    raise ImouError("Unable to open the private Imou stream in the video player.")
                return
            if time.monotonic() >= deadline:
                raise ImouError("The camera video player did not start.")
            self.stop_requested.wait(0.1)

    def _open_imou_tunnel(self, quality: str) -> RtspWebSocketTunnel:
        if self.imou_client is None or self.camera.imou_device is None:
            raise ImouError("The private Imou stream is not connected.")
        return RtspWebSocketTunnel(self.imou_client.secure_stream_url(
            self.camera.imou_device, 0 if quality == "HD" else 1,
        ))

    def _change_imou_quality(self, quality: str) -> None:
        tunnel = self._open_imou_tunnel(quality)
        if not mpv_request(self.socket_path, ["loadfile", tunnel.url, "replace"])[0]:
            tunnel.close()
            raise ImouError("Unable to change the camera video quality.")
        previous = self.imou_tunnel
        self.imou_tunnel = tunnel
        self.imou_stream_url = tunnel.url
        self.quality = quality
        if previous is not None:
            previous.close()

    def _ffmpeg(self, output: Path, segmented: bool) -> subprocess.Popen[bytes]:
        playlist_fd = None
        tunnel = None
        command = ["ffmpeg", "-nostats", "-loglevel", "error"]
        if self.camera.provider == IMOU_ACCOUNT_PROVIDER:
            tunnel = self._open_imou_tunnel(self.quality)
            command += ["-analyzeduration", "1", "-probesize", "32768", "-fpsprobesize", "0",
                        "-rtsp_transport", "tcp", "-i", tunnel.url]
        elif self.camera.provider == IMOU_PROVIDER:
            playlist_fd = self._private_playlist()
            command += [
                "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe,rtsp,tcp,udp,rtp",
                "-i", f"/proc/self/fd/{playlist_fd}",
            ]
        else:
            bridge = get_bridge(self.camera.uid)
            command += (["-i", bridge.url] if bridge is not None else ["-rtsp_transport", self.camera.transport, "-i", self.camera.url])
        command += ["-map", "0:v:0", "-map", "0:a?", "-c", "copy"]
        if segmented:
            command += [
                "-f", "segment", "-segment_format", "matroska",
                "-segment_time", str(CONTINUOUS_SEGMENT_SECONDS),
                "-reset_timestamps", "1", "-strftime", "1",
            ]
        else:
            command += ["-f", "matroska"]
        try:
            process = subprocess.Popen(
                command + [str(output)], stdin=subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                pass_fds=(playlist_fd,) if playlist_fd is not None else (),
            )
            if tunnel is not None:
                self.recording_tunnels[process] = tunnel
            return process
        except OSError:
            if tunnel is not None:
                tunnel.close()
            raise
        finally:
            if playlist_fd is not None:
                os.close(playlist_fd)

    @staticmethod
    def _finish(process: subprocess.Popen[bytes]) -> bool:
        try:
            if process.poll() is None:
                process.communicate(input=b"q", timeout=5)
        except (OSError, subprocess.TimeoutExpired):
            process.kill()
            process.wait(timeout=5)
        return process.returncode == 0

    def _finish_recording_process(self, process: subprocess.Popen[bytes]) -> bool:
        try:
            return self._finish(process)
        finally:
            tunnel = self.recording_tunnels.pop(process, None)
            if tunnel is not None:
                tunnel.close()

    def _stop_recording(self) -> None:
        process = self.recording
        path = self.recording_path
        self.recording = None
        self.recording_path = None
        self.recording_announced = False
        if process is None or path is None:
            return
        if self._finish_recording_process(process) and path.is_file() and path.stat().st_size > 0:
            self.recording_saved.emit(str(path))
        else:
            path.unlink(missing_ok=True)
            self.recording_failed.emit("Unable to save the recording.")

    def run(self) -> None:
        try:
            if self.camera.provider == IMOU_ACCOUNT_PROVIDER:
                self._load_imou_stream()
            else:
                self.native_bridge = get_bridge(self.camera.uid)
            deadline = time.monotonic() + (60 if self.native_bridge is not None else 30)
            while not self.stop_requested.is_set():
                if self.imou_tunnel is not None and self.imou_tunnel.error:
                    raise OSError(self.imou_tunnel.error)
                if self.native_bridge is not None and self.native_bridge.error:
                    raise OSError(self.native_bridge.error)
                if self.player.poll() is not None:
                    raise OSError("The camera video player stopped.")
                ready, configured = mpv_request(self.socket_path, ["get_property", "vo-configured"])
                frame_ready = (self.camera.provider != IMOU_ACCOUNT_PROVIDER
                               or isinstance(mpv_request(self.socket_path, ["get_property", "video-params"])[1], dict))
                if ready and configured is True and frame_ready:
                    break
                if time.monotonic() >= deadline:
                    raise OSError("The RTSP camera did not provide video.")
                self.stop_requested.wait(0.25)
            if self.stop_requested.is_set():
                return
            self.status_changed.emit("Live video")
            if self.imou_client is not None:
                self.capabilities_found.emit(["HD", "SD"], None)
            tracks_ready, tracks = mpv_request(self.socket_path, ["get_property", "track-list"])
            if tracks_ready and isinstance(tracks, list) and any(
                isinstance(track, dict) and track.get("type") == "audio" for track in tracks
            ):
                self.audio_available.emit()
            light_supported = self.native_bridge.light_supported if self.native_bridge is not None else icam365_light_request(self.camera)
            if light_supported:
                self.light_available.emit()
            if self.native_bridge is not None and self.native_bridge.light_mode is not None:
                self.light_changed.emit(self.native_bridge.light_mode)
            if self.imou_client is not None and self.camera.imou_device is not None:
                ptz_supported = self.camera.imou_device.supports("PT", "PTZ")
                try:
                    self.imou_collections = self.imou_client.collections(self.camera.imou_device)
                except ImouError:
                    self.control_failed.emit("Unable to read saved camera positions.")
            else:
                ptz_supported = self.native_bridge.ptz_supported if self.native_bridge is not None else icam365_ptz_request(self.camera)
            if ptz_supported:
                self.ptz_available.emit()
            movement_options = self.movement_options()
            last_prune = 0.0
            last_position = None
            last_progress_at = time.monotonic()
            latency_guard = LiveLatencyGuard()
            pending_light = None
            pending_ptz = None
            while not self.stop_requested.is_set():
                if self.imou_tunnel is not None and self.imou_tunnel.error:
                    raise OSError(self.imou_tunnel.error)
                if self.native_bridge is not None and self.native_bridge.error:
                    raise OSError(self.native_bridge.error)
                if self.player.poll() is not None:
                    raise OSError("The camera video player stopped.")
                try:
                    setting, quality = self.settings_requests.get_nowait()
                except queue.Empty:
                    pass
                else:
                    try:
                        self._change_imou_quality(quality)
                        latency_guard = LiveLatencyGuard()
                        last_progress_at = time.monotonic()
                        self.setting_completed.emit(setting, quality)
                    except OSError as ex:
                        self.setting_failed.emit(setting, str(ex))
                current_options = self.movement_options()
                current_ptz = self.native_bridge.ptz_supported if self.native_bridge is not None else ptz_supported
                if current_ptz and (not ptz_supported or current_options != movement_options):
                    movement_options = current_options
                    self.ptz_available.emit()
                ptz_supported = current_ptz
                with self.light_lock:
                    light_mode = self.light_request
                    self.light_request = None
                if light_mode is not None:
                    if self.native_bridge is not None:
                        if self.native_bridge.control(0x8014, struct.pack("<III", 0, 1 if light_mode == ICAM365_LIGHT_ON else 2, 0)):
                            pending_light = (light_mode, time.monotonic() + 8)
                        else:
                            self.light_failed.emit()
                    elif light_supported and icam365_light_request(self.camera, light_mode):
                        self.light_changed.emit(light_mode)
                    else:
                        self.light_failed.emit()
                with self.ptz_lock:
                    direction = self.ptz_request
                    self.ptz_request = None
                if direction is not None:
                    if self.imou_client is not None and self.camera.imou_device is not None:
                        try:
                            self.imou_client.move(self.camera.imou_device, direction, self.imou_collections)
                            self.control_completed.emit(direction)
                        except ImouError as ex:
                            self.control_failed.emit(str(ex))
                    elif self.native_bridge is not None:
                        if self.native_bridge.move_camera(direction):
                            pending_ptz = (direction, time.monotonic() + 8)
                        else:
                            self.control_failed.emit("Camera movement failed.")
                    elif ptz_supported and icam365_ptz_request(self.camera, direction):
                        self.control_completed.emit(direction)
                    else:
                        self.control_failed.emit("Camera movement failed.")
                if self.native_bridge is not None:
                    while not self.native_bridge.control_results.empty():
                        command, success = self.native_bridge.control_results.get_nowait()
                        if command == 0x8014 and pending_light is not None:
                            if success:
                                self.light_changed.emit(pending_light[0])
                            else:
                                self.light_failed.emit()
                            pending_light = None
                        elif command in (PTZ_COMMAND, PTZ_POSITION_COMMAND) and pending_ptz is not None:
                            if success:
                                self.control_completed.emit(pending_ptz[0])
                            else:
                                self.control_failed.emit("Camera movement failed.")
                            pending_ptz = None
                    if pending_light is not None and time.monotonic() > pending_light[1]:
                        self.light_failed.emit()
                        pending_light = None
                    if pending_ptz is not None and time.monotonic() > pending_ptz[1]:
                        self.control_failed.emit("Camera movement failed.")
                        pending_ptz = None
                position_ready, position = mpv_request(self.socket_path, ["get_property", "time-pos"])
                latency_guard.observe(position if position_ready else None, time.monotonic())
                if position_ready and isinstance(position, (int, float)) and position != last_position:
                    last_position = position
                    last_progress_at = time.monotonic()
                elif time.monotonic() - last_progress_at > RTSP_STALL_SECONDS:
                    raise OSError("The RTSP video stream stopped producing frames.")
                if self.continuous_enabled.is_set() and self.continuous is None:
                    try:
                        directory = continuous_directory()
                        pattern = directory / f"{safe_camera_name(self.camera.name)}_{self.camera.uid[5:13]}_%Y%m%d_%H%M%S.mkv"
                        self.continuous = self._ffmpeg(pattern, True)
                    except OSError as ex:
                        self.continuous_enabled.clear()
                        LOG.info("Continuous RTSP recording could not start: %s", ex)
                        self.continuous_failed.emit("Unable to start continuous RTSP recording.")
                elif not self.continuous_enabled.is_set() and self.continuous is not None:
                    self._finish_recording_process(self.continuous)
                    self.continuous = None
                if self.continuous is not None and self.continuous.poll() is not None:
                    self._finish_recording_process(self.continuous)
                    self.continuous = None
                    self.continuous_enabled.clear()
                    self.continuous_failed.emit("Continuous RTSP recording stopped.")
                update_local_detector(self, self.camera, self._local_detection_source)
                if self.recording_request != self.recording_path:
                    self._stop_recording()
                    if self.recording_request is not None:
                        try:
                            self.recording = self._ffmpeg(self.recording_request, False)
                            self.recording_path = self.recording_request
                        except OSError as ex:
                            LOG.info("RTSP recording could not start: %s", ex)
                            self.recording_request = None
                            self.recording_failed.emit("Unable to start the recording.")
                if self.recording is not None:
                    if self.recording.poll() is not None:
                        self._stop_recording()
                        self.recording_request = None
                    elif not self.recording_announced and self.recording_path is not None and self.recording_path.is_file() and self.recording_path.stat().st_size > 0:
                        self.recording_announced = True
                        self.recording_started.emit()
                if time.monotonic() - last_prune >= 60:
                    try:
                        prune_continuous_recordings(continuous_directory(), datetime.now(), CONTINUOUS_RETENTION)
                    except OSError as ex:
                        LOG.info("Could not prune continuous RTSP recordings: %s", ex)
                    last_prune = time.monotonic()
                self.stop_requested.wait(0.25)
        except OSError as ex:
            if not self.stop_requested.is_set():
                LOG.info("RTSP camera stream stopped: %s", ex)
                self.failed.emit(str(ex))
        finally:
            self.imou_stream_url = ""
            self.imou_client = None
            self._stop_recording()
            if self.continuous is not None:
                self._finish_recording_process(self.continuous)
                self.continuous = None
            if self.imou_tunnel is not None:
                self.imou_tunnel.close()
                self.imou_tunnel = None
            if self.local_detector is not None:
                self.local_detector.close()
                self.local_detector = None
            close_bridge(self.camera.uid)


class CameraPreview(QWidget):
    stopped = pyqtSignal()
    replay_requested = pyqtSignal(object)
    camera_replay_requested = pyqtSignal(object)
    fullscreen_requested = pyqtSignal()

    def __init__(
        self, camera: AccountDevice | RtspCamera, continuous_enabled: bool = False,
        settings: QSettings | None = None,
        local_detection_enabled: bool = False,
        on_local_event: Callable[[DetectionEvent], None] | None = None,
        on_local_error: Callable[[str], None] | None = None,
    ) -> None:
        super().__init__()
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        self.setStyleSheet("background-color: #171717;")
        self.camera = camera
        self.local_detection_enabled = local_detection_enabled
        self.on_local_event = on_local_event
        self.on_local_error = on_local_error
        self.continuous_enabled = continuous_enabled
        self.settings = settings
        self.player: subprocess.Popen[bytes] | None = None
        self.mpv_directory: tempfile.TemporaryDirectory[str] | None = None
        self.worker: StreamWorker | RtspStreamWorker | None = None
        self.closing = False
        self.retry_enabled = True
        self.retry_seconds = 2
        self.live = False
        self.recording_path: Path | None = None
        self.zoom_level = 0
        self.video_pan = (0.0, 0.0)
        self.sound_enabled = rtsp_sound_enabled(settings, camera) if isinstance(camera, RtspCamera) else False
        self.control_pending = False
        self.rtsp_ptz_available = False
        self.setting_pending = False
        self.light_on: bool | None = None
        self.retry_timer = QTimer(self)
        self.retry_timer.setSingleShot(True)
        self.retry_timer.timeout.connect(self.start)
        self.overlay_timer = QTimer(self)
        self.overlay_timer.setSingleShot(True)
        self.overlay_timer.timeout.connect(self.hide_overlay)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.video = VideoWidget()
        self.video.camera_uid = camera.uid
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.setStyleSheet("background-color: #171717;")
        self.video.clicked.connect(self.toggle_overlay)
        self.video.double_clicked.connect(lambda x, y: self.fullscreen_requested.emit())
        self.video.wheel_zoomed.connect(self.change_zoom)
        self.video.drag_moved.connect(self.pan_zoomed_video)
        self.video.dragged.connect(self.move_by_drag)
        self.frame = AspectVideoFrame(self.video)
        self.video_stack = QStackedWidget()
        self.video_stack.addWidget(self.frame)
        layout.addWidget(self.video_stack, 1)
        self.status_overlay = VideoStatusOverlay(self.video)
        self.overlay = ControlsOverlay(self.video)
        self.overlay.setObjectName("cameraControls")
        self.overlay.setStyleSheet(CAMERA_CONTROLS_STYLE)
        overlay_layout = QVBoxLayout(self.overlay)
        overlay_layout.setContentsMargins(24, 10, 24, 10)
        controls = QHBoxLayout()
        overlay_layout.addLayout(controls)
        controls.setSpacing(12)
        controls.addStretch(1)
        self.replay_button = QPushButton()
        set_button_icon(self.replay_button, "replay", "Play back recordings")
        if isinstance(camera, RtspCamera) and camera.provider != IMOU_ACCOUNT_PROVIDER:
            self.replay_button.clicked.connect(lambda: self.replay_requested.emit(self.camera))
        else:
            set_replay_menu(self.replay_button, lambda: self.camera_replay_requested.emit(self.camera),
                            lambda: self.replay_requested.emit(self.camera))
        controls.addWidget(self.replay_button)
        self.snapshot_button = QPushButton()
        self.record_button = QPushButton()
        self.zoom_out_button = QPushButton()
        self.zoom_in_button = QPushButton()
        for button, icon_name, label, handler in (
            (self.snapshot_button, "photo", "Save picture", self.take_snapshot),
            (self.record_button, "record", "Record video", self.toggle_recording),
        ):
            set_button_icon(button, icon_name, label)
            button.setEnabled(False)
            button.clicked.connect(handler)
            controls.addWidget(button)
        self.sound_button = QPushButton()
        set_button_icon(self.sound_button, "sound", "Listen to camera")
        self.sound_button.setEnabled(False)
        self.sound_button.clicked.connect(self.toggle_sound)
        self.sound_button.setVisible(not isinstance(camera, RtspCamera))
        controls.addWidget(self.sound_button)
        self.rtsp_light_button: QPushButton | None = None
        self.rtsp_light_mode: str | None = None
        self.light_action: QAction | None = None
        self.quality_actions: dict[str, QAction] = {}
        if isinstance(camera, RtspCamera):
            self.rtsp_light_button = QPushButton()
            set_icam365_light_icon(self.rtsp_light_button, None)
            self.rtsp_light_button.clicked.connect(self.toggle_rtsp_light)
            self.rtsp_light_button.hide()
            controls.addWidget(self.rtsp_light_button)
        for button, icon_name, label, handler in (
            (self.zoom_out_button, "zoom_out", "Zoom out", lambda: self.change_zoom(-1)),
            (self.zoom_in_button, "zoom_in", "Zoom in", lambda: self.change_zoom(1)),
        ):
            set_button_icon(button, icon_name, label)
            button.setEnabled(False)
            button.clicked.connect(handler)
            controls.addWidget(button)
        self.ptz_button = QPushButton()
        set_button_icon(
            self.ptz_button, "ptz", "Tilt controls" if isinstance(camera, RtspCamera) else "Pan and tilt controls"
        )
        self.ptz_button.setEnabled(False)
        self.ptz_button.clicked.connect(self.toggle_ptz_panel)
        self.ptz_panel = MovementControls(self.overlay)
        self.ptz_panel.set_tilt_only(isinstance(camera, RtspCamera))
        self.ptz_panel.command_requested.connect(self.move_camera)
        self.ptz_panel.hide()
        overlay_layout.addWidget(self.ptz_panel)
        if not isinstance(camera, RtspCamera):
            self.light_button = QPushButton()
            self.light_button.hide()
            controls.insertWidget(controls.indexOf(self.zoom_out_button), self.light_button)
            self.light_action = QAction("Turn white light on", self)
            self.light_action.setVisible(False)
            self.light_action.triggered.connect(self.toggle_light)
            self.light_action.changed.connect(self._update_light_action)
            self.light_button.clicked.connect(self.light_action.trigger)
        if not isinstance(camera, RtspCamera) or camera.provider == IMOU_ACCOUNT_PROVIDER:
            self.quality_button = QPushButton(DEFAULT_QUALITY_LABEL)
            self.quality_button.setObjectName("qualityButton")
            self.quality_button.setFixedSize(56, 44)
            self.quality_button.hide()
            controls.insertWidget(1, self.quality_button)
            self.quality_menu = QMenu(self.quality_button)
            self.quality_menu.menuAction().setVisible(False)
            self.quality_menu.menuAction().changed.connect(
                lambda: self.quality_button.setVisible(self.quality_menu.menuAction().isVisible())
            )
            self.quality_button.clicked.connect(lambda: self.quality_menu.popup(
                self.quality_button.mapToGlobal(QPoint(0, -self.quality_menu.sizeHint().height()))
            ))
            quality_group = QActionGroup(self.quality_menu)
            for quality in VIDEO_QUALITIES:
                action = self.quality_menu.addAction(quality)
                action.setCheckable(True)
                action.setVisible(False)
                action.triggered.connect(lambda checked=False, value=quality: self.choose_quality(value))
                quality_group.addAction(action)
                self.quality_actions[quality] = action
        if isinstance(camera, RtspCamera):
            self.ptz_button.hide()
        controls.addWidget(self.ptz_button)
        self.fullscreen_button = QPushButton()
        set_button_icon(self.fullscreen_button, "fullscreen", "Full screen")
        self.fullscreen_button.clicked.connect(self.fullscreen_requested.emit)
        controls.addWidget(self.fullscreen_button)
        controls.addStretch(1)
        self.video.set_controls_overlay(self.overlay)
        for button in self.overlay.findChildren(QPushButton):
            button.clicked.connect(self.show_overlay)
        self.overlay.hide()

    def show_status(self, message: str, persistent: bool = False) -> None:
        if message == "Live video":
            self.status_overlay.timer.stop()
            self.status_overlay.hide()
        else:
            self.status_overlay.display(message, persistent)

    def show_overlay(self) -> None:
        if not self.isVisible():
            return
        self.video.place_overlay()
        self.overlay.show()
        self.video.raise_interaction_layer()
        self.overlay_timer.start(OVERLAY_TIMEOUT_MS)

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
        self.video.place_overlay()
        self.show_overlay()

    def set_movement_enabled(self, enabled: bool) -> None:
        self.ptz_button.setEnabled(enabled)
        for button in self.ptz_panel.buttons:
            button.setEnabled(enabled)

    def start(self) -> None:
        if self.closing or self.worker is not None:
            return
        if (isinstance(self.camera, RtspCamera) and self.camera.imou_device is not None
                and self.camera.imou_device.privacy):
            self._on_failed(IMOU_PRIVACY_MESSAGE)
            return
        if isinstance(self.camera, RtspCamera):
            self.sound_enabled = rtsp_sound_enabled(self.settings, self.camera)
            self.sound_button.hide()
            set_button_icon(
                self.sound_button, "sound_on" if self.sound_enabled else "sound",
                "Mute camera" if self.sound_enabled else "Listen to camera",
            )
            self.rtsp_ptz_available = False
            self.ptz_button.hide()
            self.ptz_panel.hide()
        if self.rtsp_light_button is not None:
            self.rtsp_light_button.hide()
            self.rtsp_light_mode = None
            set_icam365_light_icon(self.rtsp_light_button, None)
        self.show_status("Connecting...", True)
        try:
            self.mpv_directory = tempfile.TemporaryDirectory(prefix="okam-linux-mpv-")
        except OSError:
            self.show_status("Player unavailable", True)
            self._retry()
            return
        socket_path = Path(self.mpv_directory.name) / "control.sock"
        rtsp_camera = self.camera if isinstance(self.camera, RtspCamera) else None
        playlist_fd = None
        try:
            if rtsp_camera is not None and rtsp_camera.provider == IMOU_PROVIDER:
                playlist_fd = imou_mpv_playlist(rtsp_camera)
            command = (
                mpv_rtsp_command(socket_path, int(self.video.winId()), True, rtsp_camera, self.sound_enabled, playlist_fd)
                if rtsp_camera else mpv_stream_command(socket_path, int(self.video.winId()), True)
            )
            self.player = subprocess.Popen(
                command, stdin=subprocess.DEVNULL if rtsp_camera else subprocess.PIPE,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, bufsize=0,
                pass_fds=(playlist_fd,) if playlist_fd is not None else (),
            )
        except OSError as ex:
            if rtsp_camera is not None:
                close_bridge(rtsp_camera.uid)
            message = str(ex) if isinstance(ex, CameraCredentialError) else "Player unavailable"
            self.show_status(message)
            self._cleanup_player()
            self._retry()
            return
        finally:
            if playlist_fd is not None:
                os.close(playlist_fd)
        if rtsp_camera:
            quality = self.settings.value(f"{QUALITY_SETTING}/{self.camera.uid}", "HD", str) if self.settings is not None else "HD"
            self.worker = RtspStreamWorker(rtsp_camera, self.player, socket_path, quality)
        else:
            assert self.player.stdin is not None
            try:
                continuous = ContinuousRecorder(continuous_directory(), continuous_prefix(self.camera))
            except OSError:
                continuous = None
                self.show_status("Unable to create the recording folder")
            self.worker = StreamWorker(
                self.camera, stored_camera_password(self.camera.uid) or "", self.player.stdin, continuous,
            )
            self.worker.set_display(self.isVisible())
        self.worker.set_continuous(self.continuous_enabled)
        self.worker.local_detection_enabled = self.local_detection_enabled
        self.worker.local_event_callback = self.on_local_event
        if self.on_local_error is not None:
            self.worker.local_error_callback = self.on_local_error
        self.worker.status_changed.connect(self._on_status)
        self.worker.failed.connect(self._on_failed)
        self.worker.recording_started.connect(self._on_recording_started)
        self.worker.recording_saved.connect(self._on_recording_saved)
        self.worker.recording_failed.connect(self._on_recording_failed)
        if isinstance(self.worker, RtspStreamWorker):
            self.worker.continuous_failed.connect(self._on_continuous_failed)
            self.worker.audio_available.connect(self._on_rtsp_audio_available)
            self.worker.light_available.connect(self._on_rtsp_light_available)
            self.worker.light_changed.connect(self._on_rtsp_light_changed)
            self.worker.light_failed.connect(self._on_rtsp_light_failed)
            self.worker.ptz_available.connect(self._on_rtsp_ptz_available)
            self.worker.control_completed.connect(self._on_control_finished)
            self.worker.control_failed.connect(self._on_control_failed)
        if isinstance(self.worker, StreamWorker):
            self.worker.sound_changed.connect(self._on_sound_changed)
            self.worker.sound_failed.connect(self._on_sound_failed)
            self.worker.control_completed.connect(self._on_control_finished)
            self.worker.control_failed.connect(self._on_control_failed)
        self.worker.capabilities_found.connect(self._on_capabilities)
        self.worker.setting_completed.connect(self._on_setting_completed)
        self.worker.setting_failed.connect(self._on_setting_failed)
        self.worker.finished.connect(self._on_finished)
        self.worker.start()

    def _on_status(self, message: str) -> None:
        if message == "Live video":
            self.retry_seconds = 2
            self.live = True
            self.snapshot_button.setEnabled(True)
            self.record_button.setEnabled(True)
            self.zoom_in_button.setEnabled(True)
            if self.sound_button is not None and not isinstance(self.worker, RtspStreamWorker):
                self.sound_button.setEnabled(True)
            self.set_movement_enabled(not isinstance(self.worker, RtspStreamWorker) or self.rtsp_ptz_available)
        self.show_status(message)

    def _on_failed(self, message: str) -> None:
        self.control_pending = False
        self.setting_pending = False
        self.show_status(message, True)
        self.retry_enabled = message not in (
            "The camera rejected the available credentials.", IMOU_PRIVACY_MESSAGE, IMOU_SECRET_MISSING_MESSAGE,
        )

    def _on_finished(self) -> None:
        self.live = False
        self.recording_path = None
        self.zoom_level = 0
        self.video_pan = (0.0, 0.0)
        self.sound_enabled = False
        self.control_pending = False
        self.rtsp_ptz_available = False
        self.set_movement_enabled(False)
        self.ptz_panel.hide()
        for button in (self.snapshot_button, self.record_button, self.zoom_out_button, self.zoom_in_button,
                       self.sound_button, self.ptz_button, self.rtsp_light_button):
            if button is not None:
                button.setEnabled(False)
        set_button_icon(self.record_button, "record", "Record video", 30)
        self.worker = None
        self._cleanup_player()
        if self.closing:
            self.stopped.emit()
        elif self.retry_enabled:
            self._retry()

    def _mpv_command(self, command: list[object]) -> bool:
        socket_path = Path(self.mpv_directory.name) / "control.sock" if self.mpv_directory is not None else None
        return mpv_request(socket_path, command)[0]

    def take_snapshot(self) -> None:
        if not self.live:
            return
        try:
            path = media_directory() / f"{safe_camera_name(self.camera.name)}_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        except OSError:
            self._on_failed("Unable to create the picture folder.")
            return
        if not self._mpv_command(["screenshot-to-file", str(path), "video"]):
            self._on_failed("Unable to save a picture.")

    def toggle_recording(self) -> None:
        if not self.live or self.worker is None:
            return
        if self.recording_path is None:
            try:
                self.recording_path = media_directory() / f"{safe_camera_name(self.camera.name)}_{datetime.now():%Y%m%d_%H%M%S_%f}.mkv"
            except OSError:
                self._on_failed("Unable to create the recording folder.")
                return
            self.worker.set_recording(self.recording_path)
            set_button_icon(self.record_button, "recording", "Stop recording", 30)
        else:
            self.worker.set_recording(None)
            self.recording_path = None
            set_button_icon(self.record_button, "record", "Record video", 30)

    def _on_recording_started(self) -> None:
        self.show_status("Recording video")

    def _on_recording_saved(self, path: str) -> None:
        self.show_status("Live video")

    def _on_recording_failed(self, message: str) -> None:
        self.recording_path = None
        set_button_icon(self.record_button, "record", "Record video", 30)
        self.show_status(message)

    def _on_continuous_failed(self, message: str) -> None:
        self.show_status(message)

    def _on_rtsp_light_available(self) -> None:
        if self.rtsp_light_button is not None and self.live:
            self.rtsp_light_button.setEnabled(True)
            self.rtsp_light_button.show()
            self.video.place_overlay()

    def set_rtsp_light(self, mode: str) -> None:
        if isinstance(self.worker, RtspStreamWorker) and self.live:
            if self.rtsp_light_button is not None:
                self.rtsp_light_button.setEnabled(False)
            self.worker.set_light(mode)

    def toggle_rtsp_light(self) -> None:
        self.set_rtsp_light(ICAM365_LIGHT_AUTO if self.rtsp_light_mode == ICAM365_LIGHT_ON else ICAM365_LIGHT_ON)

    def _on_rtsp_light_changed(self, mode: str) -> None:
        self.rtsp_light_mode = mode
        if self.rtsp_light_button is not None:
            set_icam365_light_icon(self.rtsp_light_button, mode)
            self.rtsp_light_button.setEnabled(self.live)

    def _on_rtsp_light_failed(self) -> None:
        if self.rtsp_light_button is not None:
            self.rtsp_light_button.setEnabled(self.live)
        self.show_status("Unable to change white light mode")

    def _on_rtsp_ptz_available(self) -> None:
        if self.live and isinstance(self.worker, RtspStreamWorker):
            self.rtsp_ptz_available = True
            self.ptz_panel.set_capabilities(*self.worker.movement_options())
            self.set_movement_enabled(not self.control_pending)
            set_button_icon(self.ptz_button, "ptz", "Pan and tilt controls" if self.worker.movement_options()[0] else "Tilt controls")
            self.ptz_button.show()
            self.video.place_overlay()

    def set_continuous(self, enabled: bool) -> None:
        self.continuous_enabled = enabled
        if self.worker is not None:
            self.worker.set_continuous(enabled)

    def change_zoom(self, step: int, x: int | None = None, y: int | None = None) -> None:
        if not self.live:
            return
        level = min(MAX_ZOOM_LEVEL, max(0, self.zoom_level + step))
        if level == self.zoom_level or not self._mpv_command(["set_property", "video-zoom", level / 2]):
            return
        if x is not None and y is not None:
            self.video_pan = zoomed_video_pan(
                self.video_pan, self.zoom_level, level, x, y, self.video.width(), self.video.height()
            )
        self.zoom_level = level
        self.apply_video_pan()
        self.zoom_out_button.setEnabled(level > 0)
        self.zoom_in_button.setEnabled(level < MAX_ZOOM_LEVEL)

    def pan_zoomed_video(self, dx: int, dy: int) -> None:
        if not self.live or self.zoom_level == 0:
            return
        self.video_pan = dragged_video_pan(
            self.video_pan, self.zoom_level, dx, dy, self.video.width(), self.video.height()
        )
        self.apply_video_pan()

    def apply_video_pan(self) -> None:
        self.video_pan = apply_mpv_video_pan(self._mpv_command, self.video_pan, self.zoom_level)

    def toggle_sound(self) -> None:
        if isinstance(self.worker, RtspStreamWorker):
            if self._mpv_command(["set_property", "mute", self.sound_enabled]):
                self._on_sound_changed(not self.sound_enabled)
            else:
                self._on_sound_failed("Unable to change camera sound")
            return
        if isinstance(self.worker, StreamWorker) and self.sound_button is not None:
            self.sound_button.setEnabled(False)
            self.worker.set_sound(not self.sound_enabled)

    def _on_rtsp_audio_available(self) -> None:
        if self.live and isinstance(self.worker, RtspStreamWorker):
            self.sound_button.setEnabled(True)
            self.sound_button.show()
            self.video.place_overlay()

    def _on_sound_changed(self, enabled: bool) -> None:
        self.sound_enabled = enabled
        if isinstance(self.camera, RtspCamera) and self.settings is not None:
            self.settings.setValue(f"{RTSP_SOUND_SETTING}/{self.camera.uid}", enabled)
            self.settings.sync()
        if self.sound_button is not None:
            set_button_icon(self.sound_button, "sound_on" if enabled else "sound",
                            "Mute camera" if enabled else "Listen to camera", 30)
            self.sound_button.setEnabled(self.live)

    def _on_sound_failed(self, message: str) -> None:
        if self.sound_button is not None:
            self.sound_button.setEnabled(self.live)
        self.show_status(message)

    def move_camera(self, direction: str) -> None:
        if not self.live or self.control_pending:
            return
        if isinstance(self.worker, RtspStreamWorker):
            queued = self.rtsp_ptz_available and self.worker.set_ptz(direction)
        elif isinstance(self.worker, StreamWorker):
            queued = self.worker.queue_control((direction,))
        else:
            queued = False
        if queued:
            self.control_pending = True
            self.set_movement_enabled(False)

    def move_by_drag(self, dx: int, dy: int) -> None:
        if self.zoom_level > 0 or not self.live or not isinstance(self.worker, RtspStreamWorker):
            return
        if self.worker.movement_options()[0] and abs(dx) >= max(abs(dy), DRAG_PIXELS_PER_STEP // 2):
            self.move_camera("Left" if dx > 0 else "Right")
        elif abs(dy) >= max(abs(dx), DRAG_PIXELS_PER_STEP // 2):
            self.move_camera("Up" if dy > 0 else "Down")

    def _on_control_finished(self, command: str) -> None:
        self.control_pending = False
        self.set_movement_enabled(self.live and (
            not isinstance(self.worker, RtspStreamWorker) or self.rtsp_ptz_available
        ))

    def _on_control_failed(self, message: str) -> None:
        self._on_control_finished("")
        self.show_status(message)

    def _on_capabilities(self, qualities: list[str], light_on: bool | None) -> None:
        self.light_on = light_on
        if self.light_action is not None:
            self.light_action.setVisible(light_on is not None)
            self._update_light_action()
        for quality, action in self.quality_actions.items():
            action.setVisible(quality in qualities)
            action.setChecked(
                self.settings is not None
                and quality == self.settings.value(f"{QUALITY_SETTING}/{self.camera.uid}", "", str)
            )
        if self.ptz_button is not None:
            self.quality_menu.menuAction().setVisible(bool(qualities))
            saved = self.settings.value(f"{QUALITY_SETTING}/{self.camera.uid}", "", str) if self.settings is not None else ""
            self.quality_button.setText(saved if saved in qualities else DEFAULT_QUALITY_LABEL)

    def _update_light_action(self) -> None:
        if self.light_action is not None:
            label = "Turn white light off" if self.light_on else "Turn white light on"
            if self.light_action.text() != label:
                self.light_action.setText(label)
            set_button_icon(self.light_button, "light_on" if self.light_on else "light",
                            "Turn white light off" if self.light_on else "Turn white light on")
            self.light_button.setVisible(self.light_action.isVisible())
            self.light_button.setEnabled(self.light_action.isEnabled())

    def _queue_setting(self, name: str, value: object) -> None:
        if not self.live or self.worker is None or self.setting_pending:
            return
        if self.worker.queue_setting(name, value):
            self.setting_pending = True
            if self.light_action is not None:
                self.light_action.setEnabled(False)
            self.quality_menu.setEnabled(False)

    def toggle_light(self) -> None:
        if self.light_on is not None:
            self._queue_setting(SETTING_LIGHT, not self.light_on)

    def choose_quality(self, quality: str) -> None:
        if self.recording_path is None:
            self._queue_setting(SETTING_QUALITY, quality)

    def _on_setting_completed(self, name: str, value: object) -> None:
        self.setting_pending = False
        if self.light_action is not None:
            self.light_action.setEnabled(True)
        self.quality_menu.setEnabled(True)
        if name == SETTING_LIGHT:
            self.light_on = bool(value)
            self._update_light_action()
        elif self.settings is not None:
            self.settings.setValue(f"{QUALITY_SETTING}/{self.camera.uid}", value)
            self.settings.sync()
            for quality, action in self.quality_actions.items():
                action.setChecked(quality == value)
            self.quality_button.setText(str(value))

    def _on_setting_failed(self, name: str, message: str) -> None:
        self.setting_pending = False
        if self.light_action is not None:
            self.light_action.setEnabled(True)
        self.quality_menu.setEnabled(True)
        if name == SETTING_QUALITY:
            saved = self.settings.value(f"{QUALITY_SETTING}/{self.camera.uid}", "", str) if self.settings is not None else ""
            for quality, action in self.quality_actions.items():
                action.setChecked(quality == saved)
        self.show_status(message)

    def _retry(self) -> None:
        if self.closing:
            return
        self.retry_timer.start(self.retry_seconds * 1000)
        self.retry_seconds = min(60, self.retry_seconds * 2)

    def _cleanup_player(self) -> None:
        stop_mpv_player(self.player)
        self.player = None
        if self.mpv_directory is not None:
            self.mpv_directory.cleanup()
            self.mpv_directory = None

    def set_display(self, enabled: bool) -> None:
        if not enabled:
            self.overlay_timer.stop()
            self.hide_overlay()
            self.status_overlay.hide()
        if isinstance(self.worker, StreamWorker):
            self.worker.set_display(enabled)

    def set_local_detection(self, enabled: bool) -> None:
        self.local_detection_enabled = enabled
        if self.worker is not None:
            self.worker.local_detection_enabled = enabled

    def stop(self) -> None:
        self.closing = True
        self.retry_timer.stop()
        self.overlay_timer.stop()
        self.hide_overlay()
        self.status_overlay.timer.stop()
        self.status_overlay.hide()
        if self.worker is not None:
            self.worker.stop()
        else:
            self._cleanup_player()
            self.stopped.emit()


class ImouReplayWorker(QThread):
    catalog_ready = pyqtSignal(list)
    playback_ready = pyqtSignal(str, str, float)
    saved = pyqtSignal(str)
    failed = pyqtSignal(str)

    def __init__(self, camera: RtspCamera) -> None:
        super().__init__()
        self.camera = camera
        self.requests: queue.Queue[tuple[str, object]] = queue.Queue()
        self.downloads: queue.Queue[tuple[ImouRecording, Path]] = queue.Queue()
        self.saving = threading.Event()
        self.stopped = threading.Event()
        self.tunnel: ImouReplayTunnel | None = None
        self.client: ImouClient | None = None
        self.playback_generation = 0

    def list_range(self, start: datetime, end: datetime) -> None:
        self.requests.put(("list", (start, end)))

    def open_recording(self, recording: ImouRecording, offset: float = 0) -> None:
        self.playback_generation += 1
        self.requests.put(("open", (recording, offset, self.playback_generation)))

    def save_recording(self, recording: ImouRecording, target: Path) -> bool:
        if self.saving.is_set() or self.stopped.is_set():
            return False
        self.saving.set()
        self.downloads.put((recording, target))
        return True

    def stop(self) -> None:
        self.stopped.set()

    def run(self) -> None:
        downloader = None
        try:
            account = self.camera.imou_account
            if account is None or self.camera.imou_device is None:
                raise ImouError("The Imou playback account configuration is invalid.")
            self.client = ImouClient(account, stored_secret(IMOU_ACCOUNT_PROVIDER, account.app_id) or "")
            downloader = threading.Thread(target=self._download, daemon=True)
            downloader.start()
            while not self.stopped.is_set():
                try:
                    command, value = self.requests.get(timeout=0.25)
                except queue.Empty:
                    continue
                try:
                    if command == "list":
                        self.catalog_ready.emit(self.client.recordings(self.camera.imou_device, *value))
                    elif command == "open":
                        recording, offset, generation = value
                        if generation != self.playback_generation:
                            continue
                        if self.tunnel is not None:
                            self.tunnel.close()
                            self.tunnel = None
                        if self.stopped.is_set():
                            break
                        duration = (recording.end - recording.start).total_seconds()
                        offset = min(max(0, math.floor(offset)), max(0, duration - 1))
                        selected = replace(recording, start=recording.start + timedelta(seconds=offset))
                        url = self.client.replay_url(self.camera.imou_device, selected)
                        if self.stopped.is_set():
                            break
                        if generation != self.playback_generation:
                            continue
                        self.tunnel = ImouReplayTunnel(url, selected.start, selected.end,
                                                      self.camera.imou_device.channel_id)
                        if not self.stopped.is_set():
                            self.playback_ready.emit(recording.key, self.tunnel.url, offset)
                except OSError as ex:
                    if not self.stopped.is_set():
                        self.failed.emit(str(ex))
        except OSError as ex:
            if not self.stopped.is_set():
                self.failed.emit(str(ex))
        finally:
            self.stopped.set()
            if self.tunnel is not None:
                self.tunnel.close()
                self.tunnel = None
            if downloader is not None:
                downloader.join()
            self.client = None

    def _download(self) -> None:
        client = None
        while not self.stopped.is_set():
            try:
                request = self.downloads.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                if client is None:
                    account = self.camera.imou_account
                    client = ImouClient(account, stored_secret(IMOU_ACCOUNT_PROVIDER, account.app_id) or "")
                self._save(*request, client=client)
            except OSError as ex:
                if not self.stopped.is_set():
                    self.failed.emit(str(ex))
            finally:
                self.saving.clear()

    def _save(self, recording: ImouRecording, target: Path, client: ImouClient | None = None) -> None:
        if self.stopped.is_set():
            return
        url = (client or self.client).replay_url(self.camera.imou_device, recording)
        if self.stopped.is_set():
            return
        tunnel = ImouReplayTunnel(url, recording.start, recording.end, self.camera.imou_device.channel_id)
        process = None
        temporary = None
        try:
            descriptor, filename = tempfile.mkstemp(prefix="intraswitch_camera_viewer_", suffix=MATROSKA_SUFFIX,
                                                   dir=target.parent)
            os.close(descriptor)
            temporary = Path(filename)
            duration = math.ceil((recording.end - recording.start).total_seconds())
            process = subprocess.Popen([
                "ffmpeg", "-nostats", "-loglevel", "error", "-analyzeduration", "1000000", "-probesize", "1048576",
                "-fpsprobesize", "0", "-f", "dhav",
                "-i", tunnel.url, "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                "-f", "matroska", "-y", str(temporary),
            ], stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + duration + 60
            while process.poll() is None and not self.stopped.is_set():
                if time.monotonic() >= deadline or tunnel.error:
                    break
                self.stopped.wait(0.1)
            succeeded = (process.poll() == 0 and not tunnel.error and temporary.is_file()
                         and temporary.stat().st_size > 0)
            if succeeded and not self.stopped.is_set():
                temporary.replace(target)
                self.saved.emit(str(target))
            elif not self.stopped.is_set():
                raise ImouError("Unable to save the camera recording.")
        finally:
            if process is not None and process.poll() is None:
                RtspStreamWorker._finish(process)
            tunnel.close()
            if temporary is not None:
                temporary.unlink(missing_ok=True)


class LocalReplayPane(QWidget):
    live_requested = pyqtSignal()
    fullscreen_requested = pyqtSignal()
    stopped = pyqtSignal()

    def __init__(
        self, camera: AccountDevice | RtspCamera, parent: QWidget,
        worker: StreamWorker | RtspStreamWorker | None = None,
        initial_detection: DetectionEvent | None = None,
        camera_recordings: bool = False,
    ) -> None:
        super().__init__(parent)
        self.camera = camera
        self.worker = worker
        self.camera_recordings = camera_recordings
        self.remote_worker: ImouReplayWorker | None = None
        self.remote_recordings: dict[Path, ImouRecording] = {}
        self.remote_days: set[datetime] = set()
        self.closing = False
        self.player: subprocess.Popen[bytes] | None = None
        self.mpv_directory: tempfile.TemporaryDirectory[str] | None = None
        self.socket_path: Path | None = None
        self.recordings: list[Path] = []
        self.detected_events: dict[Path, DetectionEvent] = {}
        self.initial_detection = initial_detection
        self.current_path: Path | None = None
        self.segment_durations: dict[Path, int] = {}
        self.pending_seek: float | None = None
        self.remote_offset = 0.0
        self.rewind_after_load = False
        self.zoom_level = 0
        self.speed_index = 0
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.video = VideoWidget()
        self.video.camera_uid = camera.uid
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.setStyleSheet("background-color: #171717;")
        self.video.clicked.connect(self.toggle_overlay)
        self.video.double_clicked.connect(lambda x, y: self.fullscreen_requested.emit())
        self.video.wheel_zoomed.connect(lambda step, x, y: self.change_zoom(step))
        self.frame = AspectVideoFrame(self.video)
        layout.addWidget(self.frame, 1)
        self.overlay = ControlsOverlay(self.video)
        self.overlay.setObjectName("cameraControls")
        self.overlay.setStyleSheet(
            "#cameraControls QPushButton { color: white; background-color: transparent;"
            " border: none; border-radius: 6px; padding: 4px; }"
            "#cameraControls QPushButton:hover { background-color: rgba(255, 255, 255, 35); }"
            "#cameraControls #pillButton { border: 2px solid white; border-radius: 8px;"
            " font-weight: 600; margin: 6px 2px; }"
            "#cameraControls QPushButton:disabled { color: rgba(255, 255, 255, 90); }"
            + TOOLTIP_STYLE
        )
        controls_layout = QVBoxLayout(self.overlay)
        controls_layout.setContentsMargins(24, 10, 24, 10)
        self.controls = ReplayControls(self.overlay)
        self.timeline = self.controls.timeline
        controls_layout.addWidget(self.controls)
        controls_layout.addWidget(self.timeline)
        self.controls.live_button.clicked.connect(self.live_requested.emit)
        self.controls.previous_button.clicked.connect(lambda: self.jump_detection(-1))
        self.controls.play_button.clicked.connect(self.toggle_playing)
        self.controls.next_button.clicked.connect(lambda: self.jump_detection(1))
        self.controls.sound_button.clicked.connect(self.toggle_sound)
        self.controls.speed_button.clicked.connect(self.change_speed)
        self.controls.snapshot_button.clicked.connect(self.take_snapshot)
        self.controls.save_button.clicked.connect(self.save_clip)
        self.controls.fullscreen_button.clicked.connect(self.fullscreen_requested.emit)
        self.timeline.seek_requested.connect(self.seek_time)
        self.controls.save_button.setEnabled(False)
        self.video.set_controls_overlay(self.overlay)
        self.overlay.hide()
        self.overlay_timer = QTimer(self)
        self.overlay_timer.setSingleShot(True)
        self.overlay_timer.timeout.connect(self.overlay.hide)
        for button in self.controls.findChildren(QPushButton):
            button.clicked.connect(self.show_overlay)
        self.status_overlay = VideoStatusOverlay(self.video)
        self.progress_timer = QTimer(self)
        self.progress_timer.timeout.connect(self.update_progress)
        self.refresh_timer = QTimer(self)
        self.refresh_timer.timeout.connect(self.refresh_recordings)
        self.refresh_timer.start(60_000)
        if camera_recordings:
            if not isinstance(camera, RtspCamera) or camera.provider != IMOU_ACCOUNT_PROVIDER:
                raise OSError("Camera playback is unavailable through this connection.")
            self.remote_worker = ImouReplayWorker(camera)
            self.controls.speed_button.setVisible(camera.imou_device.supports("LRRF"))
            self.remote_worker.catalog_ready.connect(self.on_remote_catalog)
            self.remote_worker.playback_ready.connect(self.on_remote_playback)
            self.remote_worker.saved.connect(lambda path: self.status_overlay.display(f"Recording saved: {Path(path).name}"))
            self.remote_worker.failed.connect(self.status_overlay.display)
            self.remote_worker.finished.connect(self.on_remote_finished)
            self.timeline.range_changed.connect(self.load_remote_range)
            self.remote_worker.start()
            self.refresh_timer.setInterval(300_000)
        QTimer.singleShot(0, self.refresh_recordings)

    def show_overlay(self) -> None:
        if not self.isVisible():
            return
        self.video.place_overlay()
        self.overlay.show()
        self.video.raise_interaction_layer()
        self.overlay_timer.start(OVERLAY_TIMEOUT_MS)

    def toggle_overlay(self) -> None:
        if self.overlay.isVisible():
            self.overlay_timer.stop()
            self.overlay.hide()
        else:
            self.show_overlay()

    def refresh_recordings(self) -> None:
        if self.closing:
            return
        if self.remote_worker is not None:
            self.remote_days.discard(datetime.now().replace(hour=0, minute=0, second=0, microsecond=0))
            self.load_remote_range(datetime.now() - timedelta(days=1), datetime.now())
            return
        try:
            continuous = camera_recordings(self.camera, self.active_recordings())
            self.detected_events = {event.clip: event for event in load_events(self.camera.uid) if event.clip is not None}
            self.recordings = sorted(
                [*continuous, *self.detected_events], key=lambda path: self.recording_start(path) or datetime.min,
            )
        except OSError:
            self.status_overlay.display("Unable to read local recordings")
            return
        timeline_recordings = []
        for path in self.recordings:
            event = self.detected_events.get(path)
            if event is not None:
                timeline_recordings.append(CardRecording(
                    f"{event.first:%Y%m%d%H%M%S}_001.mp4", event.first,
                    max(1, math.ceil((event.last - event.first).total_seconds())), 0,
                ))
            elif (started := segment_time(path)) is not None:
                timeline_recordings.append(CardRecording(
                    path.name, started, self.segment_durations.get(path, CONTINUOUS_SEGMENT_SECONDS), 0,
                ))
        self.timeline.set_recordings(timeline_recordings)
        if self.initial_detection is not None:
            event = self.initial_detection
            self.initial_detection = None
            self.play_detection(event)
            return
        if not self.recordings:
            self.stop_player()
            self.status_overlay.display("No local recordings from the last 24 hours")
        elif self.current_path not in self.recordings:
            latest = continuous[-1] if continuous else self.recordings[-1]
            self.play_selected(self.recordings.index(latest), rewind=True)

    def recording_start(self, path: Path) -> datetime | None:
        if path in self.remote_recordings:
            return self.remote_recordings[path].start
        event = self.detected_events.get(path)
        return event.clip_start if event is not None else segment_time(path)

    def playback_offset(self) -> float:
        if self.remote_worker is not None and self.remote_worker.tunnel is not None:
            start = self.recording_start(self.current_path)
            media_start = self.remote_worker.tunnel.media_start
            if start is not None and media_start is not None:
                return (media_start - start).total_seconds()
        return self.remote_offset

    def play_detection(self, event: DetectionEvent) -> None:
        if event.clip not in self.recordings or event.clip_start is None:
            self.status_overlay.display("Detection excerpt unavailable")
            return
        self.play_selected(self.recordings.index(event.clip), max(0.0, (event.first - event.clip_start).total_seconds()))

    def jump_detection(self, direction: int) -> None:
        events = sorted(
            [(recording.start, path, 0.0) for path, recording in self.remote_recordings.items() if recording.detection]
            if self.remote_worker is not None else [
                (event.first, event.clip, max(0.0, (event.first - event.clip_start).total_seconds()))
                for event in self.detected_events.values() if event.clip in self.recordings and event.clip_start is not None
            ], key=lambda event: event[0],
        )
        if not events:
            self.status_overlay.display("No detection in these recordings")
            return
        current = self.recording_start(self.current_path) if self.current_path is not None else None
        position_ready, position = mpv_request(self.socket_path, ["get_property", "time-pos"])
        moment = current + timedelta(seconds=position + self.playback_offset()) if current is not None and position_ready and isinstance(position, (int, float)) else self.timeline.center
        candidates = (event for event in events if event[0] < moment - timedelta(seconds=1)) if direction < 0 else (
            event for event in events if event[0] > moment + timedelta(seconds=1)
        )
        selected = list(candidates)
        if selected:
            _, path, offset = selected[-1] if direction < 0 else selected[0]
            self.play_selected(self.recordings.index(path), offset)
        else:
            self.status_overlay.display("No earlier detection" if direction < 0 else "No later detection")

    def select_relative(self, step: int) -> None:
        if self.current_path not in self.recordings:
            return
        row = self.recordings.index(self.current_path) + step
        if 0 <= row < len(self.recordings):
            self.play_selected(row)

    def active_recordings(self) -> set[Path]:
        if not isinstance(self.worker, RtspStreamWorker):
            return set()
        process = self.worker.continuous
        if process is None or process.poll() is not None:
            return set()
        active = set()
        try:
            for descriptor in Path(f"/proc/{process.pid}/fd").iterdir():
                try:
                    path = Path(os.readlink(descriptor))
                except OSError:
                    continue
                if path.suffix == MATROSKA_SUFFIX:
                    active.add(path)
        except OSError:
            return set()
        return active

    def play_selected(self, row: int, offset: float = 0, rewind: bool = False) -> None:
        if not 0 <= row < len(self.recordings):
            return
        path = self.recordings[row]
        self.stop_player()
        self.current_path = path
        self.pending_seek = offset
        self.rewind_after_load = rewind
        started = self.recording_start(path)
        if started is not None:
            self.timeline.set_center(started + timedelta(seconds=offset))
        self.status_overlay.display("Loading recording...")
        if self.remote_worker is not None:
            self.controls.save_button.setEnabled(False)
            duration = self.segment_durations[path]
            if rewind:
                offset = max(0.0, duration - REPLAY_DEFAULT_REWIND_SECONDS)
            self.remote_offset = min(max(0, math.floor(offset)), max(0, duration - 1))
            self.pending_seek = None
            self.rewind_after_load = False
            self.remote_worker.open_recording(self.remote_recordings[path], self.remote_offset)
            return
        self.start_replay_player(str(path))

    def load_remote_range(self, start: datetime, end: datetime) -> None:
        if self.remote_worker is None or self.closing:
            return
        end = min(end, datetime.now())
        day = end.replace(hour=0, minute=0, second=0, microsecond=0)
        first_day = start.replace(hour=0, minute=0, second=0, microsecond=0)
        while day >= first_day and len(self.remote_days) < MAX_REPLAY_DAYS:
            if day not in self.remote_days:
                self.remote_days.add(day)
                self.remote_worker.list_range(day, min(day + timedelta(days=1) - timedelta(seconds=1), end))
            day -= timedelta(days=1)

    def on_remote_catalog(self, recordings: list[ImouRecording]) -> None:
        if self.closing:
            return
        self.remote_recordings.update({Path(recording.key): recording for recording in recordings})
        self.recordings = sorted(self.remote_recordings, key=self.recording_start)
        self.segment_durations.update({
            path: math.ceil((recording.end - recording.start).total_seconds())
            for path, recording in self.remote_recordings.items()
        })
        self.timeline.set_recordings([
            CardRecording(path.name, recording.start, self.segment_durations[path], recording.size, recording.detection)
            for path, recording in self.remote_recordings.items()
        ])
        if not self.recordings:
            self.status_overlay.display("No camera recordings in this period")
        elif self.current_path is None:
            self.play_selected(len(self.recordings) - 1, rewind=True)

    def on_remote_playback(self, key: str, url: str, offset: float = 0) -> None:
        if not self.closing and self.current_path == Path(key) and offset == self.remote_offset:
            self.start_replay_player(url)

    def start_replay_player(self, source: str) -> None:
        try:
            self.mpv_directory = tempfile.TemporaryDirectory(prefix="okam-linux-replay-")
            self.socket_path = Path(self.mpv_directory.name) / "control.sock"
            command = [
                "mpv", "--no-config", "--no-terminal", "--really-quiet", "--vo=x11", "--osc=no",
                "--input-default-bindings=no", "--input-cursor=no", "--force-window=yes", "--keep-open=yes",
                f"--input-ipc-server={self.socket_path}", f"--wid={int(self.video.winId())}",
            ]
            if isinstance(self.camera, RtspCamera):
                command.append(RTSP_DENOISE_FILTER)
            if self.remote_worker is not None:
                command += ["--mute=yes", "--cache=no", "--cache-pause=no", "--vd-lavc-threads=1",
                            "--demuxer-lavf-format=dhav",
                            "--demuxer-lavf-probesize=1048576", "--demuxer-lavf-analyzeduration=1",
                            "--demuxer-lavf-o-add=fpsprobesize=0"]
                self.controls.sound_button.setEnabled(False)
            self.player = subprocess.Popen(
                command + [source],
                stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
        except OSError:
            self.status_overlay.display("Unable to open the recording")
            self.stop_player()
            return
        self.zoom_level = 0
        self.speed_index = 0
        self.controls.speed_button.setText(REPLAY_SPEED_LABEL.format(speed=REPLAY_SPEEDS[0]))
        set_button_icon(self.controls.play_button, "pause", "Pause")
        self.controls.save_button.setEnabled(True)
        self.progress_timer.start(500)

    def seek_time(self, moment: datetime) -> None:
        for event in self.detected_events.values():
            if event.first <= moment <= event.last:
                self.play_selected(
                    self.recordings.index(event.clip),
                    max(0.0, (moment - event.clip_start).total_seconds()),
                )
                return
        for row, path in enumerate(self.recordings):
            started = self.recording_start(path)
            if started is None:
                continue
            end = started + timedelta(seconds=self.segment_durations.get(path, CONTINUOUS_SEGMENT_SECONDS))
            if moment < end:
                offset = max(0.0, (moment - started).total_seconds())
                if self.remote_worker is None and path == self.current_path and mpv_request(self.socket_path, ["seek", offset, "absolute"])[0]:
                    self.timeline.set_center(started + timedelta(seconds=offset))
                else:
                    self.play_selected(row, offset)
                return
        self.status_overlay.display("No recording at this time")

    def update_progress(self) -> None:
        if self.remote_worker is not None and self.remote_worker.tunnel is not None and self.remote_worker.tunnel.error:
            self.status_overlay.display(self.remote_worker.tunnel.error)
            return
        if self.player is None or self.player.poll() is not None:
            self.progress_timer.stop()
            self.status_overlay.display("Playback stopped")
            return
        position_ready, current = mpv_request(self.socket_path, ["get_property", "time-pos"])
        duration_ready, duration = mpv_request(self.socket_path, ["get_property", "duration"])
        if self.remote_worker is not None and self.current_path in self.segment_durations:
            duration_ready, duration = True, self.segment_durations[self.current_path]
        if not position_ready or not duration_ready or not isinstance(current, (int, float)) or not isinstance(duration, (int, float)):
            return
        current += self.playback_offset()
        if self.remote_worker is not None:
            audio_ready, audio = mpv_request(self.socket_path, ["get_property", "audio-params"])
            self.controls.sound_button.setEnabled(audio_ready and bool(audio))
        if self.rewind_after_load:
            self.pending_seek = max(0.0, duration - REPLAY_DEFAULT_REWIND_SECONDS)
            self.rewind_after_load = False
        if self.pending_seek is not None:
            offset = min(self.pending_seek, max(0.0, duration))
            self.pending_seek = None
            if offset > 0:
                mpv_request(self.socket_path, ["seek", offset, "absolute"])
                return
        path = self.current_path
        if path is not None:
            rounded_duration = max(1, round(duration))
            if self.segment_durations.get(path) != rounded_duration:
                self.segment_durations[path] = rounded_duration
                if self.remote_worker is None:
                    self.refresh_recordings()
                if self.current_path != path:
                    return
            started = self.recording_start(path)
            if started is not None:
                self.timeline.set_center(started + timedelta(seconds=current))
        self.status_overlay.hide()
        ended, eof = mpv_request(self.socket_path, ["get_property", "eof-reached"])
        if ended and eof is True:
            self.select_relative(1)
            if path == self.current_path:
                set_button_icon(self.controls.play_button, "play", "Play")

    def toggle_playing(self) -> None:
        ready, paused = mpv_request(self.socket_path, ["get_property", "pause"])
        if ready and mpv_request(self.socket_path, ["set_property", "pause", not paused])[0]:
            if self.remote_worker is not None and self.remote_worker.tunnel is not None:
                self.remote_worker.tunnel.set_paused(not paused)
            set_button_icon(self.controls.play_button, "play" if not paused else "pause", "Play" if not paused else "Pause")

    def toggle_sound(self) -> None:
        ready, muted = mpv_request(self.socket_path, ["get_property", "mute"])
        if ready and mpv_request(self.socket_path, ["set_property", "mute", not muted])[0]:
            set_button_icon(self.controls.sound_button, "sound" if not muted else "sound_on",
                            "Listen to playback" if not muted else "Mute playback")

    def change_zoom(self, step: int) -> None:
        level = min(MAX_ZOOM_LEVEL, max(0, self.zoom_level + step))
        if mpv_request(self.socket_path, ["set_property", "video-zoom", level / 2])[0]:
            self.zoom_level = level

    def change_speed(self) -> None:
        index = (self.speed_index + 1) % len(REPLAY_SPEEDS)
        speed = REPLAY_SPEEDS[index]
        if mpv_request(self.socket_path, ["set_property", "speed", speed])[0]:
            if self.remote_worker is not None and self.remote_worker.tunnel is not None:
                self.remote_worker.tunnel.set_speed(speed)
                mpv_request(self.socket_path, ["set_property", "aid", "auto" if speed == 1 else "no"])
            self.speed_index = index
            self.controls.speed_button.setText(REPLAY_SPEED_LABEL.format(speed=speed))

    def take_snapshot(self) -> None:
        try:
            path = media_directory() / f"{safe_camera_name(self.camera.name)}_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        except OSError:
            self.status_overlay.display("Unable to create the picture folder")
            return
        if not mpv_request(self.socket_path, ["screenshot-to-file", str(path), "video"])[0]:
            self.status_overlay.display("Unable to save a picture")

    def save_clip(self) -> None:
        if self.current_path is None:
            return
        try:
            if self.remote_worker is not None:
                recording = self.remote_recordings[self.current_path]
                target = media_directory() / f"{safe_camera_name(self.camera.name)}_{recording.start:%Y%m%d_%H%M%S}.mkv"
                self.remote_worker.save_recording(recording, target)
                self.status_overlay.display("Saving camera recording...")
                return
            target = media_directory() / self.current_path.name
            shutil.copyfile(self.current_path, target)
        except OSError:
            self.status_overlay.display("Unable to save the recording")
            return
        self.status_overlay.display(f"Recording saved: {target.name}")

    def stop_player(self) -> None:
        self.progress_timer.stop()
        stop_mpv_player(self.player)
        self.player = None
        if self.mpv_directory is not None:
            self.mpv_directory.cleanup()
            self.mpv_directory = None
            self.socket_path = None

    def stop(self) -> None:
        self.closing = True
        self.overlay_timer.stop()
        self.refresh_timer.stop()
        self.overlay.hide()
        self.status_overlay.timer.stop()
        self.status_overlay.hide()
        self.stop_player()
        if self.remote_worker is not None:
            self.remote_worker.stop()
        else:
            self.stopped.emit()

    def on_remote_finished(self) -> None:
        worker = self.remote_worker
        self.remote_worker = None
        if worker is not None:
            worker.deleteLater()
        if self.closing:
            self.stopped.emit()


class AccountDialog(QDialog):
    credentials_submitted = pyqtSignal(str, str)

    def __init__(self, parent: QWidget, username: str) -> None:
        super().__init__(parent)
        self.setWindowTitle("Add O-KAM account")
        self.setMinimumWidth(360)
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.username = QLineEdit(username)
        self.username.setPlaceholderText("O-KAM account email")
        self.password = QLineEdit()
        self.password.setEchoMode(QLineEdit.EchoMode.Password)
        form.addRow("Account", self.username)
        form.addRow("Password", self.password)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(FORM_ERROR_STYLE)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def submit(self) -> None:
        if not self.username.text().strip() or not self.password.text():
            self.show_error(ACCOUNT_INPUT_MESSAGE)
            (self.username if not self.username.text().strip() else self.password).setFocus()
            return
        self.error.clear()
        self.set_loading(True)
        self.credentials_submitted.emit(self.username.text().strip(), self.password.text())

    def set_loading(self, loading: bool) -> None:
        self.username.setEnabled(not loading)
        self.password.setEnabled(not loading)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not loading)

    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.error.setText(message)
        self.raise_()
        self.activateWindow()
        self.password.setFocus()
        self.password.selectAll()


class ImouAccountDialog(QDialog):
    credentials_submitted = pyqtSignal(object, str)

    def __init__(self, parent: QWidget, account: ImouAccount | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("Imou Life account")
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        description = QLabel(
            "Use the AppId and AppSecret of your own Imou Life account.\n"
            "Cloud video uses your Imou traffic allowance."
        )
        description.setWordWrap(True)
        layout.addWidget(description)
        portal = QPushButton("Open Imou account activation")
        portal.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(IMOU_ACCOUNT_URL)))
        layout.addWidget(portal)
        form = QFormLayout()
        self.email = QLineEdit(account.email if account else "")
        self.email.setPlaceholderText("Imou Life account email")
        self.app_id = QLineEdit(account.app_id if account else "")
        self.app_id.setPlaceholderText("AppId")
        self.secret = QLineEdit()
        self.secret.setEchoMode(QLineEdit.EchoMode.Password)
        self.secret.setPlaceholderText("AppSecret" if account is None else "Leave empty to keep the saved key")
        self.region = QComboBox()
        self.region.addItems(IMOU_REGIONS)
        self.region.setCurrentText(account.region if account else "Europe")
        form.addRow("Account email", self.email)
        form.addRow("AppId", self.app_id)
        form.addRow("AppSecret", self.secret)
        form.addRow("Server region", self.region)
        layout.addLayout(form)
        self.original_account = account
        self.cloud_recording = QCheckBox("Allow continuous cloud recording (uses Imou traffic)")
        self.cloud_recording.setChecked(account.cloud_recording if account else False)
        layout.addWidget(self.cloud_recording)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet(FORM_ERROR_STYLE)
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.buttons.accepted.connect(self.submit)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def submit(self) -> None:
        try:
            account = ImouAccount(self.email.text().strip(), self.app_id.text().strip(),
                                  self.region.currentText(), self.cloud_recording.isChecked())
        except ValueError as ex:
            self.show_error(str(ex))
            (self.email if not self.email.text().strip() else self.app_id).setFocus()
            return
        if not self.secret.text() and (self.original_account is None or account.app_id != self.original_account.app_id):
            self.show_error("Enter the AppSecret of your own Imou application.")
            self.secret.setFocus()
            return
        self.error.clear()
        self.set_loading(True)
        self.credentials_submitted.emit(account, self.secret.text())

    def set_loading(self, loading: bool) -> None:
        for field in (self.email, self.app_id, self.secret, self.region, self.cloud_recording):
            field.setEnabled(not loading)
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(not loading)

    def show_error(self, message: str) -> None:
        self.set_loading(False)
        self.error.setText(message)
        self.secret.setFocus()


class DialogPlacement(QObject):
    def __init__(self, parent: QWidget, dialog: QDialog, reposition: Callable[[], None]) -> None:
        super().__init__(dialog)
        self.dialog = dialog
        self.reposition = reposition
        self.exposed = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.update_position)
        dialog.winId()
        self.watched = (parent, dialog, dialog.windowHandle())
        for watched in self.watched:
            watched.installEventFilter(self)
        self.timer.start(0)

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        if event.type() in (QEvent.Type.Show, QEvent.Type.Resize) or (
            watched is self.watched[0] and event.type() == QEvent.Type.Move
        ):
            self.timer.start(0)
        elif event.type() == QEvent.Type.Expose and not self.exposed:
            self.exposed = True
            self.timer.start(0)
        return super().eventFilter(watched, event)

    def update_position(self) -> None:
        if self.dialog.isVisible():
            self.reposition()

    def stop(self) -> None:
        self.timer.stop()
        for watched in self.watched:
            watched.removeEventFilter(self)
        self.deleteLater()


class MainWindow(QMainWindow):
    local_detection_ready = pyqtSignal(object)
    local_detection_failed = pyqtSignal(str)

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
        self.window_hints: X11WindowHints | None = None
        self.normal_geometry: QRect | None = None
        self.devices: list[AccountDevice | RtspCamera] = []
        self.device_accounts: dict[str, str] = {}
        self.previews: dict[str, CameraPreview] = {}
        self.local_replays: dict[str, LocalReplayPane] = {}
        self.retired_replays: list[LocalReplayPane] = []
        self.retired_previews: list[CameraPreview] = []
        self.preview_layout: str | None = None
        self.layout_refresh_timer = QTimer(self)
        self.layout_refresh_timer.setSingleShot(True)
        self.layout_refresh_timer.timeout.connect(self.sync_previews)
        self.camera_mask_timer = QTimer(self)
        self.camera_mask_timer.setSingleShot(True)
        self.camera_mask_timer.timeout.connect(self.update_camera_mask)
        self.pending_camera: tuple[str, str] | None = None
        self.pending_camera_replay: str | None = None
        self.pending_selection_start: tuple[str, bool] | None = None
        self.account_queue: list[tuple[str, str]] = []
        self.account_worker: AccountWorker | None = None
        self.account_dialog: AccountDialog | None = None
        self.imou_worker: ImouAccountWorker | None = None
        self.imou_dialog: ImouAccountDialog | None = None
        self.imou_queue: list[ImouAccount] = []
        self.stream_worker: StreamWorker | RtspStreamWorker | None = None
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
        self.rtsp_ptz_available = False
        self.retry_pending = False
        self.detection_worker: DetectionWorker | None = None
        self.replay: ReplayController | None = None
        self.pending_replay: tuple[datetime | None] | None = None
        self.keep_player = False
        self.latest_detection: datetime | None = None
        self.latest_local_detection: DetectionEvent | None = None
        self.pending_local_detection: DetectionEvent | None = None
        self.local_detection_ready.connect(self.on_local_detection_ready)
        self.local_detection_failed.connect(self.on_local_detection_failed)
        self.local_detection_prune_timer = QTimer(self)
        self.local_detection_prune_timer.timeout.connect(prune_events)
        self.local_detection_prune_timer.start(60 * 60 * 1000)
        QTimer.singleShot(0, prune_events)
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
        self.settings = QSettings(STORAGE_NAME, STORAGE_NAME)
        self.rtsp_cameras = load_rtsp_cameras(self.settings)
        self.imou_accounts = load_imou_accounts(self.settings)
        self.imou_cameras = load_imou_cameras(self.settings, self.imou_accounts)
        self.devices.extend(self.rtsp_cameras + self.imou_cameras)
        self.device_accounts.update({camera.uid: RTSP_ACCOUNT for camera in self.devices})
        self.account_username = self.settings.value("account/username", "", str)
        self.account_secret: str | None = None
        self.accounts = self.settings.value("accounts/okam", [], list)
        if self.account_username and self.account_username not in self.accounts:
            self.accounts.insert(0, self.account_username)
        body = QWidget()
        body.setAttribute(Qt.WidgetAttribute.WA_StyledBackground)
        body.setStyleSheet("background-color: #171717;")
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 0, 0)
        self.account_error = QLabel()
        self.account_error.setWordWrap(True)
        self.account_error.setStyleSheet(ACCOUNT_ERROR_STYLE)
        self.account_error.hide()
        layout.addWidget(self.account_error)
        self.video = VideoWidget()
        self.video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        self.video.setStyleSheet("background-color: #171717;")
        self.video.clicked.connect(self.toggle_overlay)
        self.video.dragged.connect(self.move_by_drag)
        self.video.drag_moved.connect(self.pan_zoomed_video)
        self.video.wheel_zoomed.connect(self.change_zoom)
        self.video.double_clicked.connect(lambda x, y: self.toggle_fullscreen())
        self.video.camera_drop_requested.connect(self.drop_camera)
        self.video_grid = QGridLayout()
        self.video_grid.setContentsMargins(0, 0, 0, 0)
        self.video_grid.setSpacing(0)
        self.primary_pane = QWidget()
        primary_layout = QVBoxLayout(self.primary_pane)
        primary_layout.setContentsMargins(0, 0, 0, 0)
        primary_layout.setSpacing(0)
        self.primary_frame = AspectVideoFrame(self.video)
        self.primary_video_stack = QStackedWidget()
        self.primary_video_stack.addWidget(self.primary_frame)
        primary_layout.addWidget(self.primary_video_stack, 1)
        self.video_grid.addWidget(self.primary_pane, 0, 0)
        self.empty_camera_label = QLabel(NO_VISIBLE_CAMERAS_MESSAGE, body)
        self.empty_camera_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty_camera_label.hide()
        layout.addLayout(self.video_grid, 1)
        self.overlay = ControlsOverlay(self.video)
        self.overlay.setObjectName("cameraControls")
        self.overlay.setStyleSheet(CAMERA_CONTROLS_STYLE)
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
        self.replay_button.clicked.connect(self.open_camera_replay)
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
        self.rtsp_light_mode: str | None = None
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
        ):
            set_button_icon(button, icon_name, label)
        self.live_bar = QWidget(self.overlay)
        self.live_bar.setObjectName("liveBar")
        self.live_bar.setLayout(controls)
        overlay_layout.addWidget(self.live_bar)
        self.replay_controls = ReplayControls(self.overlay)
        self.replay_bar = self.replay_controls
        self.live_button = self.replay_controls.live_button
        self.replay_play_button = self.replay_controls.play_button
        self.replay_sound_button = self.replay_controls.sound_button
        self.replay_speed_button = self.replay_controls.speed_button
        self.previous_detection_button = self.replay_controls.previous_button
        self.next_detection_button = self.replay_controls.next_button
        self.replay_snapshot_button = self.replay_controls.snapshot_button
        self.replay_save_button = self.replay_controls.save_button
        self.timeline_zoom_out_button = self.replay_controls.zoom_out_button
        self.timeline_zoom_in_button = self.replay_controls.zoom_in_button
        self.fullscreen_replay_button = self.replay_controls.fullscreen_button
        self.timeline = self.replay_controls.timeline
        set_button_icon(self.previous_detection_button, "previous_detection", "Previous detection")
        set_button_icon(self.next_detection_button, "next_detection", "Next detection")
        self.replay_save_button.setEnabled(False)
        self.live_button.clicked.connect(lambda: self.exit_replay())
        self.replay_play_button.clicked.connect(lambda: self.replay is not None and self.replay.toggle_playing())
        self.replay_sound_button.clicked.connect(self.toggle_replay_sound)
        self.previous_detection_button.clicked.connect(lambda: self.replay is not None and self.replay.jump_to_detection(-1))
        self.next_detection_button.clicked.connect(lambda: self.replay is not None and self.replay.jump_to_detection(1))
        self.replay_speed_button.clicked.connect(self.change_replay_speed)
        self.replay_snapshot_button.clicked.connect(self.take_snapshot)
        self.replay_save_button.clicked.connect(self.save_replay_clip)
        self.fullscreen_replay_button.clicked.connect(self.toggle_fullscreen)
        overlay_layout.addWidget(self.replay_bar)
        overlay_layout.addWidget(self.timeline)
        self.replay_bar.hide()
        self.timeline.hide()
        self.ptz_panel = MovementControls(self.overlay)
        self.ptz_panel.command_requested.connect(lambda command: self.control_camera((command,)))
        self.camera_buttons = self.ptz_panel.buttons
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
        self.replay_status_overlay: VideoStatusOverlay | None = None
        for button in self.overlay.findChildren(QPushButton):
            button.clicked.connect(self.show_overlay)
        self.setCentralWidget(body)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.create_tray()
        if QApplication.platformName() == "xcb":
            self.window_hints = X11WindowHints(self, self.tray is not None)
        self.set_status("Connecting to camera...")
        if self.accounts or self.rtsp_cameras or self.imou_accounts:
            QTimer.singleShot(0, self.find_cameras)
        else:
            QTimer.singleShot(0, self.setup_cameras)

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
        self.setWindowTitle(APPLICATION_NAME if text == "Live video" else f"{APPLICATION_NAME} \u00b7 {text}")
        if self.tray is not None:
            self.tray.setToolTip(f"{APPLICATION_NAME}\n{text}")

    def create_tray(self) -> None:
        QApplication.setQuitOnLastWindowClosed(False)
        menu = QMenu(self)
        self.window_action = menu.addAction("Hide window")
        self.window_action.triggered.connect(self.on_window_action_triggered)
        self.cameras_menu = menu.addMenu("Cameras")
        self.cameras_menu.aboutToShow.connect(self.update_cameras_menu)
        self.camera_layout_menu = menu.addMenu("Camera layout")
        layout_group = QActionGroup(self)
        for value, label in CAMERA_LAYOUTS:
            action = self.camera_layout_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(value == self.camera_layout())
            action.triggered.connect(lambda checked=False, orientation=value: self.set_camera_layout(orientation))
            layout_group.addAction(action)
        add_camera_menu = menu.addMenu("Add camera")
        for index, (label, callback) in enumerate(zip(CAMERA_SOURCE_LABELS, self.camera_source_actions())):
            action = add_camera_menu.addAction(f"{label}...")
            action.triggered.connect(callback)
            if index == 0:
                self.add_account_action = action
        menu.addSeparator()
        self.continuous_action = menu.addAction("Continuous recording (24 h)")
        self.continuous_action.setCheckable(True)
        self.continuous_action.setChecked(self.continuous_recording_enabled())
        self.continuous_action.toggled.connect(self.set_continuous_recording)
        self.local_detection_action = menu.addAction("Detect people and animals locally")
        self.local_detection_action.setCheckable(True)
        self.local_detection_action.setChecked(self.local_detection_enabled())
        self.local_detection_action.toggled.connect(self.set_local_detection)
        open_recordings = menu.addAction("Open recordings folder")
        open_recordings.triggered.connect(self.open_recordings_folder)
        menu.addSeparator()
        quit_action = menu.addAction("Quit")
        quit_action.triggered.connect(self.quit_application)
        menu.aboutToShow.connect(self.update_tray_menu)
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip(f"{APPLICATION_NAME}\n{self.status_text}")
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(self.on_tray_activated)
        self.tray.messageClicked.connect(self.open_detection_notification)
        self.tray.show()

    def update_tray_menu(self) -> None:
        self.window_action.setText("Hide window" if self.isVisible() else "Show window")
        self.camera_layout_menu.setEnabled(len(self.visible_devices()) > 1)
        self.add_account_action.setEnabled(self.account_worker is None)

    def update_cameras_menu(self) -> None:
        self.cameras_menu.clear()
        for device in self.ordered_devices():
            username = self.device_accounts[device.uid]
            if isinstance(device, RtspCamera):
                source = "Imou Life (local)" if device.provider == IMOU_PROVIDER else "RTSP (local)"
                if device.provider == IMOU_ACCOUNT_PROVIDER:
                    source = f"Imou Life ({device.username})"
                if device.provider == "rtsp":
                    try:
                        if load_icam365_config(device.uid) is not None:
                            source = "iCam365"
                    except OSError:
                        source = "iCam365 (configuration error)"
                label = f"{device.name} · {source}"
            else:
                label = f"{device.name} · O-KAM ({username})"
            action = self.cameras_menu.addAction(label)
            action.setCheckable(True)
            action.setChecked(self.camera_visible(device.uid))
            action.triggered.connect(
                lambda checked, uid=device.uid: self.set_camera_visible(uid, checked)
            )
        if not self.devices:
            self.cameras_menu.addAction("No cameras available").setEnabled(False)
        self.cameras_menu.addSeparator()
        refresh = self.cameras_menu.addAction("Refresh camera list")
        refresh.setEnabled(self.account_worker is None and self.imou_worker is None
                           and bool(self.accounts or self.imou_accounts))
        refresh.triggered.connect(self.refresh_cameras)
        for account in self.imou_accounts:
            account_menu = self.cameras_menu.addMenu(f"Imou Life account · {account.email}")
            account_menu.setEnabled(self.imou_worker is None)
            account_menu.addAction("Edit account...").triggered.connect(
                lambda checked=False, value=account: self.add_imou_account(value)
            )
            account_menu.addAction("Remove account").triggered.connect(
                lambda checked=False, value=account: self.remove_imou_account(value)
            )
        if (isinstance(getattr(self, "selected_device", None), RtspCamera)
                and self.selected_device.provider != IMOU_ACCOUNT_PROVIDER):
            source = "Imou Life" if self.selected_device.provider == IMOU_PROVIDER else "RTSP"
            self.cameras_menu.addAction(f"Remove selected {source} camera").triggered.connect(self.remove_selected_rtsp_camera)

    def detection_days(self) -> list[str]:
        now = datetime.now()
        days = [now.strftime(RECORD_DAY_FORMAT)]
        if now - timedelta(milliseconds=DETECTION_POLL_MS * 2) < now.replace(hour=0, minute=0, second=0, microsecond=0):
            days.insert(0, (now - timedelta(days=1)).strftime(RECORD_DAY_FORMAT))
        return days

    def check_detections(self) -> None:
        if not self.devices or self.detection_worker is not None or self.close_pending or self.replay is not None:
            return
        selected = getattr(self, "selected_device", None)
        if selected is None or not self.camera_visible(selected.uid):
            return
        if isinstance(getattr(self, "selected_device", None), RtspCamera):
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
        elif self.pending_camera is not None and self.stream_worker is None:
            self.apply_pending_camera()

    def on_detections_listed(self, names: list[str]) -> None:
        if isinstance(getattr(self, "selected_device", None), RtspCamera):
            return
        setting = f"{DETECTION_SETTING}/{self.selected_device.uid}"
        last_seen = self.settings.value(setting, "", str)
        if not last_seen and not self.settings.contains(setting) and len(self.devices) == 1:
            last_seen = self.settings.value(DETECTION_SETTING, "", str)
        new_names = sorted(name for name in names if name > last_seen)
        if not new_names:
            return
        self.settings.setValue(setting, new_names[-1])
        self.settings.sync()
        if not last_seen:
            return
        latest = recording_time(new_names[-1])
        self.latest_detection = latest
        self.latest_local_detection = None
        message = f"{self.selected_device.name} \u00b7 {latest:%d/%m %H:%M:%S}"
        if len(new_names) > 1:
            message += f" ({len(new_names)} new detections)"
        if self.tray is not None:
            self.tray.showMessage(
                "Camera detection", message, self.windowIcon(), DETECTION_MESSAGE_MS
            )
        self.show_notice(f"Detection at {latest:%d/%m %H:%M:%S}.")

    def on_detections_failed(self, message: str) -> None:
        print(message, file=sys.stderr, flush=True)

    def on_local_detection_ready(self, event: DetectionEvent) -> None:
        if self.close_pending:
            return
        replay = self.local_replays.get(event.uid)
        if replay is not None:
            replay.refresh_recordings()
        if not self.local_detection_enabled():
            return
        self.latest_local_detection = event
        description = ", ".join(event.classes)
        message = f"{event.camera} · {description} · {event.first:%d/%m %H:%M:%S}"
        if self.tray is not None:
            self.tray.showMessage("Local camera detection", message, self.windowIcon(), DETECTION_MESSAGE_MS)
        self.show_notice(message)

    def on_local_detection_failed(self, message: str) -> None:
        if not self.close_pending:
            self.show_notice(message)

    def open_detection_notification(self) -> None:
        event = self.latest_local_detection
        if event is None:
            self.enter_replay(self.latest_detection)
            return
        camera = next((camera for camera in self.devices if camera.uid == event.uid), None)
        if camera is None:
            return
        self.show_window()
        if camera.uid == getattr(getattr(self, "selected_device", None), "uid", None) or camera.uid in self.previews:
            self.open_local_replay(camera, event)
        else:
            self.pending_local_detection = event
            self.select_camera(self.device_accounts[camera.uid], camera.uid)

    def on_tray_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_window()

    def on_window_action_triggered(self) -> None:
        if self.isVisible():
            self.hide_to_tray()
            return
        self.show_window()

    def toggle_window(self) -> None:
        if self.isVisible() and not self.isMinimized() and self.isActiveWindow():
            self.hide_to_tray()
            return
        self.show_window()

    def show_window(self) -> None:
        self.showNormal()
        self.raise_()
        self.activateWindow()
        if self.stream_worker is not None:
            if isinstance(self.stream_worker, StreamWorker):
                self.stream_worker.set_display(True)
        for preview in self.previews.values():
            preview.set_display(True)
        if self.stream_worker is None and self.account_worker is None:
            self.reconnect()

    def hide_to_tray(self) -> None:
        self.exit_replay()
        if self.stream_worker is not None:
            if isinstance(self.stream_worker, StreamWorker):
                self.stream_worker.set_display(False)
        for preview in self.previews.values():
            preview.set_display(False)
        self.overlay_timer.stop()
        self.hide_overlay()
        self.hide()
        self.update_recording_badge()

    def continuous_recording_enabled(self) -> bool:
        return self.settings.value(CONTINUOUS_SETTING, True, bool)

    def local_detection_enabled(self) -> bool:
        return self.settings.value(LOCAL_DETECTION_SETTING, True, bool)

    def set_local_detection(self, enabled: bool) -> None:
        self.settings.setValue(LOCAL_DETECTION_SETTING, enabled)
        self.settings.sync()
        if self.stream_worker is not None:
            self.stream_worker.local_detection_enabled = enabled
        for preview in self.previews.values():
            preview.set_local_detection(enabled)
        self.show_notice("Local detection on." if enabled else "Local detection off.")

    def set_continuous_recording(self, enabled: bool) -> None:
        self.settings.setValue(CONTINUOUS_SETTING, enabled)
        self.settings.sync()
        if self.stream_worker is not None:
            self.stream_worker.set_continuous(enabled)
        for preview in self.previews.values():
            preview.set_continuous(enabled)
        self.show_notice("Continuous recording on." if enabled else "Continuous recording off.")

    def open_camera_replay(self) -> None:
        camera = getattr(self, "selected_device", None)
        if isinstance(camera, RtspCamera) and camera.provider == IMOU_ACCOUNT_PROVIDER:
            self.open_camera_sd_replay(camera)
        elif isinstance(camera, RtspCamera):
            self.open_local_replay(camera)
        elif camera is not None:
            self.enter_replay(None)

    def open_local_replay(self, camera: AccountDevice | RtspCamera, event: DetectionEvent | None = None,
                          camera_recordings: bool = False) -> None:
        existing = self.local_replays.get(camera.uid)
        if existing is not None and existing.camera_recordings != camera_recordings:
            self.close_local_replay(camera.uid)
        if camera.uid in self.local_replays:
            if event is not None:
                self.local_replays[camera.uid].play_detection(event)
            self.local_replays[camera.uid].show_overlay()
            return
        selected = camera.uid == getattr(getattr(self, "selected_device", None), "uid", None)
        preview = self.previews.get(camera.uid)
        if not selected and preview is None:
            return
        stack = self.primary_video_stack if selected else preview.video_stack
        worker = self.stream_worker if selected else preview.worker
        try:
            replay = LocalReplayPane(camera, self.primary_pane if selected else preview, worker, event, camera_recordings)
        except OSError:
            self.show_notice("Unable to open local recordings.")
            return
        replay.live_requested.connect(lambda uid=camera.uid: self.close_local_replay(uid))
        replay.fullscreen_requested.connect(self.toggle_fullscreen)
        replay.video.reorder_enabled = len(self.visible_devices()) > 1
        replay.video.camera_drop_requested.connect(self.drop_camera)
        self.local_replays[camera.uid] = replay
        if selected:
            self.hide_overlay()
        else:
            preview.hide_overlay()
        stack.addWidget(replay)
        stack.setCurrentWidget(replay)
        if isinstance(worker, StreamWorker):
            worker.set_display(False)
        replay.show()

    def close_local_replay(self, uid: str) -> None:
        replay = self.local_replays.pop(uid, None)
        if replay is None:
            return
        stack = self.primary_video_stack if uid == getattr(getattr(self, "selected_device", None), "uid", None) else (
            self.previews[uid].video_stack if uid in self.previews else None
        )
        replay.setParent(self)
        replay.hide()
        self.retired_replays.append(replay)
        replay.stopped.connect(lambda current=replay: self._local_replay_stopped(current))
        replay.stop()
        if stack is not None:
            stack.setCurrentIndex(0)
            stack.removeWidget(replay)
        if isinstance(replay.worker, StreamWorker):
            replay.worker.set_display(True)

    def _local_replay_stopped(self, replay: LocalReplayPane) -> None:
        self.retired_replays.remove(replay)
        replay.deleteLater()
        if self.close_pending:
            QTimer.singleShot(0, self.close)

    def open_camera_sd_replay(self, camera: AccountDevice | RtspCamera) -> None:
        if isinstance(camera, RtspCamera) and camera.provider == IMOU_ACCOUNT_PROVIDER:
            self.open_local_replay(camera, camera_recordings=True)
            return
        username = self.device_accounts.get(camera.uid)
        if username is None:
            return
        if camera.uid == getattr(getattr(self, "selected_device", None), "uid", None):
            self.enter_replay(None)
            return
        self.pending_camera_replay = camera.uid
        self.select_camera(username, camera.uid)

    def open_recordings_folder(self) -> None:
        try:
            directory = continuous_directory()
        except OSError:
            self.show_notice("Unable to open the recordings folder.")
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(directory)))

    def quit_application(self) -> None:
        self.quit_requested = True
        self.exit_replay(False)
        self.close()

    def save_rtsp_cameras(self) -> None:
        records = [
            {"uid": camera.uid, "name": camera.name, "url": camera.url, "transport": camera.transport,
             "provider": camera.provider, "username": camera.username}
            for camera in self.rtsp_cameras
        ]
        self.settings.setValue(RTSP_CAMERAS_SETTING, json.dumps(records))
        self.settings.sync()

    def camera_visible(self, uid: str) -> bool:
        selected = getattr(self, "selected_device", None)
        saved_uid = self.settings.value("camera/selected_uid", getattr(selected, "uid", ""), str)
        default = self.settings.value(MULTIVIEW_SETTING, True, bool) or uid == saved_uid
        return self.settings.value(f"{CAMERA_VISIBLE_SETTING}/{uid}", default, bool)

    def visible_devices(self) -> list[AccountDevice | RtspCamera]:
        return [camera for camera in self.ordered_devices() if self.camera_visible(camera.uid)]

    def save_camera_visibility(self, uid: str, enabled: bool) -> None:
        for camera in self.devices:
            setting = f"{CAMERA_VISIBLE_SETTING}/{camera.uid}"
            if not self.settings.contains(setting):
                self.settings.setValue(setting, self.camera_visible(camera.uid))
        self.settings.setValue(f"{CAMERA_VISIBLE_SETTING}/{uid}", enabled)
        self.settings.remove(MULTIVIEW_SETTING)
        self.settings.sync()

    def set_camera_visible(self, uid: str, enabled: bool) -> None:
        if uid not in self.device_accounts:
            return
        selected = getattr(self, "selected_device", None)
        selected_was_visible = selected is not None and self.camera_visible(selected.uid)
        self.save_camera_visibility(uid, enabled)
        if not enabled and self.pending_camera is not None and self.pending_camera[1] == uid:
            self.pending_camera = None
        if not selected_was_visible or not self.camera_visible(selected.uid):
            remaining = self.visible_devices()
            if remaining:
                camera = remaining[0]
                self.select_camera(self.device_accounts[camera.uid], camera.uid)
            else:
                self.pending_camera = None
                self.pending_camera_replay = None
                self.pending_selection_start = None
                self.pending_replay = None
                self.exit_replay(False)
                if selected is not None:
                    self.close_local_replay(selected.uid)
                self.stop_stream()
                self.disable_live_controls()
                self.set_status(NO_VISIBLE_CAMERAS_MESSAGE)
        self.sync_previews()

    def camera_layout(self) -> str:
        value = self.settings.value(MULTIVIEW_LAYOUT_SETTING, "horizontal", str)
        return value if value in CAMERA_LAYOUT_VALUES else "horizontal"

    def effective_camera_layout(self) -> str:
        preferred = self.camera_layout()
        if preferred == "grid" or not self.isVisible() or len(self.visible_devices()) < 2:
            return preferred
        screen = self.screen() or QApplication.primaryScreen()
        available = screen.availableGeometry()
        if self.width() >= available.width() * 0.85 and self.width() > self.height() * 1.5:
            return "horizontal"
        if self.height() >= available.height() * 0.85 and self.height() > self.width() * 0.8:
            return "vertical"
        return preferred

    def set_camera_layout(self, value: str) -> None:
        if value not in CAMERA_LAYOUT_VALUES:
            return
        self.settings.setValue(MULTIVIEW_LAYOUT_SETTING, value)
        self.settings.sync()
        self.sync_previews()

    def camera_grid_dimensions(self) -> tuple[int, int]:
        count = len(self.visible_devices())
        layout = self.effective_camera_layout()
        if not count:
            return 1, 1
        if layout == "grid":
            return CAMERA_GRID_COLUMNS, max(CAMERA_GRID_MIN_ROWS, math.ceil(count / CAMERA_GRID_COLUMNS))
        columns = 1 if layout == "vertical" or count == 1 else CAMERA_GRID_COLUMNS
        return columns, math.ceil(count / columns)

    def update_camera_mask(self) -> None:
        self.video_grid.activate()
        self.place_video_overlays()
        count = len(self.visible_devices())
        if self.isFullScreen() or self.effective_camera_layout() != "grid" or not count:
            self.clearMask()
            return
        columns, rows = self.camera_grid_dimensions()
        if count == columns * rows:
            self.clearMask()
            return
        offset = self.centralWidget().mapTo(self, QPoint())
        region = QRegion(self.rect()).subtracted(QRegion(self.video_grid.geometry().translated(offset)))
        for index in range(count):
            row, column = divmod(index, columns)
            cell = self.video_grid.cellRect(row, column)
            region = region.united(QRegion(cell.translated(offset)))
            if column:
                region = region.united(QRegion(cell.united(self.video_grid.cellRect(row, column - 1)).translated(offset)))
            if row:
                region = region.united(QRegion(cell.united(self.video_grid.cellRect(row - 1, column)).translated(offset)))
        if region != self.mask():
            self.setMask(region)

    def ordered_devices(self) -> list[AccountDevice | RtspCamera]:
        try:
            saved = json.loads(self.settings.value(CAMERA_ORDER_SETTING, "[]", str))
        except (ValueError, TypeError):
            saved = []
        order = {uid: index for index, uid in enumerate(saved) if isinstance(uid, str)} if isinstance(saved, list) else {}
        return sorted(self.devices, key=lambda camera: order.get(camera.uid, len(order)))

    def swap_cameras(self, source_uid: str, target_uid: str) -> None:
        order = [camera.uid for camera in self.ordered_devices()]
        if source_uid not in order or target_uid not in order or source_uid == target_uid:
            return
        source_index = order.index(source_uid)
        target_index = order.index(target_uid)
        order[source_index], order[target_index] = order[target_index], order[source_index]
        self.settings.setValue(CAMERA_ORDER_SETTING, json.dumps(order))
        self.settings.sync()
        self.sync_previews()

    def drop_camera(self, source_uid: str, position: QPoint) -> None:
        visible = self.visible_devices()
        if not self.isVisible() or len(visible) < 2 or source_uid not in {camera.uid for camera in visible}:
            return
        selected_uid = getattr(getattr(self, "selected_device", None), "uid", None)
        for camera in visible:
            pane = self.primary_pane if camera.uid == selected_uid else self.previews.get(camera.uid)
            if pane is not None and pane.isVisible() and QRect(pane.mapToGlobal(QPoint()), pane.size()).contains(position):
                self.swap_cameras(source_uid, camera.uid)
                return

    def _retire_preview(self, preview: CameraPreview) -> None:
        self.close_local_replay(preview.camera.uid)
        self.video_grid.removeWidget(preview)
        preview.hide()
        self.retired_previews.append(preview)
        preview.stopped.connect(lambda current=preview: self._preview_stopped(current))
        preview.stop()

    def _preview_stopped(self, preview: CameraPreview) -> None:
        self.retired_previews.remove(preview)
        preview.deleteLater()
        if self.pending_selection_start is not None and self.pending_selection_start[0] == preview.camera.uid:
            QTimer.singleShot(0, self._finish_selected_camera)
        elif not self.close_pending and self.camera_visible(preview.camera.uid):
            QTimer.singleShot(0, self.sync_previews)
        if self.close_pending and self.stream_worker is None and self.account_worker is None:
            QTimer.singleShot(0, self.close)

    def _finish_selected_camera(self) -> None:
        pending = self.pending_selection_start
        self.pending_selection_start = None
        if (pending is None or self.close_pending
                or getattr(getattr(self, "selected_device", None), "uid", None) != pending[0]
                or not self.camera_visible(pending[0])):
            return
        if pending[1]:
            self.enter_replay(None)
        else:
            self.watch_live()

    def sync_previews(self) -> None:
        if self.quit_requested or self.close_pending:
            return
        selected = getattr(self, "selected_device", None)
        visible = self.visible_devices()
        self.video.camera_uid = selected.uid if selected is not None else ""
        self.video.reorder_enabled = len(visible) > 1
        for replay in self.local_replays.values():
            replay.video.reorder_enabled = self.video.reorder_enabled
        cameras = [camera for camera in visible if selected is None or camera.uid != selected.uid]
        layout = self.effective_camera_layout()
        changed = len(cameras) != len(self.previews) or layout != self.preview_layout
        desired = {camera.uid for camera in cameras}
        for uid, preview in list(self.previews.items()):
            if uid not in desired:
                del self.previews[uid]
                self._retire_preview(preview)
        self.video_grid.removeWidget(self.primary_pane)
        self.primary_pane.setVisible(selected is not None and self.camera_visible(selected.uid))
        self.video_grid.removeWidget(self.empty_camera_label)
        self.empty_camera_label.setVisible(not visible)
        if not visible:
            self.video_grid.addWidget(self.empty_camera_label, 0, 0)
        columns, rows = self.camera_grid_dimensions()
        for column in range(max(columns, self.video_grid.columnCount())):
            self.video_grid.setColumnStretch(column, 1 if column < columns else 0)
        for row in range(max(rows, self.video_grid.rowCount())):
            self.video_grid.setRowStretch(row, 1 if row < rows else 0)
        for index, camera in enumerate(visible):
            row, column = divmod(index, columns)
            if selected is not None and camera.uid == selected.uid:
                self.video_grid.addWidget(self.primary_pane, row, column)
                continue
            preview = self.previews.get(camera.uid)
            if preview is None:
                if any(retired.camera.uid == camera.uid for retired in self.retired_previews):
                    continue
                preview = CameraPreview(
                    camera, self.continuous_recording_enabled(), self.settings,
                    self.local_detection_enabled(), self.local_detection_ready.emit,
                    self.local_detection_failed.emit,
                )
                preview.video.camera_drop_requested.connect(self.drop_camera)
                preview.replay_requested.connect(self.open_local_replay)
                preview.camera_replay_requested.connect(self.open_camera_sd_replay)
                preview.fullscreen_requested.connect(self.toggle_fullscreen)
                self.previews[camera.uid] = preview
                self.video_grid.addWidget(preview, row, column)
                preview.show()
                preview.start()
            else:
                self.video_grid.addWidget(preview, row, column)
            preview.video.reorder_enabled = self.video.reorder_enabled
        self.preview_layout = layout
        self.camera_mask_timer.start(0)
        if len(cameras) == 1 and layout == "horizontal" and self.width() < 1120:
            self.resize(1120, self.height())
        if changed:
            self.aspect_fitted = False
            if self.isVisible():
                QTimer.singleShot(0, self.fit_video_aspect)

    def add_rtsp_camera(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Add RTSP camera")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit()
        name.setPlaceholderText("Entrance")
        url = QLineEdit()
        url.setPlaceholderText("rtsp://camera-host:554/stream")
        transport = QComboBox()
        transport.addItems(["TCP", "UDP"])
        form.addRow("Name", name)
        form.addRow("RTSP URL", url)
        form.addRow("Transport", transport)
        layout.addLayout(form)
        error = QLabel()
        error.setStyleSheet("color: #bd4242;")
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)

        def accept_camera() -> None:
            if not name.text().strip():
                error.setText("Enter a camera name.")
                name.setFocus()
            elif not valid_rtsp_url(url.text().strip()):
                error.setText("Enter an RTSP URL without embedded credentials.")
                url.setFocus()
            else:
                dialog.accept()

        buttons.accepted.connect(accept_camera)
        if self.exec_camera_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        camera = RtspCamera(
            f"rtsp:{uuid.uuid4().hex}", name.text().strip(), url.text().strip(),
            transport.currentText().lower(),
        )
        self.register_rtsp_camera(camera)

    def add_imou_camera(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Add Imou Life camera (local)")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit()
        name.setPlaceholderText("Driveway")
        address = QLineEdit()
        address.setPlaceholderText("192.168.1.100")
        port = QSpinBox()
        port.setRange(1, 65535)
        port.setValue(554)
        channel = QSpinBox()
        channel.setRange(1, 128)
        username = QLineEdit("admin")
        password = QLineEdit()
        password.setEchoMode(QLineEdit.EchoMode.Password)
        password.setPlaceholderText("Camera safety code or device password")
        form.addRow("Name", name)
        form.addRow("Local IP address", address)
        form.addRow("RTSP port", port)
        form.addRow("Channel", channel)
        form.addRow("Camera username", username)
        form.addRow("Device password", password)
        layout.addLayout(form)
        error = QLabel()
        error.setStyleSheet("color: #bd4242;")
        layout.addWidget(error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        layout.addWidget(buttons)
        buttons.rejected.connect(dialog.reject)
        uid = f"rtsp:{uuid.uuid4().hex}"

        def accept_camera() -> None:
            if not name.text().strip():
                error.setText("Enter a camera name.")
                name.setFocus()
                return
            try:
                imou_rtsp_url(address.text().strip(), port.value(), channel.value())
            except ValueError:
                error.setText("Enter a valid local camera IP address.")
                address.setFocus()
                return
            if not username.text().strip() or not password.text():
                error.setText("Enter the camera username and device password.")
                (username if not username.text().strip() else password).setFocus()
                return
            if not save_device_secret(IMOU_PROVIDER, uid, password.text(), "Imou camera device password"):
                error.setText("Unable to save the device password in the desktop keyring.")
                return
            dialog.accept()

        buttons.accepted.connect(accept_camera)
        if self.exec_camera_dialog(dialog) != QDialog.DialogCode.Accepted:
            return
        camera = RtspCamera(
            uid, name.text().strip(), imou_rtsp_url(address.text().strip(), port.value(), channel.value()),
            "tcp", IMOU_PROVIDER, username.text().strip(),
        )
        self.register_rtsp_camera(camera)

    def register_rtsp_camera(self, camera: RtspCamera) -> None:
        self.rtsp_cameras.append(camera)
        self.devices.append(camera)
        self.device_accounts[camera.uid] = RTSP_ACCOUNT
        self.save_rtsp_cameras()
        self.select_camera(RTSP_ACCOUNT, camera.uid)

    def remove_selected_rtsp_camera(self) -> None:
        camera = getattr(self, "selected_device", None)
        if not isinstance(camera, RtspCamera):
            return
        if (camera.provider == IMOU_PROVIDER and stored_secret(IMOU_PROVIDER, camera.uid) is not None
                and not clear_device_secret(IMOU_PROVIDER, camera.uid)):
            self.show_notice("Unable to remove the Imou password from the desktop keyring.")
            return
        self.rtsp_cameras.remove(camera)
        self.devices.remove(camera)
        self.device_accounts.pop(camera.uid, None)
        self.save_rtsp_cameras()
        self.settings.remove(f"{RTSP_SOUND_SETTING}/{camera.uid}")
        self.settings.remove(f"{CAMERA_VISIBLE_SETTING}/{camera.uid}")
        self.settings.remove("camera/selected_uid")
        self.settings.remove("camera/selected_account")
        self.settings.sync()
        remaining = self.visible_devices()
        if remaining:
            replacement = remaining[0]
            self.select_camera(self.device_accounts[replacement.uid], replacement.uid)
        else:
            self.stop_stream()
            if self.devices:
                self.selected_device = self.devices[0]
            else:
                del self.selected_device
            self.sync_previews()
            self.set_status(NO_VISIBLE_CAMERAS_MESSAGE if self.devices else "No cameras available.")

    def camera_source_actions(self) -> tuple[Callable[[], None], ...]:
        return self.change_account, self.add_rtsp_camera, self.add_imou_camera, self.add_imou_account

    def save_imou_accounts(self) -> None:
        self.settings.setValue(IMOU_ACCOUNTS_SETTING, json.dumps([asdict(account) for account in self.imou_accounts]))
        self.settings.setValue(IMOU_CAMERAS_SETTING, json.dumps([
            {"app_id": camera.imou_account.app_id, "device": asdict(camera.imou_device)}
            for camera in self.imou_cameras
        ]))
        self.settings.sync()

    def add_imou_account(self, account: ImouAccount | bool | None = None) -> None:
        if self.imou_dialog is not None:
            self.imou_dialog.raise_()
            self.imou_dialog.activateWindow()
            return
        if self.imou_worker is not None:
            self.show_notice(IMOU_LOADING_MESSAGE)
            return
        self.show_window_without_stream()
        dialog = ImouAccountDialog(self, account if isinstance(account, ImouAccount) else None)
        self.imou_dialog = dialog
        dialog.credentials_submitted.connect(self.start_imou_lookup)
        try:
            self.exec_camera_dialog(dialog)
        finally:
            self.imou_dialog = None
            dialog.secret.clear()
            dialog.deleteLater()

    def start_imou_lookup(self, account: ImouAccount, secret: str = "") -> None:
        if self.imou_worker is not None:
            if self.imou_dialog is not None:
                self.imou_dialog.show_error(IMOU_LOADING_MESSAGE)
            return
        self.imou_worker = ImouAccountWorker(account, secret or None)
        self.imou_worker.devices_found.connect(self.on_imou_devices_found)
        self.imou_worker.failed.connect(self.on_imou_failed)
        self.imou_worker.finished.connect(self.on_imou_finished)
        self.imou_worker.start()

    def on_imou_devices_found(self, devices: list[ImouDevice]) -> None:
        if self.close_pending or self.quit_requested:
            return
        account = self.imou_worker.account
        self.imou_accounts = [value for value in self.imou_accounts if value.app_id != account.app_id] + [account]
        cameras = [imou_account_camera(account, device) for device in devices]
        self.replace_imou_cameras(account, cameras)
        self.save_imou_accounts()
        if self.imou_dialog is not None:
            self.imou_dialog.accept()
        self.show_notice(f"Found {len(cameras)} Imou camera(s).")

    def replace_imou_cameras(self, account: ImouAccount, cameras: list[RtspCamera]) -> None:
        previous = {camera.uid: camera for camera in self.imou_cameras if camera.imou_account.app_id == account.app_id}
        current = {camera.uid: camera for camera in cameras}
        changed = {uid for uid, camera in previous.items() if current.get(uid) != camera}
        for uid in changed:
            self.close_local_replay(uid)
            if uid in self.previews:
                self._retire_preview(self.previews.pop(uid))
        self.imou_cameras = [camera for camera in self.imou_cameras if camera.uid not in previous] + cameras
        self.devices = [camera for camera in self.devices if camera.uid not in previous] + cameras
        for uid in previous:
            self.device_accounts.pop(uid, None)
        self.device_accounts.update({camera.uid: RTSP_ACCOUNT for camera in cameras})
        selected = getattr(self, "selected_device", None)
        if selected is not None and selected.uid in changed:
            replacement = current.get(selected.uid) or next(iter(self.visible_devices()), None)
            if replacement is not None:
                self.pending_camera = (self.device_accounts[replacement.uid], replacement.uid)
            self.stop_stream()
            if self.stream_worker is None:
                if self.pending_camera is not None:
                    self.apply_pending_camera()
                elif not self.devices:
                    del self.selected_device
        elif selected is not None and selected.uid in current:
            self.selected_device = current[selected.uid]

    def on_imou_failed(self, message: str) -> None:
        if self.close_pending or self.quit_requested:
            return
        if self.imou_dialog is not None:
            self.imou_dialog.show_error(message)
        else:
            self.show_notice(message)

    def on_imou_finished(self) -> None:
        worker = self.imou_worker
        self.imou_worker = None
        if worker is not None:
            worker.deleteLater()
        if self.close_pending:
            self.close()
        elif self.imou_queue:
            self.start_imou_lookup(self.imou_queue.pop(0))
        elif self.account_worker is None:
            if self.devices:
                selected = getattr(self, "selected_device", None)
                if selected is None or selected.uid not in self.device_accounts:
                    self.on_account_finished()
                else:
                    self.sync_previews()
                    if self.stream_worker is None:
                        self.watch_live()
            else:
                self.set_status("No cameras available.")

    def refresh_imou_accounts(self) -> None:
        if self.imou_worker is None and self.imou_accounts:
            self.imou_queue = list(self.imou_accounts)
            self.start_imou_lookup(self.imou_queue.pop(0))

    def remove_imou_account(self, account: ImouAccount) -> None:
        if self.imou_worker is not None:
            self.show_notice(IMOU_LOADING_MESSAGE)
            return
        if (stored_secret(IMOU_ACCOUNT_PROVIDER, account.app_id) is not None
                and not clear_device_secret(IMOU_ACCOUNT_PROVIDER, account.app_id)):
            self.show_notice("Unable to remove the Imou account key from the desktop keyring.")
            return
        removed = [camera for camera in self.imou_cameras if camera.imou_account.app_id == account.app_id]
        self.replace_imou_cameras(account, [])
        self.imou_accounts = [value for value in self.imou_accounts if value.app_id != account.app_id]
        for camera in removed:
            self.settings.remove(f"{RTSP_SOUND_SETTING}/{camera.uid}")
            self.settings.remove(f"{CAMERA_VISIBLE_SETTING}/{camera.uid}")
        self.save_imou_accounts()
        self.sync_previews()

    def center_camera_dialog(self, dialog: QDialog) -> None:
        parent_frame = self.frameGeometry()
        screen = QApplication.screenAt(parent_frame.center()) or self.screen()
        dialog.setScreen(screen)
        dialog.adjustSize()
        frame = dialog.frameGeometry()
        frame.moveCenter(parent_frame.center())
        available = screen.availableGeometry()
        frame.moveLeft(max(available.left(), min(frame.left(), available.right() - frame.width() + 1)))
        frame.moveTop(max(available.top(), min(frame.top(), available.bottom() - frame.height() + 1)))
        dialog.move(frame.topLeft())

    def exec_camera_dialog(self, dialog: QDialog) -> int:
        self.center_camera_dialog(dialog)
        placement = DialogPlacement(self, dialog, lambda: self.center_camera_dialog(dialog))
        try:
            return dialog.exec()
        finally:
            placement.stop()

    def setup_cameras(self) -> None:
        self.show_window_without_stream()
        dialog = QDialog(self)
        dialog.setWindowTitle("Add camera")
        layout = QVBoxLayout(dialog)
        sources = QComboBox()
        sources.addItems(CAMERA_SOURCE_LABELS)
        form = QFormLayout()
        form.addRow("Camera source", sources)
        layout.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if self.exec_camera_dialog(dialog) == QDialog.DialogCode.Accepted:
            self.camera_source_actions()[sources.currentIndex()]()
        dialog.deleteLater()

    def change_account(self) -> None:
        if self.account_dialog is not None:
            self.account_dialog.raise_()
            self.account_dialog.activateWindow()
            return
        if self.account_worker is not None:
            self.show_notice(ACCOUNT_LOADING_MESSAGE)
            return
        self.show_window_without_stream()
        dialog = AccountDialog(self, self.account_username)
        self.account_dialog = dialog
        dialog.credentials_submitted.connect(self.start_account_lookup)
        try:
            self.exec_camera_dialog(dialog)
        finally:
            self.account_dialog = None
            dialog.deleteLater()

    def find_cameras(self) -> None:
        if self.account_worker is not None:
            return
        self.devices = list(self.rtsp_cameras + self.imou_cameras)
        self.device_accounts = {camera.uid: RTSP_ACCOUNT for camera in self.devices}
        self.refresh_cameras()

    def refresh_cameras(self) -> None:
        if self.account_worker is not None:
            return
        self.refresh_imou_accounts()
        self.account_queue = [
            (username, password)
            for username in self.accounts
            if (password := stored_account_password(username))
        ]
        if not self.account_queue:
            if self.devices:
                self.on_account_finished()
            elif self.imou_worker is not None:
                self.set_status("Finding Imou cameras...")
            elif self.accounts:
                self.change_account()
            else:
                self.setup_cameras()
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        self.set_status("Finding cameras...")
        self.start_next_account_lookup()

    def start_next_account_lookup(self) -> None:
        username, password = self.account_queue.pop(0)
        self.start_account_lookup(username, password)

    def start_account_lookup(self, username: str, password: str) -> None:
        if self.account_worker is not None:
            if self.account_dialog is not None:
                self.account_dialog.show_error(ACCOUNT_LOADING_MESSAGE)
            return
        self.account_error.hide()
        self.account_username = username
        self.account_secret = password
        self.set_status("Finding cameras...")
        self.account_worker = AccountWorker(self.account_username, self.account_secret)
        self.account_worker.devices_found.connect(self.on_devices_found)
        self.account_worker.failed.connect(self.on_account_failed)
        self.account_worker.finished.connect(self.on_account_finished)
        self.account_worker.start()

    def on_devices_found(self, devices: list[AccountDevice]) -> None:
        if self.account_dialog is not None:
            self.account_dialog.accept()
        if not save_account_password(self.account_username, self.account_secret):
            self.set_status("The keyring could not save the account.")
        else:
            if self.account_username not in self.accounts:
                self.accounts.append(self.account_username)
            self.settings.setValue("accounts/okam", self.accounts)
            if not self.settings.value("account/username", "", str):
                self.settings.setValue("account/username", self.account_username)
            self.settings.sync()
        for device in devices:
            if device.uid not in self.device_accounts:
                self.devices.append(device)
                self.device_accounts[device.uid] = self.account_username
        if devices:
            self.retry_pending = False
            saved_uid = self.settings.value("camera/selected_uid", "", str)
            if not hasattr(self, "selected_device") or self.selected_device.uid not in self.device_accounts:
                self.selected_device = next(
                    (device for device in self.devices if device.uid == saved_uid), self.devices[0]
                )
            if not self.status_text.startswith("The keyring could not"):
                self.set_status(f"Found {len(self.devices)} camera(s).")
        else:
            if not self.devices:
                self.set_status("No cameras are visible to this account.")

    def select_camera(self, username: str, uid: str) -> None:
        if self.device_accounts.get(uid) != username:
            return
        if not self.camera_visible(uid):
            self.save_camera_visibility(uid, True)
        if uid != self.pending_camera_replay:
            self.pending_camera_replay = None
        if getattr(self, "selected_device", None) is not None and self.selected_device.uid == uid:
            self.sync_previews()
            self.show_window()
            return
        for replay_uid in list(self.local_replays):
            self.close_local_replay(replay_uid)
        self.pending_camera = (username, uid)
        self.reconnect_timer.stop()
        self.retry_pending = False
        self.exit_replay(False)
        if self.stream_worker is not None:
            self.stop_stream()
        elif self.detection_worker is None:
            self.apply_pending_camera()

    def apply_pending_camera(self) -> None:
        if self.pending_camera is None:
            return
        username, uid = self.pending_camera
        self.pending_camera = None
        if uid not in self.device_accounts or not self.camera_visible(uid):
            return
        self.selected_device = next(device for device in self.devices if device.uid == uid)
        self.settings.setValue("camera/selected_uid", uid)
        self.settings.setValue("camera/selected_account", username)
        self.settings.sync()
        self.latest_detection = None
        self.rtsp_ptz_available = False
        self.sync_quality_actions()
        self.on_capabilities_found([], None)
        self.rtsp_light_mode = None
        self.replay_button.setEnabled(True)
        if isinstance(self.selected_device, RtspCamera) and self.selected_device.provider == IMOU_ACCOUNT_PROVIDER:
            set_replay_menu(self.replay_button, lambda: self.open_camera_sd_replay(self.selected_device),
                            lambda: self.open_local_replay(self.selected_device))
        else:
            self.replay_button.setMenu(None)
        self.ptz_button.setEnabled(not isinstance(self.selected_device, RtspCamera))
        self.ptz_button.setVisible(not isinstance(self.selected_device, RtspCamera))
        self.sound_button.setVisible(not isinstance(self.selected_device, RtspCamera))
        self.ptz_panel.hide()
        self.stop_player()
        switching_preview = uid in self.previews or any(preview.camera.uid == uid for preview in self.retired_previews)
        replay_requested = self.pending_camera_replay == uid
        self.pending_camera_replay = None
        self.pending_selection_start = (uid, replay_requested) if switching_preview else None
        self.sync_previews()
        self.show_window_without_stream()
        if not switching_preview:
            self.pending_selection_start = (uid, replay_requested)
            self._finish_selected_camera()

    def on_account_failed(self, message: str) -> None:
        self.retry_pending = message != ACCOUNT_REJECTED_MESSAGE and self.account_dialog is None
        self.set_status(message)
        self.account_error.setText(message)
        self.account_error.show()
        if self.account_dialog is not None:
            self.account_dialog.show_error(message)
        elif message == ACCOUNT_REJECTED_MESSAGE:
            self.show_window_without_stream()

    def enter_replay(self, start: datetime | None) -> None:
        if not self.devices or self.close_pending or isinstance(getattr(self, "selected_device", None), RtspCamera):
            return
        if not self.camera_visible(self.selected_device.uid):
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
        if self.replay_status_overlay is None:
            self.replay_status_overlay = VideoStatusOverlay(self.video)
        self.replay.status_changed.connect(self.replay_status_overlay.display)
        self.replay.playing_changed.connect(self.on_replay_playing_changed)
        self.replay.clip_available.connect(self.replay_save_button.setEnabled)
        self.live_bar.hide()
        self.ptz_panel.hide()
        self.replay_bar.show()
        self.timeline.show()
        self.replay_save_button.setEnabled(False)
        self.set_status("Playback")
        self.replay.start(self.player.stdin, start)

    def exit_replay(self, resume: bool = True) -> None:
        if self.replay is None:
            return
        replay = self.replay
        self.replay = None
        replay.stop()
        if self.replay_status_overlay is not None:
            self.replay_status_overlay.hide()
        self.replay_bar.hide()
        self.timeline.hide()
        self.live_bar.show()
        if resume and self.stream_worker is None and self.account_worker is None:
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
            self.show_notice("Unable to save the recording.")
            return
        if path is not None:
            self.show_notice(f"Recording saved: {path}")

    def on_account_finished(self) -> None:
        self.account_worker = None
        self.account_secret = None
        if self.close_pending:
            self.close()
        elif self.account_queue:
            self.start_next_account_lookup()
        elif self.devices:
            saved_uid = self.settings.value("camera/selected_uid", "", str)
            self.selected_device = next(
                (device for device in self.devices if device.uid == saved_uid),
                next((device for device in self.devices if device.name == "Jardin"), self.devices[0]),
            )
            if not self.camera_visible(self.selected_device.uid) and self.visible_devices():
                self.selected_device = self.visible_devices()[0]
            self.rtsp_ptz_available = False
            self.replay_button.setEnabled(True)
            self.ptz_button.setEnabled(not isinstance(self.selected_device, RtspCamera))
            self.ptz_button.setVisible(not isinstance(self.selected_device, RtspCamera))
            self.sound_button.setVisible(not isinstance(self.selected_device, RtspCamera))
            if isinstance(self.selected_device, RtspCamera):
                self.ptz_panel.hide()
            if saved_uid != self.selected_device.uid:
                self.settings.setValue("camera/selected_uid", self.selected_device.uid)
                self.settings.setValue("camera/selected_account", self.device_accounts[self.selected_device.uid])
                if not saved_uid:
                    legacy_detection = self.settings.value(DETECTION_SETTING, "", str)
                    if legacy_detection:
                        self.settings.setValue(f"{DETECTION_SETTING}/{self.selected_device.uid}", legacy_detection)
                    legacy_quality = self.settings.value(QUALITY_SETTING, "", str)
                    if legacy_quality:
                        self.settings.setValue(f"{QUALITY_SETTING}/{self.selected_device.uid}", legacy_quality)
                self.settings.sync()
            self.sync_quality_actions()
            self.sync_previews()
            if self.stream_worker is None:
                self.watch_live()
        elif self.retry_pending:
            self.schedule_reconnect(self.status_text)

    def schedule_reconnect(self, reason: str) -> None:
        delay = min(RECONNECT_MAX_SECONDS, 2 ** (self.reconnect_attempts + 1))
        self.reconnect_attempts += 1
        self.reconnect_timer.start(delay * 1000)
        self.set_status(f"{reason} Reconnecting in {delay}s.")

    def reconnect(self) -> None:
        if self.devices and hasattr(self, "selected_device"):
            if self.camera_visible(self.selected_device.uid):
                self.watch_live()
        else:
            self.find_cameras()

    def watch_live(self) -> None:
        if not self.devices or self.stream_worker is not None:
            return
        if isinstance(self.selected_device, RtspCamera) and self.selected_device.provider == IMOU_ACCOUNT_PROVIDER:
            set_replay_menu(self.replay_button, lambda: self.open_camera_sd_replay(self.selected_device),
                            lambda: self.open_local_replay(self.selected_device))
        if not self.camera_visible(self.selected_device.uid):
            self.set_status(NO_VISIBLE_CAMERAS_MESSAGE)
            return
        if (isinstance(self.selected_device, RtspCamera) and self.selected_device.imou_device is not None
                and self.selected_device.imou_device.privacy):
            self.set_status(IMOU_PRIVACY_MESSAGE)
            return
        self.reconnect_timer.stop()
        self.retry_pending = False
        rtsp_camera = self.selected_device if isinstance(self.selected_device, RtspCamera) else None
        self.sound_enabled = rtsp_sound_enabled(self.settings, rtsp_camera) if rtsp_camera is not None else False
        if not (self.start_player(rtsp_camera) if rtsp_camera else self.start_player()):
            return
        if rtsp_camera is None:
            assert self.player.stdin is not None
        self.stream_error = False
        self.stream_live = False
        self.control_pending = False
        self.rtsp_ptz_available = False
        if rtsp_camera is not None:
            self.ptz_button.hide()
            self.ptz_panel.hide()
        self.reset_recording_state()
        self.zoom_level = 0
        set_button_icon(
            self.sound_button, "sound_on" if self.sound_enabled else "sound",
            "Mute camera" if self.sound_enabled else "Listen to camera",
        )
        if rtsp_camera is not None:
            assert self.mpv_socket is not None
            self.stream_worker = RtspStreamWorker(rtsp_camera, self.player, self.mpv_socket,
                                                  self.settings.value(f"{QUALITY_SETTING}/{rtsp_camera.uid}", "HD", str))
        else:
            try:
                continuous = ContinuousRecorder(continuous_directory(), safe_camera_name(self.selected_device.name))
            except OSError:
                continuous = None
                self.show_notice("Unable to create the continuous recording folder.")
            self.stream_worker = StreamWorker(
                self.selected_device,
                stored_camera_password(self.selected_device.uid) or "",
                self.player.stdin,
                continuous,
            )
        self.stream_worker.set_continuous(self.continuous_recording_enabled())
        self.stream_worker.local_detection_enabled = self.local_detection_enabled()
        self.stream_worker.local_event_callback = self.local_detection_ready.emit
        self.stream_worker.local_error_callback = self.local_detection_failed.emit
        if isinstance(self.stream_worker, StreamWorker):
            self.stream_worker.set_display(self.isVisible() and not self.isMinimized())
        self.stream_worker.status_changed.connect(self.on_stream_status)
        self.stream_worker.failed.connect(self.on_stream_error)
        if isinstance(self.stream_worker, StreamWorker):
            self.stream_worker.control_completed.connect(self.on_control_completed)
            self.stream_worker.control_failed.connect(self.on_control_failed)
            self.stream_worker.sound_changed.connect(self.on_sound_changed)
            self.stream_worker.sound_failed.connect(self.on_sound_failed)
        self.stream_worker.recording_started.connect(self.on_recording_started)
        self.stream_worker.recording_saved.connect(self.on_recording_saved)
        self.stream_worker.recording_failed.connect(self.on_recording_failed)
        if isinstance(self.stream_worker, RtspStreamWorker):
            self.stream_worker.audio_available.connect(self.on_rtsp_audio_available)
            self.stream_worker.continuous_failed.connect(self.on_continuous_failed)
            self.stream_worker.light_available.connect(self.on_rtsp_light_available)
            self.stream_worker.light_changed.connect(self.on_rtsp_light_changed)
            self.stream_worker.light_failed.connect(self.on_rtsp_light_failed)
            self.stream_worker.ptz_available.connect(self.on_rtsp_ptz_available)
            self.stream_worker.control_completed.connect(self.on_control_completed)
            self.stream_worker.control_failed.connect(self.on_control_failed)
        if isinstance(self.stream_worker, StreamWorker):
            self.stream_worker.detections_listed.connect(self.on_detections_listed)
            self.stream_worker.detections_failed.connect(self.on_detections_failed)
        self.stream_worker.capabilities_found.connect(self.on_capabilities_found)
        self.stream_worker.setting_completed.connect(self.on_setting_completed)
        self.stream_worker.setting_failed.connect(self.on_setting_failed)
        self.stream_worker.finished.connect(self.on_stream_finished)
        self.stream_worker.start()
        QTimer.singleShot(500, self.video.raise_interaction_layer)

    def start_player(self, rtsp_camera: RtspCamera | None = None) -> bool:
        if rtsp_camera is None and self.player is not None and self.player.poll() is None:
            self._mpv_command(["set_property", "video-zoom", 0])
            self.zoom_level = 0
            self.video_pan = (0.0, 0.0)
            self.apply_video_pan()
            return True
        self.stop_player()
        self.mpv_directory = tempfile.TemporaryDirectory(prefix="okam-linux-mpv-")
        self.mpv_socket = Path(self.mpv_directory.name) / "control.sock"
        playlist_fd = None
        try:
            if rtsp_camera is not None and rtsp_camera.provider == IMOU_PROVIDER:
                playlist_fd = imou_mpv_playlist(rtsp_camera)
            self.player = subprocess.Popen(
                mpv_rtsp_command(self.mpv_socket, int(self.video.winId()), True, rtsp_camera, self.sound_enabled, playlist_fd)
                if rtsp_camera else mpv_stream_command(self.mpv_socket, int(self.video.winId()), True),
                stdin=subprocess.DEVNULL if rtsp_camera else subprocess.PIPE,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                pass_fds=(playlist_fd,) if playlist_fd is not None else (),
            )
        except OSError as ex:
            if rtsp_camera is not None:
                close_bridge(rtsp_camera.uid)
            self.mpv_socket = None
            self.mpv_directory.cleanup()
            self.mpv_directory = None
            self.set_status(str(ex) if isinstance(ex, CameraCredentialError) else "The mpv video player is unavailable.")
            return False
        finally:
            if playlist_fd is not None:
                os.close(playlist_fd)
        return True

    def stop_player(self) -> None:
        stop_mpv_player(self.player)
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
            if self.pending_local_detection is not None and self.pending_local_detection.uid == self.selected_device.uid:
                event = self.pending_local_detection
                self.pending_local_detection = None
                QTimer.singleShot(0, lambda: self.open_local_replay(self.selected_device, event))
            rtsp = isinstance(self.selected_device, RtspCamera)
            self.set_controls_enabled(not self.control_pending)
            self.sound_button.setEnabled(not rtsp)
            self.snapshot_button.setEnabled(True)
            self.record_button.setEnabled(True)
            self.zoom_out_button.setEnabled(False)
            self.zoom_in_button.setEnabled(True)
            self.light_button.setEnabled(not rtsp)
            self.quality_button.setEnabled(not rtsp)

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
        rtsp = isinstance(getattr(self, "selected_device", None), RtspCamera)
        options = self.stream_worker.movement_options() if rtsp and isinstance(self.stream_worker, RtspStreamWorker) else (not rtsp, {} if rtsp else None)
        self.ptz_panel.set_capabilities(*options)
        for button in self.camera_buttons:
            button.setEnabled(enabled and (not rtsp or self.rtsp_ptz_available) and not button.isHidden())
        self.ptz_button.setEnabled(enabled and (not rtsp or self.rtsp_ptz_available))

    def on_rtsp_ptz_available(self) -> None:
        if self.stream_live and isinstance(self.stream_worker, RtspStreamWorker):
            self.rtsp_ptz_available = True
            self.ptz_button.show()
            set_button_icon(self.ptz_button, "ptz", "Pan and tilt controls" if self.stream_worker.movement_options()[0] else "Tilt controls")
            self.set_controls_enabled(not self.control_pending)
            self.video.place_overlay()

    def show_overlay(self) -> None:
        if not self.isVisible() or self.isMinimized():
            return
        self.video.place_overlay()
        self.overlay.show()
        self.video.raise_interaction_layer()
        self.overlay_timer.start(OVERLAY_TIMEOUT_MS)

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
            "Hide movement controls" if self.ptz_panel.isVisible() else "Movement controls"
        )
        self.video.place_overlay()
        self.show_overlay()

    def _mpv_command(self, command: list[object]) -> bool:
        return mpv_request(self.mpv_socket, command)[0]

    def take_snapshot(self) -> None:
        if not self.stream_live and self.replay is None:
            return
        try:
            path = media_directory() / f"{safe_camera_name(self.selected_device.name)}_{datetime.now():%Y%m%d_%H%M%S_%f}.png"
        except OSError:
            self.show_notice("Unable to create the picture folder.")
            return
        if self._mpv_command(["screenshot-to-file", str(path), "video"]):
            self.show_notice(f"Picture saved: {path}")
        else:
            self.show_notice("Unable to save a picture.")

    def toggle_recording(self) -> None:
        if not self.stream_live or self.stream_worker is None:
            return
        if self.recording_path is None:
            try:
                path = media_directory() / f"{safe_camera_name(self.selected_device.name)}_{datetime.now():%Y%m%d_%H%M%S_%f}.mkv"
            except OSError:
                self.show_notice("Unable to create the recording folder.")
                return
            self.stream_worker.set_recording(path)
            self.recording_path = path
            set_button_icon(self.record_button, "recording", "Stop recording")
            self.show_notice("Waiting for a key frame to start recording...")
        else:
            self.stream_worker.set_recording(None)
            self.reset_recording_state()
            self.show_notice("Saving recording...")

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
        self.show_notice(message)

    def on_continuous_failed(self, message: str) -> None:
        if hasattr(self, "continuous_action"):
            self.continuous_action.setChecked(False)
        else:
            self.set_continuous_recording(False)
        self.show_notice(message)

    def change_zoom(self, step: int, x: int | None = None, y: int | None = None) -> None:
        if not self.stream_live and self.replay is None:
            return
        level = min(MAX_ZOOM_LEVEL, max(0, self.zoom_level + step))
        if level == self.zoom_level:
            return
        if not self._mpv_command(["set_property", "video-zoom", level / 2]):
            self.show_notice("Unable to change zoom.")
            return
        if x is not None and y is not None:
            self.video_pan = zoomed_video_pan(
                self.video_pan, self.zoom_level, level, x, y, self.video.width(), self.video.height()
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
        for preview in self.previews.values():
            set_button_icon(
                preview.fullscreen_button,
                "exit_fullscreen" if self.isFullScreen() else "fullscreen",
                "Exit full screen" if self.isFullScreen() else "Full screen",
            )

    def fit_video_aspect(self) -> None:
        if self.isFullScreen():
            return
        count = len(self.visible_devices())
        if not count:
            return
        columns, rows = self.camera_grid_dimensions()
        available_height = QApplication.primaryScreen().availableGeometry().height() - 80
        width = self.width()
        height = rows * round(width / columns * VIDEO_ASPECT_HEIGHT / VIDEO_ASPECT_WIDTH)
        height += (rows - 1) * self.video_grid.spacing()
        if height > available_height:
            width = round(available_height / rows * VIDEO_ASPECT_WIDTH / VIDEO_ASPECT_HEIGHT * columns)
            height = available_height
        if self.window_hints is not None:
            self.window_hints.set_aspect_ratio(width, height)
        self.resize(width, height)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        if getattr(self, "previews", None) and self.effective_camera_layout() != self.preview_layout:
            self.layout_refresh_timer.start(0)
        if getattr(self, "camera_mask_timer", None) is not None:
            self.camera_mask_timer.start(0)

    def showEvent(self, event: QShowEvent) -> None:
        super().showEvent(event)
        if not self.aspect_fitted:
            self.aspect_fitted = True
            QTimer.singleShot(0, self.fit_video_aspect)
        QTimer.singleShot(0, self.update_recording_badge)

    def moveEvent(self, event: QMoveEvent) -> None:
        super().moveEvent(event)
        self.place_video_overlays()

    def place_video_overlays(self) -> None:
        self.video.place_overlay()
        for preview in self.previews.values():
            preview.video.place_overlay()
        for replay in self.local_replays.values():
            replay.video.place_overlay()

    def changeEvent(self, event: QEvent) -> None:
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange:
            self.centralWidget().setStyleSheet("background-color: black;" if self.isFullScreen()
                                              else "background-color: #171717;")
            self.camera_mask_timer.start(0)
            if self.isMinimized():
                self.overlay_timer.stop()
                self.hide_overlay()
            self.update_recording_badge()

    def quality_label(self) -> str:
        device = getattr(self, "selected_device", None)
        setting = f"{QUALITY_SETTING}/{device.uid}" if device is not None else QUALITY_SETTING
        quality = self.settings.value(setting, "", str)
        return quality if quality in VIDEO_QUALITIES else DEFAULT_QUALITY_LABEL

    def show_quality_menu(self) -> None:
        self.quality_menu.popup(
            self.quality_button.mapToGlobal(QPoint(0, -self.quality_menu.sizeHint().height()))
        )

    def on_capabilities_found(self, qualities: list[str], light_on: bool | None) -> None:
        for quality, action in self.quality_actions.items():
            action.setVisible(quality in qualities)
        self.quality_button.setVisible(bool(qualities))
        self.quality_button.setEnabled(self.stream_live and bool(qualities))
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

    def on_rtsp_light_available(self) -> None:
        if self.sender() is not self.stream_worker or not self.stream_live:
            return
        set_icam365_light_icon(self.light_button, self.rtsp_light_mode)
        self.light_button.setEnabled(True)
        self.light_button.show()
        self.video.place_overlay()

    def set_rtsp_light(self, mode: str) -> None:
        if not self.stream_live or not isinstance(self.stream_worker, RtspStreamWorker):
            return
        self.light_button.setEnabled(False)
        self.stream_worker.set_light(mode)

    def on_rtsp_light_changed(self, mode: str) -> None:
        if self.sender() is not self.stream_worker:
            return
        self.rtsp_light_mode = mode
        set_icam365_light_icon(self.light_button, mode)
        self.light_button.setEnabled(self.stream_live)
        self.show_notice("White light on." if mode == ICAM365_LIGHT_ON else "Automatic white light.")

    def on_rtsp_light_failed(self) -> None:
        if self.sender() is not self.stream_worker:
            return
        self.light_button.setEnabled(self.stream_live)
        self.show_notice("Unable to change white light mode.")

    def queue_setting(self, name: str, value: object, message: str) -> bool:
        if not self.stream_live or self.stream_worker is None or self.setting_pending:
            return False
        if not self.stream_worker.queue_setting(name, value):
            return False
        self.setting_pending = True
        self.light_button.setEnabled(False)
        self.quality_button.setEnabled(False)
        self.show_notice(message)
        return True

    def toggle_light(self) -> None:
        if isinstance(self.stream_worker, RtspStreamWorker):
            self.set_rtsp_light(ICAM365_LIGHT_AUTO if self.rtsp_light_mode == ICAM365_LIGHT_ON else ICAM365_LIGHT_ON)
            return
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
            self.show_notice("Stop recording before changing video quality.")
            return
        if not self.queue_setting(SETTING_QUALITY, quality, f"Changing video quality to {quality}..."):
            self.sync_quality_actions()

    def sync_quality_actions(self) -> None:
        current = self.quality_label()
        for quality, action in self.quality_actions.items():
            action.setChecked(quality == current)
        self.quality_button.setText(current)

    def on_setting_completed(self, name: str, value: object) -> None:
        self.setting_pending = False
        self.light_button.setEnabled(self.stream_live)
        self.quality_button.setEnabled(self.stream_live)
        if name == SETTING_LIGHT:
            self.light_on = bool(value)
            self.update_light_button()
            self.show_notice("White light on." if self.light_on else "White light off.")
        else:
            self.settings.setValue(f"{QUALITY_SETTING}/{self.selected_device.uid}", value)
            self.settings.sync()
            self.sync_quality_actions()
            self.show_notice(f"Video quality set to {value}.")

    def on_setting_failed(self, name: str, message: str) -> None:
        self.setting_pending = False
        self.light_button.setEnabled(self.stream_live)
        self.quality_button.setEnabled(self.stream_live)
        if name == SETTING_QUALITY:
            self.sync_quality_actions()
        self.show_notice(message)

    def toggle_sound(self) -> None:
        if isinstance(self.stream_worker, RtspStreamWorker):
            if self._mpv_command(["set_property", "mute", self.sound_enabled]):
                self.on_sound_changed(not self.sound_enabled)
            else:
                self.on_sound_failed("Unable to change camera sound.")
            return
        if not isinstance(self.stream_worker, StreamWorker):
            return
        self.sound_button.setEnabled(False)
        self.show_notice("Changing camera sound...")
        self.stream_worker.set_sound(not self.sound_enabled)

    def on_rtsp_audio_available(self) -> None:
        if self.sender() is self.stream_worker and self.stream_live:
            self.sound_button.setEnabled(True)
            self.sound_button.show()
            self.video.place_overlay()

    def on_sound_changed(self, enabled: bool) -> None:
        self.sound_enabled = enabled
        if isinstance(self.selected_device, RtspCamera):
            self.settings.setValue(f"{RTSP_SOUND_SETTING}/{self.selected_device.uid}", enabled)
            self.settings.sync()
        set_button_icon(
            self.sound_button,
            "sound_on" if enabled else "sound",
            "Mute camera" if enabled else "Listen to camera",
        )
        self.sound_button.setEnabled(self.stream_live)
        self.show_notice("Camera sound on." if enabled else "Camera sound off.")

    def on_sound_failed(self, message: str) -> None:
        self.sound_button.setEnabled(self.stream_live)
        self.show_notice(message)

    def pan_zoomed_video(self, dx: int, dy: int) -> None:
        if self.zoom_level == 0 or (not self.stream_live and self.replay is None):
            return
        self.video_pan = dragged_video_pan(
            self.video_pan, self.zoom_level, dx, dy, self.video.width(), self.video.height()
        )
        self.apply_video_pan()

    def apply_video_pan(self) -> None:
        self.video_pan = apply_mpv_video_pan(self._mpv_command, self.video_pan, self.zoom_level)

    def move_by_drag(self, dx: int, dy: int) -> None:
        if self.zoom_level > 0 or self.replay is not None:
            return
        if isinstance(getattr(self, "selected_device", None), RtspCamera):
            if isinstance(self.stream_worker, RtspStreamWorker) and self.stream_worker.movement_options()[0] and abs(dx) >= max(abs(dy), DRAG_PIXELS_PER_STEP // 2):
                self.control_camera(("Left" if dx > 0 else "Right",))
            elif abs(dy) >= max(abs(dx), DRAG_PIXELS_PER_STEP // 2):
                self.control_camera(("Up" if dy > 0 else "Down",))
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
        if not self.stream_live or self.control_pending or self.stream_worker is None:
            return
        if isinstance(self.stream_worker, RtspStreamWorker):
            queued = self.rtsp_ptz_available and len(commands) == 1 and self.stream_worker.set_ptz(commands[0])
        else:
            queued = self.stream_worker.queue_control(commands)
        if not queued:
            return
        self.control_pending = True
        self.set_controls_enabled(False)
        command = commands[0]
        if command.startswith("Preset "):
            self.show_notice(f"Moving camera to {command.lower()}...")
        else:
            self.show_notice(f"Moving camera {command.lower()}...")

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
        self.show_notice(message)

    def on_stream_error(self, message: str) -> None:
        self.stream_error = True
        self.retry_pending = message not in (
            "The camera rejected the available credentials.", IMOU_PRIVACY_MESSAGE, IMOU_SECRET_MISSING_MESSAGE,
        )
        self.stream_live = False
        self.control_pending = False
        self.rtsp_ptz_available = False
        self.sound_enabled = False
        self.disable_live_controls()
        if isinstance(getattr(self, "selected_device", None), RtspCamera):
            self.ptz_button.hide()
            self.ptz_panel.hide()
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
        self.rtsp_ptz_available = False
        self.sound_enabled = False
        selected = getattr(self, "selected_device", None)
        if selected is not None and not self.camera_visible(selected.uid):
            self.retry_pending = False
        if not (self.retry_pending or self.keep_player) or self.close_pending:
            self.stop_player()
        self.keep_player = False
        self.reset_recording_state()
        self.zoom_level = 0
        self.disable_live_controls()
        if isinstance(getattr(self, "selected_device", None), RtspCamera):
            self.ptz_button.hide()
            self.ptz_panel.hide()
        if self.retry_pending and not self.close_pending:
            self.schedule_reconnect("Camera disconnected.")
        elif not self.stream_error:
            self.set_status("Camera stopped.")
        if self.close_pending:
            self.close()
        elif self.pending_camera is not None:
            if self.detection_worker is None:
                self.apply_pending_camera()
        elif self.pending_replay is not None:
            start = self.pending_replay[0]
            self.pending_replay = None
            self.enter_replay(start)
        elif not self.visible_devices():
            self.set_status(NO_VISIBLE_CAMERAS_MESSAGE)

    def closeEvent(self, event: object) -> None:
        if self.tray is not None and not self.quit_requested:
            event.ignore()
            self.hide_to_tray()
            return
        self.reconnect_timer.stop()
        self.layout_refresh_timer.stop()
        self.camera_mask_timer.stop()
        self.retry_pending = False
        self.imou_queue.clear()
        if self.imou_worker is not None:
            self.imou_worker.requestInterruption()
        for uid in list(self.local_replays):
            self.close_local_replay(uid)
        for uid, preview in list(self.previews.items()):
            del self.previews[uid]
            self._retire_preview(preview)
        if (
            self.account_worker is not None
            or self.imou_worker is not None
            or self.stream_worker is not None
            or self.detection_worker is not None
            or self.retired_previews
            or self.retired_replays
        ):
            self.close_pending = True
            self.stop_stream()
            event.ignore()
            return
        event.accept()
        if _LOCAL_DETECTION_ENGINE is not None:
            _LOCAL_DETECTION_ENGINE.close()
        if self.window_hints is not None:
            self.window_hints.close()
        if self.tray is not None:
            self.tray.hide()
            QApplication.quit()


def main() -> int:
    if len(sys.argv) == 2 and sys.argv[1] == "--forget-account":
        settings = QSettings(STORAGE_NAME, STORAGE_NAME)
        accounts = settings.value("accounts/okam", [], list)
        username = settings.value("account/username", "", str)
        if username and username not in accounts:
            accounts.append(username)
        if any(not clear_account_password(account) for account in accounts):
            return 1
        settings.remove("account/username")
        settings.remove("accounts/okam")
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
    try:
        organize_events()
    except OSError as ex:
        LOG.warning("Could not organize local detections: %s", ex)
    try:
        directory = continuous_directory()
    except OSError:
        directory = None
    if directory is not None:
        raw_paths = tuple(directory.glob(f"*{MATROSKA_SUFFIX}{RAW_RECORDING_SUFFIX}"))
        threading.Thread(
            target=lambda: (
                recover_continuous_recordings(directory, raw_paths),
                prune_continuous_recordings(directory, datetime.now(), CONTINUOUS_RETENTION),
            ),
            daemon=True,
        ).start()
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
    settings = QSettings(STORAGE_NAME, STORAGE_NAME)
    accounts = settings.value("accounts/okam", [], list)
    username = settings.value("account/username", "", str)
    if username and username not in accounts:
        accounts.append(username)
    available = [(account, stored_account_password(account)) for account in accounts]
    if not any(password for account, password in available):
        print("No saved O-KAM account. Start Camera Viewer and sign in first.", file=sys.stderr)
        return 1
    for account, password in available:
        if not password:
            continue
        try:
            devices = Eye4AccountClient(opener=account_request).enumerate(account, password)
        except AccountError as ex:
            print(f"{account}: {ex}", file=sys.stderr)
            return 1
        for device in devices:
            credential = stored_camera_password(device.uid) or device.device_password
            print(f"{account}: {device.name} ({device.uid}): {credential or 'not provided by O-KAM; the camera uses its initial password'}")
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
