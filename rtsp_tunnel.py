from __future__ import annotations

import hashlib
import hmac
import ipaddress
import queue
import re
import select
import socket
import ssl
import subprocess
import threading
import time
import urllib.parse
from collections import deque
from dataclasses import dataclass, replace
from datetime import datetime

import websocket

from icam365 import LIVE_MEDIA_OUTPUT_OPTIONS, broadcast_media, start_media_server


RTSP_HEADER_END = b"\r\n\r\n"
RTSP_MESSAGE_LIMIT = 4 * 1024 * 1024
RTSP_TUNNEL_TIMEOUT = 15
IMOU_REPLAY_TIME_FORMAT = "%Y_%m_%d_%H_%M_%S"
IMOU_REPLAY_RTSP_PATH = "/cam/playback"


@dataclass(frozen=True)
class LocalRtspConnection:
    url: str
    username: str = "admin"
    certificate_sha256: str = ""

    def __post_init__(self) -> None:
        parsed = urllib.parse.urlsplit(self.url)
        host = ipaddress.ip_address(parsed.hostname or "")
        if (parsed.scheme != "rtsp" or not host.is_private or host.is_loopback
                or host.is_multicast or host.is_unspecified or parsed.username or parsed.password
                or not 1 <= (parsed.port if parsed.port is not None else 554) <= 65535 or not self.username.strip()
                or parsed.fragment
                or any(character in self.url + self.username for character in ("\r", "\n", "\x00"))
                or self.certificate_sha256 and not re.fullmatch(r"[a-f0-9]{64}", self.certificate_sha256)):
            raise ValueError("Enter a valid local RTSP camera connection.")

    def recording(self, start: datetime, end: datetime) -> LocalRtspConnection:
        if end <= start:
            raise ValueError("The camera recording must end after its start.")
        parsed = urllib.parse.urlsplit(self.url)
        channel = urllib.parse.parse_qs(parsed.query).get("channel", ["1"])[0]
        if not channel.isdecimal() or int(channel) < 1:
            raise ValueError("The local camera channel is invalid.")
        query = urllib.parse.urlencode({
            "channel": channel, "subtype": "0", "starttime": start.strftime(IMOU_REPLAY_TIME_FORMAT),
            "endtime": end.strftime(IMOU_REPLAY_TIME_FORMAT),
        })
        return replace(self, url=urllib.parse.urlunsplit(parsed._replace(path=IMOU_REPLAY_RTSP_PATH, query=query)))


def local_tls_context() -> ssl.SSLContext:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return context


def rtsp_certificate_sha256(url: str) -> str:
    parsed = urllib.parse.urlsplit(LocalRtspConnection(url).url)
    with socket.create_connection((parsed.hostname, parsed.port or 554), timeout=RTSP_TUNNEL_TIMEOUT) as connection:
        with local_tls_context().wrap_socket(connection, server_hostname=parsed.hostname) as secure:
            return hashlib.sha256(secure.getpeercert(binary_form=True)).hexdigest()


def loopback_media_listener() -> socket.socket:
    listener = socket.socket()
    try:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(0.5)
        return listener
    except OSError:
        listener.close()
        raise


