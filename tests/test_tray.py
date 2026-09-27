import os
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

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
            self.window.selected_device = SimpleNamespace(name="Jardin")
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
            self.assertEqual(self.window.settings.value("detections/last_seen"), "20260926150001_011.mp4")

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
