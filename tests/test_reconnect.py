import os
import unittest
from types import SimpleNamespace

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

    def test_live_button_returns_to_live_video(self) -> None:
        stopped: list[bool] = []
        reconnected: list[bool] = []
        self.window.show()
        self.window.replay = SimpleNamespace(stop=lambda: stopped.append(True))
        self.window.reconnect = lambda: reconnected.append(True)
        self.window.live_button.click()
        self.assertEqual(stopped, [True])
        self.assertEqual(reconnected, [True])
        self.assertIsNone(self.window.replay)

    def test_drag_pans_zoomed_video_instead_of_moving_camera(self) -> None:
        moves: list[tuple[str, ...]] = []
        commands: list[list[object]] = []
        self.window.control_camera = moves.append
        self.window._mpv_command = lambda command: commands.append(command) or True
        self.window.stream_live = True
        self.window.zoom_level = 2
        self.window.video.resize(1000, 500)
        self.window.pan_zoomed_video(100, 0)
        self.window.move_by_drag(100, 0)
        self.assertEqual(moves, [])
        self.assertAlmostEqual(self.window.video_pan[0], 0.05)
        self.window.pan_zoomed_video(10000, 0)
        self.assertAlmostEqual(self.window.video_pan[0], 0.25)
        self.assertEqual(commands[-2], ["set_property", "video-pan-x", 0.25])

    def test_wheel_zoom_keeps_the_point_under_the_cursor(self) -> None:
        self.window._mpv_command = lambda command: True
        self.window.stream_live = True
        self.window.video.resize(1000, 500)
        self.window.change_zoom(2, 750, 250)
        self.assertEqual(self.window.zoom_level, 2)
        self.assertAlmostEqual(self.window.video_pan[0], -0.125)
        self.window.change_zoom(-2, 750, 250)
        self.assertEqual(self.window.video_pan, (0.0, 0.0))

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
