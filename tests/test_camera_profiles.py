import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QDialog, QDialogButtonBox

from app import (
    CAMERA_PROFILE_NAME_MESSAGE, CAMERA_PROFILE_REPLACE_MESSAGE, CAMERA_PROFILES_SETTING, CameraPreview,
    CameraProfile, CameraProfileDialog, MainWindow, RtspCamera, load_camera_profiles,
)


class CameraProfilesTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_profiles_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()
        self.garden = SimpleNamespace(uid="garden", name="Garden")
        self.entrance = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:554/stream")
        self.kitchen = RtspCamera("rtsp:kitchen", "Kitchen", "rtsp://192.0.2.11:554/stream")
        self.window.devices = [self.garden, self.entrance, self.kitchen]
        self.window.device_accounts = {self.garden.uid: "account@example.com", self.entrance.uid: "rtsp",
                                       self.kitchen.uid: "rtsp"}
        self.window.selected_device = self.garden
        self.preview_start = patch.object(CameraPreview, "start")
        self.preview_start.start()
        self.window.watch_live = Mock()
        self.window.sync_previews()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.preview_start.stop()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def profile_actions(self):
        self.window.update_camera_profiles_menu()
        return [action for action in self.window.camera_profiles_menu.actions() if not action.isSeparator()]

    def save_profile(self, name: str) -> None:
        def accept(dialog: CameraProfileDialog) -> QDialog.DialogCode:
            dialog.name.setText(name)
            return QDialog.DialogCode.Accepted
        with patch.object(self.window, "exec_camera_dialog", side_effect=accept):
            self.profile_actions()[-2].trigger()

    def visible_uids(self) -> list[str]:
        return [camera.uid for camera in self.window.visible_devices()]

    def test_profile_restores_cameras_order_and_layout_after_restart(self) -> None:
        self.assertEqual([action.text() for action in self.profile_actions()],
                         ["No saved profiles", "Save current view...", "Remove profile"])
        self.window.set_camera_visible(self.kitchen.uid, False)
        self.window.swap_cameras(self.garden.uid, self.entrance.uid)
        self.window.set_camera_layout("vertical")
        self.save_profile("  Entrance first  ")
        self.assertEqual(load_camera_profiles(self.settings),
                         [CameraProfile("Entrance first", (self.entrance.uid, self.garden.uid), "vertical")])
        self.assertTrue(self.profile_actions()[0].isChecked())
        self.window.set_camera_visible(self.kitchen.uid, True)
        self.window.swap_cameras(self.entrance.uid, self.kitchen.uid)
        self.window.set_camera_layout("grid")
        self.window.update_camera_layout_menu()
        self.assertTrue(self.window.camera_layout_actions["grid"].isChecked())
        self.assertFalse(self.profile_actions()[0].isChecked())
        self.profile_actions()[0].trigger()
        self.assertEqual(self.visible_uids(), [self.entrance.uid, self.garden.uid])
        self.assertEqual(self.window.camera_layout(), "vertical")
        self.assertEqual(list(self.window.previews), [self.entrance.uid])
        self.assertIs(self.window.video_grid.itemAtPosition(0, 0).widget(), self.window.previews[self.entrance.uid])
        self.assertIs(self.window.video_grid.itemAtPosition(1, 0).widget(), self.window.primary_pane)
        self.window.update_camera_layout_menu()
        self.assertTrue(self.window.camera_layout_actions["vertical"].isChecked())
        self.assertTrue(self.profile_actions()[0].isChecked())
        self.window.watch_live.assert_not_called()
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            restarted = MainWindow()
        try:
            restarted.devices = [self.garden, self.entrance, self.kitchen]
            self.assertEqual([camera.uid for camera in restarted.visible_devices()],
                             [self.entrance.uid, self.garden.uid])
            self.assertEqual(restarted.camera_layout(), "vertical")
        finally:
            restarted.quit_requested = True
            restarted.close()

    def test_profile_without_selected_camera_selects_its_first_camera(self) -> None:
        self.window.set_camera_visible(self.garden.uid, False)
        self.window.set_camera_visible(self.entrance.uid, False)
        self.application.processEvents()
        self.window.watch_live.reset_mock()
        self.save_profile("Kitchen")
        self.window.set_camera_visible(self.garden.uid, True)
        self.window.set_camera_visible(self.kitchen.uid, False)
        self.application.processEvents()
        self.window.watch_live.reset_mock()
        self.assertIs(self.window.selected_device, self.garden)
        self.profile_actions()[0].trigger()
        self.application.processEvents()
        self.assertIs(self.window.selected_device, self.kitchen)
        self.assertEqual(self.visible_uids(), [self.kitchen.uid])
        self.assertEqual(self.window.previews, {})
        self.window.watch_live.assert_called_once_with()

    def test_saving_an_existing_name_replaces_it_and_profiles_can_be_removed(self) -> None:
        self.save_profile("Night")
        self.window.set_camera_visible(self.kitchen.uid, False)
        self.save_profile("night")
        self.save_profile("All day")
        self.assertEqual(load_camera_profiles(self.settings), [
            CameraProfile("All day", (self.garden.uid, self.entrance.uid), "horizontal"),
            CameraProfile("night", (self.garden.uid, self.entrance.uid), "horizontal"),
        ])
        remove_menu = self.profile_actions()[-1].menu()
        self.assertEqual([action.text() for action in remove_menu.actions()], ["All day", "night"])
        remove_menu.actions()[1].trigger()
        self.assertEqual([profile.name for profile in load_camera_profiles(self.settings)], ["All day"])

    def test_invalid_records_and_removed_cameras_are_ignored(self) -> None:
        self.settings.setValue(CAMERA_PROFILES_SETTING, json.dumps([
            {"name": "Kitchen only", "cameras": ["rtsp:removed", self.kitchen.uid], "layout": "grid"},
            {"name": "", "cameras": [], "layout": "grid"},
            {"name": "Letters", "cameras": "garden", "layout": "grid"},
            {"name": "Unknown layout", "cameras": [], "layout": "diagonal"},
            "invalid",
        ]))
        profiles = load_camera_profiles(self.settings)
        self.assertEqual([profile.name for profile in profiles], ["Kitchen only"])
        self.window.apply_camera_profile(profiles[0])
        self.application.processEvents()
        self.assertEqual(self.visible_uids(), [self.kitchen.uid])
        self.assertEqual(self.window.camera_layout(), "grid")
        self.assertTrue(self.profile_actions()[0].isChecked())
        self.settings.setValue(CAMERA_PROFILES_SETTING, "not json")
        self.assertEqual(load_camera_profiles(self.settings), [])

    def test_dialog_requires_a_name_and_announces_replacement(self) -> None:
        dialog = CameraProfileDialog(self.window, ["Night"])
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Rejected)
        self.assertEqual(dialog.message.text(), CAMERA_PROFILE_NAME_MESSAGE)
        dialog.name.setText(" night ")
        self.assertEqual(dialog.message.text(), CAMERA_PROFILE_REPLACE_MESSAGE)
        dialog.name.setText("Day")
        self.assertEqual(dialog.message.text(), "")
        dialog.buttons.button(QDialogButtonBox.StandardButton.Save).click()
        self.assertEqual(dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(dialog.profile_name(), "Day")
        dialog.deleteLater()


if __name__ == "__main__":
    unittest.main()
