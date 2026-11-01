from __future__ import annotations

import ipaddress
import json
import logging
import os
import queue
import re
import secrets
import socket
import struct
import subprocess
import tempfile
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from hardware import disable_hardware_encode, hardware_encode_device
from icam365 import broadcast_media, start_media_server, write_media


CONFIG_PATH = Path.home() / ".config/camera-viewer/v380.json"
DEFAULT_PORT = 8800
DEFAULT_USERNAME = "admin"
DISCOVERY_PORT = 10009
DISCOVERY_REPLY_PORT = 10008
DISCOVERY_COMMAND = b"NVDEVSEARCH^100"
DISCOVERY_RESULT = "NVDEVRESULT"
STATIC_PASSWORD_KEY = b"macrovideo+*#!^@"
PASSWORD_ALPHABET = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
PASSWORD_LENGTH = 48
RANDOM_KEY_LENGTH = 16
MEDIA_KEY_MIDDLE = 0x618123462C14795C
MEDIA_KEY_TAIL = 0x82800DF0
ENCRYPTED_VERSION = 30
LOGIN_COMMAND = 1167
LOGIN_RESPONSE = 1168
STREAM_LOGIN_COMMAND = 301
STREAM_LOGIN_RESPONSE = 401
STREAM_START_COMMAND = 303
STREAM_START_FLAG = 0x3001
LOGIN_SUCCESS = 1001
LOGIN_ERRORS = {1011: "Invalid camera username.", 1012: "Invalid camera password.", 1018: "Invalid camera device id."}
LOGIN_UNKNOWN1_LAN = 120
LOGIN_VERSION = 31
AUDIO_ENABLED = 4097
MAIN_STREAM_QUALITY = 1
SECONDARY_STREAM_QUALITY = 0
FRAME_HEADER_SIZE = 12
FRAME_MARKER = 0x7F
MEDIA_HEADER_SIZE = 16
MAX_PAYLOAD_SIZE = 20000
AUDIO_TYPES = (0x1A, 0x16)
CONTROL_TYPE = 0x5B
AUDIO_HEADER_SIZE = 16
CRYPT_STRIDE = 80
CRYPT_BLOCK = 64
AES_BLOCK = 16
SOCKET_TIMEOUT = 5
AUTH_TIMEOUT = 5
VIDEO_STALL_SECONDS = 12
RETRY_SECONDS = 3
PTZ_PULSE_SECONDS = 0.6

