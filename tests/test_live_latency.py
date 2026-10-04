import os
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from app import (
    IMOU_ACCOUNT_PROVIDER, IMOU_PROVIDER, LiveCatchUp, LiveLatencyGuard, RtspCamera, RtspStreamWorker,
)

DELAY_FAILURE = "Live playback delay increased by 9.0s. Reconnecting to live video."
STALL_FAILURE = "The RTSP video stream stopped producing frames."
URL = "rtsp://example.invalid/live"


class LiveLatencyGuardTest(unittest.TestCase):
    def test_real_time_playback_accepts_arbitrary_timestamp_origins(self):
        for origin in (0, -200, 100000):
            guard = LiveLatencyGuard()
            for elapsed in (0, 1, 60, 3600):
                self.assertIsNone(guard.observe(origin + elapsed, 100 + elapsed))

    def test_sustained_delay_triggers_at_confirmation_boundary(self):
        guard = LiveLatencyGuard()
        for position, now in ((0, 100), (1, 106.99), (1, 107), (3.99, 109.99)):
            self.assertIsNone(guard.observe(position, now))
        self.assertEqual(guard.observe(4, 110), 6)

    def test_temporary_delay_resets_the_confirmation_period(self):
        guard = LiveLatencyGuard()
        for position, now in ((0, 100), (1, 107), (3, 108), (4, 110), (6, 112)):
            self.assertIsNone(guard.observe(position, now))
        self.assertEqual(guard.observe(7, 113), 6)

    def test_catching_up_updates_the_reference_position(self):
        guard = LiveLatencyGuard()
        for position, now in ((0, 100), (20, 110), (21, 117)):
            self.assertIsNone(guard.observe(position, now))
        self.assertEqual(guard.observe(24, 120), 6)

    def test_unavailable_or_invalid_positions_interrupt_confirmation(self):
        for missing in (None, "unknown", True, float("nan"), float("inf")):
            guard = LiveLatencyGuard()
            for position, now in ((0, 100), (1, 107), (missing, 110), (4, 111), (6.9, 113.9)):
                self.assertIsNone(guard.observe(position, now))
            self.assertEqual(guard.observe(7, 114), 7)


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
    def __init__(self, end):
        self.now = 100.0
        self.end = end

    def is_set(self):
        return self.now >= self.end

    def wait(self, timeout):
        self.now += timeout


class LiveLatencyWorkerTest(unittest.TestCase):
    def run_worker(self, properties, loads=None, end=120):
        clock = StreamClock(end)
        worker = RtspStreamWorker(RtspCamera("test", "Entrance", URL), SimpleNamespace(poll=lambda: None), Path("/unused"))
        worker.stop_requested = clock
        failures = []
        worker.failed.connect(failures.append)
        speeds = []

        def request(path, command):
            if command[:2] == ["set_property", "speed"]:
                speeds.append(command[2])
                return True, None
            if command[0] == "loadfile" and loads is not None:
                loads.append((clock.now, *command[1:]))
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
        for buffered in ({}, {"demuxer-cache-duration": lambda now: 0.9}):
            loads = []
            failures, _, close_bridge, stop_recording = self.run_worker({
                "vo-configured": lambda now: True,
                "time-pos": lambda now: (now - 100) / 2,
                **buffered,
            }, loads)
            self.assertEqual(len(failures), 1)
            self.assertIn("playback delay", failures[0])
            self.assertEqual(loads, [])
            close_bridge.assert_called_once_with("test")
            stop_recording.assert_called_once_with()

    def test_delayed_video_still_arriving_reloads_only_the_player(self):
        loads = []
        failures, speeds, _, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: 5000 + now - loads[0][0] if loads else 0.5,
            "demuxer-cache-duration": lambda now: 0.2 if loads else 10.0,
        }, loads, 140)
        self.assertEqual(failures, [])
        self.assertEqual(loads, [(109.0, URL, "replace")])
        self.assertEqual(speeds, [1.5, 1.0])

    def test_reloaded_player_that_stays_frozen_reconnects(self):
        loads = []
        failures, _, close_bridge, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: 0.5,
            "demuxer-cache-duration": lambda now: 10.0,
        }, loads, 140)
        self.assertEqual(loads, [(109.0, URL, "replace")])
        self.assertEqual(failures, [DELAY_FAILURE])
        close_bridge.assert_called_once_with("test")

    def test_player_reloads_again_after_it_has_played_live_video(self):
        loads = []
        failures, _, _, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: 0.5 if not loads else min(now, loads[-1][0] + 2) + 5000 * len(loads),
            "demuxer-cache-duration": lambda now: 10.0,
        }, loads, 135)
        self.assertEqual(failures, [])
        self.assertEqual([moment for moment, _, _ in loads], [109.0, 120.0, 131.0])

    def test_reloaded_player_without_a_new_position_reconnects(self):
        loads = []
        failures, _, _, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: None if loads else 0.5,
            "demuxer-cache-duration": lambda now: 10.0,
        }, loads, 140)
        self.assertEqual(loads, [(109.0, URL, "replace")])
        self.assertEqual(failures, [STALL_FAILURE])

    def test_frozen_player_without_buffered_video_reconnects(self):
        loads = []
        failures, _, _, _ = self.run_worker({
            "vo-configured": lambda now: True,
            "time-pos": lambda now: 0.5,
            "demuxer-cache-duration": lambda now: 0.0,
        }, loads)
        self.assertEqual(loads, [])
        self.assertEqual(failures, [DELAY_FAILURE])

    def test_player_reload_needs_a_stream_shared_with_the_recorder_or_a_direct_camera_address(self):
        direct = RtspStreamWorker(RtspCamera("test", "Entrance", URL), SimpleNamespace(), Path("/unused"))
        self.assertEqual(direct._live_url(), URL)
        direct.native_bridge = SimpleNamespace(url="http://127.0.0.1:1/native")
        self.assertEqual(direct._live_url(), "http://127.0.0.1:1/native")
        local = RtspStreamWorker(
            RtspCamera("imou", "Kitchen", URL, provider=IMOU_ACCOUNT_PROVIDER, local_connection=object()),
            SimpleNamespace(), Path("/unused"),
        )
        local.imou_stream_url = "http://127.0.0.1:2/local"
        self.assertEqual(local._live_url(), "http://127.0.0.1:2/local")
        cloud = RtspStreamWorker(RtspCamera("imou", "Kitchen", URL, provider=IMOU_ACCOUNT_PROVIDER), SimpleNamespace(), Path("/unused"))
        cloud.imou_stream_url = "rtsp://127.0.0.1:3/cloud"
        self.assertIsNone(cloud._live_url())
        self.assertIsNone(RtspStreamWorker(RtspCamera("manual", "Garage", URL, provider=IMOU_PROVIDER), SimpleNamespace(), Path("/unused"))._live_url())
        with patch("app.mpv_request", return_value=(True, None)) as request:
            for buffered in (None, True, "10", 0.99):
                self.assertFalse(direct._reload_player(buffered))
            request.assert_not_called()
            self.assertTrue(direct._reload_player(1))
            request.assert_called_once_with(Path("/unused"), ["loadfile", "http://127.0.0.1:1/native", "replace"])
            self.assertFalse(cloud._reload_player(10.0))

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
