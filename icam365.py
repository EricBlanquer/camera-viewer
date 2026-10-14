from __future__ import annotations

import argparse
import bisect
import getpass
import hashlib
import json
import logging
import os
import queue
import secrets
import socket
import struct
import subprocess
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple

from cs2pppp import PpppSession, configure_tables, decode_init_string
from cs2pppp._protocol import header
from okam_native.cs2 import _DECODE_LOOKUP, _SHUFFLE

from icam365_cloud import (
    ACCOUNT_SIGN_IN_ERROR, AccountCredentialsRejected, CloudSession, CloudSessionRejected,
)


CONFIG_PATH = Path.home() / ".config/camera-viewer/icam365.json"
ACCOUNT_SECRET_CATEGORY = "icam365-account"
ACCOUNT_SECRET_LABEL = "Camera Viewer iCam365 account"
DEFAULT_AREA_CODE = "33"
MAX_FRAME_SIZE = 1024 * 1024
MAX_PENDING_PACKETS = 4096
MAX_CHANNEL_BUFFER = 8 * 1024 * 1024
PACKET_GAP_TIMEOUT = 8
RECEIVE_POLL_SECONDS = 0.05
STOP_ACK_TIMEOUT_SECONDS = 5
CLOSE_NOTIFICATION_RETRIES = 2
LIVE_AUDIO_CLOCK_FILTER = "aresample=async=1000"
LIVE_MEDIA_OUTPUT_OPTIONS = (
    "-max_interleave_delta", "100000", "-muxdelay", "0", "-muxpreload", "0",
    "-mpegts_flags", "resend_headers", "-flush_packets", "1", "-f", "mpegts", "pipe:1",
)
AAC_SAMPLE_RATE = 8000
AAC_FRAME_SAMPLES = 1024
ALAW_INPUT = ("-f", "alaw", "-ar", str(AAC_SAMPLE_RATE), "-ac", "1")
LIVE_VIDEO_INPUT = ("-probesize", "100000", "-analyzeduration", "1000000", "-use_wallclock_as_timestamps", "1",
                    "-r", "8", "-f", "hevc")
LIVE_AUDIO_INPUT = ("-use_wallclock_as_timestamps", "1", *ALAW_INPUT)
PLAYBACK_VIDEO_INPUT = ("-probesize", "2000000", "-analyzeduration", "1000000", "-f", "mpegts")
PLAYBACK_AUDIO_INPUT = ("-probesize", "32", *ALAW_INPUT)
ALAW_SILENCE = b"\xd5"
LIVE_CHANNELS = (0, 1, 2)
LIVE_STOP_COMMANDS = ((0x2FF, struct.pack("<II", 2, 0)), (0x301, struct.pack("<II", 1, 0)))
QUALITY_COMMAND = 0x320
MAIN_STREAM_QUALITY = 1
SECONDARY_STREAM_QUALITY = 5
PTZ_COMMAND = 0x1001
PTZ_POSITION_COMMAND = 0x0408
PRESETS_REQUEST_COMMAND = 0x0452
PRESETS_RESPONSE_COMMAND = 0x0453
NATIVE_PTZ_DIRECTIONS = {"Up": 1, "Down": 2, "Left": 3, "Right": 6}
GENERIC_RESPONSE_COMMAND = 1
AUTHENTICATION_RESPONSE_COMMAND = 0x8003
FEATURES_REQUEST_COMMAND = 0x8024
FEATURES_RESPONSE_COMMAND = 0x8025
LIST_RECORDINGS_COMMAND = 0x8000
LIST_RECORDINGS_RESPONSE = 0x8001
PLAY_CONTROL_COMMAND = 0x031A
PLAY_CONTROL_RESPONSE = 0x031B
PLAY_STOP = 0x01
PLAY_FORWARD = 0x04
PLAY_START = 0x10
PLAYBACK_AV_INDEXES = (3, 5)
PLAYBACK_CHANNELS = (0, *PLAYBACK_AV_INDEXES, *(index + 1 for index in PLAYBACK_AV_INDEXES))
PLAYBACK_RATE_FEATURE = "pbrate"
PLAYBACK_SPEED_PARAMETERS = {1: 0, 2: 1}
VIDEO_CODEC = 80
AUDIO_CODEC = 138
KEY_FRAME_FLAG = 1
SYNC_MESSAGE_TYPE = 0
TIME_DAY = struct.Struct("<HBBBBBB")
RECORDING_LIST_HEADER = struct.Struct("<IIBBBB")
RECORDING_ENTRY = struct.Struct("<IB")
RECORDING_ENTRY_SIZES = {0: 16, 1: 20}
MAX_RECORDING_SECONDS = 24 * 60 * 60
CAMERA_RECORDING_PREFIX = "icam365-"
REPLAY_CONNECT_TIMEOUT = 45
REPLAY_AUTHENTICATION_TIMEOUT = 15
REPLAY_FEATURES_TIMEOUT = 3
REPLAY_CLOSE_TIMEOUT = 15
REPLAY_REQUEST_TIMEOUT = 120
RECORDING_LIST_TIMEOUT = 20
PLAYBACK_START_TIMEOUT = 15
PLAYBACK_IDLE_TIMEOUT = 10
PLAYBACK_DISCONTINUITY_SECONDS = 2
PLAYBACK_FRAME_SECONDS = 0.08
PLAYBACK_WAIT_SECONDS = 0.25
PLAYBACK_POSITION_SAMPLES = 4096
PLAYBACK_QUEUE_FRAMES = 4096
SAVE_SPEED = 2
AUDIO_LAG_SECONDS = 0.5
AUDIO_PLACEMENT_SAMPLES = AAC_SAMPLE_RATE // 5
AUDIO_PADDING_SAMPLES = 320
TS_SYNC_BYTE = 0x47
TS_PAYLOAD_SIZE = 184
TS_PAT_PID = 0
TS_PMT_PID = 0x1000
TS_VIDEO_PID = 0x100
TS_PROGRAM_NUMBER = 1
TS_HEVC_STREAM_TYPE = 0x24
TS_VIDEO_STREAM_ID = 0xE0
TS_CLOCK_RATE = 90000
TS_START_SECONDS = 1
MAX_PLAYBACK_BUFFER = 64 * 1024 * 1024
MUXER_STOP_TIMEOUT = 3
RECORDINGS_UNAVAILABLE_MESSAGE = "Unable to connect to the iCam365 camera recordings."
RECORDING_LIST_ERROR = "The iCam365 camera could not list its recordings."
NO_RECORDING_MESSAGE = "The camera sent no recording for this time."
MUXER_STOPPED_MESSAGE = "The iCam365 media muxer stopped."
LOG = logging.getLogger("okam-linux.icam365")
_CONFIG_LOCK = threading.Lock()
_REJECTED_SIGN_INS: set[bytes] = set()


def stored_account_password(username: str) -> str | None:
    try:
        result = subprocess.run(
            ["secret-tool", "lookup", "application", "okam-linux", ACCOUNT_SECRET_CATEGORY, username],
            capture_output=True, check=False,
        )
    except OSError:
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    try:
        return result.stdout.decode("utf-8").removesuffix("\n")
    except UnicodeError:
        return None


def save_account_password(username: str, password: str) -> bool:
    try:
        result = subprocess.run(
            ["secret-tool", "store", f"--label={ACCOUNT_SECRET_LABEL}", "application", "okam-linux",
             ACCOUNT_SECRET_CATEGORY, username],
            input=password.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
        )
        return result.returncode == 0
    except OSError:
        return False


