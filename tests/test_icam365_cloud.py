import json
import threading
import unittest
from unittest.mock import MagicMock, patch

from icam365 import NativeBridge, NativeConfig
from icam365_cloud import CLOUD_SESSION_ERROR, MAX_RESPONSE_SIZE, CloudSession, NoRedirect


class CloudCredentialTest(unittest.TestCase):
    def record(self):
        return {
            "p2p_id": "TEST-000001-ABCDE", "p2p_platform": "ppcs:AA", "password": "old-password",
            "cloud_session": {
                "origin": "https://api-we01.tange365.com", "token": "private-token", "appid": "1",
                "uuid": "camera-uuid", "query": {"platform": "android"},
            },
        }

    def response(self, uuid="camera-uuid", password="new-password"):
        return {"code": 200, "data": {"items": [{
            "uuid": uuid, "p2p_id": "TEST-000001-ABCDE", "p2p_platform": "ppcs:AA", "password": password,
        }]}}

    def opener(self, response):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
        return opener

    def test_rotated_credential_is_fetched_before_native_connection(self):
        config = NativeConfig.from_record(self.record())
        opener = self.opener(self.response())
        with patch("icam365_cloud.urllib.request.build_opener", return_value=opener):
            fresh = config.refreshed()
        self.assertEqual(fresh.password, "new-password")
        self.assertEqual(config.password, "old-password")
        self.assertIs(fresh.cloud_session, config.cloud_session)
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["uuid"], "camera-uuid")
        self.assertNotIn("private-token", request.full_url)
        self.assertNotIn("private-token", repr(fresh))

    def test_wrong_camera_expired_session_and_malformed_response_are_rejected(self):
        config = NativeConfig.from_record(self.record())
        for response in (self.response(uuid="other-camera"), {"code": 401}, {"data": []}):
            with self.subTest(response=response):
                with patch("icam365_cloud.urllib.request.build_opener", return_value=self.opener(response)):
                    with self.assertRaisesRegex(OSError, CLOUD_SESSION_ERROR):
                        config.refreshed()
        self.assertEqual(config.password, "old-password")

    def test_changed_native_id_and_invalid_password_are_rejected(self):
        config = NativeConfig.from_record(self.record())
        for field, value in (("p2p_id", "TEST-000002-ABCDE"), ("password", [])):
            response = self.response()
            response["data"]["items"][0][field] = value
            with patch("icam365_cloud.urllib.request.build_opener", return_value=self.opener(response)):
                with self.assertRaises(OSError):
                    config.refreshed()

    def test_request_failure_does_not_expose_token(self):
        config = NativeConfig.from_record(self.record())
        opener = self.opener({})
        opener.open.side_effect = OSError("private-token")
        with patch("icam365_cloud.urllib.request.build_opener", return_value=opener):
            with self.assertRaisesRegex(OSError, CLOUD_SESSION_ERROR) as raised:
                config.refreshed()
        self.assertNotIn("private-token", str(raised.exception))

    def test_large_response_and_redirect_are_rejected(self):
        config = NativeConfig.from_record(self.record())
        opener = self.opener({})
        opener.open.return_value.__enter__.return_value.read.return_value = b"x" * (MAX_RESPONSE_SIZE + 1)
        with patch("icam365_cloud.urllib.request.build_opener", return_value=opener):
            with self.assertRaises(OSError):
                config.refreshed()
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "https://other.example"))

    def test_untrusted_cloud_origin_and_query_are_rejected(self):
        for key, value in (("origin", "https://other.example"), ("query", {"uuid": "other-camera"})):
            record = self.record()
            record["cloud_session"][key] = value
            with self.assertRaises(OSError):
                NativeConfig.from_record(record)

    def test_static_config_keeps_working_without_cloud_session(self):
        record = self.record()
        del record["cloud_session"]
        config = NativeConfig.from_record(record)
        self.assertIs(config.refreshed(), config)

    def test_bridge_uses_refreshed_secret_and_cached_secret_during_cloud_outage(self):
        for unavailable in (False, True):
            with self.subTest(unavailable=unavailable):
                bridge = NativeBridge.__new__(NativeBridge)
                bridge.config = NativeConfig.from_record(self.record())
                bridge.stopped = threading.Event()
                bridge.process = None
                bridge.server = MagicMock()
                bridge.error = None
                bridge.main_stream = True
                bridge._start_muxer = MagicMock(return_value=[])
                session = MagicMock()
                session.authenticated = False
                session.receive.return_value = ([], [(0x8003, b"\0" * 4)])
                session.start_media.side_effect = lambda main_stream: bridge.stopped.set()
                opener = self.opener(self.response())
                if unavailable:
                    opener.open.side_effect = OSError("Network unavailable")
                with patch("icam365_cloud.urllib.request.build_opener", return_value=opener):
                    with patch("icam365.NativeSession", return_value=session) as native, patch("icam365.LOG"):
                        bridge._run()
                expected = "old-password" if unavailable else "new-password"
                self.assertEqual(native.call_args.args[0].password, expected)
                session.start_media.assert_called_once_with(True)
                session.close.assert_called_once()
                self.assertIsNone(bridge.error)


if __name__ == "__main__":
    unittest.main()
