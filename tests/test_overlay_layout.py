import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QRect, QSettings
from PyQt6.QtWidgets import QApplication, QPushButton

from app import CameraPreview, IMOU_ACCOUNT_PROVIDER, MainWindow, ReplayControls, RtspCamera
from imou import IMOU_TRAFFIC_MESSAGE


class OverlayLayoutTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_")
        settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.window.devices = [SimpleNamespace(uid="garden", name="Garden"),
                               RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.1/video"),
                               RtspCamera("imou:living", "Living room", "", provider=IMOU_ACCOUNT_PROVIDER),
                               RtspCamera("imou:kitchen", "Kitchen", "", provider=IMOU_ACCOUNT_PROVIDER)]
        self.window.selected_device = self.window.devices[0]
        self.window.device_accounts = {camera.uid: "test" for camera in self.window.devices}
        with patch.object(CameraPreview, "start"), patch.object(self.window, "fit_video_aspect"):
            self.window.set_camera_layout("grid")
            self.window.show()
        self.window.on_capabilities_found([], False)
        for preview in self.window.previews.values():
            preview.sound_button.show()
            preview.ptz_button.show()
        self.application.processEvents()

    def tearDown(self) -> None:
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def test_live_overlays_share_dimensions_and_keep_all_controls_inside_each_video(self) -> None:
        for width in (720, 980, 1920, 720, 980):
            with self.subTest(window_width=width), patch.object(self.window, "fit_video_aspect"):
                self.window.resize(width, round(width * 9 / 16))
                self.application.processEvents()
                sizes = []
                for pane in (self.window, *self.window.previews.values()):
                    pane.show_overlay()
                    self.application.processEvents()
                    pane.video.place_overlay()
                    overlay = pane.overlay
                    sizes.append((overlay.width(), overlay.height()))
                    video_area = QRect(pane.video.mapToGlobal(pane.video.rect().topLeft()), pane.video.size())
                    self.assertTrue(video_area.contains(overlay.geometry()))
                    for button in overlay.findChildren(QPushButton):
                        if button.isVisibleTo(overlay):
                            button_area = QRect(button.mapTo(overlay, button.rect().topLeft()), button.size())
                            self.assertTrue(overlay.rect().contains(button_area), button.accessibleName())
                self.assertEqual(len({height for width, height in sizes}), 1, sizes)
                self.assertLessEqual(max(width for width, height in sizes) - min(width for width, height in sizes), 1, sizes)

    def test_playback_controls_fit_when_switching_modes_and_resizing(self) -> None:
        self.window.live_bar.hide()
        self.window.replay_bar.show()
        self.window.timeline.show()
        for width in (720, 980, 1920, 720):
            with self.subTest(window_width=width), patch.object(self.window, "fit_video_aspect"):
                self.window.resize(width, round(width * 9 / 16))
                self.application.processEvents()
                self.window.show_overlay()
                self.application.processEvents()
                self.window.video.place_overlay()
                overlay = self.window.overlay
                video_area = QRect(self.window.video.mapToGlobal(self.window.video.rect().topLeft()), self.window.video.size())
                self.assertTrue(video_area.contains(overlay.geometry()))
                for button in self.window.replay_controls.findChildren(QPushButton):
                    if button.isVisibleTo(overlay):
                        button_area = QRect(button.mapTo(overlay, button.rect().topLeft()), button.size())
                        self.assertTrue(overlay.rect().contains(button_area), button.accessibleName())

    def test_live_and_playback_overlays_omit_fullscreen_buttons(self) -> None:
        for controls in (self.window.overlay, *(preview.overlay for preview in self.window.previews.values()),
                         ReplayControls(self.window.overlay)):
            self.assertFalse(any(button.property("iconName") in ("fullscreen", "exit_fullscreen")
                                 for button in controls.findChildren(QPushButton)))

    def test_long_camera_error_fits_inside_its_video(self) -> None:
        for width in (720, 980):
            with self.subTest(window_width=width), patch.object(self.window, "fit_video_aspect"):
                self.window.resize(width, round(width * 9 / 16))
                self.application.processEvents()
                for pane in self.window.previews.values():
                    status = pane.video.status_overlay
                    status.display(IMOU_TRAFFIC_MESSAGE, True)
                    self.application.processEvents()
                    video_area = QRect(pane.video.mapToGlobal(pane.video.rect().topLeft()), pane.video.size())
                    self.assertTrue(video_area.contains(status.geometry()))
                    label = status.label
                    self.assertGreaterEqual(label.height(), label.heightForWidth(label.width()))


if __name__ == "__main__":
    unittest.main()