PTZ_COMMANDS = {
    "Right": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xEA, 3, 0xE8, 3, 0, 0, 1, 0]),
    "Left": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE9, 3, 0xE8, 3, 0, 0, 1, 0]),
    "Up": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xEB, 3, 0, 0, 1, 0]),
    "Down": bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xEC, 3, 0, 0, 1, 0]),
}
PTZ_STOP = bytes([0xAA, 0, 0, 0, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0xE8, 3, 0, 0, 1, 0])
LIGHT_COMMANDS = {
    "on": bytes([0xC4, 0, 0, 0, 0xE9, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "off": bytes([0xC4, 0, 0, 0, 0xEA, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
    "auto": bytes([0xC4, 0, 0, 0, 0xEB, 3, 0, 0, 0, 1, 0, 0, 0, 0, 0, 0]),
}

LIVE_VIDEO_INPUT = ("-probesize", "500000", "-analyzeduration", "2000000", "-use_wallclock_as_timestamps", "1", "-f", "hevc")
LIVE_AUDIO_INPUT = ("-use_wallclock_as_timestamps", "1", "-f", "alaw", "-ar", "8000", "-ac", "1")
LIVE_AUDIO_CLOCK_FILTER = "aresample=async=1000"
# Same reason as the iCam365 bridge: a player that joins after the parameter
# sets went by cannot decode a single frame, so write them before every
# keyframe instead of once per session.
LIVE_MEDIA_OUTPUT_OPTIONS = (
    "-max_interleave_delta", "100000", "-muxdelay", "0", "-muxpreload", "0",
    "-bsf:v", "dump_extra=freq=keyframe",
    "-mpegts_flags", "resend_headers", "-flush_packets", "1", "-f", "mpegts", "pipe:1",
)
AAC_SAMPLE_RATE = 8000
AAC_FRAME_SAMPLES = 1024
MUXER_STOPPED_MESSAGE = "The V380 media muxer stopped."
LENS_COUNT = 3
MAX_FRAME_BYTES = 8 * 1024 * 1024
RELAY_FPS = 12
RELAY_WIDTH = 640
RELAY_HEIGHT = 360
RELAY_GOP = "24"
RELAY_BITRATE = "700k"
RELAY_MAXRATE = "900k"
RELAY_BUFFER = "1200k"
RELAY_START_DELAY_SECONDS = 10
RELAY_RETRY_SECONDS = 5
RELAY_MAX_RETRY_SECONDS = 300
RELAY_STABLE_SECONDS = 20
CONTINUOUS_HOST_GRACE_SECONDS = 30
RELAY_PATH_PATTERN = re.compile(r"/[A-Za-z0-9_/-]{1,64}")

LOG = logging.getLogger("okam-linux.v380")
_CONFIG_LOCK = threading.Lock()


def _is_local_address(address: str) -> bool:
    try:
        host = ipaddress.ip_address(address)
    except ValueError:
        return False
    return host.is_private and not (host.is_loopback or host.is_multicast or host.is_unspecified)


def valid_relay_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    try:
        parsed.port
    except ValueError:
        return False
    return (parsed.scheme == "rtsp" and parsed.hostname is not None and _is_local_address(parsed.hostname)
            and parsed.username is None and parsed.password is None and not parsed.query
            and not parsed.fragment and RELAY_PATH_PATTERN.fullmatch(parsed.path) is not None)


def relay_command(source: str, relay_url: str, lenses: int = LENS_COUNT,
                  device: str | None = None) -> list[str]:
    outputs = "".join(f"[s{lens}]" for lens in range(lenses))
    upload = ",format=nv12,hwupload" if device else ""
    crops = ";".join(
        f"[s{lens}]crop=iw:ih/{lenses}:0:{lens}*ih/{lenses},"
        f"scale={RELAY_WIDTH}:{RELAY_HEIGHT}{upload}[l{lens}]"
        for lens in range(lenses)
    )
    command = ["ffmpeg", "-nostdin", "-nostats", "-loglevel", "error"]
    if device:
        command += ["-vaapi_device", device]
    command += [
        "-hwaccel", "auto" if not device else "vaapi",
    ]
    if device:
        command += ["-hwaccel_output_format", "nv12"]
    command += [
        "-analyzeduration", "10000000", "-probesize", "50000000", "-i", source, "-an",
        "-filter_complex", f"[0:v]fps={RELAY_FPS},split={lenses}{outputs};{crops}",
    ]
    for lens in range(lenses):
        if device:
            encoder = ["-c:v", "h264_vaapi"]
        else:
            encoder = ["-c:v", "libx264", "-preset", "veryfast", "-tune", "zerolatency",
                       "-pix_fmt", "yuv420p"]
        command += [
            "-map", f"[l{lens}]", *encoder,
            "-g", RELAY_GOP, "-b:v", RELAY_BITRATE, "-maxrate", RELAY_MAXRATE,
            "-bufsize", RELAY_BUFFER, "-f", "rtsp", "-rtsp_transport", "tcp", f"{relay_url}{lens + 1}",
        ]
    return command


def _ecb_encrypt(key: bytes, data: bytes) -> bytes:
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(data) + encryptor.finalize()


def _ecb_decrypt(key: bytes, data: bytes) -> bytes:
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    return decryptor.update(data) + decryptor.finalize()


def password_blob(password: str) -> bytes:
    padded = password.encode()[:PASSWORD_LENGTH].ljust(PASSWORD_LENGTH, b"\0")
    random_key = bytes(secrets.choice(PASSWORD_ALPHABET) for _ in range(RANDOM_KEY_LENGTH))
    return random_key + _ecb_encrypt(random_key, _ecb_encrypt(STATIC_PASSWORD_KEY, padded))


def media_key(ticket: int) -> bytes:
    return struct.pack("<IQI", ticket, MEDIA_KEY_MIDDLE, MEDIA_KEY_TAIL)


def decrypt_media(key: bytes, payload: bytes) -> bytes:
    """Decrypt the encrypted CRYPT_BLOCK window of every CRYPT_STRIDE bytes.

    ECB has no chaining, so every 16-byte block is independent: the whole
    payload is decrypted in a single AES call instead of building one cipher
    per 64-byte window. The per-window version ran at about 3 MB/s and burned
    a full core per camera.
    """
    data = bytearray(payload)
    offsets = range(0, len(data) - CRYPT_BLOCK + 1, CRYPT_STRIDE)
    if not offsets:
        return bytes(data)

    gathered = b"".join(bytes(data[offset:offset + CRYPT_BLOCK]) for offset in offsets)
    decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
    clear = decryptor.update(gathered) + decryptor.finalize()

    for index, offset in enumerate(offsets):
        start = index * CRYPT_BLOCK
        data[offset:offset + CRYPT_BLOCK] = clear[start:start + CRYPT_BLOCK]
    return bytes(data)


@dataclass(frozen=True)
class V380Config:
    did: int
    port: int = DEFAULT_PORT
    username: str = DEFAULT_USERNAME
    address: str = ""
    relay_url: str = ""

    @classmethod
    def from_record(cls, record: dict) -> V380Config:
        did = record.get("did")
        port = record.get("port", DEFAULT_PORT)
        username = record.get("username", DEFAULT_USERNAME)
        address = record.get("address", "")
        relay_url = record.get("relay_url", "")
        if (not isinstance(did, int) or not 0 < did <= 0xFFFFFFFF
                or not isinstance(port, int) or not 1 <= port <= 65535
                or not isinstance(username, str) or not 1 <= len(username.encode()) <= 32
                or not isinstance(address, str) or (address and not _is_local_address(address))
                or not isinstance(relay_url, str) or (relay_url and not valid_relay_url(relay_url))):
            raise OSError("Invalid V380 connection configuration.")
        return cls(did, port, username, address, relay_url)

    def record(self) -> dict:
        record = {"did": self.did, "port": self.port, "username": self.username, "address": self.address}
        if self.relay_url:
            record["relay_url"] = self.relay_url
        return record


def _read_records() -> dict:
    if not CONFIG_PATH.exists():
        return {}
    try:
        if CONFIG_PATH.stat().st_mode & 0o077:
            raise OSError("The V380 connection file must be private (mode 600).")
        records = json.loads(CONFIG_PATH.read_text())
    except (ValueError, TypeError):
        raise OSError("Invalid V380 connection configuration.") from None
    if not isinstance(records, dict):
        raise OSError("Invalid V380 connection configuration.")
    return records


def _write_records(records: dict) -> None:
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary_path = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=CONFIG_PATH.parent, prefix=".v380-", delete=False) as output:
            temporary_path = Path(output.name)
            os.fchmod(output.fileno(), 0o600)
            json.dump(records, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_path, CONFIG_PATH)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def store_config(uid: str, config: V380Config) -> None:
    with _CONFIG_LOCK:
        records = _read_records()
        records[uid] = config.record()
        _write_records(records)


def load_config(uid: str) -> V380Config | None:
    record = _read_records().get(uid)
    try:
        return V380Config.from_record(record) if isinstance(record, dict) else None
    except (ValueError, TypeError, AttributeError):
        raise OSError("Invalid V380 connection configuration.") from None


def remove_config(uid: str) -> None:
    with _CONFIG_LOCK:
        records = _read_records()
        if records.pop(uid, None) is not None:
            _write_records(records)


@dataclass(frozen=True)
class DiscoveredCamera:
    did: int
    address: str
    mac: str


def discover(timeout: float = 1.5) -> list[DiscoveredCamera]:
    found: dict[str, DiscoveredCamera] = {}
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sock.bind(("", DISCOVERY_PORT))
    except OSError:
        sock.close()
        return []
    try:
        deadline = time.monotonic() + timeout
        sock.sendto(DISCOVERY_COMMAND, ("255.255.255.255", DISCOVERY_REPLY_PORT))
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data, source = sock.recvfrom(2048)
            except socket.timeout:
                break
            fields = data.decode("ascii", errors="replace").split("^")
            if len(fields) < 13 or fields[0] != DISCOVERY_RESULT:
                continue
            if fields[3] != source[0] or not _is_local_address(fields[3]):
                continue
            mac = fields[2]
            try:
                did = int(fields[12])
            except ValueError:
                continue
            if mac not in found:
                found[mac] = DiscoveredCamera(did, fields[3], mac)
    finally:
        sock.close()
    return list(found.values())


def _recv_exact(sock: socket.socket, size: int, timeout: float = SOCKET_TIMEOUT) -> bytes:
    sock.settimeout(timeout)
    data = bytearray()
    while len(data) < size:
        chunk = sock.recv(size - len(data))
        if not chunk:
            raise OSError("The V380 camera closed the connection.")
        data.extend(chunk)
    return bytes(data)


class V380Session:
    def __init__(self, config: V380Config, password: str, main_stream: bool = True) -> None:
        self.config = config
        self.password = password
        self.main_stream = main_stream
        self.socket: socket.socket | None = None
        self.ticket = 0
        self.version = 0
        self.width = 0
        self.height = 0

    def _resolve_address(self) -> str:
        if self.config.address:
            return self.config.address
        for camera in discover():
            if camera.did == self.config.did:
                return camera.address
        raise OSError("The V380 camera was not found on the local network.")

    def authenticate(self) -> None:
        address = self._resolve_address()
        auth = socket.create_connection((address, self.config.port), timeout=SOCKET_TIMEOUT)
        try:
            request = bytearray(520)
            struct.pack_into("<iIBII", request, 0, LOGIN_COMMAND, LOGIN_UNKNOWN1_LAN, LOGIN_VERSION, 1, self.config.did)
            request[49:81] = self.config.username.encode()[:32].ljust(32, b"\0")
            request[81:145] = password_blob(self.password)
            auth.sendall(request)
            response = _recv_exact(auth, 256, AUTH_TIMEOUT)
        finally:
            auth.close()
        command, result = struct.unpack_from("<ii", response)
        if command != LOGIN_RESPONSE:
            raise OSError("The V380 camera returned an unexpected login response.")
        if result != LOGIN_SUCCESS:
            raise OSError(LOGIN_ERRORS.get(result, "The V380 camera rejected authentication."))
        self.version = response[12]
        self.ticket = struct.unpack_from("<I", response, 13)[0]
        self.address = address

    def open_stream(self) -> None:
        self.socket = socket.create_connection((self.address, self.config.port), timeout=SOCKET_TIMEOUT)
        self.socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        quality = MAIN_STREAM_QUALITY if self.main_stream else SECONDARY_STREAM_QUALITY
        login = bytearray(256)
        struct.pack_into("<I", login, 0, STREAM_LOGIN_COMMAND)
        struct.pack_into("<I", login, 4, self.config.did)
        struct.pack_into("<H", login, 12, 20)
        struct.pack_into("<I", login, 14, self.ticket)
        struct.pack_into("<I", login, 22, AUDIO_ENABLED)
        struct.pack_into("<I", login, 26, quality)
        self.socket.sendall(login)
        response = _recv_exact(self.socket, MEDIA_HEADER_SIZE, SOCKET_TIMEOUT)
        command, result = struct.unpack_from("<ii", response)
        if command != STREAM_LOGIN_RESPONSE or result in (-11, -12):
            raise OSError("The V380 camera refused the video stream.")
        self.width = struct.unpack_from("<I", response, 10)[0] if len(response) >= 14 else 0
        self.height = struct.unpack_from("<I", response, 14)[0] if len(response) >= 18 else 0
        start = bytearray(256)
        struct.pack_into("<I", start, 0, STREAM_START_COMMAND)
        struct.pack_into("<H", start, 4, STREAM_START_FLAG)
        self.socket.sendall(start)

    def send_control(self, payload: bytes) -> bool:
        if self.socket is None:
            return False
        try:
            self.socket.sendall(payload)
            return True
        except OSError:
            return False

    def frames(self):
        assert self.socket is not None
        buffer = bytearray()
        total = current = 0
        start_type = 0
        expected = 0
        assembling = False
        while True:
            marker = _recv_exact(self.socket, 1)
            if marker[0] != FRAME_MARKER:
                continue
            header = marker + _recv_exact(self.socket, FRAME_HEADER_SIZE - 1)
            frame_type = header[1]
            total, current, length = struct.unpack_from("<HHH", header, 3)
            if length == 0 or length > MAX_PAYLOAD_SIZE or total == 0 or current >= total:
                assembling = False
                continue
            payload = _recv_exact(self.socket, length)
            if current == 0:
                buffer = bytearray(payload)
                start_type = frame_type
                expected = 1
                assembling = True
            elif assembling and current == expected and len(buffer) + length <= MAX_FRAME_BYTES:
                buffer.extend(payload)
                expected += 1
            else:
                assembling = False
                continue
            if current != total - 1:
                continue
            assembling = False
            if start_type == CONTROL_TYPE or len(buffer) < MEDIA_HEADER_SIZE:
                continue
            yield start_type, bytes(buffer)

    def close(self) -> None:
        if self.socket is not None:
            try:
                self.socket.close()
            finally:
                self.socket = None


class NativeBridge:
    def __init__(self, config: V380Config, password: str, main_stream: bool = True) -> None:
        self.config = config
        self.password = password
        self.main_stream = main_stream
        self.stopped = threading.Event()
        self.error: str | None = None
        self.width = 0
        self.height = 0
        self.light_supported = True
        self.ptz_supported = True
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_lock = threading.Lock()
        self.control_requests: queue.Queue[bytes] = queue.Queue(maxsize=16)
        self.video_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=128)
        self.audio_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=512)
        self.process: subprocess.Popen | None = None
        self.server, self.server_thread, self.url = start_media_server(self)
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        self.relay_thread: threading.Thread | None = None
        if config.relay_url:
            self.relay_thread = threading.Thread(target=self._relay, daemon=True)
            self.relay_thread.start()

    def _relay(self) -> None:
        delay = RELAY_START_DELAY_SECONDS
        quick_failures = 0
        while not self.stopped.wait(delay):
            device = hardware_encode_device()
            try:
                process = subprocess.Popen(
                    relay_command(self.url, self.config.relay_url, device=device),
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
            except OSError as ex:
                LOG.warning("V380 relay could not start: %s", ex)
                quick_failures += 1
                delay = min(RELAY_RETRY_SECONDS * 2 ** quick_failures, RELAY_MAX_RETRY_SECONDS)
                continue
            started = time.monotonic()
            LOG.info("V380 relay started to %s", self.config.relay_url)
            while process.poll() is None and not self.stopped.wait(1):
                pass
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)
                return
            quick_failures = 0 if time.monotonic() - started >= RELAY_STABLE_SECONDS else quick_failures + 1
            if device and quick_failures >= 2:
                LOG.warning("V380 hardware relay keeps failing; switching back to libx264")
                disable_hardware_encode()
                quick_failures = 0
            delay = min(RELAY_RETRY_SECONDS * 2 ** quick_failures, RELAY_MAX_RETRY_SECONDS)
            LOG.info("V380 relay stopped with exit code %s; retrying in %s s", process.returncode, delay)

    def control(self, payload: bytes) -> bool:
        try:
            self.control_requests.put_nowait(payload)
            return True
        except queue.Full:
            return False

    def move_camera(self, direction: str) -> bool:
        command = PTZ_COMMANDS.get(direction)
        return command is not None and self.control(command)

    def stop_movement(self) -> bool:
        return self.control(PTZ_STOP)

    def set_light(self, mode: str) -> bool:
        command = LIGHT_COMMANDS.get(mode)
        return command is not None and self.control(command)

    def _muxer_failed(self) -> None:
        if not self.stopped.is_set():
            self.error = MUXER_STOPPED_MESSAGE
            self.stopped.set()

    def _broadcast(self) -> None:
        broadcast_media(self.process, self.stopped, self.subscribers, self.subscriber_lock, self.stream_head)
        self._muxer_failed()

    def _start_muxer(self) -> list[threading.Thread]:
        video_read, video_write = os.pipe()
        audio_read, audio_write = os.pipe()
        try:
            self.process = subprocess.Popen([
                "ffmpeg", "-nostdin", "-nostats", "-loglevel", "error",
                "-thread_queue_size", "512", *LIVE_VIDEO_INPUT, "-i", f"/proc/self/fd/{video_read}",
                "-thread_queue_size", "512", *LIVE_AUDIO_INPUT, "-i", f"/proc/self/fd/{audio_read}",
                "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-af", LIVE_AUDIO_CLOCK_FILTER,
                "-c:a", "aac", "-b:a", "32k", "-output_ts_offset", str(AAC_FRAME_SAMPLES / AAC_SAMPLE_RATE),
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
            threading.Thread(target=write_media, args=(video_write, self.video_queue, self.stopped, self._muxer_failed), daemon=True),
            threading.Thread(target=write_media, args=(audio_write, self.audio_queue, self.stopped, self._muxer_failed), daemon=True),
        ]
        for thread in threads:
            thread.start()
        broadcaster = threading.Thread(target=self._broadcast, daemon=True)
        broadcaster.start()
        return [*threads, broadcaster]

    def _run(self) -> None:
        session = None
        threads: list[threading.Thread] = []
        try:
            session = V380Session(self.config, self.password, self.main_stream)
            session.authenticate()
            session.open_stream()
            self.width, self.height = session.width, session.height
            key = media_key(session.ticket)
            needs_decrypt = session.version > ENCRYPTED_VERSION
            first_keyframe = False
            last_video = time.monotonic()
            threads = self._start_muxer()
            for start_type, buffer in session.frames():
                if self.stopped.is_set():
                    break
                if time.monotonic() - last_video > VIDEO_STALL_SECONDS:
                    raise OSError("The V380 native stream stopped producing video.")
                while not self.control_requests.empty():
                    session.send_control(self.control_requests.get_nowait())
                payload = buffer[MEDIA_HEADER_SIZE:]
                if needs_decrypt and len(payload) >= AES_BLOCK:
                    payload = decrypt_media(key, payload)
                if start_type in AUDIO_TYPES:
                    self.audio_queue.put(payload, timeout=1)
                    continue
                last_video = time.monotonic()
                if not first_keyframe:
                    if not _has_parameter_set(payload):
                        continue
                    first_keyframe = True
                self.video_queue.put(payload, timeout=1)
        except (OSError, ValueError, queue.Full) as ex:
            if not self.stopped.is_set():
                self.error = str(ex) if isinstance(ex, OSError) else "The V380 native stream disconnected."
                LOG.warning("V380 native stream disconnected: %s", self.error)
        finally:
            self.stopped.set()
            if session is not None:
                session.close()
            self._shutdown_muxer(threads)
            self.server.shutdown()
            self.server.server_close()

    def _shutdown_muxer(self, threads: list[threading.Thread]) -> None:
        if self.process is not None:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except Exception:
                self.process.kill()
                self.process.wait(timeout=3)
        for thread in threads:
            thread.join(timeout=2)
        if self.process is not None and self.process.stdout is not None:
            self.process.stdout.close()

    def close(self) -> None:
        self.stopped.set()
        self.thread.join(timeout=15)
        if self.relay_thread is not None:
            self.relay_thread.join(timeout=10)
        self.server_thread.join(timeout=2)


def _has_parameter_set(payload: bytes) -> bool:
    index = payload.find(b"\x00\x00\x01")
    while index >= 0 and index + 3 < len(payload):
        nal_type = (payload[index + 3] >> 1) & 0x3F
        if nal_type in (32, 33, 34):
            return True
        index = payload.find(b"\x00\x00\x01", index + 3)
    return False


_CONTINUOUS_LENSES: dict[str, dict[int, float | None]] = {}
_CONTINUOUS_LOCK = threading.Lock()


def register_continuous_lens(uid: str, lens: int) -> None:
    with _CONTINUOUS_LOCK:
        _CONTINUOUS_LENSES.setdefault(uid, {})[lens] = None


def unregister_continuous_lens(uid: str, lens: int) -> None:
    with _CONTINUOUS_LOCK:
        lenses = _CONTINUOUS_LENSES.get(uid)
        if lenses is not None and lens in lenses:
            lenses[lens] = time.monotonic() + CONTINUOUS_HOST_GRACE_SECONDS


def continuous_host_lens(uid: str) -> int | None:
    with _CONTINUOUS_LOCK:
        lenses = _CONTINUOUS_LENSES.get(uid, {})
        now = time.monotonic()
        for lens in [lens for lens, expiry in lenses.items() if expiry is not None and expiry <= now]:
            del lenses[lens]
        return min(lenses) if lenses else None


_BRIDGES: dict[str, NativeBridge] = {}
_BRIDGE_REFS: dict[str, int] = {}
_BRIDGE_LOCK = threading.Lock()


def get_bridge(uid: str, password: str, main_stream: bool = True) -> NativeBridge | None:
    with _BRIDGE_LOCK:
        bridge = _BRIDGES.get(uid)
        if bridge is not None and not bridge.stopped.is_set():
            _BRIDGE_REFS[uid] += 1
            return bridge
        config = load_config(uid)
        if config is None:
            return None
        bridge = NativeBridge(config, password, main_stream)
        _BRIDGES[uid] = bridge
        _BRIDGE_REFS[uid] = 1
        return bridge


def peek_bridge(uid: str) -> NativeBridge | None:
    with _BRIDGE_LOCK:
        bridge = _BRIDGES.get(uid)
        return bridge if bridge is not None and not bridge.stopped.is_set() else None


def release_bridge(uid: str) -> None:
    with _BRIDGE_LOCK:
        if uid not in _BRIDGE_REFS:
            return
        _BRIDGE_REFS[uid] -= 1
        if _BRIDGE_REFS[uid] > 0:
            return
        bridge = _BRIDGES.pop(uid, None)
        _BRIDGE_REFS.pop(uid, None)
    if bridge is not None:
        bridge.close()
