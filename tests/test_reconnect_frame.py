import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtGui import QColor, QPixmap
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, MainWindow, RtspCamera


class ReconnectFrameTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_frame_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        self.camera = RtspCamera("camera", "Kitchen", "rtsp://example.invalid")
        self.color = QColor("#277baa")
        self.snapshot_paths = []

    def tearDown(self):
        self.directory.cleanup()

    def player_response(self, socket_path, command):
        if command[0] == "screenshot-to-file":
            image = QPixmap(640, 360)
            image.fill(self.color)
            self.snapshot_paths.append(Path(command[1]))
            self.assertTrue(image.save(command[1]))
            return True, None
        if command == ["get_property", "video-out-params"]:
            return True, {"w": 640, "h": 360}
        return True, 1.0

    def assert_camera_image(self, video):
        self.application.processEvents()
        center = video.rect().center()
        self.assertEqual(video.grab().toImage().pixelColor(center), self.color)
        self.assertTrue(self.snapshot_paths)
        self.assertTrue(all(not path.exists() for path in self.snapshot_paths))

    def test_preview_keeps_camera_image_until_replacement_player_has_a_frame(self):
        preview = CameraPreview(self.camera, settings=self.settings)
        preview.resize(640, 360)
        preview.show()
        preview.mpv_directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_mpv_")
        preview._on_status("Live video")
        try:
            with patch("app.mpv_request", side_effect=self.player_response), patch("app.stop_mpv_player"):
                preview._on_failed("The camera stopped sending video.")
                preview._on_finished()
                preview.retry_timer.stop()
                self.assert_camera_image(preview.video)
                preview.resize(800, 450)
                self.assert_camera_image(preview.video)
                preview.show_status("Connecting...", True)
                self.assert_camera_image(preview.video)
                preview.mpv_directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_mpv_")
                with patch("app.mpv_request", return_value=(True, None)):
                    preview._on_status("Live video")
                    preview.video.refresh_reconnected_frame()
                    self.assert_camera_image(preview.video)
                preview.video.refresh_reconnected_frame()
                self.assertTrue(preview.video.retained_frame.isNull())
                self.assertFalse(preview.video.frame_placeholder.isVisible())
        finally:
            preview.stop()
            preview.close()

    def test_primary_camera_keeps_frame_across_retry_but_clears_it_on_camera_change(self):
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            window = MainWindow()
        window.devices = [self.camera]
        window.selected_device = self.camera
        window.device_accounts = {self.camera.uid: "test"}
        window.show()
        window.mpv_socket = Path(self.directory.name) / "control.sock"
        window.stream_live = True
        try:
            with patch("app.mpv_request", side_effect=self.player_response), patch("app.stop_mpv_player"):
                window.on_stream_error("The camera stopped sending video.")
                window.on_stream_finished()
                window.reconnect_timer.stop()
                self.assert_camera_image(window.video)
                window.stop_player(preserve_frame=True)
                self.assert_camera_image(window.video)
                window.stop_player()
                self.assertTrue(window.video.retained_frame.isNull())
        finally:
            window.quit_requested = True
            window.close()


if __name__ == "__main__":
    unittest.main()
