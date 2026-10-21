import io
import os
import unittest
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from okam_native.cs2 import LOGIN_RESPONSE_COMMAND
from okam_native.p2p import P2PError

from app import (
    CAMERA_STATUS_PATH, GUARD_RESPONSE_COMMAND, PATROL_MANUAL_PAUSE_SECONDS, PATROL_MOTION_LIMIT_SECONDS,
    PATROL_STEP_SECONDS, PRESET_COMMANDS, PTZ_RESPONSE_COMMAND, SETTING_GUARD, CameraFrameReader, StreamWorker,
    available_presets, guard_preset, patrol_order,
)


class CameraLiveSessionTest(unittest.TestCase):
    def test_live_session_requests_camera_status_before_the_camera_closes_it(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())

        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 10
            if clock.now >= 220:
                worker.stop_requested.set()
            return b"frame", 0

        requests, responses = self.stream(worker, clock, read)
        self.assertEqual(requests, [
            "livestream.cgi?streamid=10&substream=2&",
            CAMERA_STATUS_PATH,
            CAMERA_STATUS_PATH,
            "livestream.cgi?streamid=16&substream=0&",
        ])
        self.assertEqual(responses, [LOGIN_RESPONSE_COMMAND] * 2)

    def test_live_session_ends_when_camera_frames_keep_arriving_late(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())

        def read(reader: CameraFrameReader, _session: object) -> tuple[bytes, int]:
            clock.now += 1
            reader.timestamp = 5000 + (clock.now - 100) / 2
            if clock.now >= 140:
                worker.stop_requested.set()
            return b"frame", 0

        with self.assertRaisesRegex(P2PError, "playback delay increased by 7.5s"):
            self.stream(worker, clock, read)
        self.assertEqual(clock.now, 116)

    def test_live_session_starts_on_the_requested_stream_and_changes_it_without_reconnecting(self) -> None:
        clock = SimpleNamespace(now=100.0)
        continuous = Mock()
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO(), continuous)
        worker.set_continuous(True)
        worker.set_main_stream(False)

        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 1
            if clock.now == 103:
                worker.set_main_stream(True)
            if clock.now >= 106:
                worker.stop_requested.set()
            return b"frame", 0

        requests, _ = self.stream(worker, clock, read)
        self.assertEqual(requests, [
            "livestream.cgi?streamid=10&substream=4&",
            "livestream.cgi?streamid=10&substream=2&",
            "livestream.cgi?streamid=16&substream=0&",
        ])
        self.assertEqual(continuous.write.call_count, 6)
        continuous.close.assert_called_once_with()

    def test_frames_refused_by_a_stalled_player_resume_at_the_next_keyframe(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())
        frames = [(b"first", False), (b"refused", False), (b"skipped", False), (b"key", True), (b"next", False)]
        worker.player = Mock()
        worker.player.write.side_effect = [True, False, True, True]

        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 1
            if len(frames) == 1:
                worker.stop_requested.set()
            return frames.pop(0)[0], 0

        self.stream(worker, clock, read, lambda frame: (True, frame == b"key"))
        self.assertEqual([call.args[0] for call in worker.player.write.call_args_list], [b"first", b"refused", b"key", b"next"])

    def test_player_feed_closes_when_the_session_ends(self) -> None:
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())
        worker.player = Mock()
        with patch.object(worker, "_stream", side_effect=P2PError("camera closed the native P2P session")), \
                patch("sys.stderr"):
            worker.run()
        worker.player.close.assert_called_once_with()

    def test_patrol_moves_back_and_forth_between_saved_positions(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = self.patrol_worker()
        read = self.patrol_read(worker, clock, 100 + PATROL_STEP_SECONDS * 9)
        requests, _ = self.stream(worker, clock, read, control_result=(PTZ_RESPONSE_COMMAND, 0))
        self.assertEqual(self.moves(requests), [1, 2, 3, 4, 5, 4, 3, 2, 1])

    def test_patrol_waits_after_a_manual_movement(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = self.patrol_worker()
        read = self.patrol_read(worker, clock, 100 + PATROL_MANUAL_PAUSE_SECONDS + 10,
                                {103: lambda: worker.queue_control(("Preset 5",))})
        requests, _ = self.stream(worker, clock, read, control_result=(PTZ_RESPONSE_COMMAND, 0))
        self.assertEqual(self.moves(requests), [1, 5, 2])
        self.assertEqual(worker.patrol_next_at, 103 + PATROL_MANUAL_PAUSE_SECONDS + PATROL_STEP_SECONDS)

    def test_patrol_stays_while_something_moves_in_front_of_the_camera(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = self.patrol_worker()
        detector = worker.local_detector = Mock()
        detector.moved_at_rest.side_effect = lambda since: 100 < clock.now < 125
        read = self.patrol_read(worker, clock, 100 + PATROL_STEP_SECONDS * 5)
        requests, _ = self.stream(worker, clock, read, control_result=(PTZ_RESPONSE_COMMAND, 0))
        self.assertEqual(self.moves(requests), [1, 2])
        rests = [call.args[0] for call in detector.rest.call_args_list]
        self.assertEqual(rests[:2], [None, datetime(2026, 10, 22, 0, 40)])
        self.assertEqual(rests[2], None)

    def test_patrol_resumes_when_movement_lasts_too_long(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = self.patrol_worker()
        worker.local_detector = Mock()
        worker.local_detector.moved_at_rest.side_effect = lambda since: clock.now > 100
        read = self.patrol_read(worker, clock, 100 + PATROL_STEP_SECONDS * 2 + PATROL_MOTION_LIMIT_SECONDS)
        requests, _ = self.stream(worker, clock, read, control_result=(PTZ_RESPONSE_COMMAND, 0))
        self.assertEqual(self.moves(requests), [1, 2])

    def test_guard_position_is_set_and_disabled_on_the_camera(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())
        completed: list[object] = []
        worker.setting_completed.connect(lambda name, value: completed.append((name, value)))
        values = iter(("Preset 4", None))

        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 1
            value = next(values, False)
            if value is False:
                worker.stop_requested.set()
            else:
                worker.queue_setting(SETTING_GUARD, value)
            return b"frame", 0

        requests, _ = self.stream(worker, clock, read, control_result=(GUARD_RESPONSE_COMMAND, 0))
        self.assertEqual([request for request in requests if request.startswith("set_sensor_preset")], [
            "set_sensor_preset.cgi?sensorid=255&presetid=4&",
            "set_sensor_preset.cgi?sensorid=255&presetid=255&",
        ])
        self.assertEqual(completed, [(SETTING_GUARD, "Preset 4"), (SETTING_GUARD, None)])

    def test_saved_positions_and_guard_come_from_the_camera_status(self) -> None:
        self.assertEqual(available_presets({"preset_value": "63488"}), [f"Preset {index}" for index in range(1, 6)])
        self.assertEqual(available_presets({"preset_value": str(0b1010100000000000)}), ["Preset 1", "Preset 3", "Preset 5"])
        self.assertEqual(available_presets({"preset_value": "invalid"}), [])
        self.assertEqual(patrol_order(["Preset 1", "Preset 3", "Preset 5"]), ["Preset 1", "Preset 3", "Preset 5", "Preset 3"])
        self.assertEqual(patrol_order(["Preset 2"]), ["Preset 2"])
        self.assertEqual(guard_preset({"watch_preset": "3"}), "Preset 3")
        self.assertIsNone(guard_preset({"watch_preset": "255"}))
        self.assertIsNone(guard_preset({"watch_preset": "0"}))
        self.assertIsNone(guard_preset({}))

    @staticmethod
    def patrol_worker() -> StreamWorker:
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())
        worker.patrol_presets = available_presets({"preset_value": "63488"})
        worker.set_patrol(True)
        return worker

    @staticmethod
    def patrol_read(worker: StreamWorker, clock: SimpleNamespace, stop_at: float,
                    actions: dict[float, object] | None = None) -> object:
        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 1
            (actions or {}).get(clock.now, lambda: None)()
            if clock.now >= stop_at:
                worker.stop_requested.set()
            return b"frame", 0

        return read

    @staticmethod
    def moves(requests: list[str]) -> list[int]:
        presets = {f"decoder_control.cgi?command={value}&onestep=0&": int(command.removeprefix("Preset "))
                   for command, value in PRESET_COMMANDS.items()}
        return [presets[request] for request in requests if request in presets]

    def stream(self, worker: StreamWorker, clock: SimpleNamespace, read: object,
               inspect: object = lambda frame: (True, False),
               control_result: tuple[int, int] | None = None) -> tuple[list[str], list[int]]:
        requests: list[str] = []
        responses: list[int] = []
        if not isinstance(worker.player, Mock):
            worker.player = Mock()
        with patch("app.prepare_camera_connection", return_value=("client", "service")), \
                patch("app.select_camera_password", return_value="password"), \
                patch("app.open_camera_session", return_value=Mock()), \
                patch("app.authenticate_camera", return_value=SimpleNamespace(user="admin", password="password")), \
                patch("app.make_cgi_request", side_effect=lambda path, user, password: path), \
                patch("app.write_command", side_effect=lambda session, request: requests.append(request)), \
                patch("app.read_command_result", return_value=control_result), \
                patch("app.datetime", wraps=datetime) as wall_clock, \
                patch("app.read_response_fields", side_effect=lambda *arguments: responses.append(arguments[1])), \
                patch("app.inspect_h264", side_effect=inspect), \
                patch("app.update_local_detector", return_value=None), \
                patch("app.CameraFrameReader.read", read), \
                patch("app.time.monotonic", side_effect=lambda: clock.now), \
                patch.object(worker, "_read_capabilities"):
            wall_clock.now.return_value = datetime(2026, 10, 22, 0, 40)
            worker._stream()
        return requests, responses


if __name__ == "__main__":
    unittest.main()