@dataclass(frozen=True)
class NativePreset:
    number: int
    name: str
    channel: int
    position: tuple[float, float, float]

    def payload(self) -> bytes:
        return struct.pack("<fffii", *self.position, self.channel, self.number)


def decode_presets(payload: bytes) -> dict[str, NativePreset]:
    if len(payload) < 4:
        raise ValueError("Invalid camera presets.")
    count, kind, channel = struct.unpack_from("<HBB", payload)
    sizes = {1: 36, 2: 48, 3: 4, 4: 16}
    size = sizes.get(kind)
    if size is None or len(payload) != 4 + count * size:
        raise ValueError("Invalid camera presets.")
    presets = {}
    for offset in range(4, len(payload), size):
        flags, number = struct.unpack_from("<HH", payload, offset)
        if flags & 4:
            continue
        position = struct.unpack_from("<fff", payload, offset + 4) if kind in (2, 4) else (-1.0, -1.0, -1.0)
        name_offset = offset + (16 if kind == 2 else 4)
        name = payload[name_offset:name_offset + 32].split(b"\0", 1)[0].decode("utf-8", errors="replace") if kind in (1, 2) else ""
        command = f"Preset {number}"
        presets[command] = NativePreset(number, name or command, channel, position)
    return presets


@dataclass(frozen=True, repr=False)
class NativeConfig:
    did: str
    platform: str
    password: str
    cloud_session: CloudSession | None = None

    @classmethod
    def from_record(cls, record: dict) -> NativeConfig:
        if not all(isinstance(record.get(key), str) for key in ("p2p_id", "p2p_platform", "password")):
            raise OSError("Invalid iCam365 native connection configuration.")
        did = record["p2p_id"].split(",", 1)[0]
        platform = record.get("p2p_platform", "")
        password = record.get("password", "")
        parts = did.split("-")
        if (
            len(parts) != 3 or not parts[0].isascii() or not parts[0].isalnum()
            or not parts[1].isdigit() or not parts[2].isascii() or not parts[2].isalnum()
            or len(parts[0]) > 8 or len(parts[2]) > 8 or int(parts[1]) > 0xFFFFFFFF
            or not platform.startswith("ppcs:") or len(platform) > 4096
            or not isinstance(password, str) or not 1 <= len(password.encode()) <= 48
        ):
            raise OSError("Invalid iCam365 native connection configuration.")
        cloud = record.get("cloud_session")
        return cls(did, platform[5:], password, CloudSession.from_record(cloud) if cloud is not None else None)

    def refreshed(self) -> NativeConfig:
        if self.cloud_session is None:
            return self
        session = self.cloud_session
        try:
            record = session.device_record()
        except CloudSessionRejected:
            session = signed_in_session(session)
            record = session.device_record()
        refreshed = self.with_device_record(record, session)
        if session is not self.cloud_session or refreshed.password != self.password:
            try:
                store_config(refreshed)
            except (OSError, ValueError, TypeError, AttributeError):
                LOG.warning("Unable to save the refreshed iCam365 connection.")
        return refreshed

    def with_device_record(self, record: dict, session: CloudSession) -> NativeConfig:
        config = NativeConfig.from_record(record)
        if config.did != self.did:
            raise OSError("The iCam365 account returned a different camera.")
        return NativeConfig(config.did, config.platform, config.password, session)


def signed_in_session(session: CloudSession) -> CloudSession:
    password = stored_account_password(session.username) if session.username else None
    if not password:
        raise OSError(ACCOUNT_SIGN_IN_ERROR)
    attempt = hashlib.sha256(f"{session.username}\0{password}".encode()).digest()
    if attempt in _REJECTED_SIGN_INS:
        raise OSError(ACCOUNT_SIGN_IN_ERROR)
    try:
        renewed = session.signed_in(password)
    except AccountCredentialsRejected:
        _REJECTED_SIGN_INS.add(attempt)
        raise
    LOG.info("Renewed the iCam365 account session.")
    return renewed


def read_records() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        if CONFIG_PATH.stat().st_mode & 0o077:
            raise OSError("The iCam365 connection file must be private (mode 600).")
        records = json.loads(CONFIG_PATH.read_text())
    except (ValueError, TypeError):
        raise OSError("Invalid iCam365 native connection configuration.") from None
    if not isinstance(records, dict):
        raise OSError("Invalid iCam365 native connection configuration.")
    return records


def write_records(records: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_PATH.parent, prefix=".icam365-", delete=False) as output:
            temporary_path = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(records, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, CONFIG_PATH)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def store_config(config: NativeConfig) -> None:
    with _CONFIG_LOCK:
        records = read_records()
        matches = [record for record in records.values()
                   if isinstance(record, dict) and record.get("p2p_id", "").split(",", 1)[0] == config.did]
        if not matches:
            raise OSError("The iCam365 connection is no longer configured.")
        for record in matches:
            record["password"] = config.password
            if config.cloud_session is not None:
                record["cloud_session"] = config.cloud_session.record()
        write_records(records)


def load_config(uid: str) -> NativeConfig | None:
    record = read_records().get(uid)
    try:
        return NativeConfig.from_record(record) if isinstance(record, dict) else None
    except (ValueError, TypeError, AttributeError):
        raise OSError("Invalid iCam365 native connection configuration.") from None


class OrderedChannel:
    def __init__(self, expected: int = 0) -> None:
        self.expected = expected
        self.pending: dict[int, bytes] = {}
        self.pending_bytes = 0
        self.gap_since: float | None = None

    def feed(self, index: int, payload: bytes) -> bytes:
        distance = (index - self.expected) & 0xFFFF
        if distance >= 0x8000 or index in self.pending:
            return b""
        if distance > MAX_PENDING_PACKETS or self.pending_bytes + len(payload) > MAX_CHANNEL_BUFFER:
            raise OSError("The iCam365 media channel exceeded its receive buffer.")
        self.pending[index] = payload
        self.pending_bytes += len(payload)
        chunks = []
        while self.expected in self.pending:
            chunk = self.pending.pop(self.expected)
            self.pending_bytes -= len(chunk)
            chunks.append(chunk)
            self.expected = (self.expected + 1) & 0xFFFF
        if not self.pending:
            self.gap_since = None
        elif chunks or self.gap_since is None:
            self.gap_since = time.monotonic()
        return b"".join(chunks)

    def check_timeout(self) -> None:
        if self.gap_since is not None and time.monotonic() - self.gap_since > PACKET_GAP_TIMEOUT:
            raise OSError("The iCam365 media channel lost packets.")


class MediaFrame(NamedTuple):
    codec: int
    flags: int
    timestamp: int
    payload: bytes


class MediaFrames:
    def __init__(self, codecs: tuple[int, ...]) -> None:
        self.codecs = codecs
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        return [(frame.codec, frame.flags, frame.payload) for frame in self.feed_timed(data) if frame.codec]

    def feed_timed(self, data: bytes) -> list[MediaFrame]:
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 16:
            codec, flags = self.buffer[0], self.buffer[2]
            size, timestamp = struct.unpack_from("<II", self.buffer, 8)
            if codec not in (0, *self.codecs) or size > MAX_FRAME_SIZE:
                raise OSError("Invalid iCam365 media frame.")
            if len(self.buffer) < 16 + size:
                break
            frames.append(MediaFrame(codec, flags, timestamp, bytes(self.buffer[16:16 + size])))
            del self.buffer[:16 + size]
        return frames


