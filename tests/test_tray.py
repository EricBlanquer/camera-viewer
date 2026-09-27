import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import MainWindow


class TrayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
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
            self.window.on_account_finished()
            self.assertIs(self.window.selected_device, entrance)
            self.assertEqual(self.window.settings.value("accounts/okam"), ["first@example.com"])

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
