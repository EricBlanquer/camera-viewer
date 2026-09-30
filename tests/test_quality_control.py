import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, IMOU_ACCOUNT_PROVIDER, MainWindow, RtspCamera


class QualityControlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.camera = RtspCamera("imou:kitchen", "Kitchen", "", provider=IMOU_ACCOUNT_PROVIDER)
        self.preview = CameraPreview(self.camera, settings=self.settings)

    def tearDown(self) -> None:
        self.preview.stop()
        self.preview.close()
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_imou_main_quality_remains_enabled_after_live_status(self) -> None:
        self.window.selected_device = self.camera
        self.window.on_capabilities_found(["HD", "SD"], None)
        self.window.on_stream_status("Live video")
        self.assertTrue(self.window.quality_button.isEnabled())

    def test_plain_rtsp_preview_accepts_empty_capabilities_without_quality_controls(self) -> None:
        preview = CameraPreview(RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.1/video"))
        try:
            preview._on_capabilities([], None)
            self.assertFalse(preview.quality_button.isVisibleTo(preview))
            self.assertFalse(preview.quality_button.isEnabled())
        finally:
            preview.close()

    def test_main_and_preview_offer_the_same_supported_qualities_in_either_signal_order(self) -> None:
        self.window.selected_device = self.camera
        self.settings.setValue(f"camera/quality/{self.camera.uid}", "HD")
        self.preview._on_capabilities(["HD", "SD"], None)
        self.assertFalse(self.preview.quality_button.isEnabled())
        self.preview._on_status("Live video")
        self.window.on_stream_status("Live video")
        self.window.on_capabilities_found(["HD", "SD"], None)
        for pane in (self.window, self.preview):
            self.assertEqual([name for name, action in pane.quality_actions.items() if action.isVisible()], ["HD", "SD"])
            self.assertEqual(pane.quality_button.text(), "HD")
            self.assertTrue(pane.quality_actions["HD"].isChecked())
            self.assertTrue(pane.quality_button.isEnabled())

    def test_preview_rejected_quality_restores_selection_while_recording(self) -> None:
        self.settings.setValue(f"camera/quality/{self.camera.uid}", "HD")
        self.preview.live = True
        self.preview._on_capabilities(["HD", "SD"], None)
        self.preview.worker = SimpleNamespace(queue_setting=Mock(return_value=True), stop=Mock())
        self.preview.recording_path = Path("recording.mkv")
        self.preview.quality_actions["SD"].trigger()
        self.preview.worker.queue_setting.assert_not_called()
        self.assertTrue(self.preview.quality_actions["HD"].isChecked())
        self.assertEqual(self.preview.quality_button.text(), "HD")
        self.preview.worker = None

    def test_okam_and_imou_use_the_same_pending_success_and_failure_behavior(self) -> None:
        for camera in (self.camera, SimpleNamespace(uid="garden", name="Garden")):
            with self.subTest(camera=camera.name):
                preview = CameraPreview(camera, settings=self.settings)
                preview.worker = SimpleNamespace(queue_setting=Mock(return_value=True))
                try:
                    self.settings.setValue(f"camera/quality/{camera.uid}", "HD")
                    preview._on_capabilities(["HD", "SD"], None)
                    preview._on_status("Live video")
                    preview.quality_actions["SD"].trigger()
                    preview.worker.queue_setting.assert_called_once_with("quality", "SD")
                    self.assertFalse(preview.quality_button.isEnabled())
                    self.assertFalse(preview.quality_menu.isEnabled())
                    preview._on_setting_completed("quality", "SD")
                    self.assertTrue(preview.quality_button.isEnabled())
                    self.assertEqual(preview.quality_button.text(), "SD")
                    self.assertEqual(self.settings.value(f"camera/quality/{camera.uid}"), "SD")
                    preview.quality_actions["HD"].trigger()
                    preview._on_setting_failed("quality", "Rejected")
                    self.assertTrue(preview.quality_button.isEnabled())
                    self.assertTrue(preview.quality_actions["SD"].isChecked())
                    self.assertEqual(preview.quality_button.text(), "SD")
                finally:
                    preview.worker = None
                    preview.close()

    def test_switching_camera_clears_capabilities_and_restores_its_own_quality(self) -> None:
        first = self.camera
        second = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.1/video")
        self.settings.setValue(f"camera/quality/{first.uid}", "HD")
        self.settings.setValue(f"camera/quality/{second.uid}", "SD")
        self.window.selected_device = first
        self.window.stream_live = True
        self.window.on_capabilities_found(["HD", "SD"], None)
        self.assertTrue(self.window.quality_button.isEnabled())
        self.window.selected_device = second
        self.window.sync_quality_actions()
        self.assertFalse(self.window.quality_button.isVisibleTo(self.window))
        self.assertFalse(self.window.quality_button.isEnabled())
        self.assertEqual(self.window.quality_button.text(), "SD")
        self.window.selected_device = first
        self.window.on_capabilities_found(["HD", "SD"], None)
        self.assertTrue(self.window.quality_button.isEnabled())
        self.assertEqual(self.window.quality_button.text(), "HD")


if __name__ == "__main__":
    unittest.main()