def stream_quality(main_stream: bool) -> bytes:
    return struct.pack("<II", 0, MAIN_STREAM_QUALITY if main_stream else SECONDARY_STREAM_QUALITY)


class NativePpppSession(PpppSession):
    def _recv(self, timeout: float = 0.4) -> list[tuple[bytes, tuple[str, int]]]:
        packets = super()._recv(timeout)
        for data, address in packets:
            if self._uid and data == header(0x42, 20) + self._uid:
                self._send(header(0x43, 0), address)
        return packets


class NativeSession:
    def __init__(self, config: NativeConfig, channels: tuple[int, ...] = LIVE_CHANNELS) -> None:
        decoded = decode_init_string(config.platform, lut=_DECODE_LOOKUP)
        if not decoded.lib_ok:
            raise OSError("Invalid iCam365 directory configuration.")
        configure_tables(prop_table=_SHUFFLE)
        self.config = config
        self.session = NativePpppSession(real_did=config.did, servers=decoded.servers)
        self.channels = {index: OrderedChannel() for index in channels}
        self.stop_commands = LIVE_STOP_COMMANDS
        self.sequence = 0
        self.pending: dict[int, tuple[bytes, float, int]] = {}
        self.control_buffer = bytearray()
        self.last_alive = 0.0
        self.authenticated = False

    def open(self) -> None:
        try:
            self.session.open(timeout=12, prefer="direct")
        except Exception:
            self.close()
            raise OSError("Unable to connect to the iCam365 native service.") from None
        self.send(0x8002, b"\0" * 8 + self.config.password.encode().ljust(48, b"\0") + b"\0" * 4)

    def send(self, command: int, payload: bytes = b"") -> None:
        index = self.sequence
        self.sequence = (index + 1) & 0xFFFF
        packet = self.session._drw_packet(struct.pack("<II", command, len(payload)) + payload, channel=0, idx=index)
        self._send(packet)
        self.pending[index] = (packet, time.monotonic(), 0)

    def _send(self, packet: bytes) -> None:
        peer = self.session.peer
        if peer is None:
            raise OSError("The iCam365 native connection is closed.")
        self.session._send(packet, (peer.ip, peer.port))

    def _receive_packets(self) -> Iterator[tuple[bytes, tuple[str, int]]]:
        sock = self.session.sock
        if sock is None:
            raise OSError("The iCam365 native connection is closed.")
        deadline = time.monotonic() + RECEIVE_POLL_SECONDS
        while (remaining := deadline - time.monotonic()) > 0:
            sock.settimeout(remaining)
            try:
                yield sock.recvfrom(65535)
            except (socket.timeout, BlockingIOError):
                break

    def receive(self, control_only: bool = False) -> tuple[list[tuple[int, bytes]], list[tuple[int, bytes]]]:
        media, commands = [], []
        now = time.monotonic()
        if now - self.last_alive > 1:
            self.session.alive()
            self.last_alive = now
        for index, (packet, sent_at, retries) in list(self.pending.items()):
            if now - sent_at >= 0.5:
                if retries >= 20:
                    raise OSError("The iCam365 camera did not acknowledge a command.")
                self._send(packet)
                self.pending[index] = (packet, now, retries + 1)
        for data, address in self._receive_packets():
            peer = self.session.peer
            if peer is None or address != (peer.ip, peer.port) or len(data) < 4:
                continue
            if len(data) != 4 + int.from_bytes(data[2:4], "big"):
                continue
            if data[:2] == b"\xf1\xe0":
                self._send(header(0xE1, 0))
            elif data[:2] in (b"\xf1\x41", b"\xf1\x42") and data[4:] == self.session._uid:
                reply = header(0x42, 20) + self.session._uid if data[1] == 0x41 else header(0x43, 0)
                self._send(reply)
            elif data[:2] == b"\xf1\xf0":
                raise OSError("The iCam365 camera closed its native connection.")
            elif data[:2] == b"\xf1\xd1" and len(data) >= 10 and data[5] == 0:
                count = int.from_bytes(data[6:8], "big")
                if len(data) == 8 + count * 2:
                    for offset in range(8, len(data), 2):
                        self.pending.pop(int.from_bytes(data[offset:offset + 2], "big"), None)
            elif data[:2] == b"\xf1\xd0" and len(data) >= 8 and data[4] == 0xD1:
                channel, index = data[5], int.from_bytes(data[6:8], "big")
                if channel not in self.channels:
                    continue
                self._send(header(0xD1, 6) + bytes([0xD1, channel, 0, 1]) + data[6:8])
                if control_only:
                    continue
                ordered = self.channels[channel].feed(index, data[8:])
                if channel == 0:
                    self.control_buffer.extend(ordered)
                elif ordered:
                    media.append((channel, ordered))
        if control_only:
            return media, commands
        for index, channel in self.channels.items():
            try:
                channel.check_timeout()
            except OSError:
                LOG.warning(
                    "iCam365 packet gap: channel=%d expected=%d buffered=%d transport=%s",
                    index, channel.expected, len(channel.pending), self.session._via,
                )
                raise
        while len(self.control_buffer) >= 8:
            command, size = struct.unpack_from("<II", self.control_buffer)
            if size > MAX_FRAME_SIZE:
                raise OSError("Invalid iCam365 command response.")
            if len(self.control_buffer) < 8 + size:
                break
            commands.append((command, bytes(self.control_buffer[8:8 + size])))
            del self.control_buffer[:8 + size]
        return media, commands

    def start_media(self, main_stream: bool = True) -> None:
        self.authenticated = True
        self.send(FEATURES_REQUEST_COMMAND)
        self.send(0x8012, b"\0" * 8)
        self.send(0x1FF, struct.pack("<II", 2, 0))
        self.send(QUALITY_COMMAND, stream_quality(main_stream))
        self.send(0x300, struct.pack("<II", 1, 0))

    def close(self) -> None:
        try:
            if self.authenticated and self.session.peer:
                for command, payload in self.stop_commands:
                    self.send(command, payload)
                deadline = time.monotonic() + STOP_ACK_TIMEOUT_SECONDS
                while self.pending and time.monotonic() < deadline:
                    self.receive(control_only=True)
        except OSError:
            pass
        finally:
            try:
                if self.session.sock is not None:
                    targets = {
                        (target.ip, target.port + offset)
                        for target in self.session.punch_targets for offset in range(-3, 4)
                        if 1 <= target.port + offset <= 65535
                    }
                    if self.session.peer is not None:
                        targets.add((self.session.peer.ip, self.session.peer.port))
                    for _ in range(CLOSE_NOTIFICATION_RETRIES):
                        for target in targets:
                            self.session._send(header(0xF0, 0), target)
            finally:
                self.session.close()


class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        bridge = self.server.bridge
        if self.path != bridge.stream_path:
            self.send_error(404)
            return
        subscriber: queue.Queue[bytes | None] = queue.Queue(maxsize=256)
        with bridge.subscriber_lock:
            bridge.subscribers.add(subscriber)
        try:
            self.connection.settimeout(3)
            self.send_response(200)
            self.send_header("Content-Type", "video/mp2t")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while not bridge.stopped.is_set() or not subscriber.empty():
                try:
                    data = subscriber.get(timeout=1)
                except queue.Empty:
                    continue
                if data is None:
                    break
                self.wfile.write(data)
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with bridge.subscriber_lock:
                bridge.subscribers.discard(subscriber)

    def log_message(self, format: str, *args) -> None:
        pass


