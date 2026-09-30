import json
import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog

from imou import IMOU_PRIVACY_MESSAGE, ImouAccount, ImouClient, ImouDevice, ImouError, ImouRecording, imou_signature
from app import (
    IMOU_ACCOUNT_PROVIDER, IMOU_ACCOUNTS_SETTING, IMOU_CAMERAS_SETTING,
    CameraPreview, ImouAccountDialog, ImouAccountWorker, ImouReplayWorker, LocalReplayPane, MainWindow, RtspCamera, RtspStreamWorker,
    imou_account_camera, load_imou_accounts, load_imou_cameras, mpv_rtsp_command,
)


ACCOUNT = ImouAccount("owner@example.com", "application-id")
DEVICE = ImouDevice("device-id", "Kitchen", "0")
STREAM_URL = "rtsp://stream.example.com:8554/private?digest=private-stream-token"


def response(data=None, code="0", message=""):
    return {"result": {"code": code, "data": data or {}, "msg": message}}


class ImouClientTest(unittest.TestCase):
    def client(self, replies):
        self.requests = []

        def open_request(request):
            self.requests.append((request.full_url, json.loads(request.data)))
            return replies.pop(0)

        return ImouClient(ACCOUNT, "private-application-key", open_request)

    def test_signature_matches_the_current_official_hmac_example(self):
        self.assertEqual(imou_signature("test123456789test123456789", 1706511734,
                                        "f5a1ae2d-c09c-4d39-a744-83a5c2c653c2"),
                         "xjhCQBoJ9hRDsCjyDcHjtDNzRZ3ZJezcawsfWeiaoxU=")

    def test_authenticated_discovery_keeps_device_names_and_mask_semantics(self):
        client = self.client([
            response({"accessToken": "private-token", "expireTime": 3600}),
            response({"deviceList": [
                {"deviceId": "device-id", "deviceName": "Kitchen", "channelList": [
                    {"channelId": 0, "channelName": "device-id-1", "cameraStatus": "off"},
                ]},
                {"deviceId": "private-device", "deviceName": "Living room", "channelList": [
                    {"channelId": "0", "cameraStatus": "on"},
                ]},
            ]}),
        ])
        self.assertEqual(client.devices(), [DEVICE, ImouDevice("private-device", "Living room", "0", privacy=True)])
        self.assertNotIn("token", self.requests[0][1]["params"])
        self.assertEqual(self.requests[1][1]["params"]["token"], "private-token")
        self.assertNotIn("private-application-key", json.dumps(self.requests))

    def test_expired_token_is_replaced_once_and_stream_url_is_returned(self):
        client = self.client([
            response({"accessToken": "old-token"}), response(code="TK1002"),
            response({"accessToken": "new-token"}), response({"url": STREAM_URL}),
        ])
        self.assertEqual(client.stream_url(DEVICE), STREAM_URL)
        self.assertEqual(self.requests[-1][1]["params"], {
            "deviceId": DEVICE.device_id, "channelId": "0", "streamId": 0, "token": "new-token",
        })
        client = self.client([
            response({"accessToken": "old-token"}), response(code="TK1002"),
            response({"accessToken": "new-token"}), response(code="TK1002"),
        ])
        with self.assertRaises(ImouError):
            client.stream_url(DEVICE)
        self.assertEqual(len(self.requests), 4)

    def test_cross_region_redirect_is_limited_to_official_gateways(self):
        client = self.client([
            response({"accessToken": "token", "currentDomain": "https://openapi-sg.easy4ip.com"}),
            response({"url": STREAM_URL}),
        ])
        client.stream_url(DEVICE)
        self.assertTrue(self.requests[-1][0].startswith("https://openapi-sg.easy4ip.com/"))
        for domain in ("http://openapi-fk.easy4ip.com", "https://elsewhere.example.com",
                       "https://openapi-fk.easy4ip.com:invalid", "https://openapi-fk.easy4ip.com/?token=other"):
            with self.subTest(domain=domain):
                client = self.client([response({"accessToken": "token", "currentDomain": domain})])
                with self.assertRaisesRegex(ImouError, "unexpected account server"):
                    client.stream_url(DEVICE)
                self.assertEqual(len(self.requests), 1)

    def test_privacy_blocks_stream_requests_and_errors_omit_server_secrets(self):
        client = self.client([])
        with self.assertRaisesRegex(ImouError, IMOU_PRIVACY_MESSAGE):
            client.stream_url(replace(DEVICE, privacy=True))
        self.assertEqual(self.requests, [])
        client = self.client([response(code="SN1001", message="private-application-key")])
        with self.assertRaisesRegex(ImouError, "rejected") as failure:
            client.devices()
        self.assertNotIn("private-application-key", str(failure.exception))

    def test_exhausted_cloud_traffic_has_an_actionable_error_without_token_retry(self):
        for code in ("FL1004", "FL1005"):
            with self.subTest(code=code):
                client = self.client([response({"accessToken": "token"}), response({"kitToken": "kit"}),
                                      response(code=code, message="private-service-detail")])
                with self.assertRaisesRegex(ImouError, "cloud traffic is exhausted") as failure:
                    client.secure_stream_url(DEVICE)
                self.assertEqual(failure.exception.code, code)
                self.assertNotIn("private-service-detail", str(failure.exception))
                self.assertEqual(len(self.requests), 3)

    def test_paging_preserves_channels_and_rejects_repeated_pages(self):
        record = {"deviceId": "recorder", "deviceName": "NVR", "channelList": [
            {"channelId": 0, "channelName": "Front"}, {"channelId": 1, "channelName": "Back"},
        ]}
        client = self.client([
            response({"accessToken": "token"}), response({"deviceList": [record]}), response({"deviceList": []}),
        ])
        with patch("imou.IMOU_PAGE_SIZE", 1):
            self.assertEqual([device.name for device in client.devices()], ["Front", "Back"])
        self.assertEqual(self.requests[-1][1]["params"]["page"], 2)
        client = self.client([
            response({"accessToken": "token"}), response({"deviceList": [record]}), response({"deviceList": [record]}),
        ])
        with patch("imou.IMOU_PAGE_SIZE", 1), self.assertRaisesRegex(ImouError, "repeated"):
            client.devices()

    def test_malformed_service_responses_are_rejected(self):
        for data in ({"deviceList": {}}, {"deviceList": [None]}, {"deviceList": [
            {"deviceId": 123, "channelList": [{"channelId": 0}]},
        ]}):
            client = self.client([response({"accessToken": "token"}), response(data)])
            with self.subTest(data=data), self.assertRaises(ImouError):
                client.devices()
        for url in ("https://stream.example.com", "rtsp://stream.example.com:0/", "rtsp://host/\nsecret", None):
            client = self.client([response({"accessToken": "token"}), response({"url": url})])
            with self.subTest(url=url), self.assertRaisesRegex(ImouError, "usable private"):
                client.stream_url(DEVICE)

    def test_movement_uses_device_capabilities_and_preserves_privacy(self):
        camera = replace(DEVICE, abilities="PT,LocalStorage")
        client = self.client([response({"accessToken": "token"}), response()])
        client.move(camera, "Left", {})
        self.assertTrue(self.requests[-1][0].endswith("/controlMovePTZ"))
        self.assertEqual(self.requests[-1][1]["params"], {
            "deviceId": DEVICE.device_id, "channelId": "0", "operation": "2", "duration": 500, "token": "token",
        })
        for unavailable in (DEVICE, replace(camera, privacy=True)):
            with self.assertRaises(ImouError):
                client.move(unavailable, "Up", {})
        self.assertEqual(len(self.requests), 2)
        self.assertEqual(client.collections(camera), {})

    def test_saved_positions_keep_their_names_and_use_the_same_command(self):
        camera = replace(DEVICE, abilities="PT,CollectionPoint")
        client = self.client([
            response({"accessToken": "token"}), response({"collections": [{"name": "Door"}, {"name": "Window"}]}),
            response(),
        ])
        positions = client.collections(camera)
        self.assertEqual(positions, {"Preset 1": "Door", "Preset 2": "Window"})
        client.move(camera, "Preset 2", positions)
        self.assertTrue(self.requests[-1][0].endswith("/turnCollection"))
        self.assertEqual(self.requests[-1][1]["params"]["name"], "Window")

    def test_recordings_keep_camera_times_and_distinguish_motion_from_continuous(self):
        start = datetime(2026, 9, 30, 2, 0)
        record = {"recordId": "/private/clip.mp4", "beginTime": "2026-09-30 02:00:00",
                  "endTime": "2026-09-30 02:05:00", "fileLength": 12345, "type": "normal"}
        client = self.client([response({"accessToken": "token"}), response({"records": [record]}),
                              response({"records": [dict(record, recordId="/private/event.mp4", type="videomotion")]}),
                              response({"records": []})])
        with patch("imou.IMOU_RECORD_PAGE_SIZE", 1):
            recordings = client.recordings(DEVICE, start, start + timedelta(hours=1))
        self.assertEqual([recording.detection for recording in recordings], [False, True])
        self.assertEqual(recordings[0].start, start)
        self.assertEqual(self.requests[-1][1]["params"]["queryRange"], "3-3")
        self.assertNotIn("private", recordings[0].key)
        client = self.client([response({"accessToken": "token"}), response({"records": [None]})])
        with self.assertRaisesRegex(ImouError, "metadata"):
            client.recordings(DEVICE, start, start + timedelta(hours=1))

    def test_playback_requests_private_sd_video_and_caches_its_scoped_token(self):
        start = datetime(2026, 9, 30, 2, 0)
        recording = ImouRecording("private-record", start, start + timedelta(minutes=5), False, 12345)
        url = "rtsp://gateway.example.com:8556/private?digest=secret"
        client = self.client([response({"accessToken": "token"}), response({"kitToken": "playback-token"}),
                              response({"url": url, "isEncrypt": False, "streamType": "rtsv"}),
                              response({"url": url, "isEncrypt": False, "streamType": "rtsv"})])
        self.assertEqual(client.replay_url(DEVICE, recording), url)
        self.assertEqual(client.replay_url(DEVICE, recording), url)
        self.assertEqual([request[0].rsplit("/", 1)[-1] for request in self.requests],
                         ["accessToken", "getKitToken", "getEncryptKitStreamUrl", "getEncryptKitStreamUrl"])
        params = self.requests[-1][1]["params"]
        self.assertEqual(params["businessType"], "localRecord")
        self.assertEqual(params["beginTime"], "2026-09-30 02:00:00")
        self.assertFalse(params["encryptStreamFlag"])
        self.assertTrue(params["rtsvEnable"])
        self.assertNotIn("protoType", params)
        with self.assertRaisesRegex(ImouError, IMOU_PRIVACY_MESSAGE):
            client.replay_url(replace(DEVICE, privacy=True), recording)

    def test_live_quality_and_playback_keep_separate_scoped_tokens(self):
        start = datetime(2026, 9, 30, 2, 0)
        recording = ImouRecording("private-record", start, start + timedelta(minutes=5), False, 12345)
        url = "rtsp://gateway.example.com:8556/private?digest=secret"
        client = self.client([
            response({"accessToken": "token"}), response({"kitToken": "live-token"}),
            response({"url": url, "isEncrypt": False}), response({"kitToken": "replay-token"}),
            response({"url": url, "isEncrypt": False, "streamType": "rtsv"}), response({"url": url, "isEncrypt": False}),
        ])
        client.secure_stream_url(DEVICE, 1)
        client.replay_url(DEVICE, recording)
        client.secure_stream_url(DEVICE)
        params = [request[1]["params"] for request in self.requests]
        self.assertEqual([item["type"] for item in params if "type" in item], ["1", "2"])
        streams = [item for item in params if "businessType" in item]
        self.assertEqual([item["kitToken"] for item in streams], ["live-token", "replay-token", "live-token"])
        self.assertEqual([item["streamId"] for item in streams], [1, 0, 0])
        self.assertEqual(streams[0]["beginTime"], "")
        with self.assertRaises(ImouError):
            client.secure_stream_url(replace(DEVICE, privacy=True))
        with self.assertRaises(ValueError):
            client.secure_stream_url(DEVICE, 2)


