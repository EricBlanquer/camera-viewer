import io
import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings, Qt
from PyQt6.QtWidgets import QApplication, QWidget

from app import AspectVideoFrame, CameraPreview, LocalReplayDialog, MainWindow, RTSP_ACCOUNT, RtspCamera, RtspStreamWorker, StreamWorker, load_rtsp_cameras, mpv_rtsp_command, valid_rtsp_url


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

    def tray_action(self, label: str):
        return next(action for action, button in self.window.tray_actions if button.toolTip() == label)

    def test_tray_actions_follow_overlay_buttons(self) -> None:
        self.window.show()
        self.window.snapshot_button.setEnabled(True)
        self.window.update_tray_menu()
        self.assertTrue(self.tray_action("Save picture").isEnabled())
        self.assertFalse(self.tray_action("Record video").isEnabled())
        self.assertFalse(self.tray_action("Turn white light on").isVisible())
        self.window.on_capabilities_found([], False)
        self.window.update_tray_menu()
        self.assertTrue(self.tray_action("Turn white light on").isVisible())
        self.assertFalse(self.window.quality_menu.menuAction().isVisible())

    def test_tray_action_clicks_its_button(self) -> None:
        self.window.show()
        clicks: list[bool] = []
        self.window.snapshot_button.setEnabled(True)
        self.window.snapshot_button.clicked.connect(lambda: clicks.append(True))
        self.tray_action("Save picture").trigger()
        self.assertEqual(clicks, [True])

    def test_closing_window_hides_it_to_tray(self) -> None:
        self.window.show()
        self.window.close()
        self.assertFalse(self.window.isVisible())
        self.window.reconnect = lambda: None
        self.window.toggle_window()
        self.assertTrue(self.window.isVisible())

    def test_detections_notify_only_after_the_first_check(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            self.window.selected_device = SimpleNamespace(name="Jardin", uid="garden")
            self.window.devices = [self.window.selected_device]
            messages: list[tuple[str, str]] = []
            self.window.tray.showMessage = lambda title, text, *args: messages.append((title, text))
            self.window.on_detections_listed(["20260926091500_011.mp4"])
            self.assertEqual(messages, [])
            self.window.on_detections_listed(["20260926091500_011.mp4"])
            self.assertEqual(messages, [])
            self.window.on_detections_listed(
                ["20260926091500_011.mp4", "20260926143210_011.mp4", "20260926150001_011.mp4"]
            )
            self.assertEqual(messages, [("Camera detection", "Jardin \u00b7 26/09 15:00:01 (2 new detections)")])
            self.assertEqual(self.window.settings.value("detections/last_seen/garden"), "20260926150001_011.mp4")

    def test_tray_lists_and_switches_account_cameras(self) -> None:
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
        self.assertFalse(actions[1].isChecked())
        chosen: list[tuple[str, str]] = []
        self.window.select_camera = lambda account, uid: chosen.append((account, uid))
        actions[1].trigger()
        self.assertEqual(chosen, [("second@example.com", "entrance")])

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
            chosen: list[tuple[str, str]] = []
            self.window.select_camera = lambda account, uid: chosen.append((account, uid))
            actions[1].trigger()
            self.assertEqual(chosen, [(RTSP_ACCOUNT, camera.uid)])
            self.window.accounts = []
            with patch.object(self.window, "watch_live"):
                self.window.find_cameras()
            self.assertEqual(self.window.devices, [camera])

    def test_rtsp_url_rejects_embedded_credentials_and_invalid_ports(self) -> None:
        self.assertTrue(valid_rtsp_url("rtsp://192.0.2.10:8001/0"))
        self.assertFalse(valid_rtsp_url("rtsp://user:password@192.0.2.10:8001/0"))
        self.assertFalse(valid_rtsp_url("rtsp://192.0.2.10:bad/0"))
        camera = RtspCamera("rtsp:test", "Other", "rtsp://192.0.2.10:554/stream", "tcp")
        self.assertIn("--rtsp-transport=tcp", mpv_rtsp_command(Path("/tmp/control.sock"), 1, True, camera))
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            self.window.settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            self.window.rtsp_cameras = [camera]
            self.window.save_rtsp_cameras()
            self.assertEqual(load_rtsp_cameras(self.window.settings), [camera])

    def test_show_all_cameras_keeps_a_preview_for_each_other_camera(self) -> None:
        garden = SimpleNamespace(name="Jardin", uid="garden")
        entrance = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        self.window.devices = [garden, entrance]
        self.window.selected_device = garden
        with patch.object(CameraPreview, "start") as start_preview:
            self.window.sync_previews()
            self.assertEqual(list(self.window.previews), [entrance.uid])
            self.assertIs(self.window.video_grid.itemAtPosition(0, 1).widget(), self.window.previews[entrance.uid])
            self.window.primary_label.moved.emit(garden.uid, entrance.uid)
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
            self.window.set_show_all_cameras(False)
            self.assertEqual(self.window.previews, {})
            self.assertFalse(self.window.settings.value("view/show_all_cameras", True, bool))

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
            self.assertFalse(self.window.overlay_timer.isActive())
            self.window.resize(screen.width() // 2, screen.height())
            self.window.sync_previews()
            self.assertEqual(self.window.effective_camera_layout(), "vertical")
            self.assertIs(self.window.video_grid.itemAtPosition(1, 0).widget(), self.window.previews["rtsp:entrance"])

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
            self.assertEqual(request.call_args.args[1], ["set_property", "video-zoom", 0.5])
            preview.toggle_recording()
            self.assertIn("Entrée_", str(preview.recording_path))
            preview.toggle_recording()
            self.assertIsNone(preview.recording_path)
        self.assertEqual(preview.worker.set_recording.call_count, 2)
        preview.set_continuous(False)
        preview.worker.set_continuous.assert_called_once_with(False)
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

    def test_okam_preview_light_and_quality_target_its_worker(self) -> None:
        camera = SimpleNamespace(name="Jardin", uid="garden")
        preview = CameraPreview(camera, settings=self.window.settings)
        preview.worker = StreamWorker(camera, "", io.BytesIO())
        preview.live = True
        preview._on_capabilities(["HD"], False)
        self.assertTrue(preview.light_action.isVisible())
        self.assertTrue(preview.quality_actions["HD"].isVisible())
        preview.toggle_light()
        self.assertEqual(preview.worker.settings.get_nowait(), ("light", True))
        preview._on_setting_completed("light", True)
        self.assertEqual(preview.light_action.text(), "Turn white light off")
        preview.choose_quality("HD")
        self.assertEqual(preview.worker.settings.get_nowait(), ("quality", "HD"))
        preview._on_setting_completed("quality", "HD")
        self.assertEqual(self.window.settings.value("camera/quality/garden"), "HD")
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

    def test_local_replay_dialog_lists_recordings_for_its_camera(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        recording = Path(self.settings_directory.name) / "Entrée_entrance_20260927_180000.mkv"
        recording.write_bytes(b"video")
        with patch("app.QTimer.singleShot"), patch("app.camera_recordings", return_value=[recording]), patch(
            "app.subprocess.Popen"
        ) as player:
            player.return_value.stdin = None
            player.return_value.poll.return_value = None
            dialog = LocalReplayDialog(camera, self.window)
            dialog.refresh_recordings()
            self.assertEqual(dialog.recordings.count(), 1)
            self.assertEqual(dialog.recordings.currentItem().data(Qt.ItemDataRole.UserRole), str(recording))
            self.assertEqual(player.call_args.args[0][-1], str(recording))
            with patch("app.mpv_request", return_value=(True, None)) as request:
                dialog.change_speed()
                self.assertEqual(request.call_args.args[1], ["set_property", "speed", 2])
                self.assertEqual(dialog.speed_button.text(), "2×")
            dialog.close()
            player.return_value.terminate.assert_called_once()

    def test_local_replay_excludes_the_open_recording(self) -> None:
        camera = RtspCamera("rtsp:entrance", "Entrée", "rtsp://192.0.2.10:8001/0")
        recording = Path(self.settings_directory.name) / "Entrée_entrance_20260927_180000.mkv"
        recording.write_bytes(b"video")
        worker = RtspStreamWorker(camera, Mock(), recording.parent / "control.sock")
        worker.continuous = SimpleNamespace(pid=os.getpid(), poll=lambda: None)
        with patch("app.QTimer.singleShot"):
            dialog = LocalReplayDialog(camera, self.window, worker)
        with recording.open("rb"):
            self.assertIn(recording, dialog.active_recordings())
        dialog.close()

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
            self.window.settings.setValue("camera/quality/garden", "HD")
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
            self.assertEqual(self.window.quality_button.text(), "Auto")

    def test_recording_badge_shows_elapsed_time(self) -> None:
        self.window.show()
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