def start_media_server(owner):
    owner.stream_path = "/" + secrets.token_urlsafe(24)
    server = ThreadingHTTPServer(("127.0.0.1", 0), StreamHandler)
    server.daemon_threads = True
    server.bridge = owner
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, f"http://127.0.0.1:{server.server_port}{owner.stream_path}"


def write_media(fd: int, source: queue.Queue, stopped: threading.Event, failed: Callable[[], None]) -> None:
    try:
        with os.fdopen(fd, "wb", buffering=0) as output:
            while not stopped.is_set():
                try:
                    data = source.get(timeout=0.5)
                except queue.Empty:
                    continue
                if data is None:
                    break
                remaining = memoryview(data)
                while remaining:
                    written = output.write(remaining)
                    if written is None or written == 0:
                        raise OSError("The iCam365 media muxer stopped accepting data.")
                    remaining = remaining[written:]
    except OSError:
        failed()


def start_media_muxer(
    video_source: queue.Queue, audio_source: queue.Queue, stopped: threading.Event, failed: Callable[[], None],
    video_input: tuple[str, ...], audio_input: tuple[str, ...], audio_filter: tuple[str, ...],
    output_options: tuple[str, ...], stdout: int,
) -> tuple[subprocess.Popen, list[threading.Thread]]:
    video_read, video_write = os.pipe()
    audio_read, audio_write = os.pipe()
    try:
        process = subprocess.Popen([
            "ffmpeg", "-nostdin", "-nostats", "-loglevel", "error",
            "-thread_queue_size", "512", *video_input, "-i", f"/proc/self/fd/{video_read}",
            "-thread_queue_size", "512", *audio_input, "-i", f"/proc/self/fd/{audio_read}",
            "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", *audio_filter, "-c:a", "aac", "-b:a", "32k",
            "-output_ts_offset", str(AAC_FRAME_SAMPLES / AAC_SAMPLE_RATE),
            *output_options,
        ], stdin=subprocess.DEVNULL, stdout=stdout, stderr=subprocess.DEVNULL,
            pass_fds=(video_read, audio_read), bufsize=0)
    except OSError:
        os.close(video_write)
        os.close(audio_write)
        raise
    finally:
        os.close(video_read)
        os.close(audio_read)
    threads = [
        threading.Thread(target=write_media, args=(video_write, video_source, stopped, failed), daemon=True),
        threading.Thread(target=write_media, args=(audio_write, audio_source, stopped, failed), daemon=True),
    ]
    for thread in threads:
        thread.start()
    return process, threads


def camera_features(payload: bytes) -> dict:
    features = json.loads(payload.rstrip(b"\0")).get("feature", {})
    if not isinstance(features, dict):
        raise ValueError("Invalid iCam365 camera features.")
    return features


def broadcast_media(process, stopped, subscribers, subscriber_lock) -> None:
    try:
        while not stopped.is_set():
            data = process.stdout.read(188 * 32)
            if not data:
                break
            with subscriber_lock:
                for subscriber in list(subscribers):
                    try:
                        subscriber.put_nowait(data)
                    except queue.Full:
                        subscribers.remove(subscriber)
                        try:
                            while True:
                                subscriber.get_nowait()
                        except queue.Empty:
                            subscriber.put_nowait(None)
    except OSError:
        pass


class NativeBridge:
    def __init__(self, config: NativeConfig) -> None:
        self.config = config
        self.stopped = threading.Event()
        self.error: str | None = None
        self.light_mode: str | None = None
        self.light_supported = False
        self.ptz_supported = False
        self.pan_supported = False
        self.main_stream = True
        self.presets: dict[str, NativePreset] = {}
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_lock = threading.Lock()
        self.control_requests: queue.Queue[tuple[int, bytes]] = queue.Queue(maxsize=16)
        self.control_results: queue.Queue[tuple[int, bool]] = queue.Queue()
        self.video_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
        self.process: subprocess.Popen | None = None
        self.server, self.server_thread, self.url = start_media_server(self)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def control(self, command: int, payload: bytes) -> bool:
        try:
            self.control_requests.put_nowait((command, payload))
            return True
        except queue.Full:
            return False

    def set_main_stream(self, enabled: bool) -> None:
        if enabled != self.main_stream and self.control(QUALITY_COMMAND, stream_quality(enabled)):
            self.main_stream = enabled

    def move_camera(self, direction: str) -> bool:
        if not self.ptz_supported:
            return False
        control = NATIVE_PTZ_DIRECTIONS.get(direction)
        if control is not None:
            if direction in ("Left", "Right") and not self.pan_supported:
                return False
            return self.control(PTZ_COMMAND, bytes([control, 0, 0, 0, 0, 6, 0, 0]))
        preset = self.presets.get(direction)
        return preset is not None and self.control(PTZ_POSITION_COMMAND, preset.payload())

    def _muxer_failed(self) -> None:
        if not self.stopped.is_set():
            self.error = MUXER_STOPPED_MESSAGE
            self.stopped.set()

    def _broadcast(self) -> None:
        broadcast_media(self.process, self.stopped, self.subscribers, self.subscriber_lock)
        self._muxer_failed()

    def _start_muxer(self) -> list[threading.Thread]:
        self.process, threads = start_media_muxer(
            self.video_queue, self.audio_queue, self.stopped, self._muxer_failed, LIVE_VIDEO_INPUT, LIVE_AUDIO_INPUT,
            ("-af", LIVE_AUDIO_CLOCK_FILTER), LIVE_MEDIA_OUTPUT_OPTIONS, subprocess.PIPE,
        )
        broadcaster = threading.Thread(target=self._broadcast, daemon=True)
        broadcaster.start()
        return [*threads, broadcaster]

    def _run(self) -> None:
        session = None
        threads = []
        video, audio = MediaFrames((80,)), MediaFrames((138,))
        first_keyframe = False
        last_video = time.monotonic()
        ptz_stop_at = None
        ptz_stop_index = None
        refresh_error = None
        try:
            try:
                self.config = self.config.refreshed()
            except OSError as ex:
                refresh_error = str(ex)
                LOG.warning("iCam365 credential refresh failed: %s", refresh_error)
            session = NativeSession(self.config)
            session.open()
            auth_deadline = time.monotonic() + 15
            while not self.stopped.is_set():
                media, commands = session.receive()
                for command, payload in commands:
                    if command == AUTHENTICATION_RESPONSE_COMMAND:
                        if len(payload) < 4 or struct.unpack_from("<i", payload)[0] != 0:
                            raise OSError(refresh_error or "The iCam365 camera rejected authentication.")
                        if not session.authenticated:
                            session.start_media(self.main_stream)
                            threads = self._start_muxer()
                            last_video = time.monotonic()
                    elif command == FEATURES_RESPONSE_COMMAND:
                        try:
                            features = camera_features(payload)
                            self.light_supported = features.get("DoubleLight", "").startswith("Yes")
                            ptz = features.get("SupportPTZ", "").split(",")
                            self.ptz_supported = ptz[0] in ("Yes", "Relative", "Hybrid", "Absolute")
                            self.pan_supported = self.ptz_supported and "VertOnly" not in ptz
                            if self.ptz_supported and "PresetPos" in ptz:
                                session.send(PRESETS_REQUEST_COMMAND, b"\0" * 8)
                        except (ValueError, AttributeError, TypeError):
                            pass
                    elif command == PRESETS_RESPONSE_COMMAND:
                        try:
                            self.presets = decode_presets(payload)
                        except ValueError:
                            LOG.warning("Invalid iCam365 preset response.")
                    elif command == 1 and len(payload) >= 8:
                        request, error = struct.unpack_from("<II", payload)
                        if request == PTZ_POSITION_COMMAND:
                            self.control_results.put((request, error == 0))
                    elif command == 0x8013 and len(payload) >= 9:
                        self.light_mode = {1: "on", 2: "2"}.get(payload[8])
                    elif command == 0x8015:
                        success = len(payload) >= 4 and struct.unpack_from("<i", payload)[0] == 0
                        self.control_results.put((command - 1, success))
                if not session.authenticated and time.monotonic() > auth_deadline:
                    raise OSError("The iCam365 camera did not authenticate.")
                if session.authenticated:
                    while not self.control_requests.empty():
                        command, payload = self.control_requests.get_nowait()
                        session.send(command, payload)
                        if command == PTZ_COMMAND and payload and payload[0] in NATIVE_PTZ_DIRECTIONS.values():
                            ptz_stop_at = time.monotonic() + 0.12
                    if ptz_stop_at is not None and time.monotonic() >= ptz_stop_at:
                        ptz_stop_index = session.sequence
                        session.send(0x1001, b"\0" * 8)
                        ptz_stop_at = None
                    if ptz_stop_index is not None and ptz_stop_index not in session.pending:
                        self.control_results.put((0x1001, True))
                        ptz_stop_index = None
                    for channel, data in media:
                        frames = video.feed(data) if channel == 2 else audio.feed(data)
                        for codec, flags, payload in frames:
                            if codec == 80:
                                if flags & 1:
                                    first_keyframe = True
                                if first_keyframe:
                                    self.video_queue.put(payload, timeout=1)
                                    last_video = time.monotonic()
                            else:
                                self.audio_queue.put(payload, timeout=1)
                    if time.monotonic() - last_video > 12:
                        raise OSError("The iCam365 native stream stopped producing video.")
        except (OSError, ValueError, queue.Full) as ex:
            if not self.stopped.is_set():
                self.error = str(ex) if isinstance(ex, OSError) else "The iCam365 native stream disconnected."
                LOG.warning("iCam365 native stream disconnected: %s", self.error)
        finally:
            self.stopped.set()
            if session is not None:
                if session.authenticated and ptz_stop_at is not None:
                    try:
                        session.send(0x1001, b"\0" * 8)
                    except OSError:
                        pass
                session.close()
            if self.process is not None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=3)
            for thread in threads:
                thread.join(timeout=2)
            if self.process is not None and self.process.stdout is not None:
                self.process.stdout.close()
            self.server.shutdown()
            self.server.server_close()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=45)
        self.server_thread.join(timeout=2)