class LocalRtspTunnel:
    def __init__(self, configuration: LocalRtspConnection, delivery_speed: int = 1) -> None:
        if delivery_speed not in (1, 2, 4):
            raise ValueError("Unsupported local recording delivery speed.")
        self.configuration = configuration
        self.delivery_speed = delivery_speed
        self.request_buffer = bytearray()
        self.listener = loopback_media_listener()
        parsed = urllib.parse.urlsplit(configuration.url)
        self.url = urllib.parse.urlunsplit((
            "rtsp", f"127.0.0.1:{self.listener.getsockname()[1]}", parsed.path, parsed.query, "",
        ))
        self.stopped = threading.Event()
        self.client: socket.socket | None = None
        self.connection: socket.socket | None = None
        self.error = ""
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _connect(self) -> None:
        parsed = urllib.parse.urlsplit(self.configuration.url)
        self.connection = socket.create_connection((parsed.hostname, parsed.port or 554), timeout=RTSP_TUNNEL_TIMEOUT)
        if self.configuration.certificate_sha256:
            self.connection = local_tls_context().wrap_socket(self.connection, server_hostname=parsed.hostname)
            fingerprint = hashlib.sha256(self.connection.getpeercert(binary_form=True)).hexdigest()
            if not hmac.compare_digest(fingerprint, self.configuration.certificate_sha256):
                self.error = "The local camera certificate changed. Configure its local access again."
                raise OSError(self.error)
        self.connection.setblocking(False)
        self.client.setblocking(False)

    def _run(self) -> None:
        try:
            while not self.stopped.is_set():
                try:
                    self.client, _ = self.listener.accept()
                    break
                except TimeoutError:
                    continue
            if self.client is None or self.stopped.is_set():
                return
            self._connect()
            pending = {self.client: bytearray(), self.connection: bytearray()}
            draining = False
            while not self.stopped.is_set():
                readable = [] if draining else [source for source in pending
                    if len(pending[self.connection if source is self.client else self.client]) < RTSP_MESSAGE_LIMIT]
                writable = [target for target, data in pending.items() if data]
                if draining and not writable:
                    return
                readers, writers, _ = select.select(readable, writable, [], 0.2)
                if (self.connection in readable and isinstance(self.connection, ssl.SSLSocket)
                        and self.connection.pending() and self.connection not in readers):
                    readers.append(self.connection)
                for source in readers:
                    target = self.connection if source is self.client else self.client
                    try:
                        data = source.recv(min(65536, RTSP_MESSAGE_LIMIT - len(pending[target])))
                    except ConnectionResetError:
                        if source is self.client:
                            return
                        raise
                    except (ssl.SSLWantReadError, ssl.SSLWantWriteError, BlockingIOError):
                        continue
                    if not data:
                        draining = True
                        continue
                    pending[target].extend(self._client_data(data) if source is self.client else data)
                for target in writers:
                    try:
                        written = target.send(pending[target])
                    except (BrokenPipeError, ConnectionResetError):
                        if target is self.client:
                            return
                        raise
                    except (ssl.SSLWantWriteError, ssl.SSLWantReadError, BlockingIOError):
                        continue
                    del pending[target][:written]
        except (OSError, ValueError):
            if not self.stopped.is_set() and not self.error:
                self.error = "The local camera connection failed. Check the camera network or VPN."
        finally:
            self._close_sockets()

    def _client_data(self, data: bytes) -> bytes:
        if self.delivery_speed == 1:
            return data
        self.request_buffer.extend(data)
        output = bytearray()
        while (length := rtsp_message_length(self.request_buffer)) is not None and len(self.request_buffer) >= length:
            message = bytes(self.request_buffer[:length])
            del self.request_buffer[:length]
            if message.startswith(b"PLAY "):
                end = message.index(RTSP_HEADER_END)
                header = re.sub(rb"\r\nSpeed:[^\r\n]*", b"", message[:end], flags=re.IGNORECASE)
                message = header + f"\r\nSpeed: {self.delivery_speed:g}\r\n\r\n".encode() + message[end + 4:]
            output.extend(message)
        return bytes(output)

    def _close_sockets(self) -> None:
        self.stopped.set()
        for connection in (self.connection, self.client, self.listener):
            if connection is not None:
                try:
                    connection.close()
                except OSError:
                    pass

    def close(self) -> None:
        self._close_sockets()
        self.thread.join(timeout=RTSP_TUNNEL_TIMEOUT + 2)


class LocalRtspMediaBridge:
    def __init__(self, tunnel: LocalRtspTunnel, playlist_fd: int) -> None:
        self.tunnel = tunnel
        self.stopped = threading.Event()
        self.failure = ""
        self.subscribers: set[queue.Queue] = set()
        self.subscriber_lock = threading.Lock()
        self.server, self.server_thread, self.url = start_media_server(self)
        try:
            self.process = subprocess.Popen([
                "ffmpeg", "-nostdin", "-nostats", "-loglevel", "error",
                "-f", "concat", "-safe", "0", "-protocol_whitelist", "file,pipe,rtsp,tcp,udp,rtp",
                "-i", f"/proc/self/fd/{playlist_fd}", "-map", "0:v:0", "-map", "0:a?", "-c", "copy",
                *LIVE_MEDIA_OUTPUT_OPTIONS,
            ], pass_fds=(playlist_fd,), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL, bufsize=0)
        except OSError:
            self.stopped.set()
            self.server.shutdown()
            self.server.server_close()
            self.server_thread.join(timeout=2)
            tunnel.close()
            raise
        self.thread = threading.Thread(target=self._broadcast, daemon=True)
        self.thread.start()

    @property
    def error(self) -> str:
        return self.tunnel.error or self.failure

    def _broadcast(self) -> None:
        broadcast_media(self.process, self.stopped, self.subscribers, self.subscriber_lock)
        if not self.stopped.is_set():
            self.failure = "The local camera media stream stopped. Check the camera network or VPN."
            self.stopped.set()

    def close(self) -> None:
        self.stopped.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=3)
        self.tunnel.close()
        self.thread.join(timeout=3)
        self.process.stdout.close()
        self.server.shutdown()
        self.server.server_close()
        self.server_thread.join(timeout=2)


