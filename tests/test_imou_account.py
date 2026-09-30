import json
import os
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject, QSettings, pyqtSignal
from PyQt6.QtWidgets import QApplication, QDialog

from imou import IMOU_PRIVACY_MESSAGE, ImouAccount, ImouClient, ImouDevice, ImouError, imou_signature
from app import (
    IMOU_ACCOUNT_PROVIDER, IMOU_ACCOUNTS_SETTING, IMOU_CAMERAS_SETTING,
    CameraPreview, ImouAccountDialog, ImouAccountWorker, MainWindow, RtspCamera, RtspStreamWorker,
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
        self.assertIn("--no-audio", command)
        self.assertIn("--demuxer-lavf-o-add=use_wallclock_as_timestamps=1", command)
        self.assertNotIn(STREAM_URL, " ".join(command))
        socket_path = Path(self.directory.name) / "socket"
        socket_path.touch()
        player = Mock()
        player.poll.return_value = None
        worker = RtspStreamWorker(camera, player, socket_path)
        with patch("app.stored_secret", return_value="private-application-key"), patch("app.ImouClient") as client, patch(
            "app.mpv_request", return_value=(True, None)
        ) as ipc:
            client.return_value.stream_url.return_value = STREAM_URL
            worker._load_imou_stream()
            ipc.assert_called_once_with(socket_path, ["loadfile", STREAM_URL, "replace"])
            client.return_value.stream_url.return_value = STREAM_URL + "-renewed"

            def start_recorder(arguments, **options):
                fd = options["pass_fds"][0]
                playlist = os.read(fd, 4096).decode()
                self.assertIn(STREAM_URL + "-renewed", playlist)
                self.assertIn("option use_wallclock_as_timestamps 1", playlist)
                self.assertNotIn("private-stream-token", " ".join(arguments))
                self.assertNotIn("0:a?", arguments)
                return Mock()

            with patch("app.subprocess.Popen", side_effect=start_recorder):
                worker._ffmpeg(Path(self.directory.name) / "clip.mkv", False)
            self.assertEqual(client.return_value.stream_url.call_count, 2)

    def test_cloud_continuous_recording_requires_account_opt_in(self):
        worker = RtspStreamWorker(imou_account_camera(ACCOUNT, DEVICE), Mock(), Path("/tmp/test.sock"))
        worker.set_continuous(True)
        self.assertFalse(worker.continuous_enabled.is_set())
        worker.camera = imou_account_camera(replace(ACCOUNT, cloud_recording=True), DEVICE)
        worker.set_continuous(True)
        self.assertTrue(worker.continuous_enabled.is_set())
        worker.set_continuous(False)
        self.assertFalse(worker.continuous_enabled.is_set())

    def test_privacy_creates_no_player_and_does_not_retry(self):
        camera = imou_account_camera(ACCOUNT, replace(DEVICE, privacy=True))
        preview = CameraPreview(camera, settings=self.settings)
        with patch("app.subprocess.Popen") as player:
            preview.start()
            player.assert_not_called()
        self.assertIn(IMOU_PRIVACY_MESSAGE, preview.label.text())
        self.assertFalse(preview.retry_timer.isActive())
        preview.stop()
        self.window.devices = [camera]
        self.window.device_accounts = {camera.uid: "rtsp"}
        self.window.selected_device = camera
        with patch.object(self.window, "start_player") as start:
            self.window.watch_live()
            start.assert_not_called()

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
        with patch("app.stored_secret", return_value="saved-key"), patch(
            "app.clear_device_secret", return_value=True
        ) as clear:
            self.window.remove_imou_account(ACCOUNT)
        clear.assert_called_once_with(IMOU_ACCOUNT_PROVIDER, ACCOUNT.app_id)
        self.assertEqual(self.window.devices, [local])
        self.assertEqual(load_imou_accounts(self.settings), [])


if __name__ == "__main__":
    unittest.main()
