import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

from app import MainWindow


class ReconnectTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.window = MainWindow()

    def tearDown(self) -> None:
        self.window.close()

    def test_transport_failure_schedules_retry_and_stop_cancels_it(self) -> None:
        self.window.on_stream_error("camera closed the native P2P session")
        self.window.on_stream_finished()
        self.assertTrue(self.window.reconnect_timer.isActive())
        self.assertTrue(self.window.stop_button.isEnabled())
        self.window.stop_stream()
        self.assertFalse(self.window.reconnect_timer.isActive())
        self.assertFalse(self.window.stop_button.isEnabled())

    def test_rejected_camera_credentials_do_not_retry(self) -> None:
        self.window.on_stream_error("The camera rejected the available credentials.")
        self.window.on_stream_finished()
        self.assertFalse(self.window.reconnect_timer.isActive())

    def test_drag_moves_camera_opposite_to_image_motion(self) -> None:
        commands: list[tuple[str, ...]] = []
        self.window.control_camera = commands.append
        for dx, dy in ((90, 0), (-90, 0), (0, 90), (0, -90)):
            self.window.move_by_drag(dx, dy)
        self.assertEqual(commands, [("Left",), ("Right",), ("Up",), ("Down",)])


if __name__ == "__main__":
    unittest.main()
