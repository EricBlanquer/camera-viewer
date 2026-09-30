import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QMouseEvent
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, MainWindow, RtspCamera, X11


class VideoDragTest(unittest.TestCase):
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
        self.start_preview = self.preview_start.start()
        self.window.set_camera_layout("grid")
        with patch.object(self.window, "fit_video_aspect"):
            self.window.show()
            self.window.resize(900, 700)
            self.application.processEvents()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.preview_start.stop()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def video(self, index):
        return self.window.video if index == 0 else self.window.previews[self.cameras[index].uid].video

    def drag(self, source, destination, button=Qt.MouseButton.LeftButton) -> None:
        origin = source.rect().center()
        target = source.mapFromGlobal(destination)
        for kind, position, buttons in (
            (QEvent.Type.MouseButtonPress, origin, button),
            (QEvent.Type.MouseMove, target, button),
            (QEvent.Type.MouseButtonRelease, target, Qt.MouseButton.NoButton),
        ):
            event = QMouseEvent(kind, QPointF(position), QPointF(source.mapToGlobal(position)),
                                Qt.MouseButton.NoButton if kind == QEvent.Type.MouseMove else button,
                                buttons, Qt.KeyboardModifier.NoModifier)
            self.application.sendEvent(source, event)

    def test_video_drag_exchanges_primary_and_preview_without_restarting_streams(self) -> None:
        primary = self.window.primary_pane
        preview = self.window.previews[self.cameras[1].uid]
        moved, panned, clicked = Mock(), Mock(), Mock()
        self.window.video.dragged.connect(moved)
        self.window.video.drag_moved.connect(panned)
        self.window.video.clicked.connect(clicked)
        self.drag(self.video(0), self.video(1).mapToGlobal(self.video(1).rect().center()))
        self.assertIs(self.window.video_grid.itemAtPosition(0, 0).widget(), preview)
        self.assertIs(self.window.video_grid.itemAtPosition(0, 1).widget(), primary)
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [self.cameras[index].uid for index in (1, 0, 2)])
        self.assertEqual(self.start_preview.call_count, 2)
        moved.assert_not_called()
        panned.assert_not_called()
        clicked.assert_not_called()
        self.assertFalse(self.window.video.click_timer.isActive())

    def test_preview_video_drag_exchanges_two_previews_and_persists_order(self) -> None:
        self.drag(self.video(1), self.video(2).mapToGlobal(self.video(2).rect().center()))
        order = [self.cameras[index].uid for index in (0, 2, 1)]
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()], order)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            restored = MainWindow()
        try:
            restored.devices = self.cameras
            self.assertEqual([camera.uid for camera in restored.ordered_devices()], order)
        finally:
            restored.quit_requested = True
            restored.close()

    def test_drag_outside_or_back_to_its_source_does_not_click_pan_or_reorder(self) -> None:
        original = [camera.uid for camera in self.window.ordered_devices()]
        moved, panned = Mock(), Mock()
        video = self.video(0)
        video.dragged.connect(moved)
        video.drag_moved.connect(panned)
        self.drag(video, self.window.mapToGlobal(QPoint(-100, -100)))
        start = video.rect().center()
        video._start_drag(start.x(), start.y())
        video._move_drag(start.x() + 100, start.y())
        video._finish_drag(start.x(), start.y())
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()], original)
        self.assertFalse(video.click_timer.isActive())
        moved.assert_not_called()
        panned.assert_not_called()

    def test_click_and_double_click_remain_available_on_video(self) -> None:
        video = self.video(0)
        double_clicked = Mock()
        video.double_clicked.connect(double_clicked)
        with patch.object(self.window, "toggle_fullscreen"):
            video._start_drag(20, 20)
            video._move_drag(21, 20)
            video._finish_drag(21, 20)
            self.assertTrue(video.click_timer.isActive())
            video._start_drag(20, 20)
            video._finish_drag(20, 20)
        double_clicked.assert_called_once_with(20, 20)
        self.assertFalse(video.click_timer.isActive())

    def test_single_camera_left_drag_keeps_movement_controls(self) -> None:
        for camera in self.cameras[1:]:
            self.window.set_camera_visible(camera.uid, False)
        moved = Mock()
        video = self.video(0)
        video.dragged.connect(moved)
        self.drag(video, video.mapToGlobal(video.rect().center() + QPoint(100, 0)))
        moved.assert_called_once_with(100, 0)
        self.assertFalse(video.click_timer.isActive())

    def test_dropping_on_empty_grid_cell_does_not_reorder(self) -> None:
        cell = self.window.video_grid.cellRect(1, 1)
        point = self.window.centralWidget().mapToGlobal(cell.center())
        self.drag(self.video(0), point)
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()], [camera.uid for camera in self.cameras])

    def test_controls_follow_their_video_after_reordering(self) -> None:
        self.window.resize(1280, 1000)
        self.application.processEvents()
        preview = self.window.previews[self.cameras[1].uid]
        self.window.show_overlay()
        preview.show_overlay()
        self.drag(self.video(0), self.video(1).mapToGlobal(self.video(1).rect().center()))
        self.application.processEvents()
        for video, overlay in ((self.window.video, self.window.overlay), (preview.video, preview.overlay)):
            self.assertTrue(overlay.isVisible())
            self.assertLessEqual(abs(overlay.geometry().center().x() - video.mapToGlobal(video.rect().center()).x()), 1)
            self.assertLess(overlay.geometry().bottom(), video.mapToGlobal(video.rect().bottomRight()).y())

    def test_right_drag_preserves_pan_and_tilt_without_reordering(self) -> None:
        video = self.video(0)
        moved, panned = Mock(), Mock()
        video.dragged.connect(moved)
        video.drag_moved.connect(panned)
        self.drag(video, video.mapToGlobal(video.rect().center() + QPoint(100, 0)), Qt.MouseButton.RightButton)
        moved.assert_called_once_with(100, 0)
        panned.assert_called_once_with(100, 0)
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()], [camera.uid for camera in self.cameras])
        self.assertFalse(video.click_timer.isActive())

    def test_native_video_mouse_events_exchange_cameras(self) -> None:
        video = self.video(1)
        origin = video.rect().center()
        target = video.mapFromGlobal(self.video(0).mapToGlobal(self.video(0).rect().center()))
        events = [SimpleNamespace(type=kind, detail=1, event_x=point.x(), event_y=point.y())
                  for kind, point in ((X11.ButtonPress, origin), (X11.MotionNotify, target),
                                      (X11.ButtonRelease, target))]
        display = SimpleNamespace(pending_events=lambda: len(events), next_event=lambda: events.pop(0))
        video.x_display, video.input_window = display, object()
        try:
            video.read_mouse_events()
        finally:
            video.x_display, video.input_window = None, None
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [self.cameras[index].uid for index in (1, 0, 2)])

    def test_camera_panes_have_no_headers_or_gaps(self) -> None:
        panes = [self.window.primary_pane, *self.window.previews.values()]
        self.assertEqual(self.window.video_grid.spacing(), 0)
        for pane in panes:
            self.assertEqual(pane.layout().count(), 1)
            stack = self.window.primary_video_stack if pane is self.window.primary_pane else pane.video_stack
            self.assertEqual(stack.geometry(), pane.rect())

    def test_local_playback_video_can_be_reordered(self) -> None:
        camera = self.cameras[1]
        with patch("app.QTimer.singleShot"):
            self.window.open_local_replay(camera)
        pane = self.window.local_replays[camera.uid]
        self.application.processEvents()
        self.drag(pane.video, self.video(0).mapToGlobal(self.video(0).rect().center()))
        self.assertEqual([camera.uid for camera in self.window.ordered_devices()],
                         [self.cameras[index].uid for index in (1, 0, 2)])
        self.assertIs(self.window.previews[camera.uid].video_stack.currentWidget(), pane)
        self.window.close_local_replay(camera.uid)


if __name__ == "__main__":
    unittest.main()
