import os
import unittest

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

from app import ACCOUNT_REJECTED_MESSAGE, MainWindow


class FakePlayer:
    def __init__(self) -> None:
        self.stdin = None
        self.terminated = False

    def poll(self) -> int | None:
        return 0 if self.terminated else None

    def terminate(self) -> None:
        self.terminated = True

    def wait(self, timeout: float) -> int:
        return 0


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
        self.window.stop_stream()
        self.assertFalse(self.window.reconnect_timer.isActive())

    def test_player_keeps_last_image_while_reconnecting(self) -> None:
        player = FakePlayer()
        self.window.player = player
        self.window.on_stream_error("camera closed the native P2P session")
        self.window.on_stream_finished()
        self.assertIs(self.window.player, player)
        self.assertFalse(player.terminated)
        self.window.stop_stream()
        self.assertIsNone(self.window.player)
        self.assertTrue(player.terminated)

    def test_rejected_camera_credentials_do_not_retry(self) -> None:
        self.window.on_stream_error("The camera rejected the available credentials.")
        self.window.on_stream_finished()
        self.assertFalse(self.window.reconnect_timer.isActive())

    def test_unreachable_account_service_schedules_retry(self) -> None:
        self.window.show()
        self.window.on_account_failed("O-KAM account lookup is unreachable.")
        self.window.on_account_finished()
        self.assertTrue(self.window.reconnect_timer.isActive())
        self.assertIn("Reconnecting in 2s.", self.window.status_text)

    def test_rejected_account_login_does_not_retry(self) -> None:
        self.window.show()
        self.window.on_account_failed(ACCOUNT_REJECTED_MESSAGE)
        self.window.on_account_finished()
        self.assertFalse(self.window.reconnect_timer.isActive())

    def test_drag_moves_camera_opposite_to_image_motion(self) -> None:
        commands: list[tuple[str, ...]] = []
        self.window.control_camera = commands.append
        for dx, dy in ((90, 0), (-90, 0), (0, 90), (0, -90)):
            self.window.move_by_drag(dx, dy)
        self.assertEqual(commands, [("Left",), ("Right",), ("Up",), ("Down",)])


if __name__ == "__main__":
    unittest.main()
