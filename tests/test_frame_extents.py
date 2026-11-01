import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class FrameExtentsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xvfb-run"), "Window manager frame verification requires Xvfb")
    def test_frame_extents_are_read_and_reported_when_they_change(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="xcb")
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        completed = subprocess.run(["xvfb-run", "-a", sys.executable, __file__, "--x11-check"],
                                   env=environment, capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


def check_frame_extents():
    from PyQt6.QtCore import QMargins
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    from Xlib import Xatom, display as xdisplay
    from app import NET_FRAME_EXTENTS_ATOM, X11WindowHints

    application = QApplication([])
    window = QWidget()
    window.show()
    changes = []
    hints = X11WindowHints(window, lambda: changes.append(hints.frame_margins()))
    assert hints.frame_margins() is None
    manager = xdisplay.Display()
    try:
        target = manager.create_resource_object("window", int(window.winId()))
        target.change_property(manager.intern_atom(NET_FRAME_EXTENTS_ATOM), Xatom.CARDINAL, 32, [1, 2, 3, 4])
        manager.flush()
        for _ in range(50):
            if changes:
                break
            QTest.qWait(20)
        assert changes == [QMargins(1, 3, 2, 4)], changes
    finally:
        hints.close()
        manager.close()
        window.close()
        application.quit()


if __name__ == "__main__":
    if sys.argv[1:] == ["--x11-check"]:
        check_frame_extents()
    else:
        unittest.main()
