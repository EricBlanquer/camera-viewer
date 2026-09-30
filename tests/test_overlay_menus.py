import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QPushButton

from app import CameraPreview, IMOU_ACCOUNT_PROVIDER, MainWindow, RtspCamera, set_replay_menu


def luminance(color) -> float:
    channels = [channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
                for channel in (color.redF(), color.greenF(), color.blueF())]
    return sum(channel * weight for channel, weight in zip(channels, (0.2126, 0.7152, 0.0722)))


class OverlayMenusTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def assert_menu_contrast(self, menu) -> None:
        action = menu.actions()[0]
        try:
            for state in ("normal", "selected", "disabled"):
                with self.subTest(state=state):
                    action.setEnabled(state != "disabled")
                    menu.show()
                    menu.setActiveAction(action if state == "selected" else None)
                    self.application.processEvents()
                    image = menu.grab().toImage()
                    area = menu.actionGeometry(action)
                    background = luminance(image.pixelColor(area.right() - 8, area.center().y()))
                    self.assertLess(background, 0.3)
                    threshold = 3.0 if state == "disabled" else 4.5
                    readable_pixels = sum(
                        (luminance(image.pixelColor(x, y)) + 0.05) / (background + 0.05) >= threshold
                        for x in range(area.left() + 8, area.right() - 24)
                        for y in range(area.top() + 3, area.bottom() - 3)
                    )
                    self.assertGreater(readable_pixels, 20)
        finally:
            action.setEnabled(True)
            menu.close()

    def test_all_camera_overlays_omit_video_quality_choices(self) -> None:
        cameras = (
            RtspCamera("imou:kitchen", "Kitchen", "", provider=IMOU_ACCOUNT_PROVIDER),
            RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.1/video"),
            SimpleNamespace(uid="garden", name="Garden"),
        )
        for camera in cameras:
            with self.subTest(camera=camera.uid):
                preview = CameraPreview(camera, settings=self.settings)
                try:
                    self.window.selected_device = camera
                    self.window.on_capabilities_found(["HD", "SD"], False)
                    self.window.on_stream_status("Live video")
                    preview._on_status("Live video")
                    preview._on_capabilities(["HD", "SD"], False)
                    for pane in (self.window, preview):
                        buttons = pane.overlay.findChildren(QPushButton)
                        self.assertFalse(any(button.objectName() == "qualityButton" for button in buttons))
                        self.assertFalse({button.text() for button in buttons} & {"Auto", "HD", "SD", "Super HD", "Low"})
                finally:
                    preview.close()

    def test_preview_playback_menu_has_readable_text_on_dark_video(self) -> None:
        preview = CameraPreview(SimpleNamespace(uid="garden", name="Garden"), settings=self.settings)
        try:
            self.assert_menu_contrast(preview.replay_button.menu())
        finally:
            preview.close()

    def test_main_playback_menu_has_readable_text_on_dark_video(self) -> None:
        set_replay_menu(self.window.replay_button, lambda: None, lambda: None)
        self.assert_menu_contrast(self.window.replay_button.menu())

    def test_tray_submenu_has_readable_text(self) -> None:
        self.window.create_tray()
        self.assert_menu_contrast(self.window.camera_layout_menu)


if __name__ == "__main__":
    unittest.main()
