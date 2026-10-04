import hashlib
import http.client
import json
import os
import socket
import ssl
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from dataclasses import asdict, replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, call, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QCheckBox, QDialog, QDialogButtonBox, QLineEdit

from app import (
    IMOU_ACCOUNT_PROVIDER, IMOU_LOCAL_CONNECTIONS_SETTING, IMOU_PROVIDER, CameraPreview,
    ImouReplayWorker, LocalReplayPane, MainWindow, RtspStreamWorker, imou_account_camera, imou_rtsp_url, load_imou_cameras,
    mpv_rtsp_command,
)
from imou import ImouAccount, ImouDevice, ImouRecording
from rtsp_tunnel import LocalRtspConnection, LocalRtspMediaBridge, LocalRtspTunnel, rtsp_certificate_sha256


ACCOUNT = ImouAccount("owner@example.com", "application-id")
DEVICE = ImouDevice("device-id", "Kitchen", "0")
CONNECTION = LocalRtspConnection(imou_rtsp_url("192.168.1.108"))


class LocalRtspTunnelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_tls_")
        certificate = Path(cls.directory.name) / "certificate.pem"
        key = Path(cls.directory.name) / "key.pem"
        subprocess.run([
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=local-camera-test", "-keyout", str(key), "-out", str(certificate),
        ], check=True, capture_output=True, timeout=10)
        cls.context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        cls.context.load_cert_chain(certificate, key)
        cls.fingerprint = hashlib.sha256(ssl.PEM_cert_to_DER_cert(certificate.read_text())).hexdigest()

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    def test_only_valid_private_connections_and_certificate_pins_are_accepted(self):
        for url in ("rtsp://127.0.0.1/video", "rtsp://8.8.8.8/video", "rtsp://0.0.0.0/video",
                    "rtsp://224.0.0.1/video", "rtsp://camera.example/video", "http://192.168.1.108/video",
                    "rtsp://admin:secret@192.168.1.108/video", "rtsp://192.168.1.108:0/video",
                    "rtsp://192.168.1.108:65536/video", "rtsp://192.168.1.108/video\n",
                    "rtsp://192.168.1.108/video#fragment"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                LocalRtspConnection(url)
        for username, fingerprint in (("", ""), ("admin\r", ""), ("admin", "invalid")):
            with self.subTest(username=username, fingerprint=fingerprint), self.assertRaises(ValueError):
                LocalRtspConnection(CONNECTION.url, username, fingerprint)
        self.assertEqual(LocalRtspConnection("rtsp://[fd00::108]/video").username, "admin")

    def exchange(self, encrypted, expected_pin=None, capture_certificate=False):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)
        received = []
        failures = []
        request = b"DESCRIBE /video RTSP/1.0\r\nAuthorization: test-private-credential\r\n\r\n"
        response = b"RTSP/1.0 200 OK\r\n\r\n" + b"$\x00\x00\x04data" * 32768

        def serve():
            try:
                connection, _ = listener.accept()
                with connection:
                    stream = self.context.wrap_socket(connection, server_side=True) if encrypted else connection
                    with stream:
                        stream.settimeout(3)
                        message = bytearray()
                        while b"\r\n\r\n" not in message:
                            part = stream.recv(4096)
                            if not part:
                                break
                            message.extend(part)
                        received.append(bytes(message))
                        if message:
                            stream.sendall(response)
            except OSError as ex:
                failures.append(type(ex).__name__)

        thread = threading.Thread(target=serve, daemon=True)
        thread.start()
        create_connection = socket.create_connection
        tunnel = None
        try:
            with patch("rtsp_tunnel.socket.create_connection", side_effect=lambda address, timeout:
                       create_connection(listener.getsockname(), timeout)), patch("rtsp_tunnel.RTSP_MESSAGE_LIMIT", 1024):
                if capture_certificate:
                    self.assertEqual(rtsp_certificate_sha256(CONNECTION.url), self.fingerprint)
                else:
                    pin = (expected_pin if expected_pin is not None else self.fingerprint) if encrypted else ""
                    tunnel = LocalRtspTunnel(replace(CONNECTION, certificate_sha256=pin))
                    self.assertEqual(tunnel.listener.getsockname()[0], "127.0.0.1")
                    self.assertNotIn("credential", tunnel.url)
                    with socket.socket() as client:
                        client.settimeout(3)
                        client.connect(tunnel.listener.getsockname())
                        client.sendall(request)
                        time.sleep(0.05)
                        data = bytearray()
                        while True:
                            try:
                                part = client.recv(8192)
                            except ConnectionResetError:
                                if expected_pin is None:
                                    raise
                                break
                            if not part:
                                break
                            data.extend(part)
                    tunnel.thread.join(3)
                    self.assertFalse(tunnel.thread.is_alive())
                    if expected_pin is not None:
                        self.assertEqual(data, b"")
                        self.assertIn("certificate changed", tunnel.error)
                    else:
                        self.assertEqual(data, response)
                        self.assertEqual(tunnel.error, "")
                thread.join(3)
                self.assertFalse(thread.is_alive())
        finally:
            if tunnel is not None:
                tunnel.close()
            listener.close()
            thread.join(3)
        self.assertEqual(failures, [])
        self.assertEqual(received, [b"" if capture_certificate or expected_pin is not None else request])

    def test_plain_and_pinned_tls_preserve_bytes_and_flush_eof_with_backpressure(self):
        for encrypted in (False, True):
            with self.subTest(encrypted=encrypted):
                self.exchange(encrypted)

    def test_certificate_change_rejects_connection_before_forwarding_credentials(self):
        self.exchange(True, expected_pin="0" * 64)

    def test_configuration_captures_certificate_without_sending_credentials(self):
        self.exchange(True, capture_certificate=True)

    def test_close_releases_listener_before_any_client_connects(self):
        tunnel = LocalRtspTunnel(CONNECTION)
        tunnel.close()
        self.assertFalse(tunnel.thread.is_alive())
        self.assertEqual(tunnel.listener.fileno(), -1)
        self.assertEqual(tunnel.error, "")

    def test_recording_url_preserves_local_identity_and_selects_the_main_stream(self):
        connection = LocalRtspConnection("rtsp://192.168.1.108:8554/cam/realmonitor?channel=3&subtype=1",
                                         "camera-user", "a" * 64)
        start = datetime(2026, 9, 30, 23, 59, 50)
        end = start + timedelta(seconds=30)
        recording = connection.recording(start, end)
        self.assertEqual(recording.username, connection.username)
        self.assertEqual(recording.certificate_sha256, connection.certificate_sha256)
        self.assertEqual(recording.url, "rtsp://192.168.1.108:8554/cam/playback?channel=3&subtype=0&"
                         "starttime=2026_09_30_23_59_50&endtime=2026_10_01_00_00_20")
        with self.assertRaises(ValueError):
            connection.recording(end, start)
        with self.assertRaises(ValueError):
            replace(connection, url="rtsp://192.168.1.108/cam/realmonitor?channel=0").recording(start, end)

    def test_recording_delivery_speed_preserves_fragmented_requests_authentication_and_media(self):
        tunnel = LocalRtspTunnel(CONNECTION, delivery_speed=4)
        request = (b"PLAY rtsp://127.0.0.1:40000/cam/playback RTSP/1.0\r\nCSeq: 4\r\n"
                   b"Authorization: Digest private-authentication\r\nRange: npt=0-\r\nSpeed: 1\r\n\r\n")
        media = b"$\x01\x00\x04data"
        try:
            self.assertEqual(tunnel._client_data(request[:20]), b"")
            output = tunnel._client_data(request[20:] + media[:3])
            self.assertIn(b"Authorization: Digest private-authentication\r\n", output)
            self.assertIn(b"Range: npt=0-\r\n", output)
            self.assertIn(b"Speed: 4\r\n", output)
            self.assertEqual(output.count(b"Speed:"), 1)
            self.assertEqual(tunnel._client_data(media[3:]), media)
            teardown = b"TEARDOWN /recording RTSP/1.0\r\nCSeq: 5\r\n\r\n"
            self.assertEqual(tunnel._client_data(teardown), teardown)
        finally:
            tunnel.close()

    def test_one_media_source_broadcasts_complete_bytes_to_multiple_local_consumers(self):
        read_fd, write_fd = os.pipe()
        descriptor = os.memfd_create("local-media-test")
        source = Mock(error="")
        process = Mock()
        process.stdout = os.fdopen(read_fd, "rb")
        process.poll.return_value = 0
        with patch("rtsp_tunnel.subprocess.Popen", return_value=process) as start:
            bridge = LocalRtspMediaBridge(source, descriptor)
        clients = []
        try:
            endpoint = urllib.parse.urlsplit(bridge.url)
            self.assertEqual(endpoint.hostname, "127.0.0.1")
            for index in range(2):
                client = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=3)
                client.request("GET", endpoint.path)
                response = client.getresponse()
                self.assertEqual(response.status, 200)
                clients.append((client, response))
            self.assertEqual(len(bridge.subscribers), 2)
            data = b"transport-stream-data" * 100
            os.write(write_fd, data)
            os.close(write_fd)
            write_fd = None
            for client, response in clients:
                self.assertEqual(response.read(), data)
            start.assert_called_once()
            self.assertEqual(start.call_args.kwargs["pass_fds"], (descriptor,))
        finally:
            if write_fd is not None:
                os.close(write_fd)
            for client, response in clients:
                client.close()
            bridge.close()
            os.close(descriptor)
        source.close.assert_called_once()
        self.assertFalse(bridge.thread.is_alive())
        self.assertFalse(bridge.server_thread.is_alive())

    def test_sparse_audio_does_not_hold_back_live_video(self):
        fixture = Path(self.directory.name) / "sparse-audio.mkv"
        subprocess.run([
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
            "-i", "testsrc2=size=160x90:rate=10", "-f", "lavfi",
            "-i", "sine=frequency=440:sample_rate=16000", "-t", "12", "-c:v", "mpeg2video",
            "-af", "aselect=lt(t\\,1)+gte(t\\,11)", "-c:a", "aac", str(fixture),
        ], check=True, capture_output=True, timeout=10)
        descriptor = os.memfd_create("sparse-audio-playlist")
        os.write(descriptor, f"ffconcat version 1.0\nfile '{fixture}'\noption analyzeduration 1000000\n".encode())
        os.lseek(descriptor, 0, os.SEEK_SET)
        start_process = subprocess.Popen
        with patch("rtsp_tunnel.subprocess.Popen", side_effect=lambda command, **options:
                   start_process(command[:1] + ["-re"] + command[1:], **options)):
            bridge = LocalRtspMediaBridge(Mock(error=""), descriptor)
        endpoint = urllib.parse.urlsplit(bridge.url)
        client = http.client.HTTPConnection(endpoint.hostname, endpoint.port, timeout=8)
        received = bytearray()
        lock = threading.Lock()

        def receive():
            try:
                while True:
                    data = response.read1(8192)
                    if not data:
                        return
                    with lock:
                        received.extend(data)
            except OSError:
                pass

        reader = None
        try:
            client.request("GET", endpoint.path)
            response = client.getresponse()
            reader = threading.Thread(target=receive, daemon=True)
            reader.start()
            time.sleep(4)
            capture = Path(self.directory.name) / "sparse-audio.ts"
            with lock:
                capture.write_bytes(received)
            packets = json.loads(subprocess.run([
                "ffprobe", "-v", "error", "-select_streams", "v", "-show_packets",
                "-show_entries", "packet=pts_time", "-of", "json", str(capture),
            ], check=True, capture_output=True, text=True, timeout=5).stdout)["packets"]
            timestamps = [float(packet["pts_time"]) for packet in packets if "pts_time" in packet]
            self.assertGreater(max(timestamps) - min(timestamps), 2)
        finally:
            bridge.close()
            client.close()
            if reader is not None:
                reader.join(3)
                self.assertFalse(reader.is_alive())
            os.close(descriptor)


class ImouLocalUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_local_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.camera = imou_account_camera(ACCOUNT, DEVICE)
        self.window.imou_accounts = [ACCOUNT]
        self.window.imou_cameras = [self.camera]
        self.window.devices = [self.camera]
        self.window.device_accounts = {self.camera.uid: "rtsp"}
        self.window.selected_device = self.camera
        self.window.save_imou_accounts()

    def tearDown(self):
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_local_configuration_preserves_identity_and_keeps_password_out_of_settings(self):
        def configure(dialog):
            fields = {field.placeholderText(): field for field in dialog.findChildren(QLineEdit)}
            fields["192.168.1.100"].setText("192.168.1.108")
            fields["Camera safety code or device password"].setText("private-device-key")
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()

        with patch.object(QDialog, "exec", configure), patch("app.stored_secret", return_value=None), patch(
            "app.save_device_secret", return_value=True
        ) as save, patch("app.rtsp_certificate_sha256", return_value="a" * 64), patch.object(
            CameraPreview, "start"
        ), patch.object(self.window, "watch_live"):
            self.window.configure_imou_local(self.camera)
        camera = load_imou_cameras(self.settings, [ACCOUNT])[0]
        self.assertEqual(camera.uid, self.camera.uid)
        self.assertEqual(camera.imou_device, DEVICE)
        self.assertEqual(camera.local_connection, replace(CONNECTION, certificate_sha256="a" * 64))
        save.assert_called_once_with(IMOU_PROVIDER, camera.uid, "private-device-key", "Imou camera device password")
        self.assertNotIn("private-device-key", Path(self.settings.fileName()).read_text())
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/local-test.sock"))
        worker.set_continuous(True)
        self.assertTrue(worker.continuous_enabled.is_set())

        def disable(dialog):
            next(box for box in dialog.findChildren(QCheckBox) if box.text().startswith("Use local")).setChecked(False)
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()

        with patch.object(QDialog, "exec", disable), patch("app.stored_secret", return_value=None), patch.object(
            CameraPreview, "start"
        ), patch.object(self.window, "watch_live"):
            self.window.configure_imou_local(camera)
        self.assertIsNone(load_imou_cameras(self.settings, [ACCOUNT])[0].local_connection)

    def test_live_recording_and_detection_never_request_cloud_video_or_expose_cli_passwords(self):
        camera = replace(self.camera, local_connection=CONNECTION)
        socket_path = Path(self.directory.name) / "socket"
        socket_path.touch()
        player = Mock()
        player.poll.return_value = None
        worker = RtspStreamWorker(camera, player, socket_path)
        self.assertIn("--idle=yes", mpv_rtsp_command(socket_path, 1, False, camera))
        transports = [Mock(url=f"rtsp://127.0.0.1:{40000 + index}/video") for index in range(2)]
        bridges = [Mock(url=f"http://127.0.0.1:{41000 + index}/media") for index in range(2)]
        created = []

        def start_bridge(tunnel, descriptor):
            self.assertIn("admin:private%40key@127.0.0.1", os.read(descriptor, 4096).decode())
            bridge = bridges[len(created)]
            created.append(bridge)
            return bridge

        def inspect_recorder(command, **options):
            self.assertIn(bridges[0].url, command)
            self.assertEqual(options["pass_fds"], ())
            self.assertNotIn("private@key", " ".join(command))
            return Mock(returncode=0)

        with patch("app.stored_secret", return_value="private@key"), patch("app.LocalRtspTunnel", side_effect=transports), patch(
            "app.ImouClient"
        ) as cloud, patch("app.mpv_request", return_value=(True, None)) as ipc, patch(
            "app.LocalRtspMediaBridge", side_effect=start_bridge
        ):
            worker._load_imou_stream()
            self.assertEqual([call.args[1] for call in ipc.call_args_list[-2:]], [
                ["loadfile", bridges[0].url, "replace"], ["set_property", "video-aspect-override", "-1"],
            ])
            with patch("app.subprocess.Popen", side_effect=inspect_recorder):
                recorder = worker._ffmpeg(Path(self.directory.name) / "clip.mkv", False)
            worker._finish_recording_process(recorder)
            bridges[0].close.assert_not_called()
            self.assertEqual(worker._local_detection_source(), (["-i", bridges[0].url], None, False))
            descriptor = worker._private_playlist()
            os.close(descriptor)
            self.assertEqual(len(created), 1)
            worker._change_imou_quality("SD")
            self.assertEqual([call.args[1] for call in ipc.call_args_list[-2:]], [
                ["loadfile", bridges[1].url, "replace"], ["set_property", "video-aspect-override", "16:9"],
            ])
            bridges[0].close.assert_called_once()
            cloud.return_value.secure_stream_url.assert_not_called()
            cloud.return_value.stream_url.assert_not_called()
            self.assertEqual(worker.camera.uid, self.camera.uid)

    def test_missing_device_key_closes_local_transport_without_cloud_fallback(self):
        worker = RtspStreamWorker(replace(self.camera, local_connection=CONNECTION), Mock(), Path("/tmp/local-test.sock"))
        worker.imou_client = Mock()
        with patch("app.stored_secret", return_value=None), patch("app.LocalRtspTunnel") as local:
            with self.assertRaises(OSError):
                worker._ffmpeg(Path(self.directory.name) / "clip.mkv", False)
            local.assert_not_called()
            with self.assertRaises(OSError):
                worker._open_imou_tunnel("HD")
            local.return_value.close.assert_called()
        worker.imou_client.secure_stream_url.assert_not_called()

    def test_camera_recording_seek_uses_local_rtsp_without_requesting_cloud_video(self):
        connection = replace(CONNECTION, certificate_sha256="a" * 64)
        camera = replace(self.camera, local_connection=connection)
        worker = ImouReplayWorker(camera)
        start = datetime(2026, 9, 30, 12)
        recording = ImouRecording("camera-record", start, start + timedelta(minutes=5), False, 12345)
        with patch("app.stored_secret", return_value="private-device-key"), patch("app.ImouClient") as cloud, patch(
            "app.LocalRtspTunnel", return_value=Mock(url="rtsp://127.0.0.1:40000/recording", error="")
        ) as local, patch("app.ImouReplayTunnel", return_value=Mock(url="http://127.0.0.1:40001/recording")):
            try:
                worker.open_recording(recording, 60)
                worker.start()
                deadline = time.monotonic() + 2
                while not local.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(local.called)
                requested = local.call_args.args[0]
                query = urllib.parse.parse_qs(urllib.parse.urlsplit(requested.url).query)
                self.assertEqual(urllib.parse.urlsplit(requested.url).path, "/cam/playback")
                self.assertEqual(query, {"channel": ["1"], "subtype": ["0"],
                                        "starttime": ["2026_09_30_12_01_00"], "endtime": ["2026_09_30_12_05_00"]})
                self.assertEqual(requested.certificate_sha256, connection.certificate_sha256)
                self.assertEqual(requested.username, connection.username)
                cloud.return_value.replay_url.assert_not_called()
            finally:
                worker.stop()
                self.assertTrue(worker.wait(5000))

    def test_local_camera_download_preserves_credentials_and_uses_the_same_atomic_save(self):
        camera = replace(self.camera, local_connection=CONNECTION)
        worker = ImouReplayWorker(camera)
        worker.client = Mock()
        start = datetime(2026, 9, 30, 12)
        recording = ImouRecording("camera-record", start, start + timedelta(seconds=15), False, 12345)
        target = Path(self.directory.name) / "saved.mkv"
        target.write_bytes(b"previous recording")
        existing_files = set(target.parent.iterdir())
        descriptors = []

        def download(command, **options):
            self.assertIn("concat", command)
            self.assertNotIn("dhav", command)
            self.assertNotIn("private-device-key", " ".join(command))
            descriptor, = options["pass_fds"]
            descriptors.append(descriptor)
            self.assertIn("admin:private-device-key@127.0.0.1", os.read(descriptor, 4096).decode())
            Path(command[-1]).write_bytes(b"complete camera recording")
            return Mock(poll=Mock(return_value=0))

        with patch("app.stored_secret", return_value="private-device-key"), patch(
            "app.LocalRtspTunnel", return_value=Mock(url="rtsp://127.0.0.1:40000/recording", error="")
        ) as local, patch("app.subprocess.Popen", side_effect=download):
            worker._save(recording, target)
            local.return_value.close.assert_called_once()
        worker.client.replay_url.assert_not_called()
        self.assertEqual(target.read_bytes(), b"complete camera recording")
        self.assertEqual(set(target.parent.iterdir()), existing_files)
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_local_replay_failures_and_privacy_never_fall_back_to_cloud_video(self):
        start = datetime(2026, 9, 30, 12)
        recording = ImouRecording("camera-record", start, start + timedelta(minutes=5), False, 12345)
        worker = ImouReplayWorker(replace(self.camera, local_connection=CONNECTION))
        worker.client = Mock()
        with patch("app.stored_secret", return_value=None), patch("app.LocalRtspTunnel") as local:
            with self.assertRaisesRegex(OSError, "password is unavailable"):
                worker._open_tunnel(recording)
            local.assert_not_called()
        with patch("app.stored_secret", return_value="private-key"), patch(
            "app.LocalRtspTunnel", side_effect=OSError("Local connection failed")
        ):
            with self.assertRaisesRegex(OSError, "Local connection failed"):
                worker._open_tunnel(recording)
        worker.camera = replace(worker.camera, imou_device=replace(DEVICE, privacy=True))
        with patch("app.LocalRtspTunnel") as local:
            with self.assertRaises(OSError):
                worker._open_tunnel(recording)
            local.assert_not_called()
        worker.client.replay_url.assert_not_called()

    def test_local_replay_player_keeps_credentials_private_and_uses_shared_pause_controls(self):
        camera = replace(self.camera, local_connection=CONNECTION)
        descriptors = []

        def player(command, **options):
            descriptor, = options["pass_fds"]
            descriptors.append(descriptor)
            self.assertIn(f"--playlist=/proc/self/fd/{descriptor}", command)
            self.assertIn("--rtsp-transport=tcp", command)
            self.assertIn("--length=240", command)
            self.assertIn("--demuxer-readahead-secs=240", command)
            self.assertNotIn("--demuxer-lavf-format=dhav", command)
            self.assertNotIn("private-device-key", " ".join(command))
            self.assertIn("admin:private-device-key@127.0.0.1", os.read(descriptor, 4096).decode())
            return Mock(poll=Mock(return_value=0))

        with patch("app.ImouReplayWorker"), patch("app.QTimer.singleShot"), patch(
            "app.stored_secret", return_value="private-device-key"
        ), patch("app.subprocess.Popen", side_effect=player):
            pane = LocalReplayPane(camera, self.window, camera_recordings=True)
            try:
                pane.remote_worker.tunnel = Mock(spec=LocalRtspTunnel)
                pane.remote_offset = 60
                pane.current_path = Path("recording")
                pane.segment_durations[pane.current_path] = 300
                self.assertEqual(pane.playback_offset(), 60)
                pane.start_replay_player("rtsp://127.0.0.1:40000/recording")
                with patch("app.mpv_request", return_value=(True, False)) as ipc:
                    pane.toggle_playing()
                    ipc.assert_any_call(pane.socket_path, ["set_property", "pause", True])
                    for speed in (2, 4, 1):
                        pane.change_speed()
                        ipc.assert_any_call(pane.socket_path, ["set_property", "speed", speed])
                    ipc.assert_any_call(pane.socket_path, ["set_property", "aid", "auto"])
            finally:
                pane.stop()
                pane.deleteLater()
        for descriptor in descriptors:
            with self.assertRaises(OSError):
                os.fstat(descriptor)

    def test_invalid_saved_connection_does_not_restore_cloud_video(self):
        for record in ("[]", "invalid", json.dumps({self.camera.uid: "invalid"}),
                       json.dumps({self.camera.uid: {"url": "rtsp://8.8.8.8/video"}})):
            with self.subTest(record=record):
                self.settings.setValue(IMOU_LOCAL_CONNECTIONS_SETTING, record)
                with self.assertRaises(ValueError):
                    imou_account_camera(ACCOUNT, DEVICE, self.settings)
                self.assertEqual(load_imou_cameras(self.settings, [ACCOUNT]), [])

    def test_removal_clears_local_settings_and_rolls_back_keyring_failures(self):
        self.settings.setValue(IMOU_LOCAL_CONNECTIONS_SETTING, json.dumps({self.camera.uid: asdict(CONNECTION)}))
        with patch("app.stored_secret", return_value="saved-key"), patch("app.clear_device_secret", side_effect=[True, False]), patch(
            "app.save_device_secret", return_value=True
        ) as restore:
            self.window.remove_imou_account(ACCOUNT)
        restore.assert_called_once_with(IMOU_ACCOUNT_PROVIDER, ACCOUNT.app_id, "saved-key", "Imou camera credential")
        self.assertEqual(self.window.imou_accounts, [ACCOUNT])
        with patch("app.stored_secret", return_value="saved-key"), patch("app.clear_device_secret", return_value=True) as clear:
            self.window.remove_imou_account(ACCOUNT)
        self.assertEqual(clear.call_args_list, [call(IMOU_ACCOUNT_PROVIDER, ACCOUNT.app_id), call(IMOU_PROVIDER, self.camera.uid)])
        self.assertEqual(json.loads(self.settings.value(IMOU_LOCAL_CONNECTIONS_SETTING)), {})
        self.assertEqual(self.window.imou_cameras, [])
