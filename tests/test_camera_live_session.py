import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from okam_native.cs2 import LOGIN_RESPONSE_COMMAND
from okam_native.p2p import P2PError

from app import CAMERA_STATUS_PATH, CameraFrameReader, StreamWorker


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

    def stream(self, worker: StreamWorker, clock: SimpleNamespace, read: object) -> tuple[list[str], list[int]]:
        requests: list[str] = []
        responses: list[int] = []
        with patch("app.prepare_camera_connection", return_value=("client", "service")), \
                patch("app.select_camera_password", return_value="password"), \
                patch("app.open_camera_session", return_value=Mock()), \
                patch("app.authenticate_camera", return_value=SimpleNamespace(user="admin", password="password")), \
                patch("app.make_cgi_request", side_effect=lambda path, user, password: path), \
                patch("app.write_command", side_effect=lambda session, request: requests.append(request)), \
                patch("app.read_command_result", return_value=None), \
                patch("app.read_response_fields", side_effect=lambda *arguments: responses.append(arguments[1])), \
                patch("app.inspect_h264", return_value=(True, False)), \
                patch("app.update_local_detector", return_value=None), \
                patch("app.CameraFrameReader.read", read), \
                patch("app.time.monotonic", side_effect=lambda: clock.now), \
                patch.object(worker, "_read_capabilities"):
            worker._stream()
        return requests, responses


if __name__ == "__main__":
    unittest.main()