_BRIDGES: dict[str, NativeBridge] = {}
_BRIDGE_LOCK = threading.Lock()


def get_bridge(uid: str) -> NativeBridge | None:
    with _BRIDGE_LOCK:
        bridge = _BRIDGES.get(uid)
        if bridge is not None:
            return bridge
        config = load_config(uid)
        if config is None:
            return None
        bridge = NativeBridge(config)
        _BRIDGES[uid] = bridge
        return bridge


def close_bridge(uid: str) -> None:
    with _BRIDGE_LOCK:
        bridge = _BRIDGES.pop(uid, None)
    if bridge is not None:
        bridge.close()


@dataclass(frozen=True)
class NativeRecording:
    start: datetime
    duration: int
    event: int
    size: int = 0

    @property
    def end(self) -> datetime:
        return self.start + timedelta(seconds=self.duration)

    @property
    def detection(self) -> bool:
        return self.event != 0

    @property
    def key(self) -> str:
        return f"{CAMERA_RECORDING_PREFIX}{self.start:%Y%m%d%H%M%S}-{self.event}"


def time_day(moment: datetime) -> bytes:
    return TIME_DAY.pack(moment.year, moment.month, moment.day, moment.isoweekday() % 7,
                         moment.hour, moment.minute, moment.second)


def recording_list_request(start: datetime, end: datetime) -> bytes:
    return time_day(start) + time_day(end) + struct.pack("<IB3x", 0, 0)


def decode_recording_list(payload: bytes) -> tuple[list[NativeRecording], int, int, bool]:
    if len(payload) < RECORDING_LIST_HEADER.size:
        raise ValueError(RECORDING_LIST_ERROR)
    _, total, _, last, count, entry_type = RECORDING_LIST_HEADER.unpack_from(payload)
    size = RECORDING_ENTRY_SIZES.get(entry_type)
    if size is None or len(payload) < RECORDING_LIST_HEADER.size + count * size:
        raise ValueError(RECORDING_LIST_ERROR)
    recordings = []
    for offset in range(RECORDING_LIST_HEADER.size, RECORDING_LIST_HEADER.size + count * size, size):
        year, month, day, _, hour, minute, second = TIME_DAY.unpack_from(payload, offset)
        duration, event = RECORDING_ENTRY.unpack_from(payload, offset + TIME_DAY.size)
        start = datetime(year, month, day, hour, minute, second)
        if 0 < duration <= MAX_RECORDING_SECONDS:
            recordings.append(NativeRecording(start, duration, event))
    return recordings, count, total, bool(last)


def play_control(av_index: int, command: int, parameter: int = 0, moment: datetime | None = None) -> bytes:
    return (struct.pack("<III", av_index, command, parameter)
            + (time_day(moment) if moment is not None else bytes(TIME_DAY.size)) + struct.pack("<I", 0))


def mpeg_crc32(data: bytes) -> int:
    crc = 0xFFFFFFFF
    for byte in data:
        crc ^= byte << 24
        for _ in range(8):
            crc = ((crc << 1) ^ 0x04C11DB7 if crc & 0x80000000 else crc << 1) & 0xFFFFFFFF
    return crc


def program_section(table_id: int, table_id_extension: int, body: bytes) -> bytes:
    section = struct.pack(">BHHBBB", table_id, 0xB000 | (len(body) + 9), table_id_extension, 0xC1, 0, 0) + body
    return section + struct.pack(">I", mpeg_crc32(section))


def presentation_time(value: int) -> bytes:
    return bytes([
        0x21 | ((value >> 29) & 0x0E), (value >> 22) & 0xFF, 0x01 | ((value >> 14) & 0xFE),
        (value >> 7) & 0xFF, 0x01 | ((value << 1) & 0xFE),
    ])


