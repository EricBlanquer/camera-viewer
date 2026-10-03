import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from app import LiveCatchUp, LiveLatencyGuard, RtspCamera, RtspStreamWorker


class LiveLatencyGuardTest(unittest.TestCase):
    def test_real_time_playback_accepts_arbitrary_timestamp_origins(self):
        for origin in (0, -200, 100000):
            guard = LiveLatencyGuard()
            for elapsed in (0, 1, 60, 3600):
                guard.observe(origin + elapsed, 100 + elapsed)

    def test_sustained_delay_triggers_at_confirmation_boundary(self):
        guard = LiveLatencyGuard()
        guard.observe(0, 100)
        guard.observe(1, 106.99)
        guard.observe(1, 107)
        guard.observe(3.99, 109.99)
        with self.assertRaisesRegex(OSError, "playback delay increased by 6.0s"):
            guard.observe(4, 110)

    def test_temporary_delay_resets_the_confirmation_period(self):
        guard = LiveLatencyGuard()
        for position, now in ((0, 100), (1, 107), (3, 108), (4, 110), (6, 112)):
            guard.observe(position, now)
        with self.assertRaises(OSError):
            guard.observe(7, 113)

    def test_catching_up_updates_the_reference_position(self):
        guard = LiveLatencyGuard()
        guard.observe(0, 100)
        guard.observe(20, 110)
        guard.observe(21, 117)
        with self.assertRaises(OSError):
            guard.observe(24, 120)

    def test_unavailable_or_invalid_positions_interrupt_confirmation(self):
        for missing in (None, "unknown", True, float("nan"), float("inf")):
            guard = LiveLatencyGuard()
            guard.observe(0, 100)
            guard.observe(1, 107)
            guard.observe(missing, 110)
            guard.observe(4, 111)
            guard.observe(6.9, 113.9)
            with self.assertRaises(OSError):
                guard.observe(7, 114)


class LiveCatchUpTest(unittest.TestCase):
    def test_buffered_delay_speeds_playback_after_the_observation_period(self):
        catch_up = LiveCatchUp()
        self.assertEqual([catch_up.speed(3.0, now) for now in (100, 102, 104.9)], [1.0, 1.0, 1.0])
        self.assertEqual(catch_up.speed(3.0, 105), 1.5)
        self.assertEqual(catch_up.speed(0.9, 106), 1.15)
        self.assertEqual(catch_up.speed(0.36, 107), 1.05)
        self.assertEqual(catch_up.speed(0.35, 108), 1.0)

    def test_recent_low_buffer_keeps_normal_speed_until_it_leaves_the_window(self):
        catch_up = LiveCatchUp()
        catch_up.speed(0.1, 100)
        for now in range(101, 113):
            self.assertEqual(catch_up.speed(2.0, now), 1.0)
        self.assertEqual(catch_up.speed(2.0, 112.1), 1.45)

    def test_unavailable_or_invalid_buffer_restarts_the_observation(self):
        for missing in (None, "unknown", True, float("nan"), float("inf")):
            catch_up = LiveCatchUp()
            catch_up.speed(3.0, 100)
            self.assertEqual(catch_up.speed(3.0, 105), 1.5)
            self.assertEqual(catch_up.speed(missing, 106), 1.0)
            self.assertEqual(catch_up.speed(3.0, 107), 1.0)
            self.assertEqual(catch_up.speed(3.0, 111.9), 1.0)
            self.assertEqual(catch_up.speed(3.0, 112), 1.5)


class StreamClock:
    def __init__(self):
        self.now = 100.0

    def is_set(self):
        return self.now >= 120

    def wait(self, timeout):
        self.now += timeout


class LiveLatencyWorkerTest(unittest.TestCase):
    def run_worker(self, properties):
        clock = StreamClock()
        worker = RtspStreamWorker(
            RtspCamera("test", "Entrance", "rtsp://example.invalid/live"),
            SimpleNamespace(poll=lambda: None), Path("/unused"),
        )
        worker.stop_requested = clock
        failures = []
        worker.failed.connect(failures.append)
        speeds = []

        def request(path, command):
            if command[:2] == ["set_property", "speed"]:
                speeds.append(command[2])
                return True, None
            if command[0] == "get_property" and command[1] in properties:
                return True, properties[command[1]](clock.now)
            return False, None

        with patch("app.get_bridge", return_value=None), \
                patch("app.close_bridge") as close_bridge, \
                patch("app.mpv_request", side_effect=request), \
                patch("app.icam365_light_request", return_value=False), \
                patch("app.icam365_ptz_request", return_value=False), \
                patch("app.prune_continuous_recordings"), \
                patch("app.continuous_directory", return_value=Path("/unused")), \
                patch("app.time.monotonic", side_effect=lambda: clock.now), \
                patch.object(worker, "_stop_recording") as stop_recording:
            worker.run()
        return failures, speeds, close_bridge, stop_recording

    def test_advancing_but_delayed_video_reconnects_and_closes_transport(self):
        failures, _, close_bridge, stop_recording = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: (now - 100) / 2,
        })
        self.assertEqual(len(failures), 1)
        self.assertIn("playback delay", failures[0])
        close_bridge.assert_called_once_with("test")
        stop_recording.assert_called_once_with()

    def test_buffered_live_video_plays_faster_until_it_reaches_live(self):
        failures, speeds, _, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: now - 100,
            "demuxer-cache-duration": lambda now: 2.0 if now < 110 else 0.2,
        })
        self.assertEqual(failures, [])
        self.assertEqual(speeds, [1.45, 1.0])


if __name__ == "__main__":
    unittest.main()