def rewrite_rtsp_message(message: bytes, source: bytes, target: bytes) -> bytes:
    header, separator, body = message.partition(RTSP_HEADER_END)
    if not separator:
        raise OSError("The camera returned an incomplete RTSP message.")
    lines = header.replace(source, target).split(b"\r\n")
    rewritten_body = body.replace(source, target)
    lines = [line for line in lines if not line.lower().startswith(b"content-length:")]
    if rewritten_body:
        lines.append(f"Content-Length: {len(rewritten_body)}".encode())
    return b"\r\n".join(lines) + separator + rewritten_body


def rtsp_message_length(buffer: bytes, interleaved_header: int = 4) -> int | None:
    if buffer.startswith(b"$"):
        if len(buffer) < interleaved_header:
            return None
        length = int.from_bytes(buffer[2:interleaved_header], "big")
        if length > RTSP_MESSAGE_LIMIT:
            raise OSError("The camera recording frame is too large.")
        return interleaved_header + length
    end = buffer.find(RTSP_HEADER_END)
    if end < 0:
        if len(buffer) > RTSP_MESSAGE_LIMIT:
            raise OSError("The camera RTSP header is too large.")
        return None
    length = 0
    for line in buffer[:end].split(b"\r\n"):
        if line.lower().startswith((b"content-length:", b"private-length:")):
            try:
                length = int(line.split(b":", 1)[1])
            except ValueError:
                raise OSError("The camera returned an invalid RTSP length.") from None
    if not 0 <= length <= RTSP_MESSAGE_LIMIT:
        raise OSError("The camera RTSP message is too large.")
    return end + len(RTSP_HEADER_END) + length


class WebSocketMediaTunnel:
    gateway_path = ""
    player_scheme = "rtsp"

    def __init__(self, upstream: str) -> None:
        try:
            parsed = urllib.parse.urlsplit(upstream if "://" in upstream else "rtsp://" + upstream)
            if (parsed.scheme != "rtsp" or not parsed.hostname or parsed.username or parsed.password
                    or parsed.port != 8556 or any(character in upstream for character in ("\r", "\n", "\x00"))):
                raise ValueError()
        except (ValueError, TypeError):
            raise OSError("The camera returned an invalid secure RTSP gateway.") from None
        self.upstream = upstream.encode()
        self.gateway = f"wss://{parsed.netloc}{self.gateway_path}"
        self.listener = loopback_media_listener()
        self.url = f"{self.player_scheme}://127.0.0.1:{self.listener.getsockname()[1]}/recording"
        self.stopped = threading.Event()
        self.client: socket.socket | None = None
        self.connection: websocket.WebSocket | None = None
        self.error = ""
        self.finishing = False
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def _accept(self) -> bool:
        while not self.stopped.is_set():
            try:
                self.client, _ = self.listener.accept()
                break
            except TimeoutError:
                continue
        if self.client is None or self.stopped.is_set():
            return False
        self.client.settimeout(1)
        self.connection = websocket.create_connection(self.gateway, timeout=RTSP_TUNNEL_TIMEOUT)
        self.connection.settimeout(1)
        return True

    def _run(self) -> None:
        raise NotImplementedError()

    def _close_sockets(self) -> None:
        self.stopped.set()
        for connection in (self.connection, self.client, self.listener):
            if connection is not None:
                try:
                    connection.close()
                except (OSError, websocket.WebSocketException):
                    pass

    def _offline(self, payload: bytes) -> bool:
        if payload.startswith(b"$") or (b"OffLine:" not in payload and b"is_session_end: true" not in payload):
            return False
        self.finishing = b"File Over" in payload or b"FileOver" in payload or b"is_session_end: true" in payload
        if not self.finishing:
            self.error = "The camera stream is unavailable."
        return True

    def close(self) -> None:
        self._close_sockets()
        self.thread.join(timeout=RTSP_TUNNEL_TIMEOUT + 2)
        self.upstream = b""


