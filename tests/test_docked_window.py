import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QMargins, QRect, QSettings, Qt
from PyQt6.QtWidgets import QApplication, QWidget

from app import AspectVideoFrame, CameraPreview, MainWindow, RtspCamera, compute_mosaic, docked_span, mosaic_grid


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


class AspectVideoFrameFillTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.frame = AspectVideoFrame(QWidget())
        self.frame.show()

    def tearDown(self) -> None:
        self.frame.close()
        self.frame.deleteLater()

    def test_a_near_widescreen_frame_is_filled_only_in_fill_mode(self) -> None:
        self.frame.resize(640, 337)
        self.assertEqual(self.frame.video.geometry(), QRect(20, 0, 599, 337))
        self.frame.set_fill(True)
        self.assertEqual(self.frame.video.geometry(), QRect(0, 0, 640, 337))
        self.frame.set_fill(False)
        self.assertEqual(self.frame.video.geometry(), QRect(20, 0, 599, 337))

    def test_a_distant_shape_keeps_its_bars_in_fill_mode(self) -> None:
        self.frame.set_fill(True)
        self.frame.resize(640, 690)
        self.assertEqual(self.frame.video.geometry(), QRect(0, 165, 640, 360))


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

    def fills(self) -> list[bool]:
        return [frame.fill for frame in self.window.findChildren(AspectVideoFrame)]

    def test_only_a_docked_window_fills_its_panes(self) -> None:
        self.assertFalse(any(self.fills()))
        self.dock()
        self.assertGreaterEqual(len(self.fills()), len(self.cameras))
        self.assertTrue(all(self.fills()))
        self.window.showNormal()
        self.window.setGeometry(100, 100, 300, 200)
        self.settle()
        self.window.update_fullscreen_mosaic()
        self.assertFalse(any(self.fills()))

    def test_a_window_moved_into_its_dock_after_resizing_is_docked(self) -> None:
        area = self.window.screen().availableGeometry()
        frame = self.window.frameGeometry().size() - self.window.size()
        self.window.setGeometry(area.left() + 50, area.top() + 50,
                                area.width() - frame.width(), area.height() - frame.height())
        self.settle()
        self.assertFalse(self.window.window_docked())
        self.assertFalse(any(self.fills()))
        self.window.move(area.topLeft())
        self.settle()
        self.assertTrue(self.window.window_docked())
        self.assertTrue(all(self.fills()))

    def test_the_window_manager_frame_decides_the_docked_state(self) -> None:
        area = self.window.screen().availableGeometry()
        margins = QMargins(0, 32, 0, 0)
        self.window.window_hints = SimpleNamespace(frame_margins=lambda: margins, work_areas=lambda: [], close=lambda: None)
        self.window.setGeometry(area.marginsRemoved(margins))
        self.settle()
        self.window.refresh_window_shape()
        self.assertTrue(self.window.window_docked())
        self.assertTrue(all(self.fills()))
        margins = QMargins()
        self.window.refresh_window_shape()
        self.assertFalse(self.window.window_docked())
        self.assertFalse(any(self.fills()))

    def test_the_work_area_of_the_window_monitor_decides_the_docked_state(self) -> None:
        screen = self.window.screen().availableGeometry()
        work = screen.adjusted(0, 0, 0, -28)
        self.window.window_hints = SimpleNamespace(frame_margins=lambda: None, work_areas=lambda: [work],
                                                   close=lambda: None)
        frame = self.window.frameGeometry().size() - self.window.size()
        self.window.setGeometry(work.left(), work.top(), work.width() - frame.width(), work.height() - frame.height())
        self.settle()
        self.assertTrue(self.window.window_docked())
        self.window.window_hints = SimpleNamespace(frame_margins=lambda: None, work_areas=lambda: [],
                                                   close=lambda: None)
        self.assertFalse(self.window.window_docked())

    def place_frame(self, tile: QRect) -> None:
        offset = self.window.geometry().topLeft() - self.window.frameGeometry().topLeft()
        frame = self.window.frameGeometry().size() - self.window.size()
        self.window.setGeometry(tile.left() + offset.x(), tile.top() + offset.y(),
                                tile.width() - frame.width(), tile.height() - frame.height())

    def fit_in_tile(self, tile: QRect) -> QRect:
        self.place_frame(tile)
        self.settle()
        self.assertTrue(self.window.window_docked())
        self.window.fit_docked_window()
        self.settle()
        return self.window.frameGeometry()

    def test_a_dock_without_a_filled_layout_shrinks_to_its_videos_and_stays_docked(self) -> None:
        work = QRect(0, 0, 2400, 1400)
        with patch.object(self.window, "available_area", return_value=work), \
                patch.object(self.window, "current_mosaic", return_value=None):
            docked = self.order()
            self.place_frame(QRect(0, 0, 1200, 1400))
            self.settle()
            tiled = self.window.size()
            with patch("app.QApplication.queryKeyboardModifiers", return_value=Qt.KeyboardModifier.MetaModifier):
                self.window.fit_docked_window()
                self.settle()
            self.assertEqual(self.window.size(), tiled)
            self.assertTrue(self.window.dock_fit_timer.isActive())
            fitted = self.fit_in_tile(QRect(0, 0, 1200, 1400))
            columns, rows = self.window.camera_grid_dimensions()
            video = self.window.centralWidget().size()
            self.assertAlmostEqual(video.width() * rows / (video.height() * columns), 16 / 9, delta=0.02)
            self.assertEqual(fitted.topLeft(), work.topLeft())
            self.assertEqual(fitted.width(), 1200)
            self.assertLess(fitted.height(), work.height())
            self.assertTrue(self.window.window_docked())
            self.assertEqual(self.order(), docked)
            self.window.fit_docked_window()
            self.settle()
            self.assertEqual(self.window.frameGeometry(), fitted)

    def test_top_and_bottom_docks_fit_like_a_side_dock(self) -> None:
        work = QRect(0, 0, 2400, 1400)
        with patch.object(self.window, "available_area", return_value=work), \
                patch.object(self.window, "current_mosaic", return_value=None):
            side = self.fit_in_tile(QRect(0, 0, 1200, 1400))
            top = self.fit_in_tile(QRect(0, 0, 2400, 700))
            self.assertEqual(top, side)
            bottom = self.fit_in_tile(QRect(0, 700, 2400, 700))
            self.assertEqual(bottom.size(), side.size())
            self.assertEqual(bottom.bottomLeft(), work.bottomLeft())

    def test_a_docked_window_keeps_its_size(self) -> None:
        self.dock()
        size = self.window.size()
        self.window.fit_video_aspect()
        self.settle()
        self.assertEqual(self.window.size(), size)


if __name__ == "__main__":
    unittest.main()
