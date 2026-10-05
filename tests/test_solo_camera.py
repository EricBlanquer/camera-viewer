import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app import ALL_CAMERAS_LABEL, CURSOR_HIDE_DELAY_MS, SOLO_CAMERA_LABEL, CameraPreview, MainWindow, RtspCamera


class SoloCameraTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.cameras = [RtspCamera(f"rtsp:{index}", f"Camera {index}", f"rtsp://192.0.2.{index + 1}/video")
                        for index in range(3)]
        self.window.devices = self.cameras
        self.window.selected_device = self.cameras[0]
        self.window.device_accounts = {camera.uid: "rtsp" for camera in self.cameras}
        self.preview_start = patch.object(CameraPreview, "start").start()
        self.fit = patch.object(MainWindow, "fit_video_aspect").start()
        self.window.set_camera_layout("grid")
        self.window.show()
        self.window.resize(900, 500)
        self.window.activateWindow()
        self.application.processEvents()
        self.normal_geometry = self.window.geometry()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        patch.stopall()
        self.directory.cleanup()

    def settle(self) -> None:
        for _ in range(3):
            self.application.processEvents()

    def panes(self) -> dict[str, object]:
        return {self.cameras[0].uid: self.window.primary_pane, **self.window.previews}

    def test_corner_button_appears_when_the_pointer_moves_over_one_of_several_videos(self) -> None:
        preview = self.window.previews[self.cameras[1].uid]
        corner = preview.corner_overlay
        self.assertFalse(corner.isVisible())
        self.assertEqual(corner.button.toolTip(), SOLO_CAMERA_LABEL)
        preview.video.pointer_moved.emit()
        self.settle()
        self.assertTrue(corner.isVisible())
        self.assertFalse(self.window.corner_overlay.isVisible())
        video = preview.video
        top_right = video.mapToGlobal(video.rect().topRight())
        self.assertLessEqual(corner.geometry().right(), top_right.x())
        self.assertGreaterEqual(corner.geometry().top(), top_right.y())
        self.assertGreater(corner.geometry().left(), video.mapToGlobal(video.rect().center()).x())
        self.assertEqual(corner.timer.interval(), CURSOR_HIDE_DELAY_MS)
        corner.timer.timeout.emit()
        self.assertFalse(corner.isVisible())
        self.window.set_camera_visible(self.cameras[1].uid, False)
        self.window.set_camera_visible(self.cameras[2].uid, False)
        self.settle()
        self.window.video.pointer_moved.emit()
        self.settle()
        self.assertFalse(self.window.corner_overlay.isVisible())

    def test_solo_button_shows_one_video_in_full_screen_and_returns_to_the_mosaic(self) -> None:
        solo = self.cameras[2].uid
        previews = dict(self.window.previews)
        self.preview_start.reset_mock()
        self.window.previews[solo].corner_overlay.button.click()
        self.settle()
        self.assertTrue(self.window.isFullScreen())
        self.assertEqual({uid for uid, pane in self.panes().items() if pane.isVisible()}, {solo})
        self.assertEqual(self.window.camera_grid_dimensions(), (1, 1))
        self.assertEqual(self.window.previews[solo].geometry(), self.window.video_grid.contentsRect())
        self.assertEqual(self.window.previews[solo].width(), self.window.video_grid.geometry().width())
        self.assertEqual(self.window.previews[solo].corner_overlay.button.toolTip(), ALL_CAMERAS_LABEL)
        self.assertEqual(self.window.previews, previews)
        self.window.previews[solo].corner_overlay.button.click()
        self.settle()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.geometry(), self.normal_geometry)
        self.assertTrue(all(pane.isVisible() for pane in self.panes().values()))
        self.assertEqual(self.window.camera_grid_dimensions(), (2, 2))
        self.assertEqual(self.window.previews[solo].corner_overlay.button.toolTip(), SOLO_CAMERA_LABEL)
        self.assertEqual(self.window.previews, previews)
        self.preview_start.assert_not_called()

    def test_hidden_videos_keep_their_overlays_out_of_the_solo_view(self) -> None:
        other = self.window.previews[self.cameras[1].uid]
        other.video.status_overlay.display("The camera stopped sending video.", True)
        self.settle()
        self.assertTrue(other.video.status_overlay.isVisible())
        self.window.toggle_solo_camera(self.cameras[0].uid)
        QTest.qWait(10)
        self.assertTrue(self.window.primary_pane.isVisible())
        self.assertFalse(other.isVisible())
        self.assertFalse(other.video.status_overlay.isVisible())
        self.window.toggle_solo_camera(self.cameras[0].uid)
        QTest.qWait(10)
        self.assertTrue(other.video.status_overlay.isVisible())

    def test_leaving_full_screen_by_double_click_ends_the_solo_view(self) -> None:
        self.window.toggle_solo_camera(self.cameras[1].uid)
        self.settle()
        self.window.previews[self.cameras[1].uid].video.double_clicked.emit(0, 0)
        self.settle()
        self.assertFalse(self.window.isFullScreen())
        self.assertIsNone(self.window.solo_uid)
        self.assertTrue(all(pane.isVisible() for pane in self.panes().values()))

    def test_solo_view_from_the_full_screen_mosaic_returns_to_it(self) -> None:
        self.window.toggle_fullscreen()
        self.settle()
        self.window.toggle_solo_camera(self.cameras[1].uid)
        self.settle()
        self.window.toggle_solo_camera(self.cameras[1].uid)
        self.settle()
        self.assertTrue(self.window.isFullScreen())
        self.assertIsNone(self.window.solo_uid)
        self.assertTrue(all(pane.isVisible() for pane in self.panes().values()))

    def test_hiding_the_solo_camera_restores_the_remaining_videos(self) -> None:
        self.window.toggle_solo_camera(self.cameras[2].uid)
        self.settle()
        self.window.set_camera_visible(self.cameras[2].uid, False)
        self.settle()
        self.assertIsNone(self.window.solo_uid)
        self.assertTrue(self.window.primary_pane.isVisible())
        self.assertTrue(self.window.previews[self.cameras[1].uid].isVisible())


if __name__ == "__main__":
    unittest.main()
