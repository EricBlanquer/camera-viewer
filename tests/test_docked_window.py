import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, MainWindow, RtspCamera, compute_mosaic, docked_span, mosaic_grid


class DockedSpanTest(unittest.TestCase):
    def test_halves_quarters_and_full_spans_touching_an_edge_are_docked(self) -> None:
        self.assertTrue(docked_span(0, 1280, 0, 2560))
        self.assertTrue(docked_span(1280, 1280, 0, 2560))
        self.assertTrue(docked_span(0, 2560, 0, 2560))
        self.assertTrue(docked_span(3, 1277, 0, 2560))

    def test_free_positions_and_sizes_are_not_docked(self) -> None:
        self.assertFalse(docked_span(100, 1280, 0, 2560))
        self.assertFalse(docked_span(0, 1000, 0, 2560))


class MosaicAspectFallbackTest(unittest.TestCase):
    def test_a_near_widescreen_area_uses_the_widescreen_mosaic(self) -> None:
        compute_mosaic(6, 1280, 676, tolerant=True)
        compute_mosaic(6, 2560, 1440)
        self.assertEqual(mosaic_grid(6, 1280, 676, tolerant=True), mosaic_grid(6, 2560, 1440))
        self.assertEqual(mosaic_grid(6, 1280, 676, tolerant=True)[1][0][2:], (2, 2))
        compute_mosaic(6, 1280, 676)
        self.assertIsNone(mosaic_grid(6, 1280, 676))

    def test_a_distant_aspect_keeps_the_uniform_grid(self) -> None:
        compute_mosaic(6, 1000, 1000, tolerant=True)
        self.assertIsNone(mosaic_grid(6, 1000, 1000, tolerant=True))


class DockedWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.cameras = [RtspCamera(f"rtsp:{index}", f"Camera {index}", f"rtsp://192.0.2.{index + 1}/video")
                        for index in range(5)]
        self.window.devices = self.cameras
        self.window.selected_device = self.cameras[0]
        self.window.device_accounts = {camera.uid: "rtsp" for camera in self.cameras}
        self.preview_start = patch.object(CameraPreview, "start")
        self.preview_start.start()
        self.window.set_camera_layout("grid")
        with patch.object(self.window, "fit_video_aspect"):
            self.window.show()
            self.window.setGeometry(100, 100, 300, 200)
            self.settle()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.preview_start.stop()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def settle(self) -> None:
        for _ in range(3):
            self.application.processEvents()

    def dock(self) -> None:
        self.window.showMaximized()
        self.settle()
        size = self.window.centralWidget().size()
        compute_mosaic(len(self.cameras), size.width(), size.height(), tolerant=True)
        self.window.update_fullscreen_mosaic()
        self.settle()

    def order(self) -> list[str]:
        return [camera.uid for camera in self.window.ordered_devices()]

    def test_a_docked_window_uses_the_mosaic(self) -> None:
        self.assertFalse(self.window.window_docked())
        self.dock()
        self.assertTrue(self.window.window_docked())
        size = self.window.centralWidget().size()
        mosaic = mosaic_grid(len(self.cameras), size.width(), size.height(), tolerant=True)
        self.assertIsNotNone(mosaic)
        self.assertEqual(self.window.camera_grid_dimensions(), (mosaic[0], mosaic[0]))

    def test_docked_mode_keeps_its_own_camera_order(self) -> None:
        windowed = self.order()
        self.dock()
        self.window.swap_cameras(windowed[0], windowed[1])
        self.settle()
        docked = [windowed[1], windowed[0], *windowed[2:]]
        self.assertEqual(self.order(), docked)
        self.window.showNormal()
        self.window.setGeometry(100, 100, 300, 200)
        self.settle()
        self.window.update_fullscreen_mosaic()
        self.assertFalse(self.window.window_docked())
        self.assertEqual(self.order(), windowed)
        self.dock()
        self.assertEqual(self.order(), docked)

    def test_a_docked_window_keeps_its_size(self) -> None:
        self.dock()
        size = self.window.size()
        self.window.fit_video_aspect()
        self.settle()
        self.assertEqual(self.window.size(), size)


if __name__ == "__main__":
    unittest.main()
