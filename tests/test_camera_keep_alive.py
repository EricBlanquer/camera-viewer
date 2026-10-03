import io
import os
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from okam_native.cs2 import LOGIN_RESPONSE_COMMAND

from app import CAMERA_STATUS_PATH, StreamWorker


class CameraKeepAliveTest(unittest.TestCase):
    def test_live_session_requests_camera_status_before_the_camera_closes_it(self) -> None:
        clock = SimpleNamespace(now=100.0)
        worker = StreamWorker(SimpleNamespace(uid="uid", name="Garden", device_password=""), "", io.BytesIO())
        requests: list[str] = []
        responses: list[int] = []

        def read(_reader: object, _session: object) -> tuple[bytes, int]:
            clock.now += 10
            if clock.now >= 220:
                worker.stop_requested.set()
            return b"frame", 0

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
        self.assertEqual(requests, [
            "livestream.cgi?streamid=10&substream=2&",
            CAMERA_STATUS_PATH,
            CAMERA_STATUS_PATH,
            "livestream.cgi?streamid=16&substream=0&",
        ])
        self.assertEqual(responses, [LOGIN_RESPONSE_COMMAND] * 2)


if __name__ == "__main__":
    unittest.main()