class RtspWebSocketTunnel(WebSocketMediaTunnel):
    def _run(self) -> None:
        receiver = None
        try:
            if not self._accept():
                return
            receiver = threading.Thread(target=self._receive, daemon=True)
            receiver.start()
            buffer = b""
            while not self.stopped.is_set():
                try:
                    data = self.client.recv(65536)
                except TimeoutError:
                    continue
                if not data:
                    break
                buffer += data
                while (length := rtsp_message_length(buffer)) is not None and len(buffer) >= length:
                    message, buffer = buffer[:length], buffer[length:]
                    if not message.startswith(b"$"):
                        self.finishing = message.startswith(b"TEARDOWN ")
                        message = rewrite_rtsp_message(message, self.url.encode(), self.upstream)
                    self.connection.send_binary(message)
        except (OSError, ValueError, websocket.WebSocketException):
            if not self.stopped.is_set() and not self.finishing:
                self.error = "The secure camera recording connection failed."
        finally:
            self._close_sockets()
            if receiver is not None:
                receiver.join(timeout=2)

    def _receive(self) -> None:
        buffer = b""
        try:
            while not self.stopped.is_set():
                try:
                    data = self.connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not data:
                    break
                payload = data.encode() if isinstance(data, str) else data
                if self._offline(payload):
                    break
                buffer += payload
                if len(buffer) > RTSP_MESSAGE_LIMIT:
                    raise OSError("The camera recording message is too large.")
                while (length := rtsp_message_length(buffer)) is not None and len(buffer) >= length:
                    message, buffer = buffer[:length], buffer[length:]
                    if not message.startswith(b"$"):
                        message = rewrite_rtsp_message(message, self.upstream, self.url.encode())
                    self.client.sendall(message)
        except (OSError, ValueError, websocket.WebSocketException):
            if not self.stopped.is_set() and not self.finishing:
                self.error = "The secure camera recording connection stopped."
        finally:
            self._close_sockets()


