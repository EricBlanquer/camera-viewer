import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import icam365
from icam365 import ACCOUNT_SECRET_CATEGORY, NativeBridge, NativeConfig, load_config, sign_in
from icam365_cloud import (
    ACCOUNT_SIGN_IN_ERROR, CLOUD_SESSION_ERROR, LOGIN_URL, MAX_RESPONSE_SIZE, AccountCredentialsRejected,
    CloudSession, NoRedirect,
)


class CloudCredentialTest(unittest.TestCase):
    def record(self):
        return {
            "p2p_id": "TEST-000001-ABCDE", "p2p_platform": "ppcs:AA", "password": "old-password",
            "cloud_session": {
                "origin": "https://api-we01.tange365.com", "token": "private-token", "appid": "1",
                "uuid": "camera-uuid", "query": {"platform": "android"},
            },
        }

    def account_record(self):
        record = self.record()
        record["cloud_session"] |= {"username": "owner@example.com", "area_code": "33"}
        return record

    def response(self, uuid="camera-uuid", password="new-password"):
        return {"code": 200, "data": {"items": [{
            "uuid": uuid, "p2p_id": "TEST-000001-ABCDE", "p2p_platform": "ppcs:AA", "password": password,
        }]}}

    def login_response(self, token="renewed-token"):
        return {"code": 200, "data": {"token": token, "email": "owner@example.com"}}

    def opener(self, response):
        opener = MagicMock()
        opener.open.return_value.__enter__.return_value.read.return_value = json.dumps(response).encode()
        return opener

    def sequence_opener(self, *responses):
        opener = MagicMock()
        replies = []
        for response in responses:
            reply = MagicMock()
            reply.__enter__.return_value.read.return_value = json.dumps(response).encode()
            replies.append(reply)
        opener.open.side_effect = replies
        return opener

    def saved_config(self, directory, record):
        path = Path(directory) / "icam365.json"
        path.write_text(json.dumps({"rtsp:entrance": record}))
        path.chmod(0o600)
        return path

    def test_rotated_credential_is_fetched_before_native_connection(self):
        config = NativeConfig.from_record(self.record())
        opener = self.opener(self.response())
        with patch("icam365_cloud.urllib.request.build_opener", return_value=opener), patch("icam365.store_config"):
            fresh = config.refreshed()
        self.assertEqual(fresh.password, "new-password")
        self.assertEqual(config.password, "old-password")
        self.assertIs(fresh.cloud_session, config.cloud_session)
        request = opener.open.call_args.args[0]
        self.assertEqual(json.loads(request.data)["uuid"], "camera-uuid")
        self.assertNotIn("private-token", request.full_url)
        self.assertNotIn("private-token", repr(fresh))

    def test_malformed_and_wrong_camera_responses_are_rejected(self):
        config = NativeConfig.from_record(self.record())
        for response in (self.response(uuid="other-camera"), {"code": 200, "data": {"items": []}}):
            with self.subTest(response=response):
                with patch("icam365_cloud.urllib.request.build_opener", return_value=self.opener(response)):
                    with self.assertRaisesRegex(OSError, CLOUD_SESSION_ERROR):
                        config.refreshed()
        self.assertEqual(config.password, "old-password")

    def test_expired_session_without_account_asks_for_a_new_sign_in(self):
        config = NativeConfig.from_record(self.record())
        for response in ({"code": 401}, {"code": 51023}):
            with self.subTest(response=response):
                with patch("icam365_cloud.urllib.request.build_opener", return_value=self.opener(response)):
                    with patch("icam365.stored_account_password") as secret:
                        with self.assertRaisesRegex(OSError, ACCOUNT_SIGN_IN_ERROR):
                            config.refreshed()
                    secret.assert_not_called()

    def test_changed_native_id_and_invalid_password_are_rejected(self):
        config = NativeConfig.from_record(self.record())
        for field, value in (("p2p_id", "TEST-000002-ABCDE"), ("password", [])):
            response = self.response()
            response["data"]["items"][0][field] = value
            with patch("icam365_cloud.urllib.request.build_opener", return_value=self.opener(response)), \
                    patch("icam365.store_config"):
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
        for key, value in (("origin", "https://other.example"), ("query", {"uuid": "other-camera"}),
                           ("username", "a b"), ("area_code", "+33")):
            record = self.account_record()
            record["cloud_session"][key] = value
            with self.subTest(key=key):
                with self.assertRaises(OSError):
                    NativeConfig.from_record(record)

    def test_static_config_keeps_working_without_cloud_session(self):
        record = self.record()
        del record["cloud_session"]
        config = NativeConfig.from_record(record)
        self.assertIs(config.refreshed(), config)

    def test_expired_session_signs_in_again_and_saves_the_renewed_connection(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_") as directory:
            path = self.saved_config(directory, self.account_record())
            with patch("icam365.CONFIG_PATH", path):
                config = load_config("rtsp:entrance")
                opener = self.sequence_opener({"code": 51023}, self.login_response(), self.response())
                with patch("icam365_cloud.urllib.request.build_opener", return_value=opener), patch(
                    "icam365.stored_account_password", return_value="account-secret"
                ) as secret, patch("icam365.LOG"):
                    fresh = config.refreshed()
                saved = load_config("rtsp:entrance")
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                saved_text = path.read_text()
        secret.assert_called_once_with("owner@example.com")
        login = opener.open.call_args_list[1].args[0]
        self.assertEqual(login.full_url, LOGIN_URL)
        self.assertEqual({key: json.loads(login.data)[key] for key in ("username", "pwd", "area_code")},
                         {"username": "owner@example.com", "pwd": "account-secret", "area_code": "33"})
        self.assertIsNone(login.get_header("Authorization"))
        self.assertEqual((fresh.password, fresh.cloud_session.token), ("new-password", "renewed-token"))
        self.assertEqual((saved.password, saved.cloud_session.token), ("new-password", "renewed-token"))
        self.assertEqual((saved.cloud_session.username, saved.cloud_session.area_code), ("owner@example.com", "33"))
        self.assertNotIn("account-secret", saved_text)

    def test_rejected_account_password_is_not_sent_again(self):
        config = NativeConfig.from_record(self.account_record())
        expired = {"code": 51023}
        opener = self.sequence_opener(expired, {"code": 51021}, expired)
        with patch("icam365_cloud.urllib.request.build_opener", return_value=opener), patch(
            "icam365.stored_account_password", return_value="rejected-secret"
        ), patch("icam365.store_config"), patch.object(icam365, "_REJECTED_SIGN_INS", set()):
            with self.assertRaises(AccountCredentialsRejected):
                config.refreshed()
            with self.assertRaisesRegex(OSError, ACCOUNT_SIGN_IN_ERROR):
                config.refreshed()
        self.assertEqual([call.args[0].full_url for call in opener.open.call_args_list].count(LOGIN_URL), 1)

    def test_sign_in_saves_the_account_after_checking_the_camera(self):
        for responses, saved in (((self.login_response(), self.response()), True),
                                 (({"code": 51021},), False),
                                 ((self.login_response(), self.response(uuid="other-camera")), False)):
            with self.subTest(saved=saved), tempfile.TemporaryDirectory(
                prefix="intraswitch_camera_viewer_"
            ) as directory:
                path = self.saved_config(directory, self.record())
                before = path.read_text()
                with patch("icam365.CONFIG_PATH", path), patch(
                    "icam365_cloud.urllib.request.build_opener", return_value=self.sequence_opener(*responses)
                ), patch("icam365.saved_camera_uid", return_value="rtsp:entrance"), patch(
                    "icam365.save_account_password", return_value=True
                ) as save:
                    if saved:
                        sign_in("Entrance", "owner@example.com", "account-secret", "33")
                        renewed = load_config("rtsp:entrance")
                    else:
                        with self.assertRaises(OSError):
                            sign_in("Entrance", "owner@example.com", "account-secret", "33")
                if saved:
                    save.assert_called_once_with("owner@example.com", "account-secret")
                    self.assertEqual((renewed.cloud_session.token, renewed.cloud_session.username),
                                     ("renewed-token", "owner@example.com"))
                    self.assertNotIn("account-secret", path.read_text())
                else:
                    save.assert_not_called()
                    self.assertEqual(path.read_text(), before)

    def test_session_record_round_trip_keeps_account_identity(self):
        session = CloudSession.from_record(self.account_record()["cloud_session"])
        self.assertEqual(CloudSession.from_record(session.record()), session)
        self.assertNotIn("username", CloudSession.from_record(self.record()["cloud_session"]).record())

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
                with patch("icam365_cloud.urllib.request.build_opener", return_value=opener), patch(
                    "icam365.store_config"
                ):
                    with patch("icam365.NativeSession", return_value=session) as native, patch("icam365.LOG"):
                        bridge._run()
                expected = "old-password" if unavailable else "new-password"
                self.assertEqual(native.call_args.args[0].password, expected)
                session.start_media.assert_called_once_with(True)
                session.close.assert_called_once()
                self.assertIsNone(bridge.error)


if __name__ == "__main__":
    unittest.main()
