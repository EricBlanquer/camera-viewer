import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, MainWindow, RtspCamera


class CameraVisibilityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()
        self.first = SimpleNamespace(uid="garden", name="Garden")
        self.second = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:554/stream")
        self.window.devices = [self.first, self.second]
        self.window.device_accounts = {self.first.uid: "account@example.com", self.second.uid: "rtsp"}
        self.window.selected_device = self.first
        self.preview_start = patch.object(CameraPreview, "start")
        self.preview_start.start()
        self.window.watch_live = Mock()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.preview_start.stop()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def camera_actions(self):
        self.window.update_cameras_menu()
        return self.window.cameras_menu.actions()[:2]

    def test_checking_second_camera_keeps_first_checked_and_running(self) -> None:
        self.settings.setValue("view/show_all_cameras", False)
        self.settings.setValue("camera/selected_uid", self.first.uid)
        self.window.sync_previews()
        first, second = self.camera_actions()
        self.assertTrue(first.isChecked())
        self.assertFalse(second.isChecked())
        second.trigger()
        first, second = self.camera_actions()
        self.assertTrue(first.isChecked())
        self.assertTrue(second.isChecked())
        self.assertIs(self.window.selected_device, self.first)
        self.assertIn(self.second.uid, self.window.previews)
        self.window.watch_live.assert_not_called()
        self.assertNotIn("Show all cameras", [action.text() for action in self.window.tray.contextMenu().actions()])

    def test_unchecking_preview_stops_only_that_camera_and_survives_restart(self) -> None:
        self.window.sync_previews()
        preview = self.window.previews[self.second.uid]
        with patch.object(preview, "stop", wraps=preview.stop) as stopped:
            self.camera_actions()[1].trigger()
        stopped.assert_called_once_with()
        self.assertEqual(self.window.previews, {})
        self.assertTrue(self.camera_actions()[0].isChecked())
        self.assertFalse(self.camera_actions()[1].isChecked())
        self.window.watch_live.assert_not_called()
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            restarted = MainWindow()
        try:
            restarted.devices = [self.first, self.second]
            restarted.selected_device = self.first
            restarted.sync_previews()
            self.assertEqual(restarted.previews, {})
        finally:
            restarted.quit_requested = True
            restarted.close()

    def test_unchecking_primary_promotes_only_a_checked_camera(self) -> None:
        self.window.sync_previews()
        self.camera_actions()[0].trigger()
        self.application.processEvents()
        self.assertIs(self.window.selected_device, self.second)
        self.assertFalse(self.camera_actions()[0].isChecked())
        self.assertTrue(self.camera_actions()[1].isChecked())
        self.assertEqual(self.window.previews, {})
        self.window.watch_live.assert_called_once_with()

    def test_all_cameras_can_be_unchecked_and_checked_again(self) -> None:
        self.window.sync_previews()
        self.camera_actions()[1].trigger()
        self.camera_actions()[0].trigger()
        self.application.processEvents()
        self.assertEqual(self.window.previews, {})
        self.assertFalse(self.window.primary_pane.isVisibleTo(self.window))
        self.assertEqual(self.window.status_text, "No cameras selected.")
        with patch.object(self.window, "start_player") as player:
            MainWindow.watch_live(self.window)
            self.window.reconnect()
        player.assert_not_called()
        self.window.watch_live.assert_not_called()
        self.camera_actions()[0].trigger()
        self.assertTrue(self.camera_actions()[0].isChecked())
        self.window.watch_live.assert_called_once_with()

    def test_hidden_primary_does_not_reconnect_after_async_stop(self) -> None:
        self.camera_actions()[1].trigger()
        worker = SimpleNamespace(stop=Mock())
        self.window.stream_worker = worker
        self.camera_actions()[0].trigger()
        worker.stop.assert_called_once_with()
        self.window.retry_pending = True
        self.window.on_stream_finished()
        self.assertFalse(self.window.reconnect_timer.isActive())
        self.assertEqual(self.window.status_text, "No cameras selected.")

    def test_rechecking_preview_waits_for_its_previous_connection_to_stop(self) -> None:
        self.window.sync_previews()
        preview = self.window.previews[self.second.uid]
        preview.stop = Mock()
        self.camera_actions()[1].trigger()
        self.camera_actions()[1].trigger()
        self.assertNotIn(self.second.uid, self.window.previews)
        self.assertIn(preview, self.window.retired_previews)
        preview.stopped.emit()
        self.application.processEvents()
        self.assertIn(self.second.uid, self.window.previews)
        self.assertNotIn(preview, self.window.retired_previews)


if __name__ == "__main__":
    unittest.main()
