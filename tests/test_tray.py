import io
import itertools
import os
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QObject, QPoint, QSettings, Qt, pyqtSignal
from PyQt6.QtGui import QColor
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QPushButton, QSystemTrayIcon, QWidget

from app import AspectVideoFrame, CameraPreview, ICAM365_SERVER, IMOU_ACCOUNT_PROVIDER, LocalReplayPane, MainWindow, RTSP_ACCOUNT, RTSP_DENOISE_FILTER, RtspCamera, RtspStreamWorker, StreamWorker, icam365_light_request, icam365_ptz_request, load_rtsp_cameras, mpv_rtsp_command, valid_rtsp_url
from icam365 import NativePreset


class TrayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.settings_directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        settings = QSettings(str(Path(self.settings_directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.settings_directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_tray_keeps_window_camera_and_recording_settings(self) -> None:
        menu = self.window.tray.contextMenu()
        labels = [action.text() for action in menu.actions() if not action.isSeparator()]
        self.assertEqual(labels, [
            "Hide window",
            "Cameras",
            "Camera layout",
            "Camera profiles",
            "Secondary stream cameras",
            "Standby while a computer is on...",
            "Add camera",
            "Continuous recording (24 h)",
            "Detect people and animals locally",
            "Local detection cameras",
            "Open recordings folder",
            "Quit",
        ])

    def test_closing_window_hides_it_to_tray(self) -> None:
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.window.reconnect = lambda: None
        self.window.toggle_window()
        self.assertTrue(self.window.isVisible())

    def test_tray_click_raises_a_visible_window_behind_another_window(self) -> None:
        self.window.show()
        with patch.object(MainWindow, "isActiveWindow", return_value=False), \
                patch.object(self.window, "show_window") as show_window, \
                patch.object(self.window, "hide_to_tray") as hide_to_tray:
            self.window.update_tray_menu()
            self.assertEqual(self.window.window_action.text(), "Hide window")
            self.window.on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
            show_window.assert_called_once_with()
            hide_to_tray.assert_not_called()

    def test_tray_click_hides_the_active_window(self) -> None:
        self.window.show()
        with patch.object(MainWindow, "isActiveWindow", return_value=True), \
                patch.object(self.window, "show_window") as show_window, \
                patch.object(self.window, "hide_to_tray") as hide_to_tray:
            self.window.update_tray_menu()
            self.assertEqual(self.window.window_action.text(), "Hide window")
            self.window.on_tray_activated(QSystemTrayIcon.ActivationReason.Trigger)
            hide_to_tray.assert_called_once_with()
            show_window.assert_not_called()

    def test_tray_menu_hide_action_hides_a_visible_window(self) -> None:
        self.window.show()
        with patch.object(self.window, "hide_to_tray") as hide_to_tray:
            self.window.window_action.trigger()
            hide_to_tray.assert_called_once_with()

    def test_detections_notify_only_after_the_first_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            self.window.selected_device = SimpleNamespace(name="Jardin", uid="garden")
            self.window.devices = [self.window.selected_device]
            messages: list[tuple[str, str]] = []
            actions = []
            self.window.notifier.notify = lambda title, text, timeout, activate, fallback: (
                messages.append((title, text)), actions.append(activate))
            self.window.on_detections_listed(["20260926091500_011.mp4"])
            self.assertEqual(messages, [])
            self.window.on_detections_listed(["20260926091500_011.mp4"])
            self.assertEqual(messages, [])
            self.window.on_detections_listed(
                ["20260926091500_011.mp4", "20260926143210_011.mp4", "20260926150001_011.mp4"]
            )
            self.assertEqual(messages, [("Camera detection", "Jardin \u00b7 26/09 15:00:01 (2 new detections)")])
            self.assertEqual(self.window.settings.value("detections/last_seen/garden"), "20260926150001_011.mp4")
            self.window.device_accounts = {"garden": "first@example.com"}
            with patch.object(self.window, "enter_replay") as enter_replay:
                actions[0]()
            enter_replay.assert_called_once_with(datetime(2026, 9, 26, 15, 0, 1))

    def test_tray_lists_and_toggles_account_cameras(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = SimpleNamespace(name="Entrée", uid="entrance")
        self.window.devices = [garden, entrance]
        self.window.device_accounts = {"garden": "first@example.com", "entrance": "second@example.com"}
        self.window.selected_device = garden
        self.window.update_cameras_menu()
        actions = self.window.cameras_menu.actions()
        self.assertEqual([action.text() for action in actions[:2]], [
            "Jardin · O-KAM (first@example.com)",
            "Entrée · O-KAM (second@example.com)",
        ])
        self.assertTrue(actions[0].isChecked())
        self.assertTrue(actions[1].isChecked())
        chosen: list[tuple[str, bool]] = []
        self.window.set_camera_visible = lambda uid, enabled: chosen.append((uid, enabled))
        actions[1].trigger()
        self.assertEqual(chosen, [("entrance", False)])
        self.assertTrue(actions[0].isChecked())

    def test_selected_camera_survives_account_refresh(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            self.window.settings.setValue("camera/selected_uid", "entrance")
            self.window.accounts = []
            self.window.account_username = "first@example.com"
            self.window.account_secret = "secret"
            garden = SimpleNamespace(name="Jardin", uid="garden")
            entrance = SimpleNamespace(name="Entrée", uid="entrance")
            with patch("app.save_account_password", return_value=True):
                self.window.on_devices_found([garden, entrance])
            self.window.watch_live = lambda: None
            with patch.object(CameraPreview, "start"):
                self.window.on_account_finished()
            self.assertIs(self.window.selected_device, entrance)
            self.assertEqual(self.window.settings.value("accounts/okam"), ["first@example.com"])

    def test_rtsp_camera_persists_and_appears_beside_account_camera(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            camera = RtspCamera("rtsp:test", "Entrée", "rtsp://192.0.2.10:8001/0")
            garden = SimpleNamespace(name="Jardin", uid="garden")
            self.window.rtsp_cameras = [camera]
            self.window.save_rtsp_cameras()
            self.assertEqual(load_rtsp_cameras(self.window.settings), [camera])
            self.window.devices = [garden, camera]
            self.window.device_accounts = {"garden": "first@example.com", camera.uid: RTSP_ACCOUNT}
            self.window.selected_device = garden
            self.window.update_cameras_menu()
            actions = self.window.cameras_menu.actions()
            self.assertEqual(actions[0].text(), "Jardin · O-KAM (first@example.com)")
            self.assertEqual(actions[1].text(), "Entrée · RTSP (local)")
            chosen: list[tuple[str, bool]] = []
            self.window.set_camera_visible = lambda uid, enabled: chosen.append((uid, enabled))
            actions[1].trigger()
            self.assertEqual(chosen, [(camera.uid, False)])
            self.window.accounts = []
            with patch.object(self.window, "watch_live"):
                self.window.find_cameras()
            self.assertEqual(self.window.devices, [camera])

    def test_rtsp_url_rejects_embedded_credentials_and_invalid_ports(self) -> None:
        self.assertTrue(valid_rtsp_url("rtsp://192.0.2.10:8001/0"))
        self.assertFalse(valid_rtsp_url("rtsp://user:password@192.0.2.10:8001/0"))
        self.assertFalse(valid_rtsp_url("rtsp://192.0.2.10:bad/0"))
        self.assertEqual(RtspCamera("rtsp:test", "Other", "rtsp://192.0.2.10:554/stream").transport, "tcp")
        camera = RtspCamera("rtsp:test", "Other", "rtsp://192.0.2.10:554/stream", "tcp")
        command = mpv_rtsp_command(Path("/tmp/control.sock"), 1, True, camera)
        self.assertIn("--rtsp-transport=tcp", command)
        self.assertIn("--mute=yes", command)
        self.assertIn("--aid=no", command)
        self.assertIn("--demuxer-readahead-secs=10", command)
        self.assertIn(RTSP_DENOISE_FILTER, command)
        self.assertNotIn("--no-audio", command)
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            self.window.rtsp_cameras = [camera]
            self.window.save_rtsp_cameras()
            self.assertEqual(load_rtsp_cameras(self.window.settings), [camera])

    def test_rtsp_sound_choice_survives_player_restart(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0/av0")
        preview = CameraPreview(camera, settings=self.window.settings)
        preview.live = True
        preview._on_sound_changed(True)
        preview.close()
        restarted = CameraPreview(camera, settings=self.window.settings)
        self.assertTrue(restarted.sound_enabled)
        command = mpv_rtsp_command(Path("/tmp/control.sock"), 1, True, camera, restarted.sound_enabled)
        self.assertIn("--mute=no", command)
        self.assertIn("--aid=auto", command)
        self.assertIn("--audio-buffer=0.2", command)
        self.assertLess(command.index("--profile=low-latency"), command.index("--audio-buffer=0.2"))
        self.assertIn("--af=lavfi=[volume=25dB,alimiter=limit=0.95]", command)
        restarted.close()
        self.window.selected_device = camera
        with patch.object(self.window, "show_notice"):
            self.window.on_sound_changed(False)
        disabled = CameraPreview(camera, settings=self.window.settings)
        self.assertFalse(disabled.sound_enabled)
        disabled.close()

    def test_rtsp_player_reconnects_when_frames_stop(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0/av0")
        player = Mock()
        player.poll.return_value = None
        worker = RtspStreamWorker(camera, player, Path(self.settings_directory.name) / "player.sock")
        worker.stop_requested.wait = Mock(return_value=False)
        failures: list[str] = []
        worker.failed.connect(failures.append)
        positions: list[bool] = []

        def request(_socket: Path, command: list[object]) -> tuple[bool, object]:
            if command[1] == "time-pos":
                positions.append(True)
            return True, {"vo-configured": True, "track-list": [], "time-pos": 0, "demuxer-cache-duration": 0}[command[1]]
        with patch("app.mpv_request", side_effect=request), patch("app.icam365_light_request", return_value=False), patch(
            "app.prune_continuous_recordings"
        ), patch("app.time.monotonic", side_effect=itertools.count(100, 5).__next__):
            worker.run()
        self.assertEqual(failures, ["The RTSP video stream stopped producing frames."])
        self.assertLessEqual(len(positions), 3)

    def test_visible_cameras_keep_independent_previews_and_saved_layout(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = garden
        with patch.object(CameraPreview, "start") as start_preview:
            self.window.sync_previews()
            self.assertEqual(list(self.window.previews), [entrance.uid])
            self.assertIs(self.window.primary_frame.parentWidget(), self.window.primary_video_stack)
            self.assertIs(self.window.previews[entrance.uid].frame.parentWidget(), self.window.previews[entrance.uid].video_stack)
            self.assertIs(self.window.video_grid.itemAtPosition(0, 1).widget(), self.window.previews[entrance.uid])
            self.window.swap_cameras(garden.uid, entrance.uid)
            self.assertEqual([camera.uid for camera in self.window.ordered_devices()], [entrance.uid, garden.uid])
            self.assertIs(self.window.video_grid.itemAtPosition(0, 0).widget(), self.window.previews[entrance.uid])
            self.window.set_camera_layout("vertical")
            self.assertIs(self.window.video_grid.itemAtPosition(0, 0).widget(), self.window.previews[entrance.uid])
            self.assertIs(self.window.video_grid.itemAtPosition(1, 0).widget(), self.window.primary_pane)
            self.assertEqual(self.window.settings.value("view/camera_layout"), "vertical")
            self.assertEqual(start_preview.call_count, 1)
            self.window.selected_device = entrance
            self.window.sync_previews()
            self.assertEqual(list(self.window.previews), [garden.uid])
            self.window.device_accounts = {garden.uid: "account@example.com", entrance.uid: RTSP_ACCOUNT}
            self.window.set_camera_visible(garden.uid, False)
            self.assertEqual(self.window.previews, {})
            self.assertFalse(self.window.camera_visible(garden.uid))

    def test_video_frame_preserves_aspect_ratio_when_resized(self) -> None:
        video = QWidget()
        frame = AspectVideoFrame(video)
        frame.show()
        frame.resize(640, 500)
        self.assertEqual((video.width(), video.height()), (640, 360))
        self.assertEqual(video.y(), 70)
        frame.resize(500, 500)
        self.assertEqual((video.width(), video.height()), (500, 281))
        frame.close()

    def test_wide_docked_window_uses_horizontal_layout(self) -> None:
        screen = QApplication.primaryScreen().availableGeometry()
        self.window.devices = [
            SimpleNamespace(name="Jardin", uid="garden"),
            RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0"),
        ]
        self.window.selected_device = self.window.devices[0]
        self.window.settings.setValue("view/camera_layout", "vertical")
        with patch.object(CameraPreview, "start"):
            self.window.show()
            self.window.resize(screen.width(), screen.height() // 2)
            self.window.sync_previews()
            self.assertEqual(self.window.effective_camera_layout(), "horizontal")
            self.assertIs(self.window.video_grid.itemAtPosition(0, 1).widget(), self.window.previews["rtsp:entrance"])
            self.application.processEvents()
            primary_width = self.window.primary_pane.width()
            preview_width = self.window.previews["rtsp:entrance"].width()
            self.assertLessEqual(abs(primary_width - preview_width), 2)
            self.assertEqual(
                (self.window.video.width(), self.window.video.height()),
                (self.window.previews["rtsp:entrance"].video.width(),
                 self.window.previews["rtsp:entrance"].video.height()),
            )
            self.window.show_overlay()
            self.assertTrue(self.window.overlay.isVisible())
            self.assertTrue(self.window.overlay_timer.isActive())
            self.window.resize(screen.width() // 2, screen.height())
            self.window.sync_previews()
            self.assertEqual(self.window.effective_camera_layout(), "vertical")
            self.assertIs(self.window.video_grid.itemAtPosition(1, 0).widget(), self.window.previews["rtsp:entrance"])

    def test_grid_keeps_three_equal_panes_and_a_transparent_fourth_cell(self) -> None:
        cameras = [
            SimpleNamespace(name="Garden", uid="garden"),
            RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:554/stream"),
            RtspCamera("rtsp:kitchen", "Kitchen", "rtsp://192.0.2.11:554/stream"),
        ]
        self.window.devices = cameras
        self.window.selected_device = cameras[0]
        with patch.object(CameraPreview, "start") as start, patch.object(self.window, "fit_video_aspect"):
            self.window.set_camera_layout("grid")
            self.window.show()
            for width, height in ((600, 800), (720, 640)):
                self.window.resize(width, height)
                self.application.processEvents()
                self.assertEqual(self.window.effective_camera_layout(), "grid")
                self.window.update_camera_mask()
                panes = [self.window.primary_pane, *self.window.previews.values()]
                for index, pane in enumerate(panes):
                    row, column = divmod(index, 2)
                    self.assertIs(self.window.video_grid.itemAtPosition(row, column).widget(), pane)
                    self.assertLessEqual(abs(pane.width() - panes[0].width()), 1)
                    self.assertLessEqual(abs(pane.height() - panes[0].height()), 1)
                    self.assertTrue(self.window.mask().contains(pane.mapTo(self.window, pane.rect().center())))
                empty = self.window.video_grid.cellRect(1, 1)
                offset = self.window.centralWidget().mapTo(self.window, QPoint())
                self.assertIsNone(self.window.video_grid.itemAtPosition(1, 1))
                self.assertGreater(empty.width(), 0)
                self.assertGreater(empty.height(), 0)
                self.assertFalse(self.window.mask().contains(empty.center() + offset))
            self.assertEqual(self.window.settings.value("view/camera_layout"), "grid")
            self.window.set_camera_layout("vertical")
            self.application.processEvents()
            self.assertTrue(self.window.mask().isEmpty())
            self.assertEqual(self.window.video_grid.columnStretch(1), 0)
            self.window.set_camera_layout("grid")
            fourth = RtspCamera("rtsp:office", "Office", "rtsp://192.0.2.12:554/stream")
            self.window.devices.append(fourth)
            self.window.sync_previews()
            self.application.processEvents()
            self.assertTrue(self.window.mask().isEmpty())
            self.assertIs(self.window.video_grid.itemAtPosition(1, 1).widget(), self.window.previews[fourth.uid])
            self.assertEqual(start.call_count, 3)

    def test_fullscreen_grid_has_a_black_empty_cell_and_restores_transparency(self) -> None:
        cameras = [
            SimpleNamespace(name="Garden", uid="garden"),
            RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10/stream"),
            RtspCamera("rtsp:kitchen", "Kitchen", "rtsp://192.0.2.11/stream"),
        ]
        self.window.devices = cameras
        self.window.selected_device = cameras[0]
        with patch.object(CameraPreview, "start"), patch.object(self.window, "fit_video_aspect"):
            self.window.set_camera_layout("grid")
            self.window.show()
            self.application.processEvents()
            self.window.update_camera_mask()
            self.assertFalse(self.window.mask().isEmpty())
            self.window.toggle_fullscreen()
            self.application.processEvents()
            self.window.update_camera_mask()
            self.assertTrue(self.window.mask().isEmpty())
            empty = self.window.video_grid.cellRect(1, 1)
            self.assertEqual(self.window.centralWidget().grab().toImage().pixelColor(empty.center()), QColor("black"))
            self.window.toggle_fullscreen()
            self.application.processEvents()
            self.window.update_camera_mask()
            self.assertFalse(self.window.mask().isEmpty())

    def test_grid_reserves_empty_cells_and_clears_transparency_without_cameras(self) -> None:
        camera = SimpleNamespace(name="Garden", uid="garden")
        self.window.devices = [camera]
        self.window.device_accounts = {camera.uid: "account@example.com"}
        self.window.selected_device = camera
        with patch.object(self.window, "fit_video_aspect"):
            self.window.set_camera_layout("grid")
            self.window.show()
            self.application.processEvents()
            self.window.update_camera_mask()
            self.assertEqual(self.window.camera_grid_dimensions(), (2, 2))
            offset = self.window.centralWidget().mapTo(self.window, QPoint())
            for row, column in ((0, 1), (1, 0), (1, 1)):
                self.assertFalse(self.window.mask().contains(self.window.video_grid.cellRect(row, column).center() + offset))
            top_right = self.window.video_grid.cellRect(0, 1)
            self.assertFalse(self.window.mask().contains(QPoint(top_right.center().x(), top_right.bottom() + 1) + offset))
            self.window.set_camera_visible(camera.uid, False)
            self.application.processEvents()
            self.assertTrue(self.window.mask().isEmpty())
            self.assertTrue(self.window.empty_camera_label.isVisible())
            self.assertEqual(self.window.camera_grid_dimensions(), (1, 1))

    def test_grid_with_two_cameras_has_no_separator_in_the_empty_row(self) -> None:
        self.window.devices = [
            SimpleNamespace(name="Garden", uid="garden"),
            RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:554/stream"),
        ]
        self.window.selected_device = self.window.devices[0]
        with patch.object(CameraPreview, "start"), patch.object(self.window, "fit_video_aspect"):
            self.window.set_camera_layout("grid")
            self.window.show()
            self.application.processEvents()
            self.window.update_camera_mask()
            offset = self.window.centralWidget().mapTo(self.window, QPoint())
            bottom_left = self.window.video_grid.cellRect(1, 0)
            bottom_right = self.window.video_grid.cellRect(1, 1)
            for x in range(bottom_left.right() + 1, bottom_right.left()):
                self.assertFalse(self.window.mask().contains(QPoint(x, bottom_left.center().y()) + offset))

    def test_each_video_opens_only_its_own_controls_on_click(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = garden
        with patch.object(CameraPreview, "start"):
            self.window.show()
            self.window.sync_previews()
            self.application.processEvents()
            preview = self.window.previews[entrance.uid]
            self.assertFalse(self.window.overlay.isVisible())
            self.assertFalse(preview.overlay.isVisible())
            self.window.video.clicked.emit()
            self.assertTrue(self.window.overlay.isVisible())
            self.assertFalse(preview.overlay.isVisible())
            preview.video.clicked.emit()
            self.assertTrue(preview.overlay.isVisible())
            self.assertTrue(preview.overlay_timer.isActive())
            center = preview.video.mapToGlobal(preview.video.rect().center())
            self.assertLessEqual(abs(preview.overlay.geometry().center().x() - center.x()), 1)
            self.assertLess(preview.overlay.geometry().bottom(), preview.video.mapToGlobal(preview.video.rect().bottomRight()).y())
            preview.video.clicked.emit()
            self.assertFalse(preview.overlay.isVisible())
            self.window.video.clicked.emit()
            self.assertFalse(self.window.overlay.isVisible())

    def test_preview_controls_use_their_own_camera(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        preview = CameraPreview(camera, True)
        preview.worker = SimpleNamespace(set_recording=Mock(), set_continuous=Mock())
        preview.mpv_directory = SimpleNamespace(name=self.settings_directory.name)
        preview.live = True
        replayed: list[RtspCamera] = []
        preview.replay_requested.connect(replayed.append)
        preview.replay_button.click()
        self.assertEqual(replayed, [camera])
        with patch("app.media_directory", return_value=Path(self.settings_directory.name)), patch(
            "app.mpv_request", return_value=(True, None)
        ) as request:
            preview.take_snapshot()
            self.assertEqual(request.call_args.args[1][0], "screenshot-to-file")
            self.assertIn("Entrée_", request.call_args.args[1][1])
            preview.change_zoom(1)
            self.assertIn(["set_property", "video-zoom", 0.5], [call.args[1] for call in request.call_args_list])
            preview.video.drag_moved.emit(20, -10)
            self.assertTrue(preview.video_pan[0] > 0)
            self.assertTrue(preview.video_pan[1] < 0)
            self.assertIn("video-pan-x", [call.args[1][1] for call in request.call_args_list])
            preview.toggle_recording()
            self.assertIn("Entrée_", str(preview.recording_path))
            preview.toggle_recording()
            self.assertIsNone(preview.recording_path)
        self.assertEqual(preview.worker.set_recording.call_count, 2)
        preview.set_continuous(False)
        preview.worker.set_continuous.assert_called_once_with(False)
        preview.close()

    def test_icam365_light_endpoint_and_preview_toggle(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        with patch("app.http.client.HTTPConnection") as connection:
            response = connection.return_value.getresponse.return_value
            response.status = 200
            response.getheader.return_value = ICAM365_SERVER
            response.read.return_value = b"OK"
            self.assertTrue(icam365_light_request(camera))
            connection.return_value.request.assert_called_with("HEAD", "/whitelight")
            self.assertTrue(icam365_light_request(camera, "2"))
            connection.return_value.request.assert_called_with("GET", "/whitelight?mode=2")
            self.assertFalse(icam365_light_request(camera, "unexpected"))
        preview = CameraPreview(camera)
        preview.worker = RtspStreamWorker(camera, Mock(), Path(self.settings_directory.name) / "player.sock")
        preview.live = True
        preview._on_rtsp_light_available()
        self.assertFalse(preview.rtsp_light_button.isHidden())
        self.assertIsNone(preview.rtsp_light_button.menu())
        controls = preview.overlay.layout().itemAt(0).layout()
        self.assertLess(controls.indexOf(preview.record_button), controls.indexOf(preview.rtsp_light_button))
        self.assertLess(controls.indexOf(preview.rtsp_light_button), controls.indexOf(preview.zoom_out_button))
        preview.rtsp_light_button.click()
        self.assertEqual(preview.worker.light_request, "on")
        preview._on_rtsp_light_changed("on")
        self.assertEqual(preview.rtsp_light_button.property("iconName"), "light_on")
        preview.rtsp_light_button.click()
        self.assertEqual(preview.worker.light_request, "2")
        preview._on_rtsp_audio_available()
        self.assertFalse(preview.sound_button.isHidden())
        with patch("app.mpv_request", return_value=(True, None)) as request:
            preview.sound_button.click()
            self.assertEqual(
                [call.args[1] for call in request.call_args_list[-2:]],
                [["set_property", "aid", "auto"], ["set_property", "mute", False]],
            )
            preview.sound_button.click()
            self.assertEqual(
                [call.args[1] for call in request.call_args_list[-2:]],
                [["set_property", "aid", "no"], ["set_property", "mute", True]],
            )
            preview.sound_button.click()
        self.assertTrue(preview.sound_enabled)
        fullscreen: list[bool] = []
        preview.fullscreen_requested.connect(lambda: fullscreen.append(True))
        preview.video.double_clicked.emit(10, 10)
        self.assertEqual(fullscreen, [True])
        preview.close()

    def test_icam365_tilt_stops_even_if_command_fails(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        with patch("app.http.client.HTTPConnection") as connection, patch("app.time.sleep"):
            response = connection.return_value.getresponse.return_value
            response.status = 400
            response.getheader.return_value = ICAM365_SERVER
            self.assertTrue(icam365_ptz_request(camera))
            connection.return_value.request.assert_called_with("HEAD", "/ptzctrl")
            connection.return_value.request.reset_mock()
            response.status = 200
            response.read.return_value = b"OK"
            self.assertTrue(icam365_ptz_request(camera, "Up"))
            self.assertEqual(
                [call.args for call in connection.return_value.request.call_args_list],
                [("GET", "/ptzctrl?act=1"), ("GET", "/ptzctrl?act=0")],
            )
            connection.return_value.request.reset_mock()
            response.read.side_effect = [b"Rejected", b"OK"]
            self.assertFalse(icam365_ptz_request(camera, "Down"))
            self.assertEqual(
                [call.args for call in connection.return_value.request.call_args_list],
                [("GET", "/ptzctrl?act=3"), ("GET", "/ptzctrl?act=0")],
            )
            self.assertFalse(icam365_ptz_request(camera, "Left"))

    def test_icam365_tilt_controls_appear_only_after_probe(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        preview = CameraPreview(camera)
        preview.worker = RtspStreamWorker(camera, Mock(), Path(self.settings_directory.name) / "player.sock")
        preview.live = True
        self.assertTrue(preview.ptz_button.isHidden())
        preview._on_rtsp_ptz_available()
        self.assertFalse(preview.ptz_button.isHidden())
        self.assertIsNone(preview.ptz_button.menu())
        self.assertIs(type(preview.ptz_panel), type(self.window.ptz_panel))
        self.assertEqual(
            [button.property("iconName") for button in preview.ptz_panel.buttons if not button.isHidden()],
            ["up", "down"],
        )
        preview.ptz_panel.buttons[2].click()
        self.assertFalse(preview.ptz_panel.buttons[3].isEnabled())
        self.assertEqual(preview.worker.ptz_request, "Up")
        preview._on_control_finished("Up")
        preview.close()

    def test_selected_icam365_tilt_uses_same_ptz_panel(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:8001/0")
        self.window.selected_device = camera
        self.window.stream_worker = RtspStreamWorker(camera, Mock(), Path(self.settings_directory.name) / "player.sock")
        self.window.stream_live = True
        self.window.set_controls_enabled(True)
        self.assertFalse(self.window.ptz_button.isEnabled())
        self.window.on_rtsp_ptz_available()
        self.assertTrue(self.window.ptz_button.isEnabled())
        self.assertEqual([button.isHidden() for button in self.window.camera_buttons],
                         [True, True, False, False, True, True, True, True, True])
        self.window.control_camera(("Up",))
        self.assertEqual(self.window.stream_worker.ptz_request, "Up")
        self.assertFalse(self.window.camera_buttons[2].isEnabled())
        self.window.on_control_completed("Up")
        self.assertTrue(self.window.camera_buttons[2].isEnabled())
        self.window.stream_worker.ptz_request = None
        self.window.move_by_drag(0, -80)
        self.assertEqual(self.window.stream_worker.ptz_request, "Down")

    def test_native_ptz_pan_and_saved_positions_in_both_panes(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:8001/0")
        preview = CameraPreview(camera)
        try:
            preview.worker = RtspStreamWorker(camera, Mock(), Path(self.settings_directory.name) / "preview.sock")
            preview.worker.native_bridge = SimpleNamespace(
                pan_supported=True, presets={"Preset 1": NativePreset(1, "Lieu1", 0, (0.4, 0.3, 0.01))},
            )
            preview.live = True
            preview._on_rtsp_ptz_available()
            self.window.selected_device = camera
            self.window.stream_worker = preview.worker
            self.window.stream_live = True
            self.window.on_rtsp_ptz_available()
            for panel in (preview.ptz_panel, self.window.ptz_panel):
                self.assertEqual(
                    [button.property("iconName") for button in panel.buttons if not button.isHidden()],
                    ["left", "right", "up", "down", "preset_1"],
                )
                self.assertEqual(panel.buttons[4].toolTip(), "Go to Lieu1")
                self.assertEqual(panel.presets.objectName(), "presetControls")
            preview.ptz_panel.buttons[4].click()
            self.assertEqual(preview.worker.ptz_request, "Preset 1")
            preview.worker.ptz_request = None
            preview._on_control_finished("Preset 1")
            preview.move_by_drag(80, 0)
            self.assertEqual(preview.worker.ptz_request, "Left")
            preview.worker.ptz_request = None
            self.window.move_by_drag(-80, 0)
            self.assertEqual(preview.worker.ptz_request, "Right")
            self.assertFalse(preview.worker.set_ptz("Preset 2"))
        finally:
            self.window.stream_worker = None
            preview.close()

    def test_okam_preview_offers_camera_and_local_playback(self) -> None:
        camera = SimpleNamespace(name="Jardin", uid="garden")
        preview = CameraPreview(camera)
        camera_replays: list[object] = []
        local_replays: list[object] = []
        preview.camera_replay_requested.connect(camera_replays.append)
        preview.replay_requested.connect(local_replays.append)
        actions = preview.replay_button.menu().actions()
        actions[0].trigger()
        actions[1].trigger()
        self.assertEqual(camera_replays, [camera])
        self.assertEqual(local_replays, [camera])
        preview.close()

    def test_okam_preview_light_targets_its_worker(self) -> None:
        camera = SimpleNamespace(name="Jardin", uid="garden")
        preview = CameraPreview(camera, settings=self.window.settings)
        preview.worker = StreamWorker(camera, "", io.BytesIO())
        preview.live = True
        preview._on_capabilities(["HD"], False)
        self.assertTrue(preview.light_action.isVisible())
        preview.toggle_light()
        self.assertEqual(preview.worker.settings.get_nowait(), ("light", True))
        preview._on_setting_completed("light", True)
        self.assertEqual(preview.light_action.text(), "Turn white light off")
        preview.close()

    def test_preview_camera_playback_selects_its_own_feed(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = entrance
        self.window.device_accounts = {garden.uid: "first@example.com", entrance.uid: RTSP_ACCOUNT}
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
            with patch.object(self.window, "stop_player"), patch.object(
                self.window, "show_window_without_stream"
            ), patch.object(self.window, "enter_replay") as replay:
                self.window.open_camera_sd_replay(garden)
                replay.assert_not_called()
                self.application.processEvents()
        self.assertIs(self.window.selected_device, garden)
        self.assertIn(entrance.uid, self.window.previews)
        replay.assert_called_once_with(None)

    def test_card_detection_notification_selects_its_camera_at_the_detection(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = entrance
        self.window.device_accounts = {garden.uid: "first@example.com", entrance.uid: RTSP_ACCOUNT}
        moment = datetime(2026, 10, 2, 9, 44, 42)
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
            with patch.object(self.window, "stop_player"), patch.object(
                self.window, "show_window_without_stream"
            ), patch.object(self.window, "enter_replay") as replay:
                self.window.open_card_detection(garden.uid, moment)
                self.window.open_card_detection("removed", moment)
                replay.assert_not_called()
                self.application.processEvents()
        self.assertIs(self.window.selected_device, garden)
        replay.assert_called_once_with(moment)
        self.assertIsNone(self.window.pending_camera_replay_start)

    def test_continuous_recording_setting_reaches_other_camera(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = garden
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
        preview = self.window.previews[entrance.uid]
        self.assertTrue(preview.continuous_enabled)
        self.window.set_continuous_recording(False)
        self.assertFalse(preview.continuous_enabled)
        self.window.set_continuous_recording(True)
        self.assertTrue(preview.continuous_enabled)

    def test_selected_camera_recording_failure_keeps_continuous_recording_enabled(self) -> None:
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        garden = RtspCamera("rtsp:garden", "Jardin", "rtsp://192.0.2.11:8001/0")
        self.window.devices = [entrance, garden]
        self.window.selected_device = entrance
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
        self.window.continuous_action.setChecked(True)
        self.window.on_continuous_failed("Continuous RTSP recording stopped.")
        self.assertTrue(self.window.continuous_action.isChecked())
        self.assertTrue(self.window.continuous_recording_enabled())
        self.assertTrue(self.window.previews[garden.uid].continuous_enabled)

    def test_stopped_continuous_rtsp_recording_restarts_after_a_delay(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/intraswitch_okam_test.sock"))
        worker.set_continuous(True)
        failures = []
        worker.continuous_failed.connect(failures.append)
        stopped = Mock(returncode=1)
        stopped.poll.return_value = 1
        restarted = Mock()
        restarted.poll.return_value = None
        with patch.object(worker, "_ffmpeg", side_effect=[stopped, restarted]) as ffmpeg, patch.object(
            worker, "_finish_recording_process"
        ), patch("app.continuous_directory", return_value=Path(self.settings_directory.name)):
            worker._update_continuous(100.0)
            self.assertIs(worker.continuous, stopped)
            worker._update_continuous(101.0)
            self.assertIsNone(worker.continuous)
            self.assertEqual(failures, ["Continuous RTSP recording stopped."])
            self.assertTrue(worker.continuous_enabled.is_set())
            worker._update_continuous(105.9)
            self.assertEqual(ffmpeg.call_count, 1)
            worker._update_continuous(106.0)
        self.assertIs(worker.continuous, restarted)
        self.assertEqual(ffmpeg.call_count, 2)

    def test_continuous_rtsp_recording_start_failure_is_retried(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/intraswitch_okam_test.sock"))
        worker.set_continuous(True)
        restarted = Mock()
        restarted.poll.return_value = None
        with patch.object(worker, "_ffmpeg", side_effect=[OSError("ffmpeg unavailable"), restarted]), patch(
            "app.continuous_directory", return_value=Path(self.settings_directory.name)
        ):
            worker._update_continuous(100.0)
            self.assertIsNone(worker.continuous)
            self.assertTrue(worker.continuous_enabled.is_set())
            worker._update_continuous(104.9)
            self.assertIsNone(worker.continuous)
            worker._update_continuous(105.0)
        self.assertIs(worker.continuous, restarted)

    def test_secondary_stream_cameras_use_the_main_stream_only_in_full_screen(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        kitchen = RtspCamera("rtsp:kitchen", "Cuisine", "rtsp://192.0.2.11:554/live", provider=IMOU_ACCOUNT_PROVIDER)
        plain = RtspCamera("rtsp:plain", "Garage", "rtsp://192.0.2.12:554/live")
        self.window.devices = [garden, entrance, kitchen, plain]
        self.window.selected_device = garden
        self.window.stream_worker = Mock()
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
        with patch("app.load_icam365_config", side_effect=lambda uid: object() if uid == entrance.uid else None):
            self.window.update_secondary_stream_menu()
        actions = {action.text(): action for action in self.window.secondary_stream_menu.actions()}
        self.assertEqual(list(actions), ["Jardin", "Entrée", "Cuisine"])
        self.assertFalse(actions["Entrée"].isChecked())
        self.assertTrue(self.window.previews[entrance.uid].main_stream)
        actions["Entrée"].setChecked(True)
        actions["Jardin"].setChecked(True)
        self.assertTrue(self.window.camera_secondary_stream_enabled(entrance.uid))
        self.assertFalse(self.window.previews[entrance.uid].main_stream)
        self.assertTrue(self.window.previews[kitchen.uid].main_stream)
        self.window.stream_worker.set_main_stream.assert_called_with(False)
        with patch.object(self.window, "isFullScreen", return_value=True):
            self.window.apply_stream_quality()
        self.assertTrue(self.window.previews[entrance.uid].main_stream)
        self.window.stream_worker.set_main_stream.assert_called_with(True)
        self.window.apply_stream_quality()
        self.assertFalse(self.window.previews[entrance.uid].main_stream)
        self.window.show()
        self.window.toggle_fullscreen()
        self.application.processEvents()
        self.assertTrue(self.window.previews[entrance.uid].main_stream)
        self.window.stream_worker.set_main_stream.assert_called_with(True)
        self.window.toggle_fullscreen()
        self.application.processEvents()
        self.assertFalse(self.window.previews[entrance.uid].main_stream)
        self.window.stream_worker.set_main_stream.assert_called_with(False)
        self.window.stream_worker = None

    def test_camera_pane_starts_and_switches_its_worker_on_the_requested_stream(self) -> None:
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        preview = CameraPreview(entrance)
        preview.set_main_stream(False)
        preview.worker = Mock()
        preview.set_main_stream(True)
        preview.worker.set_main_stream.assert_called_once_with(True)
        preview.worker = None
        preview.close()

    def test_rtsp_worker_changes_the_native_stream_and_restarts_its_recording(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/intraswitch_okam_test.sock"), "SD")
        worker.native_bridge = Mock(main_stream=True)
        worker.set_continuous(True)
        recording = Mock()
        worker.continuous = recording
        player = SimpleNamespace(height=1296)
        with patch.object(worker, "_finish_recording_process") as finish, patch.object(worker, "_ffmpeg") as recorder, \
                patch("app.mpv_request", side_effect=lambda socket_path, command: (True, player.height)), \
                patch("app.continuous_directory", return_value=Path("/tmp")), \
                patch("app.time.monotonic", return_value=100.0):
            worker._select_native_stream()
            worker.native_bridge.set_main_stream.assert_called_once_with(False)
            finish.assert_called_once_with(recording)
            self.assertIsNone(worker.continuous)
            worker.native_bridge.main_stream = False
            worker._select_native_stream()
            worker.native_bridge.set_main_stream.assert_called_once_with(False)
            worker._update_continuous(101.0)
            player.height = None
            worker._update_continuous(102.0)
            recorder.assert_not_called()
            player.height = 360
            worker._update_continuous(103.0)
            recorder.assert_called_once()
            self.assertIs(worker.continuous, recorder.return_value)
            worker.set_main_stream(True)
            worker._select_native_stream()
            worker.native_bridge.set_main_stream.assert_called_with(True)
            finish.assert_called_with(recorder.return_value)
            worker._update_continuous(114.9)
            recorder.assert_called_once()
            worker._update_continuous(115.0)
            self.assertEqual(recorder.call_count, 2)

    def test_imou_worker_queues_each_stream_change_once(self) -> None:
        camera = RtspCamera("rtsp:kitchen", "Cuisine", "rtsp://192.0.2.11:554/live", provider=IMOU_ACCOUNT_PROVIDER)
        worker = RtspStreamWorker(camera, Mock(), Path("/tmp/intraswitch_okam_test.sock"))
        worker.set_main_stream(True)
        self.assertTrue(worker.settings_requests.empty())
        worker.set_main_stream(False)
        worker.set_main_stream(False)
        self.assertEqual(worker.settings_requests.get_nowait(), ("quality", "SD"))
        self.assertTrue(worker.settings_requests.empty())
        self.assertEqual(worker.quality, "HD")

    def test_local_replay_stays_in_its_camera_pane(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        recording = Path(self.settings_directory.name) / "Entrée_entrance_20260927_180000.mkv"
        recording.write_bytes(b"video")
        self.window.devices = [camera, SimpleNamespace(uid="garden", name="Jardin")]
        self.window.selected_device = self.window.devices[1]
        with patch.object(CameraPreview, "start"):
            self.window.sync_previews()
        preview = self.window.previews[camera.uid]
        with patch("app.QTimer.singleShot"), patch("app.camera_recordings", return_value=[recording]), patch(
            "app.subprocess.Popen"
        ) as player:
            player.return_value.stdin = None
            player.return_value.poll.return_value = None
            self.window.open_local_replay(camera)
            replay = self.window.local_replays[camera.uid]
            replay.refresh_recordings()
            self.assertEqual(replay.recordings, [recording])
            self.assertEqual(replay.current_path, recording)
            self.assertEqual(replay.timeline.recordings[0].name, recording.name)
            self.assertIs(type(replay.controls), type(self.window.replay_controls))
            self.assertIs(preview.video_stack.currentWidget(), replay)
            self.assertIs(self.window.primary_video_stack.currentWidget(), self.window.primary_frame)
            self.assertFalse(replay.isWindow())
            self.assertEqual(player.call_args.args[0][-1], str(recording))
            self.assertIn(RTSP_DENOISE_FILTER, player.call_args.args[0])
            with patch("app.mpv_request", return_value=(True, None)) as request:
                replay.change_speed()
                self.assertEqual(request.call_args.args[1], ["set_property", "speed", 2])
                self.assertEqual(replay.controls.speed_button.text(), "2x")
            replay.controls.live_button.click()
            self.assertIs(preview.video_stack.currentWidget(), preview.frame)
            player.return_value.terminate.assert_called_once()

    def test_fullscreen_double_click_works_on_both_video_panes(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = garden
        with patch.object(MainWindow, "toggle_fullscreen") as fullscreen, patch.object(CameraPreview, "start"):
            self.window.sync_previews()
            self.window.video.double_clicked.emit(10, 10)
            self.window.previews[entrance.uid].video.double_clicked.emit(10, 10)
            self.assertEqual(fullscreen.call_count, 2)
        self.window.on_stream_status("Live video")
        self.assertEqual(self.window.windowTitle(), "Camera Viewer")
        self.assertEqual(self.window.video.camera_uid, garden.uid)

    def test_local_replay_excludes_the_open_recording(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        recording = Path(self.settings_directory.name) / "Entrée_entrance_20260927_180000.mkv"
        recording.write_bytes(b"video")
        worker = RtspStreamWorker(camera, Mock(), recording.parent / "control.sock")
        worker.continuous = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        with patch("app.QTimer.singleShot"):
            replay = LocalReplayPane(camera, self.window, worker)
        with recording.open("rb"):
            self.assertIn(recording, replay.active_recordings())
        replay.stop()

    def test_camera_replay_loading_appears_on_video_instead_of_title(self) -> None:
        class Replay(QObject):
            status_changed = pyqtSignal(str)
            playing_changed = pyqtSignal(bool)
            clip_available = pyqtSignal(bool)

            def __init__(self, *args: object) -> None:
                super().__init__()

            def start(self, output: object, moment: object) -> None:
                self.status_changed.emit("Loading 27/09/2026 07:00:54 97%")

            def stop(self) -> None:
                pass

        camera = SimpleNamespace(uid="garden", name="Jardin")
        self.window.devices = [camera]
        self.window.selected_device = camera
        self.window.player = SimpleNamespace(stdin=io.BytesIO())
        self.window.show()
        self.window.activateWindow()
        QTest.qWait(10)
        with patch("app.ReplayController", Replay), patch.object(self.window, "start_player", return_value=True), patch(
            "app.stored_camera_password", return_value=""
        ):
            self.window.enter_replay(None)
        self.assertEqual(self.window.windowTitle(), "Camera Viewer · Playback")
        self.assertEqual(self.window.replay_status_overlay.label.text(), "Loading 27/09/2026 07:00:54 97%")
        self.assertTrue(self.window.replay_status_overlay.isVisible())
        self.window.replay.status_changed.emit("Playback 27/09/2026 07:00:54")
        self.assertFalse(self.window.replay_status_overlay.isVisible())
        self.window.exit_replay(False)

    def test_removing_selected_rtsp_camera_switches_to_remaining_camera(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            camera = RtspCamera("rtsp:test", "Entrée", "rtsp://192.0.2.10:8001/0")
            garden = SimpleNamespace(name="Jardin", uid="garden")
            self.window.rtsp_cameras = [camera]
            self.window.devices = [garden, camera]
            self.window.device_accounts = {"garden": "first@example.com", camera.uid: RTSP_ACCOUNT}
            self.window.selected_device = camera
            selected: list[tuple[str, str]] = []
            self.window.select_camera = lambda account, uid: selected.append((account, uid))
            self.window.remove_selected_rtsp_camera()
            self.assertEqual(selected, [("first@example.com", "garden")])
            self.assertEqual(self.window.devices, [garden])
            self.assertEqual(load_rtsp_cameras(self.window.settings), [])

    def test_switching_camera_stops_previous_stream_before_starting_new_one(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            garden = SimpleNamespace(name="Jardin", uid="garden")
            entrance = SimpleNamespace(name="Entrée", uid="entrance")
            self.window.devices = [garden, entrance]
            self.window.device_accounts = {"garden": "first@example.com", "entrance": "second@example.com"}
            self.window.selected_device = garden
            self.window.stream_worker = SimpleNamespace()
            stopped: list[bool] = []
            started: list[str] = []
            self.window.stop_stream = lambda: stopped.append(True)
            self.window.watch_live = lambda: started.append(self.window.selected_device.uid)
            self.window.select_camera("second@example.com", "entrance")
            self.assertEqual(stopped, [True])
            self.assertIs(self.window.selected_device, garden)
            with patch.object(CameraPreview, "start"):
                self.window.on_stream_finished()
            self.assertEqual(started, ["entrance"])
            self.assertEqual(self.window.settings.value("camera/selected_uid"), "entrance")

    def test_recording_badge_shows_elapsed_time(self) -> None:
        self.window.show()
        self.window.activateWindow()
        QTest.qWait(10)
        self.window.recording_path = Path("badge-test.mkv")
        self.window.on_recording_started()
        self.window.recording_started_at = time.monotonic() - 3661
        self.window.update_recording_badge()
        self.assertTrue(self.window.recording_badge.isVisible())
        self.assertIn("01:01:01", self.window.recording_label.text())
        self.window.reset_recording_state()
        self.assertFalse(self.window.recording_badge.isVisible())


if __name__ == "__main__":
    unittest.main()
