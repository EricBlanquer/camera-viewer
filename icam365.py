from __future__ import annotations

import argparse
import dataclasses
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
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

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
QUALITY_COMMAND = 0x320
MAIN_STREAM_QUALITY = 1
SECONDARY_STREAM_QUALITY = 5
PTZ_COMMAND = 0x1001
PTZ_POSITION_COMMAND = 0x0408
PRESETS_REQUEST_COMMAND = 0x0452
PRESETS_RESPONSE_COMMAND = 0x0453
NATIVE_PTZ_DIRECTIONS = {"Up": 1, "Down": 2, "Left": 3, "Right": 6}
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


class MediaFrames:
    def __init__(self, codecs: tuple[int, ...]) -> None:
        self.codecs = codecs
        self.buffer = bytearray()

    def feed(self, data: bytes) -> list[tuple[int, int, bytes]]:
        self.buffer.extend(data)
        frames = []
        while len(self.buffer) >= 16:
            codec, flags = self.buffer[0], self.buffer[2]
            size = struct.unpack_from("<I", self.buffer, 8)[0]
            if codec not in (0, *self.codecs) or size > MAX_FRAME_SIZE or (codec == 0 and size != 0):
                raise OSError("Invalid iCam365 media frame.")
            if len(self.buffer) < 16 + size:
                break
            if codec:
                frames.append((codec, flags, bytes(self.buffer[16:16 + size])))
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
    def __init__(self, config: NativeConfig) -> None:
        decoded = decode_init_string(config.platform, lut=_DECODE_LOOKUP)
        if not decoded.lib_ok:
            raise OSError("Invalid iCam365 directory configuration.")
        configure_tables(prop_table=_SHUFFLE)
        self.config = config
        self.session = NativePpppSession(real_did=config.did, servers=decoded.servers)
        self.channels = {index: OrderedChannel() for index in range(3)}
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
        self.send(0x8024)
        self.send(0x8012, b"\0" * 8)
        self.send(0x1FF, struct.pack("<II", 2, 0))
        self.send(QUALITY_COMMAND, stream_quality(main_stream))
        self.send(0x300, struct.pack("<II", 1, 0))

    def close(self) -> None:
        try:
            if self.authenticated and self.session.peer:
                self.send(0x2FF, struct.pack("<II", 2, 0))
                self.send(0x301, struct.pack("<II", 1, 0))
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

    def _write_media(self, fd: int, source: queue.Queue) -> None:
        try:
            with os.fdopen(fd, "wb", buffering=0) as output:
                while not self.stopped.is_set():
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
            if not self.stopped.is_set():
                self.error = "The iCam365 media muxer stopped."
                self.stopped.set()

    def _broadcast(self) -> None:
        broadcast_media(self.process, self.stopped, self.subscribers, self.subscriber_lock)
        if not self.stopped.is_set():
            self.error = "The iCam365 media muxer stopped."
            self.stopped.set()

    def _start_muxer(self) -> list[threading.Thread]:
        video_read, video_write = os.pipe()
        audio_read, audio_write = os.pipe()
        try:
            self.process = subprocess.Popen([
                "ffmpeg", "-nostdin", "-nostats", "-loglevel", "error",
                "-thread_queue_size", "512", "-probesize", "100000", "-analyzeduration", "1000000",
                "-use_wallclock_as_timestamps", "1", "-r", "8", "-f", "hevc", "-i", f"/proc/self/fd/{video_read}",
                "-thread_queue_size", "512", "-use_wallclock_as_timestamps", "1",
                "-f", "alaw", "-ar", "8000", "-ac", "1", "-i", f"/proc/self/fd/{audio_read}",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy",
                "-af", LIVE_AUDIO_CLOCK_FILTER, "-c:a", "aac", "-b:a", "32k",
                "-output_ts_offset", str(AAC_FRAME_SAMPLES / AAC_SAMPLE_RATE),
                *LIVE_MEDIA_OUTPUT_OPTIONS,
            ], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                pass_fds=(video_read, audio_read), bufsize=0)
        except OSError:
            os.close(video_write)
            os.close(audio_write)
            raise
        finally:
            os.close(video_read)
            os.close(audio_read)
        threads = [
            threading.Thread(target=self._write_media, args=(video_write, self.video_queue), daemon=True),
            threading.Thread(target=self._write_media, args=(audio_write, self.audio_queue), daemon=True),
            threading.Thread(target=self._broadcast, daemon=True),
        ]
        for thread in threads:
            thread.start()
        return threads

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
                    if command == 0x8003:
                        if len(payload) < 4 or struct.unpack_from("<i", payload)[0] != 0:
                            raise OSError(refresh_error or "The iCam365 camera rejected authentication.")
                        if not session.authenticated:
                            session.start_media(self.main_stream)
                            threads = self._start_muxer()
                            last_video = time.monotonic()
                    elif command == 0x8025:
                        try:
                            features = json.loads(payload.rstrip(b"\0")).get("feature", {})
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