class FakeImouWorker(QObject):
    devices_found = pyqtSignal(list)
    failed = pyqtSignal(str)
    finished = pyqtSignal()

    def __init__(self, account, secret=None):
        super().__init__()
        self.account = account
        self.secret = secret
        self.requestInterruption = Mock()

    def start(self):
        pass


class ImouAccountUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_imou_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()

    def tearDown(self):
        if self.window.imou_worker is not None:
            self.window.imou_worker.finished.emit()
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_account_form_retains_failed_credentials_and_adds_discovered_camera(self):
        def fill_dialog(dialog):
            self.assertIsInstance(dialog, ImouAccountDialog)
            dialog.email.setText(ACCOUNT.email)
            dialog.app_id.setText(ACCOUNT.app_id)
            dialog.secret.setText("wrong-key")
            self.assertFalse(dialog.cloud_recording.isChecked())
            dialog.buttons.accepted.emit()
            self.assertFalse(dialog.email.isEnabled())
            worker = self.window.imou_worker
            worker.failed.emit("Imou rejected the application credentials.")
            worker.finished.emit()
            self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
            self.assertTrue(dialog.email.isEnabled())
            self.assertIn("rejected", dialog.error.text())
            dialog.secret.setText("private-application-key")
            dialog.buttons.accepted.emit()
            self.window.imou_worker.devices_found.emit([DEVICE])
            self.window.imou_worker.finished.emit()
            return dialog.result()

        with patch.object(QDialog, "exec", fill_dialog), patch("app.ImouAccountWorker", FakeImouWorker), patch.object(
            self.window, "watch_live"
        ), patch.object(CameraPreview, "start"):
            self.window.add_imou_account()
        self.assertEqual(load_imou_accounts(self.settings), [ACCOUNT])
        self.assertEqual(load_imou_cameras(self.settings, [ACCOUNT]), [imou_account_camera(ACCOUNT, DEVICE)])
        self.assertNotIn("private-application-key", Path(self.settings.fileName()).read_text())
        self.window.update_cameras_menu()
        self.assertEqual(self.window.cameras_menu.actions()[0].text(), "Kitchen · Imou Life (owner@example.com)")

    def test_worker_verifies_account_before_saving_secret(self):
        worker = ImouAccountWorker(ACCOUNT, "private-application-key")
        found = []
        worker.devices_found.connect(found.append)
        with patch("app.ImouClient") as client, patch("app.save_device_secret", return_value=True) as save:
            client.return_value.devices.side_effect = ImouError("rejected")
            worker.run()
            save.assert_not_called()
        self.assertEqual(found, [])
        worker = ImouAccountWorker(ACCOUNT, "private-application-key")
        worker.devices_found.connect(found.append)
        with patch("app.ImouClient") as client, patch("app.save_device_secret", return_value=True) as save:
            client.return_value.devices.return_value = [DEVICE]
            worker.run()
            save.assert_called_once()
        self.assertEqual(found, [[DEVICE]])
        self.assertIsNone(worker.secret)

    def test_saved_account_and_cache_ignore_malformed_entries(self):
        self.window.imou_accounts = [ACCOUNT]
        self.window.imou_cameras = [imou_account_camera(ACCOUNT, DEVICE)]
        self.window.save_imou_accounts()
        self.assertEqual(load_imou_cameras(self.settings, load_imou_accounts(self.settings)), self.window.imou_cameras)
        for value in ('{}', '[null,1,{"email":4,"app_id":true}]', 'bad json'):
            self.settings.setValue(IMOU_ACCOUNTS_SETTING, value)
            self.settings.setValue(IMOU_CAMERAS_SETTING, value)
            self.assertEqual(load_imou_accounts(self.settings), [])
            self.assertEqual(load_imou_cameras(self.settings, [ACCOUNT]), [])

    def test_private_stream_reaches_player_only_over_ipc_and_recording_uses_fresh_url(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        command = mpv_rtsp_command(Path("/tmp/test-imou.sock"), 1, False, camera)
        self.assertIn("--idle=yes", command)
        self.assertNotIn("--no-audio", command)
        self.assertNotIn(STREAM_URL, " ".join(command))
        socket_path = Path(self.directory.name) / "socket"
        socket_path.touch()
        player = Mock()
        player.poll.return_value = None
        worker = RtspStreamWorker(camera, player, socket_path)
        with patch("app.stored_secret", return_value="private-application-key"), patch("app.ImouClient") as client, patch(
            "app.mpv_request", return_value=(True, None)
        ) as ipc, patch("app.RtspWebSocketTunnel") as transport:
            client.return_value.secure_stream_url.return_value = STREAM_URL
            transport.return_value.url = "rtsp://127.0.0.1:40000/recording"
            worker._load_imou_stream()
            ipc.assert_called_once_with(socket_path, ["loadfile", transport.return_value.url, "replace"])
            client.return_value.secure_stream_url.return_value = STREAM_URL + "-renewed"

            def start_recorder(arguments, **options):
                self.assertIn(transport.return_value.url, arguments)
                self.assertNotIn("private-stream-token", " ".join(arguments))
                self.assertIn("0:a?", arguments)
                process = Mock(returncode=0)
                process.poll.return_value = 0
                return process

            with patch("app.subprocess.Popen", side_effect=start_recorder):
                process = worker._ffmpeg(Path(self.directory.name) / "clip.mkv", False)
            self.assertEqual(client.return_value.secure_stream_url.call_count, 2)
            transport.assert_called_with(STREAM_URL + "-renewed")
            worker._finish_recording_process(process)
            transport.return_value.close.assert_called_once()
            self.assertEqual(worker.recording_tunnels, {})

    def test_failed_quality_change_preserves_live_transport(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/test.sock"))
        worker.imou_client = Mock()
        previous = Mock()
        worker.imou_tunnel = previous
        with patch("app.RtspWebSocketTunnel") as transport, patch("app.mpv_request", return_value=(False, None)):
            with self.assertRaises(ImouError):
                worker._change_imou_quality("SD")
            self.assertIs(worker.imou_tunnel, previous)
            self.assertEqual(worker.quality, "HD")
            previous.close.assert_not_called()
            transport.return_value.close.assert_called_once()
        self.assertTrue(worker.queue_setting("quality", "SD"))
        self.assertFalse(worker.queue_setting("quality", "LD"))

    def test_cloud_continuous_recording_requires_account_opt_in(self):
        worker = RtspStreamWorker(imou_account_camera(ACCOUNT, DEVICE), Mock(), Path("/tmp/test.sock"))
        worker.set_continuous(True)
        self.assertFalse(worker.continuous_enabled.is_set())
        worker.camera = imou_account_camera(replace(ACCOUNT, cloud_recording=True), DEVICE)
        worker.set_continuous(True)
        self.assertTrue(worker.continuous_enabled.is_set())
        worker.set_continuous(False)
        self.assertFalse(worker.continuous_enabled.is_set())

    def test_cancelled_camera_download_preserves_a_previous_saved_clip(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        worker = ImouReplayWorker(camera)
        worker.client = Mock()
        start = datetime(2026, 9, 30, 2, 0)
        recording = ImouRecording("private-record", start, start + timedelta(minutes=5), False, 12345)
        target = Path(self.directory.name) / "saved.mkv"
        target.write_bytes(b"previous complete recording")
        process = Mock()
        process.poll.return_value = None

        def start_download(arguments, **options):
            Path(arguments[-1]).write_bytes(b"partial recording")
            worker.stopped.set()
            return process

        with patch("app.ImouReplayTunnel") as tunnel, patch("app.subprocess.Popen", side_effect=start_download), patch.object(
            RtspStreamWorker, "_finish"
        ):
            tunnel.return_value.error = ""
            worker._save(recording, target)
            tunnel.return_value.close.assert_called_once()
        self.assertEqual(target.read_bytes(), b"previous complete recording")
        self.assertEqual(sorted(path.name for path in target.parent.iterdir()), ["saved.mkv"])

    def test_camera_navigation_remains_available_during_a_download(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        worker = ImouReplayWorker(camera)
        start = datetime(2026, 9, 30, 2, 0)
        recording = ImouRecording("private-record", start, start + timedelta(minutes=5), False, 12345)
        downloading = threading.Event()
        release = threading.Event()

        def save(*args, **kwargs):
            downloading.set()
            release.wait(5)

        with patch("app.stored_secret", return_value="private-application-key"), patch("app.ImouClient"), patch(
            "app.ImouReplayTunnel"
        ) as transport, patch.object(worker, "_save", side_effect=save):
            try:
                worker.start()
                worker.save_recording(recording, Path(self.directory.name) / "clip.mkv")
                self.assertTrue(downloading.wait(2))
                self.assertFalse(worker.save_recording(recording, Path(self.directory.name) / "duplicate.mkv"))
                worker.open_recording(recording)
                deadline = time.monotonic() + 2
                while not transport.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(transport.called)
                self.assertFalse(release.is_set())
            finally:
                worker.stop()
                release.set()
                self.assertTrue(worker.wait(5000))

    def test_camera_seek_uses_the_requested_camera_time_and_keeps_the_catalog_identity(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        worker = ImouReplayWorker(camera)
        start = datetime(2026, 9, 30, 2)
        recording = ImouRecording("private-record", start, start + timedelta(minutes=5), False, 12345)
        with patch("app.stored_secret", return_value="private-key"), patch("app.ImouClient") as client, patch(
            "app.ImouReplayTunnel"
        ) as transport:
            try:
                worker.open_recording(recording, 60)
                worker.open_recording(recording, 120)
                worker.start()
                deadline = time.monotonic() + 2
                while not transport.called and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(transport.called)
                requested = client.return_value.replay_url.call_args.args[1]
                self.assertEqual(requested.start, start + timedelta(seconds=120))
                self.assertEqual(requested.end, recording.end)
                self.assertEqual(requested.key, recording.key)
                self.assertEqual(transport.call_count, 1)
            finally:
                worker.stop()
                self.assertTrue(worker.wait(5000))

    def test_imou_pan_controls_use_the_existing_movement_panel(self):
        camera = imou_account_camera(ACCOUNT, replace(DEVICE, abilities="PT"))
        preview = CameraPreview(camera, settings=self.settings)
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/test.sock"))
        worker.imou_client = Mock()
        preview.worker = worker
        preview.live = True
        preview._on_rtsp_ptz_available()
        self.assertFalse(preview.ptz_panel.buttons[0].isHidden())
        self.assertTrue(preview.ptz_panel.presets.isHidden())
        preview.move_camera("Left")
        self.assertEqual(worker.ptz_request, "Left")
        self.assertTrue(preview.control_pending)
        self.assertFalse(preview.ptz_button.isEnabled())
        preview._on_control_finished("Left")
        self.assertTrue(preview.ptz_button.isEnabled())
        preview.worker = None
        preview.stop()

    def test_camera_playback_uses_the_shared_pane_without_changing_other_live_cameras(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        existing = RtspCamera("rtsp:existing", "Entrance", "rtsp://192.0.2.1/video")
        self.window.devices = [existing, camera]
        self.window.selected_device = existing
        self.window.device_accounts = {existing.uid: "rtsp", camera.uid: "rtsp"}
        with patch.object(CameraPreview, "start"), patch("app.ImouReplayWorker") as remote:
            self.window.sync_previews()
            live_worker = Mock()
            self.window.previews[camera.uid].worker = live_worker
            self.window.open_camera_sd_replay(camera)
            pane = self.window.local_replays[camera.uid]
            self.assertIsInstance(pane, LocalReplayPane)
            self.assertTrue(pane.camera_recordings)
            self.assertIs(self.window.selected_device, existing)
            self.assertIs(self.window.previews[camera.uid].worker, live_worker)
            remote.return_value.list_range.reset_mock()
            now = datetime.now()
            pane.load_remote_range(now - timedelta(hours=1), now)
            first_count = remote.return_value.list_range.call_count
            pane.load_remote_range(now - timedelta(minutes=30), now)
            self.assertGreaterEqual(first_count, 1)
            self.assertEqual(remote.return_value.list_range.call_count, first_count)
            self.window.close_local_replay(camera.uid)
            remote.return_value.stop.assert_called_once()
            self.assertIn(pane, self.window.retired_replays)
            pane.on_remote_finished()
            self.assertNotIn(pane, self.window.retired_replays)
            with patch.object(pane, "start_replay_player") as player:
                pane.current_path = Path("late")
                pane.on_remote_playback("late", "rtsp://127.0.0.1:12345/recording")
                player.assert_not_called()
            self.window.previews[camera.uid].worker = None

    def test_privacy_creates_no_player_and_does_not_retry(self):
        camera = imou_account_camera(ACCOUNT, replace(DEVICE, privacy=True))
        preview = CameraPreview(camera, settings=self.settings)
        with patch("app.subprocess.Popen") as player:
            preview.start()
            player.assert_not_called()
        self.assertIn(IMOU_PRIVACY_MESSAGE, preview.status_overlay.label.text())
        self.assertFalse(preview.status_overlay.timer.isActive())
        self.assertFalse(preview.retry_timer.isActive())
        preview.stop()
        self.window.devices = [camera]
        self.window.device_accounts = {camera.uid: "rtsp"}
        self.window.selected_device = camera
        with patch.object(self.window, "start_player") as start:
            self.window.watch_live()
            start.assert_not_called()

    def test_exhausted_imou_traffic_stops_retries_in_main_and_preview(self):
        message = "Imou cloud traffic is exhausted. Add traffic in Imou Cloud, then refresh the camera list."
        camera = imou_account_camera(ACCOUNT, DEVICE)
        preview = CameraPreview(camera, settings=self.settings)
        try:
            preview._on_failed(message)
            preview._on_finished()
            self.assertFalse(preview.retry_timer.isActive())
            self.assertFalse(preview.retry_enabled)
            self.window.selected_device = camera
            self.window.on_stream_error(message)
            self.assertFalse(self.window.retry_pending)
            self.assertIn(message, preview.status_overlay.label.text())
            preview._on_failed("Temporary network failure")
            self.window.on_stream_error("Temporary network failure")
            self.assertTrue(preview.retry_enabled)
            self.assertTrue(self.window.retry_pending)
        finally:
            preview.stop()

    def test_manual_camera_refresh_retries_a_quota_blocked_imou_preview(self):
        camera = imou_account_camera(ACCOUNT, DEVICE)
        preview = CameraPreview(camera, settings=self.settings)
        preview.retry_enabled = False
        self.window.devices = [camera]
        self.window.previews[camera.uid] = preview
        with patch.object(preview, "start") as start, patch.object(self.window, "refresh_imou_accounts"), \
                patch.object(self.window, "on_account_finished"):
            self.window.refresh_cameras()
            start.assert_called_once_with()

    def test_refresh_adds_imou_without_restarting_the_existing_camera(self):
        existing = RtspCamera("rtsp:existing", "Entrance", "rtsp://192.0.2.1/video")
        self.window.devices = [existing]
        self.window.selected_device = existing
        self.window.device_accounts = {existing.uid: "rtsp"}
        active_worker = Mock()
        self.window.stream_worker = active_worker
        with patch("app.ImouAccountWorker", FakeImouWorker), patch.object(CameraPreview, "start"), patch.object(
            self.window, "watch_live"
        ) as watch:
            self.window.start_imou_lookup(ACCOUNT)
            self.window.imou_worker.devices_found.emit([DEVICE])
            self.window.imou_worker.finished.emit()
        self.assertIs(self.window.stream_worker, active_worker)
        self.assertEqual(len(self.window.devices), 2)
        active_worker.stop.assert_not_called()
        watch.assert_not_called()
        self.window.stream_worker = None

    def test_shutdown_waits_for_account_worker_and_does_not_add_late_devices(self):
        with patch("app.ImouAccountWorker", FakeImouWorker):
            self.window.start_imou_lookup(ACCOUNT)
        worker = self.window.imou_worker
        self.window.quit_requested = True
        self.window.close()
        self.assertTrue(self.window.close_pending)
        worker.requestInterruption.assert_called_once()
        worker.devices_found.emit([DEVICE])
        self.assertEqual(self.window.devices, [])
        worker.finished.emit()
        self.assertIsNone(self.window.imou_worker)

    def test_account_removal_clears_keyring_and_preserves_other_cameras(self):
        local = RtspCamera("rtsp:other", "Front", "rtsp://192.0.2.1/video")
        camera = imou_account_camera(ACCOUNT, DEVICE)
        self.window.imou_accounts = [ACCOUNT]
        self.window.imou_cameras = [camera]
        self.window.devices = [local, camera]
        self.window.selected_device = local
        self.window.device_accounts = {local.uid: "rtsp", camera.uid: "rtsp"}
        with patch("app.stored_secret", return_value="saved-key"), patch(
            "app.clear_device_secret", return_value=False
        ):
            self.window.remove_imou_account(ACCOUNT)
        self.assertEqual(self.window.imou_cameras, [camera])
        with patch("app.stored_secret", side_effect=lambda category, identifier:
                   "saved-key" if category == IMOU_ACCOUNT_PROVIDER else None), patch(
            "app.clear_device_secret", return_value=True
        ) as clear:
            self.window.remove_imou_account(ACCOUNT)
        clear.assert_called_once_with(IMOU_ACCOUNT_PROVIDER, ACCOUNT.app_id)
        self.assertEqual(self.window.devices, [local])
        self.assertEqual(load_imou_accounts(self.settings), [])


if __name__ == "__main__":
    unittest.main()