class TransportStreamWriter:
    def __init__(self) -> None:
        self.continuity: dict[int, int] = {}
        self.tables = (
            program_section(0x00, 1, struct.pack(">HH", TS_PROGRAM_NUMBER, 0xE000 | TS_PMT_PID)),
            program_section(0x02, TS_PROGRAM_NUMBER, struct.pack(
                ">HHBHH", 0xE000 | TS_VIDEO_PID, 0xF000, TS_HEVC_STREAM_TYPE, 0xE000 | TS_VIDEO_PID, 0xF000,
            )),
        )
        self.tables_written = False

    def video(self, stream_time: float, payload: bytes, keyframe: bool) -> bytes:
        output = bytearray()
        if keyframe or not self.tables_written:
            for pid, section in zip((TS_PAT_PID, TS_PMT_PID), self.tables):
                output += self._packets(pid, b"\0" + section + b"\xff" * (TS_PAYLOAD_SIZE - 1 - len(section)), True)
            self.tables_written = True
        pts = round((stream_time + TS_START_SECONDS) * TS_CLOCK_RATE)
        header = b"\0\0\1" + bytes([TS_VIDEO_STREAM_ID]) + b"\0\0\x80\x80\x05" + presentation_time(pts)
        clock = struct.pack(">IH", ((pts - TS_CLOCK_RATE // 10) >> 1) & 0xFFFFFFFF, (((pts - TS_CLOCK_RATE // 10) & 1) << 15) | 0x7E00)
        output += self._packets(TS_VIDEO_PID, header + payload, True, (0x50 if keyframe else 0x10).to_bytes(1, "big") + clock)
        return bytes(output)

    def _packets(self, pid: int, data: bytes, start: bool, adaptation: bytes = b"") -> bytes:
        output = bytearray()
        offset = 0
        while offset < len(data):
            fields = adaptation if start and offset == 0 else b""
            room = TS_PAYLOAD_SIZE - (1 + len(fields) if fields else 0)
            chunk = data[offset:offset + room]
            stuffing = room - len(chunk)
            if stuffing and fields:
                fields += b"\xff" * stuffing
            elif stuffing:
                fields = b"\0" + b"\xff" * (stuffing - 2) if stuffing > 1 else b""
            adaptation_field = bytes([len(fields)]) + fields if fields or stuffing else b""
            counter = self.continuity.get(pid, 0)
            self.continuity[pid] = (counter + 1) % 16
            output += bytes([
                TS_SYNC_BYTE, (0x40 if start and offset == 0 else 0) | (pid >> 8), pid & 0xFF,
                (0x30 if adaptation_field else 0x10) | counter,
            ]) + adaptation_field + chunk
            offset += len(chunk)
        return bytes(output)


class PlaybackStream:
    def __init__(self, replay: NativeReplay, output: Path | None = None, until: datetime | None = None) -> None:
        self.replay = replay
        self.until = until.timestamp() if until is not None else None
        self.speed_supported = replay.speed_supported
        self.error: str | None = None
        self.returncode: int | None = None
        self.stopped = threading.Event()
        self.done = threading.Event()
        self.lock = threading.Lock()
        self.writer = TransportStreamWriter()
        self.first_timestamp: int | None = None
        self.time_offset = 0.0
        self.last_time = 0.0
        self.audio_position = 0
        self.input_ended = False
        self.held: list[tuple[queue.Queue, bytes]] = []
        self.held_bytes = 0
        self.samples: deque[tuple[float, float]] = deque(maxlen=PLAYBACK_POSITION_SAMPLES)
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_lock = threading.Lock()
        self.video_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=PLAYBACK_QUEUE_FRAMES)
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=PLAYBACK_QUEUE_FRAMES)
        self.process: subprocess.Popen | None = None
        self.server: ThreadingHTTPServer | None = None
        self.server_thread: threading.Thread | None = None
        self.url = ""
        self.threads: list[threading.Thread] = []
        try:
            if output is None:
                self.server, self.server_thread, self.url = start_media_server(self)
            self.process, self.threads = start_media_muxer(
                self.video_queue, self.audio_queue, self.stopped, self._muxer_failed,
                PLAYBACK_VIDEO_INPUT, PLAYBACK_AUDIO_INPUT, (),
                LIVE_MEDIA_OUTPUT_OPTIONS if output is None else ("-f", "matroska", "-y", str(output)),
                subprocess.PIPE if output is None else subprocess.DEVNULL,
            )
            watcher = threading.Thread(target=self._watch_muxer, daemon=True)
            watcher.start()
            self.threads.append(watcher)
        except OSError:
            self.close()
            raise

    def add(self, video: bool, timestamp: int, content: float, payload: bytes, keyframe: bool = False) -> None:
        with self.lock:
            if self.input_ended or self.stopped.is_set():
                return
            if video:
                stream_time = self._stream_time(timestamp)
                self.samples.append((stream_time, content))
                self._output(self.video_queue, self.writer.video(stream_time, payload, keyframe))
                self._pad_audio(round((stream_time - AUDIO_LAG_SECONDS) * AAC_SAMPLE_RATE))
            elif self.first_timestamp is not None:
                start = round((timestamp - self.first_timestamp) / 1000 * AAC_SAMPLE_RATE + self.time_offset * AAC_SAMPLE_RATE)
                if start > self.audio_position + AUDIO_PLACEMENT_SAMPLES:
                    self._pad_audio(start)
                if start + len(payload) >= self.audio_position - AUDIO_PLACEMENT_SAMPLES:
                    self._output(self.audio_queue, payload)
                    self.audio_position += len(payload)

    def finish(self) -> None:
        with self.lock:
            if self.input_ended or self.stopped.is_set():
                return
            self.input_ended = True
            self._flush()
            for source in (self.video_queue, self.audio_queue):
                self._put(source, None)

    def fail(self, message: str) -> None:
        if self.error is None and not self.stopped.is_set():
            self.error = message
        self.stopped.set()

    def set_speed(self, speed: int) -> None:
        if self.speed_supported and speed in PLAYBACK_SPEED_PARAMETERS:
            self.replay.request("speed", self, speed)

    def set_paused(self, paused: bool) -> None:
        if paused:
            self.replay.request("pause", self)

    def moment(self, position: float) -> datetime | None:
        with self.lock:
            samples = list(self.samples)
        if not samples:
            return None
        index = max(0, bisect.bisect_right(samples, (position, float("inf"))) - 1)
        stream_time, content = samples[index]
        return datetime.fromtimestamp(content + min(max(0.0, position - stream_time), PLAYBACK_DISCONTINUITY_SECONDS))

    def close(self) -> None:
        self.replay.request("close", self)
        self.stopped.set()
        if self.process is not None and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=MUXER_STOP_TIMEOUT)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=MUXER_STOP_TIMEOUT)
        for thread in self.threads:
            thread.join(timeout=2)
        if self.process is not None and self.process.stdout is not None:
            self.process.stdout.close()
        if self.server is not None:
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)

    def _stream_time(self, timestamp: int) -> float:
        if self.first_timestamp is None:
            self.first_timestamp = timestamp
        stream_time = (timestamp - self.first_timestamp) / 1000 + self.time_offset
        if self.samples and not 0 <= stream_time - self.last_time <= PLAYBACK_DISCONTINUITY_SECONDS:
            self.time_offset += self.last_time + PLAYBACK_FRAME_SECONDS - stream_time
            stream_time = self.last_time + PLAYBACK_FRAME_SECONDS
        self.last_time = stream_time
        return stream_time

    def _pad_audio(self, position: int) -> None:
        while self.audio_position < position:
            size = min(position - self.audio_position, AUDIO_PADDING_SAMPLES)
            self._output(self.audio_queue, ALAW_SILENCE * size)
            self.audio_position += size

    def _output(self, source: queue.Queue, data: bytes) -> None:
        if self.server is not None and not self.subscribers:
            self.held.append((source, data))
            self.held_bytes += len(data)
            if self.held_bytes > MAX_PLAYBACK_BUFFER:
                self.fail("The recording player did not connect.")
            return
        self._flush()
        self._put(source, data)

    def _flush(self) -> None:
        for source, data in self.held:
            self._put(source, data)
        self.held.clear()
        self.held_bytes = 0

    def _put(self, source: queue.Queue, data: bytes | None) -> None:
        try:
            source.put_nowait(data)
        except queue.Full:
            self.fail("The iCam365 recording muxer stopped reading.")

    def _muxer_failed(self) -> None:
        if not self.stopped.is_set():
            self.fail(MUXER_STOPPED_MESSAGE)

    def _watch_muxer(self) -> None:
        if self.server is not None:
            broadcast_media(self.process, self.stopped, self.subscribers, self.subscriber_lock)
        self.returncode = self.process.wait()
        if not self.input_ended:
            self._muxer_failed()
        self.stopped.set()
        self.done.set()


