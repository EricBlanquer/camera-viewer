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
    CURSOR_HIDE_DELAY_MS, CURSOR_POLL_MS, CameraPreview, MainWindow, RtspCamera, load_fullscreen_alignment,
    nearest_fullscreen_alignment, save_fullscreen_alignment,
)


class NearestFullscreenAlignmentTest(unittest.TestCase):
    def test_videos_snap_to_the_nearest_position(self) -> None:
        for top, expected in ((-80, "top"), (49, "top"), (51, "center"), (149, "center"), (151, "bottom"),
                              (280, "bottom")):
            with self.subTest(top=top):
                self.assertEqual(nearest_fullscreen_alignment(200, top), expected)


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
        self.slack = self.window.fullscreen_video_slack()
        self.area = self.window.video_grid.geometry()

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

    def send(self, source, kind, position: QPoint) -> None:
        button = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else Qt.MouseButton.LeftButton
        buttons = Qt.MouseButton.NoButton if kind == QEvent.Type.MouseButtonRelease else Qt.MouseButton.LeftButton
        event = QMouseEvent(kind, QPointF(source.mapFromGlobal(position)), QPointF(position), button, buttons,
                            Qt.KeyboardModifier.NoModifier)
        self.application.sendEvent(source, event)

    def press(self, source) -> QPoint:
        origin = source.mapToGlobal(source.rect().center())
        self.send(source, QEvent.Type.MouseButtonPress, origin)
        return origin

    def drag(self, source, offset: QPoint) -> None:
        origin = self.press(source)
        self.send(source, QEvent.Type.MouseMove, origin + offset)
        self.send(source, QEvent.Type.MouseButtonRelease, origin + offset)
        self.settle()

    def test_fullscreen_videos_are_centered_without_inner_letterboxing(self) -> None:
        self.assertTrue(self.window.isFullScreen())
        self.assertGreater(self.slack, 0)
        for index in (0, 1):
            self.assertEqual(self.video_top(index), self.area.top() + self.slack // 2)
            self.assertEqual(self.video(index).width(), self.area.width() // 2)
            self.assertEqual(self.video(index).height(), (self.area.height() - self.slack) // 2)

    def test_videos_show_their_landing_position_while_dragging(self) -> None:
        video = self.video(1)
        origin = self.press(video)
        self.send(video, QEvent.Type.MouseMove, origin + QPoint(0, -self.slack // 4 + 10))
        self.settle()
        self.assertEqual(self.video_top(1), self.area.top() + self.slack // 2)
        self.send(video, QEvent.Type.MouseMove, origin + QPoint(0, -self.slack // 4 - 10))
        self.settle()
        self.assertEqual(self.video_top(0), self.area.top())
        self.assertEqual(self.video_top(1), self.area.top())
        self.assertEqual(load_fullscreen_alignment(self.settings, "grid", self.window.current_camera_view()[0]),
                         "center")
        self.send(video, QEvent.Type.MouseMove, origin + QPoint(0, self.slack // 2))
        self.settle()
        self.assertEqual(self.video_top(1), self.area.top() + self.slack)
        self.send(video, QEvent.Type.MouseButtonRelease, origin + QPoint(0, self.slack // 2))
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "bottom")
        self.assertEqual(self.video_top(0), self.area.top() + self.slack)
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [camera.uid for camera in self.cameras])

    def test_videos_return_to_the_center_and_the_choice_is_remembered(self) -> None:
        self.drag(self.video(0), QPoint(0, -self.slack))
        self.assertEqual(self.window.fullscreen_alignment(), "top")
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            restored = MainWindow()
        try:
            restored.devices = self.cameras
            self.assertEqual(restored.fullscreen_alignment(), "top")
        finally:
            restored.quit_requested = True
            restored.close()
        self.drag(self.video(1), QPoint(0, self.slack // 2))
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.assertEqual(self.video_top(0), self.area.top() + self.slack // 2)
        self.assertEqual(self.settings.value("view/fullscreen_alignment", "", str), "[]")

    def test_small_vertical_drag_keeps_the_current_position(self) -> None:
        self.drag(self.video(0), QPoint(0, -self.slack // 4 + 10))
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.assertEqual(self.video_top(0), self.area.top() + self.slack // 2)

    def test_alignment_is_kept_per_layout_and_camera_selection(self) -> None:
        self.drag(self.video(0), QPoint(0, -self.slack))
        self.window.swap_cameras(self.cameras[0].uid, self.cameras[1].uid)
        self.settle()
        self.assertEqual(self.window.fullscreen_alignment(), "top")
        self.assertEqual(self.video_top(0), self.area.top())
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

    def test_dropping_on_another_video_exchanges_and_restores_the_saved_position(self) -> None:
        video = self.video(0)
        origin = self.press(video)
        self.send(video, QEvent.Type.MouseMove, origin + QPoint(0, -self.slack))
        self.settle()
        self.assertEqual(self.video_top(0), self.area.top())
        target = self.video(1).mapToGlobal(self.video(1).rect().center())
        self.send(video, QEvent.Type.MouseButtonRelease, target)
        self.settle()
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()][:2],
                         [self.cameras[1].uid, self.cameras[0].uid])
        self.assertEqual(self.window.fullscreen_alignment(), "center")
        self.assertEqual(self.video_top(0), self.area.top() + self.slack // 2)

    def test_leaving_fullscreen_removes_the_alignment_margins(self) -> None:
        self.drag(self.video(0), QPoint(0, -self.slack))
        self.assertNotEqual(self.window.video_grid.contentsMargins(), QMargins())
        with patch.object(self.window, "fit_video_aspect"):
            self.window.toggle_fullscreen()
            self.settle()
        self.assertFalse(self.window.isFullScreen())
        self.assertEqual(self.window.video_grid.contentsMargins(), QMargins())
        self.assertEqual(self.window.geometry(), self.normal_geometry)
        self.drag(self.video(0), QPoint(0, 200))
        self.assertEqual(self.window.video_grid.contentsMargins(), QMargins())
        self.assertEqual(self.window.fullscreen_alignment(), "top")

    def test_cursor_hides_after_two_idle_seconds_in_fullscreen_and_returns_on_movement(self) -> None:
        self.assertTrue(self.window.cursor_timer.isActive())
        self.assertEqual(self.window.cursor_timer.interval(), CURSOR_POLL_MS)
        idle_ticks = CURSOR_HIDE_DELAY_MS // CURSOR_POLL_MS
        with patch("app.QCursor.pos", return_value=self.window.cursor_position):
            for _ in range(idle_ticks - 1):
                self.window.check_cursor_activity()
            self.assertNotEqual(self.window.cursor().shape(), Qt.CursorShape.BlankCursor)
            self.window.check_cursor_activity()
            self.assertEqual(self.window.cursor().shape(), Qt.CursorShape.BlankCursor)
        with patch("app.QCursor.pos", return_value=self.window.cursor_position + QPoint(1, 0)):
            self.window.check_cursor_activity()
            self.assertNotEqual(self.window.cursor().shape(), Qt.CursorShape.BlankCursor)
            for _ in range(idle_ticks):
                self.window.check_cursor_activity()
        self.assertEqual(self.window.cursor().shape(), Qt.CursorShape.BlankCursor)
        with patch.object(self.window, "fit_video_aspect"):
            self.window.toggle_fullscreen()
            self.settle()
        self.assertFalse(self.window.cursor_timer.isActive())
        self.assertNotEqual(self.window.cursor().shape(), Qt.CursorShape.BlankCursor)

    def test_invalid_saved_records_are_ignored(self) -> None:
        self.settings.setValue("view/fullscreen_alignment", '[{"layout": "diagonal", "cameras": [], "alignment": "top"}, 3]')
        self.assertEqual(load_fullscreen_alignment(self.settings, "diagonal", ()), "center")
        self.settings.setValue("view/fullscreen_alignment", "not json")
        self.assertEqual(load_fullscreen_alignment(self.settings, "horizontal", ("rtsp:0",)), "center")
        save_fullscreen_alignment(self.settings, "horizontal", ("rtsp:1", "rtsp:0"), "bottom")
        self.assertEqual(load_fullscreen_alignment(self.settings, "horizontal", ("rtsp:0", "rtsp:1")), "bottom")


if __name__ == "__main__":
    unittest.main()
