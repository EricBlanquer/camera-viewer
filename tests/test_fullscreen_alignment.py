import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QEvent, QMargins, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from app import (
    CameraPreview, MainWindow, RtspCamera, dragged_fullscreen_alignment, load_fullscreen_alignment,
    save_fullscreen_alignment,
)


class DraggedFullscreenAlignmentTest(unittest.TestCase):
    def test_drag_moves_at_least_one_position_towards_the_nearest_anchor(self) -> None:
        for alignment, dy, expected in (
            ("center", -10, "top"),
            ("center", 10, "bottom"),
            ("top", 10, "center"),
            ("top", 140, "center"),
            ("top", 160, "bottom"),
            ("bottom", -10, "center"),
            ("bottom", -160, "top"),
            ("top", -50, "top"),
            ("bottom", 50, "bottom"),
            ("center", 0, "center"),
        ):
            with self.subTest(alignment=alignment, dy=dy):
                self.assertEqual(dragged_fullscreen_alignment(alignment, 200, dy), expected)


class FullscreenAlignmentTest(unittest.TestCase):
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
        self.preview_start = patch.object(CameraPreview, "start")
        self.preview_start.start()
        self.window.set_camera_visible(self.cameras[2].uid, False)
        self.window.set_camera_layout("grid")
        with patch.object(self.window, "fit_video_aspect"):
            self.window.show()
            self.window.resize(900, 500)
            self.application.processEvents()
            self.normal_geometry = self.window.geometry()
            self.window.toggle_fullscreen()
            self.settle()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.preview_start.stop()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def settle(self) -> None:
        self.application.processEvents()
        self.window.update_camera_mask()
        self.application.processEvents()

    def video(self, index):
        return self.window.video if index == 0 else self.window.previews[self.cameras[index].uid].video

    def video_top(self, index) -> int:
        return self.video(index).mapTo(self.window.centralWidget(), QPoint()).y()

    def band_point(self, above: bool) -> QPoint:
        self.window.video_grid.activate()
        area = self.window.video_grid.geometry()
        content = self.window.video_grid.contentsRect()
        y = (area.top() + content.top()) // 2 if above else (content.bottom() + area.bottom()) // 2
        return self.window.centralWidget().mapToGlobal(QPoint(area.center().x(), y))

    def drag(self, source, destination, origin=None) -> None:
        origin = source.rect().center() if origin is None else origin
        target = source.mapFromGlobal(destination)
        for kind, position, buttons in (
            (QEvent.Type.MouseButtonPress, origin, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseMove, target, Qt.MouseButton.LeftButton),
            (QEvent.Type.MouseButtonRelease, target, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(position), QPointF(source.mapToGlobal(position)),
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton,
                                buttons, Qt.KeyboardModifier.NoModifier)
            self.application.sendEvent(source, event)
        self.settle()

    def visible_uids(self) -> tuple[str, ...]:
        return tuple(camera.uid for camera in self.window.visible_devices())

    def test_fullscreen_videos_are_centered_without_inner_letterboxing(self) -> None:
        self.assertTrue(self.window.isFullScreen())
        slack = self.window.fullscreen_video_slack()
        area = self.window.video_grid.geometry()
        self.assertGreater(slack, 0)
        for index in (0, 1):
            self.assertEqual(self.video_top(index), area.top() + slack // 2)
            self.assertEqual(self.video(index).width(), area.width() // 2)
            self.assertEqual(self.video(index).height(), (area.height() - slack) // 2)

    def test_dropping_above_sticks_videos_to_the_top_and_is_remembered(self) -> None:
        area = self.window.video_grid.geometry()
        self.drag(self.video(1), self.band_point(above=True))
        self.assertEqual(self.video_top(0), area.top())
        self.assertEqual(self.video_top(1), area.top())
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [camera.uid for camera in self.cameras])
        self.assertEqual(load_fullscreen_alignment(self.settings, "grid", self.visible_uids()), "top")
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            restored = MainWindow()
        try:
            restored.devices = self.cameras
            self.assertEqual(restored.fullscreen_alignment(), "top")
        finally:
            restored.quit_requested = True
            restored.close()

    def test_dropping_below_sticks_videos_to_the_bottom_and_back_to_center(self) -> None:
        area = self.window.video_grid.geometry()
        self.drag(self.video(0), self.band_point(above=False))
        slack = self.window.fullscreen_video_slack()
        self.assertEqual(self.video_top(0), area.top() + slack)
        self.assertEqual(self.video_top(1), area.top() + slack)
        video = self.video(0)
        self.drag(video, video.mapToGlobal(QPoint(video.width() // 2, 5 - slack // 2)), QPoint(video.width() // 2, 5))
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.assertEqual(self.video_top(1), area.top() + slack // 2)
        self.assertEqual(self.settings.value("view/fullscreen_alignment", "", str), "[]")

    def test_alignment_is_kept_per_layout_and_camera_selection(self) -> None:
        self.drag(self.video(0), self.band_point(above=True))
        self.window.swap_cameras(self.cameras[0].uid, self.cameras[1].uid)
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "top")
        self.assertEqual(self.video_top(0), self.window.video_grid.geometry().top())
        self.window.set_camera_layout("horizontal")
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.window.set_camera_layout("grid")
        self.window.set_camera_visible(self.cameras[2].uid, True)
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.window.set_camera_visible(self.cameras[2].uid, False)
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "top")

    def test_dropping_on_an_empty_grid_cell_keeps_videos_in_place(self) -> None:
        self.window.video_grid.activate()
        cell = self.window.video_grid.cellRect(1, 1)
        self.drag(self.video(0), self.window.centralWidget().mapToGlobal(cell.center()))
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [camera.uid for camera in self.cameras])

    def test_dropping_on_another_video_still_exchanges_without_moving(self) -> None:
        self.drag(self.video(0), self.video(1).mapToGlobal(self.video(1).rect().center()))
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()][:2],
                         [self.cameras[1].uid, self.cameras[0].uid])
        self.assertEqual(self.window.fullscreen_alignment(), "center")

    def test_leaving_fullscreen_removes_the_alignment_margins(self) -> None:
        self.drag(self.video(0), self.band_point(above=True))
        self.assertNotEqual(self.window.video_grid.contentsMargins(), QMargins())
        with patch.object(self.window, "fit_video_aspect"):
            self.window.toggle_fullscreen()
            self.settle()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.video_grid.contentsMargins(), QMargins())
        self.assertEqual(self.window.geometry(), self.normal_geometry)
        self.drag(self.video(0), self.window.mapToGlobal(QPoint(10, -50)))
        self.assertEqual(self.window.fullscreen_alignment(), "top")

    def test_invalid_saved_records_are_ignored(self) -> None:
        self.settings.setValue("view/fullscreen_alignment", '[{"layout": "diagonal", "cameras": [], "alignment": "top"}, 3]')
        self.assertEqual(load_fullscreen_alignment(self.settings, "diagonal", ()), "center")
        self.settings.setValue("view/fullscreen_alignment", "not json")
        self.assertEqual(load_fullscreen_alignment(self.settings, "horizontal", ("rtsp:0",)), "center")
        save_fullscreen_alignment(self.settings, "horizontal", ("rtsp:1", "rtsp:0"), "bottom")
        self.assertEqual(load_fullscreen_alignment(self.settings, "horizontal", ("rtsp:0", "rtsp:1")), "bottom")


if __name__ == "__main__":
    unittest.main()
