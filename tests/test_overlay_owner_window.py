import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import Qt
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QMainWindow, QPushButton, QVBoxLayout, QWidget

from app import ControlsOverlay, VideoWidget


class OverlayOwnerWindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_overlay_built_before_its_window_is_transient_for_that_window(self) -> None:
        window = QMainWindow()
        body = QWidget()
        video = VideoWidget()
        video.setAttribute(Qt.WidgetAttribute.WA_NativeWindow)
        QVBoxLayout(body).addWidget(video)
        overlay = ControlsOverlay(video)
        QVBoxLayout(overlay).addWidget(QPushButton("Playback"))
        try:
            video.winId()
            window.setCentralWidget(body)
            window.show()
            window.activateWindow()
            QTest.qWait(10)
            overlay.show()
            QTest.qWait(10)
            self.assertTrue(overlay.isVisible())
            self.assertIs(overlay.windowHandle().transientParent(), window.windowHandle())
        finally:
            overlay.close()
            window.close()


if __name__ == "__main__":
    unittest.main()
