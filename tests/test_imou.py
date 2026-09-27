import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox, QLineEdit

from app import (
    IMOU_PROVIDER,
    RTSP_CAMERAS_SETTING,
    CameraPreview,
    MainWindow,
    RtspCamera,
    RtspStreamWorker,
    camera_stream_url,
    icam365_light_request,
    imou_mpv_playlist,
    imou_rtsp_url,
    load_rtsp_cameras,
    mpv_rtsp_command,
)


class ImouTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_imou_main_stream_address_is_local_and_selects_requested_channel(self) -> None:
        self.assertEqual(
            imou_rtsp_url("192.168.1.108", 8554, 2),
            "rtsp://192.168.1.108:8554/cam/realmonitor?channel=2&subtype=0",
        )
        for address in ("127.0.0.1", "8.8.8.8", "invalid", "192.168.1.108@elsewhere"):
            with self.subTest(address=address), self.assertRaises(ValueError):
                imou_rtsp_url(address)
        for port, channel in ((0, 1), (65536, 1), (554, 0), (554, 129)):
            with self.subTest(port=port, channel=channel), self.assertRaises(ValueError):
                imou_rtsp_url("192.168.1.108", port, channel)

    def test_keyring_secret_is_used_for_live_and_recording_but_not_settings(self) -> None:
        camera = RtspCamera(
            "rtsp:imou1", "Garage", imou_rtsp_url("192.168.1.108"), "tcp", IMOU_PROVIDER, "admin",
        )
        self.window.rtsp_cameras = [camera]
        self.window.save_rtsp_cameras()
        self.assertEqual(load_rtsp_cameras(self.settings), [camera])
        self.assertNotIn("p@ss word", Path(self.settings.fileName()).read_text())
        with patch("app.stored_secret", return_value="p@ss word"):
            playlist_fd = imou_mpv_playlist(camera)
            try:
                command = mpv_rtsp_command(Path("/tmp/imou-test.sock"), 1, True, camera, playlist_fd=playlist_fd)
                self.assertEqual(command[-1], f"--playlist=/proc/self/fd/{playlist_fd}")
                self.assertNotIn("p%40ss%20word", " ".join(command))
                self.assertIn("rtsp://admin:p%40ss%20word@192.168.1.108:554/", os.read(playlist_fd, 4096).decode())
            finally:
                os.close(playlist_fd)
            worker = RtspStreamWorker(camera, Mock(), Path("/tmp/imou-test.sock"))

            def inspect_recorder(command: list[str], **options: object) -> Mock:
                descriptor = options["pass_fds"][0]
                playlist = os.read(descriptor, 4096).decode()
                self.assertIn("rtsp://admin:p%40ss%20word@192.168.1.108:554/", playlist)
                self.assertIn("option rtsp_transport tcp", playlist)
                self.assertNotIn("p%40ss%20word", " ".join(command))
                return Mock()

            with patch("app.subprocess.Popen", side_effect=inspect_recorder) as process:
                worker._ffmpeg(Path("/tmp/imou-test.mkv"), False)
            self.assertEqual(len(process.call_args.kwargs["pass_fds"]), 1)
        with patch("app.stored_secret", return_value=None), self.assertRaises(OSError):
            camera_stream_url(camera)
        with patch("app.stored_secret", return_value=None):
            self.assertFalse(self.window.start_player(camera))
        self.assertIn("Imou camera password is unavailable", self.window.status_text)
        with patch("app.http.client.HTTPConnection") as connection:
            self.assertFalse(icam365_light_request(camera))
            connection.assert_not_called()

    def test_incomplete_imou_settings_are_ignored(self) -> None:
        self.settings.setValue(RTSP_CAMERAS_SETTING, json.dumps([{
            "uid": "rtsp:imou1", "name": "Garage", "url": imou_rtsp_url("192.168.1.108"),
            "provider": IMOU_PROVIDER,
        }]))
        self.assertEqual(load_rtsp_cameras(self.settings), [])

    def test_tray_adds_imou_camera_and_selects_it_by_name(self) -> None:
        self.window.select_camera = Mock()

        def fill_dialog(dialog: QDialog) -> int:
            edits = {edit.placeholderText(): edit for edit in dialog.findChildren(QLineEdit)}
            edits["Driveway"].setText("Garage")
            edits["192.168.1.100"].setText("192.168.1.108")
            edits["Camera safety code or device password"].setText("private-code")
            dialog.findChild(QDialogButtonBox).accepted.emit()
            return dialog.result()

        with patch.object(QDialog, "exec", fill_dialog), patch("app.save_device_secret", return_value=True) as save:
            self.window.add_imou_camera()
        camera = self.window.rtsp_cameras[0]
        self.assertEqual(camera.provider, IMOU_PROVIDER)
        self.assertEqual(camera.username, "admin")
        self.assertEqual(camera.url, imou_rtsp_url("192.168.1.108"))
        save.assert_called_once_with(IMOU_PROVIDER, camera.uid, "private-code", "Imou camera device password")
        self.window.select_camera.assert_called_once_with("rtsp", camera.uid)
        self.assertNotIn("private-code", Path(self.settings.fileName()).read_text())
        self.window.selected_device = camera
        self.window.update_cameras_menu()
        self.assertEqual(self.window.cameras_menu.actions()[0].text(), "Garage · Imou Life (local)")

    def test_removal_clears_keyring_secret(self) -> None:
        camera = RtspCamera(
            "rtsp:imou1", "Garage", imou_rtsp_url("192.168.1.108"), "tcp", IMOU_PROVIDER, "admin",
        )
        self.window.rtsp_cameras = [camera]
        self.window.devices = [camera]
        self.window.device_accounts = {camera.uid: "rtsp"}
        self.window.selected_device = camera
        with patch("app.stored_secret", return_value="private-code"), patch(
            "app.clear_device_secret", return_value=True
        ) as clear, patch.object(self.window, "stop_stream"):
            self.window.remove_selected_rtsp_camera()
        clear.assert_called_once_with(IMOU_PROVIDER, camera.uid)
        self.assertEqual(load_rtsp_cameras(self.settings), [])

    def test_removal_allows_a_missing_keyring_secret(self) -> None:
        camera = RtspCamera(
            "rtsp:imou1", "Garage", imou_rtsp_url("192.168.1.108"), "tcp", IMOU_PROVIDER, "admin",
        )
        self.window.rtsp_cameras = [camera]
        self.window.devices = [camera]
        self.window.device_accounts = {camera.uid: "rtsp"}
        self.window.selected_device = camera
        with patch("app.stored_secret", return_value=None), patch(
            "app.clear_device_secret"
        ) as clear, patch.object(self.window, "stop_stream"):
            self.window.remove_selected_rtsp_camera()
        clear.assert_not_called()
        self.assertEqual(load_rtsp_cameras(self.settings), [])

    def test_two_imou_cameras_join_the_existing_multi_camera_grid(self) -> None:
        garden = SimpleNamespace(uid="garden", name="Jardin")
        first = RtspCamera(
            "rtsp:imou1", "Garage", imou_rtsp_url("192.168.1.108"), "tcp", IMOU_PROVIDER, "admin",
        )
        second = RtspCamera(
            "rtsp:imou2", "Couloir", imou_rtsp_url("192.168.1.210"), "tcp", IMOU_PROVIDER, "admin",
        )
        self.window.devices = [garden, first, second]
        self.window.device_accounts = {garden.uid: "account@example.com", first.uid: "rtsp", second.uid: "rtsp"}
        self.window.selected_device = garden
        with patch.object(CameraPreview, "start") as start:
            self.window.sync_previews()
        self.assertEqual(start.call_count, 2)
        self.assertIs(self.window.video_grid.itemAtPosition(0, 1).widget(), self.window.previews[first.uid])
        self.assertIs(self.window.video_grid.itemAtPosition(1, 0).widget(), self.window.previews[second.uid])


if __name__ == "__main__":
    unittest.main()