class ActivePlayback:
    def __init__(self, stream: PlaybackStream, av_index: int) -> None:
        self.stream = stream
        self.av_index = av_index
        self.video = MediaFrames((VIDEO_CODEC,))
        self.audio = MediaFrames((AUDIO_CODEC,))
        self.started = time.monotonic()
        self.sync: int | None = None
        self.base: tuple[int, int] | None = None
        self.keyframe = False
        self.last_video: float | None = None

    def content_time(self, timestamp: int) -> float:
        return self.base[0] + (timestamp - self.base[1]) / 1000


class RecordingListing:
    def __init__(self, reply: queue.Queue) -> None:
        self.reply = reply
        self.recordings: list[NativeRecording] = []
        self.received = 0
        self.deadline = time.monotonic() + RECORDING_LIST_TIMEOUT


class NativeReplay:
    def __init__(self, uid: str) -> None:
        self.uid = uid
        self.ready = threading.Event()
        self.stopped = threading.Event()
        self.error: str | None = None
        self.speed_supported = False
        self.requests: queue.Queue[tuple] = queue.Queue()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, *request) -> None:
        self.requests.put(request)

    def recordings(self, start: datetime, end: datetime) -> list[NativeRecording]:
        self._wait_ready()
        reply: queue.Queue[list[NativeRecording] | str] = queue.Queue(maxsize=1)
        self.request("list", start, end, reply)
        deadline = time.monotonic() + REPLAY_REQUEST_TIMEOUT
        while True:
            try:
                result = reply.get(timeout=PLAYBACK_WAIT_SECONDS)
                break
            except queue.Empty:
                if self.stopped.is_set() or time.monotonic() > deadline:
                    raise OSError(self.error or RECORDING_LIST_ERROR) from None
        if isinstance(result, str):
            raise OSError(result)
        return result

    def play(self, moment: datetime, output: Path | None = None, until: datetime | None = None,
             speed: int = 1) -> PlaybackStream:
        self._wait_ready()
        stream = PlaybackStream(self, output, until)
        self.request("play", moment, stream, speed if self.speed_supported and speed in PLAYBACK_SPEED_PARAMETERS else 1)
        return stream

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=REPLAY_CLOSE_TIMEOUT)

    def _wait_ready(self) -> None:
        deadline = time.monotonic() + REPLAY_CONNECT_TIMEOUT
        while not self.ready.is_set() and not self.stopped.is_set() and time.monotonic() < deadline:
            self.ready.wait(PLAYBACK_WAIT_SECONDS)
        if self.stopped.is_set() or not self.ready.is_set():
            raise OSError(self.error or RECORDINGS_UNAVAILABLE_MESSAGE)

    def _connect(self) -> NativeSession:
        config = load_config(self.uid)
        if config is None:
            raise OSError("The iCam365 connection is not configured.")
        refreshed = False
        while True:
            session = NativeSession(config, PLAYBACK_CHANNELS)
            session.stop_commands = ()
            session.open()
            try:
                accepted = self._authenticate(session)
            except BaseException:
                session.close()
                raise
            if accepted:
                return session
            session.close()
            if refreshed or config.cloud_session is None:
                raise OSError("The iCam365 camera rejected authentication.")
            config = config.refreshed()
            refreshed = True

    def _authenticate(self, session: NativeSession) -> bool:
        deadline = time.monotonic() + REPLAY_AUTHENTICATION_TIMEOUT
        features_deadline = None
        while not self.stopped.is_set():
            now = time.monotonic()
            if features_deadline is not None and now > features_deadline:
                return True
            if now > deadline:
                raise OSError("The iCam365 camera did not authenticate.")
            _, commands = session.receive()
            for command, payload in commands:
                if command == AUTHENTICATION_RESPONSE_COMMAND:
                    if len(payload) < 4 or struct.unpack_from("<i", payload)[0] != 0:
                        return False
                    session.authenticated = True
                    session.send(FEATURES_REQUEST_COMMAND)
                    features_deadline = now + REPLAY_FEATURES_TIMEOUT
                elif command == FEATURES_RESPONSE_COMMAND:
                    try:
                        instructions = str(camera_features(payload).get("ExtInstructions", "")).split(";")
                    except (ValueError, AttributeError):
                        instructions = []
                    self.speed_supported = PLAYBACK_RATE_FEATURE in instructions
                    return session.authenticated
        raise OSError(RECORDINGS_UNAVAILABLE_MESSAGE)

    def _run(self) -> None:
        session = None
        playback: ActivePlayback | None = None
        listing: RecordingListing | None = None
        lists: deque[tuple[datetime, datetime, queue.Queue]] = deque()
        next_av = 0
        try:
            session = self._connect()
            self.ready.set()
            while not self.stopped.is_set():
                media, commands = session.receive()
                now = time.monotonic()
                for command, payload in commands:
                    if command == LIST_RECORDINGS_RESPONSE and listing is not None:
                        try:
                            recordings, count, total, last = decode_recording_list(payload)
                        except ValueError as ex:
                            listing.reply.put(str(ex))
                            listing = None
                            continue
                        listing.recordings.extend(recordings)
                        listing.received += count
                        if last or listing.received >= total:
                            listing.reply.put(sorted(listing.recordings, key=lambda recording: recording.start))
                            listing = None
                    elif command == GENERIC_RESPONSE_COMMAND and len(payload) >= 8 and listing is not None:
                        request, result = struct.unpack_from("<Ii", payload)
                        if request == LIST_RECORDINGS_COMMAND and result != 0:
                            listing.reply.put(RECORDING_LIST_ERROR)
                            listing = None
                    elif command == PLAY_CONTROL_RESPONSE and len(payload) >= 8 and playback is not None:
                        control, result = struct.unpack_from("<Ii", payload)
                        if control == PLAY_START and result < 0:
                            playback.stream.fail(NO_RECORDING_MESSAGE)
                            playback = self._stop(session, playback)
                while True:
                    try:
                        request = self.requests.get_nowait()
                    except queue.Empty:
                        break
                    if request[0] == "list":
                        lists.append(request[1:])
                    elif request[0] == "play":
                        _, moment, stream, speed = request
                        if playback is not None:
                            playback = self._stop(session, playback)
                        if stream.stopped.is_set():
                            continue
                        av_index = PLAYBACK_AV_INDEXES[next_av]
                        next_av = (next_av + 1) % len(PLAYBACK_AV_INDEXES)
                        session.send(PLAY_CONTROL_COMMAND, play_control(av_index, PLAY_START, 0, moment))
                        if speed != 1:
                            session.send(PLAY_CONTROL_COMMAND, play_control(
                                av_index, PLAY_FORWARD, PLAYBACK_SPEED_PARAMETERS[speed],
                            ))
                        session.stop_commands = ((PLAY_CONTROL_COMMAND, play_control(av_index, PLAY_STOP)),)
                        playback = ActivePlayback(stream, av_index)
                    elif playback is not None and request[1] is playback.stream:
                        if request[0] == "speed":
                            session.send(PLAY_CONTROL_COMMAND, play_control(
                                playback.av_index, PLAY_FORWARD, PLAYBACK_SPEED_PARAMETERS[request[2]],
                            ))
                        else:
                            playback = self._stop(session, playback)
                if playback is not None:
                    try:
                        playback = self._deliver(session, playback, media, now)
                    except OSError as ex:
                        playback.stream.fail(str(ex))
                        playback = self._stop(session, playback)
                if playback is not None:
                    if playback.last_video is None and now - playback.started > PLAYBACK_START_TIMEOUT:
                        playback.stream.fail(NO_RECORDING_MESSAGE)
                        playback = self._stop(session, playback)
                    elif playback.last_video is not None and now - playback.last_video > PLAYBACK_IDLE_TIMEOUT:
                        playback.stream.finish()
                        playback = self._stop(session, playback)
                if listing is not None and now > listing.deadline:
                    raise OSError(RECORDING_LIST_ERROR)
                if listing is None and lists:
                    start, end, reply = lists.popleft()
                    listing = RecordingListing(reply)
                    session.send(LIST_RECORDINGS_COMMAND, recording_list_request(start, end))
        except (OSError, ValueError) as ex:
            if not self.stopped.is_set():
                self.error = str(ex) if isinstance(ex, OSError) else RECORDINGS_UNAVAILABLE_MESSAGE
                LOG.warning("iCam365 recording connection failed: %s", self.error)
        finally:
            self.stopped.set()
            message = self.error or RECORDINGS_UNAVAILABLE_MESSAGE
            if playback is not None:
                playback.stream.fail(message)
            if listing is not None:
                listing.reply.put(message)
            for _, _, reply in lists:
                reply.put(message)
            while True:
                try:
                    request = self.requests.get_nowait()
                except queue.Empty:
                    break
                if request[0] == "list":
                    request[3].put(message)
                elif request[0] == "play":
                    request[2].fail(message)
            if session is not None:
                session.close()

    @staticmethod
    def _stop(session: NativeSession, playback: ActivePlayback) -> None:
        session.send(PLAY_CONTROL_COMMAND, play_control(playback.av_index, PLAY_STOP))
        session.stop_commands = ()

    def _deliver(
        self, session: NativeSession, playback: ActivePlayback, media: list[tuple[int, bytes]], now: float,
    ) -> ActivePlayback | None:
        stream = playback.stream
        for channel, data in media:
            if channel not in (playback.av_index, playback.av_index + 1):
                continue
            video = channel == playback.av_index
            for frame in (playback.video if video else playback.audio).feed_timed(data):
                if frame.codec == 0:
                    if video and frame.flags == SYNC_MESSAGE_TYPE:
                        playback.sync = frame.timestamp
                    continue
                if video:
                    if playback.sync is not None:
                        playback.base = (playback.sync, frame.timestamp)
                        playback.sync = None
                    if playback.base is None or not (playback.keyframe or frame.flags & KEY_FRAME_FLAG):
                        continue
                    playback.keyframe = True
                    playback.last_video = now
                    content = playback.content_time(frame.timestamp)
                    if stream.until is not None and content >= stream.until:
                        stream.finish()
                        return self._stop(session, playback)
                    stream.add(True, frame.timestamp, content, frame.payload, bool(frame.flags & KEY_FRAME_FLAG))
                elif playback.keyframe:
                    stream.add(False, frame.timestamp, playback.content_time(frame.timestamp), frame.payload)
        return playback


