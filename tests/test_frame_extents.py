import os
import shutil
import subprocess
import sys
import unittest
from pathlib import Path


class FrameExtentsTest(unittest.TestCase):
    @unittest.skipUnless(shutil.which("xvfb-run"), "Window manager frame verification requires Xvfb")
    def test_window_manager_frame_state_and_work_areas_are_followed(self):
        environment = dict(os.environ, QT_QPA_PLATFORM="xcb")
        environment["PYTHONPATH"] = str(Path(__file__).resolve().parents[1])
        completed = subprocess.run(["xvfb-run", "-a", sys.executable, __file__, "--x11-check"],
                                   env=environment, capture_output=True, text=True, timeout=25)
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)


def check_frame_extents():
    from PyQt6.QtCore import QMargins, QRect
    from PyQt6.QtTest import QTest
    from PyQt6.QtWidgets import QApplication, QWidget
    from Xlib import Xatom, Xutil, display as xdisplay
    from app import (GTK_WORKAREAS_ATOM_PREFIX, NET_CURRENT_DESKTOP_ATOM, NET_FRAME_EXTENTS_ATOM, NET_WM_STATE_ATOM,
                     NET_WM_STATE_MAXIMIZED_VERT_ATOM, X11WindowHints)

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

        def wait_for(condition):
            for _ in range(50):
                if condition():
                    return
                QTest.qWait(20)
            raise AssertionError("condition not reached")

        def aspect_hinted():
            normal = target.get_wm_normal_hints()
            return normal is not None and bool(normal.flags & Xutil.PAspect)

        assert aspect_hinted()
        state = manager.intern_atom(NET_WM_STATE_ATOM)
        target.change_property(state, Xatom.ATOM, 32, [manager.intern_atom(NET_WM_STATE_MAXIMIZED_VERT_ATOM)])
        manager.flush()
        wait_for(lambda: not aspect_hinted())
        assert len(changes) == 2, changes
        target.change_property(state, Xatom.ATOM, 32, [])
        manager.flush()
        wait_for(aspect_hinted)

        root = manager.screen().root
        root.change_property(manager.intern_atom(NET_CURRENT_DESKTOP_ATOM), Xatom.CARDINAL, 32, [1])
        root.change_property(manager.intern_atom(f"{GTK_WORKAREAS_ATOM_PREFIX}1"), Xatom.CARDINAL, 32,
                             [0, 0, 2560, 1440, 324, 1440, 1920, 1052])
        manager.flush()
        assert hints.work_areas() == [QRect(0, 0, 2560, 1440), QRect(324, 1440, 1920, 1052)], hints.work_areas()
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
