import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import MainWindow, RtspCamera


class CameraMenuOrderTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()
        names =["Entrée", "Maison 2", "Maison 10", "Cuisine", "salon", "Maison 1", "Jardin"]
        self.window.devices = [RtspCamera(f"rtsp:{index}", name, f"rtsp://192.0.2.{index + 1}/video")
                               for index, name in enumerate(names)]
        self.window.device_accounts = {camera.uid: "rtsp" for camera in self.window.devices}

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()

    def test_camera_menus_list_cameras_by_name_whatever_the_display_order(self) -> None:
        self.window.update_cameras_menu()
        self.window.update_local_detection_menu()
        expected = ["Cuisine", "Entrée", "Jardin", "Maison 1", "Maison 2", "Maison 10", "salon"]
        listed = [action.text().split(" · ")[0] for action in self.window.cameras_menu.actions()
                  if action.text().split(" · ")[0] in expected]
        self.assertEqual(listed, expected)
        self.assertEqual([action.text() for action in self.window.local_detection_menu.actions()
                          if action.text() in expected], expected)
        self.assertEqual([camera.name for camera in self.window.ordered_devices()],
                         ["Entrée", "Maison 2", "Maison 10", "Cuisine", "salon", "Maison 1", "Jardin"])


if __name__ == "__main__":
    unittest.main()