def saved_camera_uid(camera_name: str) -> str:
    from PyQt6.QtCore import QSettings
    settings = QSettings("O-KAM Linux", "O-KAM Linux")
    cameras = json.loads(settings.value("cameras/rtsp", "[]", str))
    matches = [camera for camera in cameras if camera.get("name") == camera_name]
    if len(matches) != 1:
        raise OSError("The saved camera name must identify exactly one camera.")
    return matches[0]["uid"]


def import_device_response(response_path: Path, camera_name: str, session_path: Path | None = None) -> None:
    uid = saved_camera_uid(camera_name)
    if response_path.stat().st_size > MAX_FRAME_SIZE:
        raise OSError("The iCam365 device response is too large.")
    response = json.loads(response_path.read_text())
    items = response.get("data", {}).get("items", [])
    if len(items) != 1:
        raise OSError("Import a response containing exactly one iCam365 device.")
    record = {key: items[0][key] for key in ("p2p_id", "p2p_platform", "password")}
    if session_path is not None:
        if session_path.stat().st_size > MAX_FRAME_SIZE:
            raise OSError("The iCam365 account session is too large.")
        session_record = json.loads(session_path.read_text())
        cloud_session = CloudSession.from_record(session_record)
        if cloud_session.uuid != items[0].get("uuid"):
            raise OSError("The iCam365 session identifies a different camera.")
        record["cloud_session"] = session_record
    config = NativeConfig.from_record(record)
    if not decode_init_string(config.platform, lut=_DECODE_LOOKUP).lib_ok:
        raise OSError("Invalid iCam365 directory configuration.")
    with _CONFIG_LOCK:
        records = read_records()
        previous = records.get(uid, {})
        if session_path is None and isinstance(previous, dict) and previous.get("cloud_session") is not None:
            cloud_session = CloudSession.from_record(previous["cloud_session"])
            if cloud_session.uuid == items[0].get("uuid"):
                record["cloud_session"] = previous["cloud_session"]
        records[uid] = record
        write_records(records)


def sign_in(camera_name: str, username: str, password: str, area_code: str = DEFAULT_AREA_CODE) -> None:
    config = load_config(saved_camera_uid(camera_name))
    if config is None or config.cloud_session is None:
        raise OSError("Import the camera with an iCam365 account session first.")
    session = CloudSession.from_record(
        config.cloud_session.record() | {"username": username, "area_code": area_code}
    ).signed_in(password)
    renewed = config.with_device_record(session.device_record(), session)
    if not save_account_password(username, password):
        raise OSError("Unable to save the iCam365 account password in the desktop keyring.")
    store_config(renewed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Import private iCam365 native connection parameters.")
    parser.add_argument("--camera-name", required=True)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--device-response", type=Path)
    source.add_argument("--sign-in", metavar="ACCOUNT")
    parser.add_argument("--cloud-session", type=Path)
    parser.add_argument("--area-code", default=DEFAULT_AREA_CODE)
    arguments = parser.parse_args()
    if arguments.sign_in is not None:
        try:
            sign_in(arguments.camera_name, arguments.sign_in, getpass.getpass("iCam365 password: "),
                    arguments.area_code)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            parser.exit(1, "Unable to sign in to the iCam365 account.\n")
        print("iCam365 account session saved.")
    else:
        try:
            import_device_response(arguments.device_response, arguments.camera_name, arguments.cloud_session)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            parser.exit(1, "Unable to import the iCam365 connection parameters.\n")
        print("Private iCam365 connection parameters saved.")