class ImouReplayTunnel(WebSocketMediaTunnel):
    gateway_path = "/httpprivateoverwebsocket"
    player_scheme = "http"

    def __init__(self, upstream: str, start: datetime, end: datetime, channel: str) -> None:
        if end <= start or not channel.isdecimal():
            raise OSError("The camera recording range is invalid.")
        self.start = start
        self.end = end
        self.channel = int(channel) + 1
        self.commands: queue.Queue[tuple[str, int | None]] = queue.Queue()
        self.sequence = 0
        self.speed = 1
        self.media_start: datetime | None = None
        self.frame_history: deque[tuple[int, ...]] = deque()
        self.seen_frames: set[tuple[int, ...]] = set()
        super().__init__(upstream)

    def _new_frame(self, media: bytes) -> bool:
        if len(media) < 24 or not media.startswith(b"DHAV") or media[4] not in (0xf0, 0xfc, 0xfd):
            return True
        key = (0 if media[4] == 0xf0 else 1, media[6], media[7],
               int.from_bytes(media[8:12], "little"), int.from_bytes(media[16:20], "little"))
        if key in self.seen_frames:
            return False
        self.seen_frames.add(key)
        self.frame_history.append(key)
        if len(self.frame_history) > 256:
            self.seen_frames.remove(self.frame_history.popleft())
        return True

    def _command(self, method: str, speed: int | None = None) -> bytes:
        self.sequence += 1
        parsed = urllib.parse.urlsplit(self.upstream.decode() if b"://" in self.upstream
                                      else "rtsp://" + self.upstream.decode())
        source = parsed.path.lstrip("/") + ("?" + parsed.query if parsed.query else "")
        source = re.sub(r"&beginTime=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}&endTime=\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", "", source)
        operation = {"PLAY": 0, "SCALE": 0, "PAUSE": 1, "KEEP_LIVE": 2}[method]
        uri = (f"/vod/playback.xav?channel={self.channel}&subtype=0"
               f"&starttime={self.start.strftime(IMOU_REPLAY_TIME_FORMAT)}"
               f"&endtime={self.end.strftime(IMOU_REPLAY_TIME_FORMAT)}&encrypt=0&method={operation}?sourceId={source}")
        headers = [f"GET {uri} HTTP/1.1", "Connection: keep-alive", f"Cseq: {self.sequence}",
                   f"Host: {parsed.hostname}:8086"]
        if method == "PLAY":
            headers += ["Accept-Sdp: Private", "User-Agent: Http Stream Client/1.0"]
        if speed is not None:
            headers.append(f"{'Speed' if speed < 4 else 'Scale'}: {speed}")
        return ("\r\n".join(headers) + "\r\n\r\n").encode()

    def _run(self) -> None:
        try:
            if not self._accept():
                return
            request = b""
            while RTSP_HEADER_END not in request and not self.stopped.is_set():
                data = self.client.recv(4096)
                if not data:
                    raise OSError("The local recording request is incomplete.")
                request += data
                if len(request) > 16384:
                    raise OSError("The local recording request is invalid.")
            if request.split(b"\r\n", 1)[0] not in (b"GET /recording HTTP/1.1", b"GET /recording HTTP/1.0"):
                raise OSError("The local recording request is invalid.")
            self.connection.send_binary(self._command("PLAY"))
            buffer = b""
            headers_sent = False
            keepalive = time.monotonic() + 20
            while not self.stopped.is_set():
                while not self.commands.empty():
                    method, speed = self.commands.get_nowait()
                    self.connection.send_binary(self._command(method, speed))
                if time.monotonic() >= keepalive:
                    self.connection.send_binary(self._command("KEEP_LIVE"))
                    keepalive = time.monotonic() + 20
                try:
                    data = self.connection.recv()
                except websocket.WebSocketTimeoutException:
                    continue
                if not data:
                    break
                payload = data.encode() if isinstance(data, str) else data
                if self._offline(payload):
                    if self.finishing and headers_sent:
                        self.client.sendall(b"0\r\n\r\n")
                    break
                buffer += payload
                if len(buffer) > RTSP_MESSAGE_LIMIT:
                    raise OSError("The camera recording message is too large.")
                while (length := rtsp_message_length(buffer, 6)) is not None and len(buffer) >= length:
                    message, buffer = buffer[:length], buffer[length:]
                    if message.startswith(b"$"):
                        if not headers_sent:
                            raise OSError("The camera sent video before accepting playback.")
                        media = message[6:]
                        if not self._new_frame(media):
                            continue
                        if (self.media_start is None and len(media) >= 24 and media.startswith(b"DHAV")
                                and media[4] in (0xfc, 0xfd)):
                            stamp = int.from_bytes(media[16:20], "little")
                            self.media_start = datetime(2000 + (stamp >> 26), (stamp >> 22) & 15,
                                                        (stamp >> 17) & 31, (stamp >> 12) & 31,
                                                        (stamp >> 6) & 63, stamp & 63)
                        self.client.sendall(f"{len(media):x}\r\n".encode() + media + b"\r\n")
                    else:
                        if not message.startswith(b"HTTP/1.1 200 "):
                            raise OSError("The camera rejected the playback command.")
                        if not headers_sent:
                            self.client.sendall(b"HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                                                b"Transfer-Encoding: chunked\r\nConnection: close\r\n\r\n")
                            headers_sent = True
        except (OSError, ValueError, websocket.WebSocketException):
            if not self.stopped.is_set() and not self.finishing:
                self.error = "The secure camera recording connection failed."
        finally:
            self._close_sockets()

    def set_speed(self, speed: int) -> None:
        if speed not in (1, 2, 4, 8):
            raise ValueError("Unsupported camera playback speed.")
        self.speed = speed
        self.commands.put(("SCALE", speed))

    def set_paused(self, paused: bool) -> None:
        self.commands.put(("PAUSE" if paused else "PLAY", None))
        if not paused and self.speed != 1:
            self.commands.put(("SCALE", self.speed))
