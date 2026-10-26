import hashlib
import json
import os
import struct
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings, QTime
from PyQt6.QtWidgets import QApplication, QDialog

from app import (
    IMOU_PROVIDER, NIGHT_VISION_CHECK_SECONDS, NIGHT_VISION_SAME_TIME_MESSAGE, NIGHT_VISION_SETTING, MainWindow,
    NightVisionDialog, NightVisionWorker, RtspCamera, imou_account_camera, imou_rtsp_url, night_vision_forced,
    night_vision_schedule, night_vision_target,
)
from dahua import (
    DAHUA_DAY_NIGHT_CONFIG, DAHUA_REALM_REQUEST, DahuaClient, DahuaError, dahua_legacy_hash, set_forced_night_vision,
)
from imou import ImouAccount, ImouDevice
from rtsp_tunnel import LocalRtspConnection

ACCOUNT = ImouAccount("owner@example.com", "application-id")
DEVICE = ImouDevice("device-id", "Salon", "0")
CONNECTION = LocalRtspConnection("rtsp://192.168.1.244:443/cam/realmonitor?channel=1&subtype=0")
CHALLENGE = b"Realm:Login to 2EB0B884E54CBCCD\r\nRandom:1186367205\r\n\r\n"
SESSION = 1007


def login_reply(body: bytes = b"", result: bytes = b"\x00\x08", session: int = SESSION) -> bytes:
    return (b"\xb0\x00\x00\x78" + struct.pack("<I", len(body)) + result + bytes(6) + struct.pack("<I", session)
            + bytes(12) + body)


def json_reply(payload: dict, chunk_size: int | None = None) -> bytes:
    data = json.dumps(payload).encode()
    chunk_size = chunk_size or len(data)
    return b"".join(
        b"\xf6\x00\x00\x78" + struct.pack("<II", len(chunk), payload["id"]) + bytes(4) + struct.pack("<I", len(data))
        + bytes(12) + chunk
        for chunk in (data[index:index + chunk_size] for index in range(0, len(data), chunk_size))
    )


class FakeConnection:
    def __init__(self, *replies: bytes) -> None:
        self.incoming = bytearray(b"".join(replies))
        self.sent: list[bytes] = []
        self.closed = False

    def sendall(self, data: bytes) -> None:
        self.sent.append(bytes(data))

    def recv(self, size: int) -> bytes:
        chunk = bytes(self.incoming[:size])
        del self.incoming[:size]
        return chunk

    def close(self) -> None:
        self.closed = True


def md5(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest().upper()


class DahuaClientTest(unittest.TestCase):
    def test_login_answers_the_challenge_and_reads_a_chunked_configuration(self) -> None:
        table = [[{"Delay": 6, "Mode": "Brightness", "Sensitivity": 2, "Type": "Mechanism"}]]
        connection = FakeConnection(
            login_reply(CHALLENGE, b"\x01\x0e", 1), login_reply(),
            json_reply({"result": True, "params": {"table": table}, "id": 1, "session": SESSION}, 17),
            json_reply({"result": True, "params": None, "id": 2, "session": SESSION}),
        )
        connect = Mock(return_value=connection)
        with DahuaClient("192.168.1.244", "admin", "admin", connect=connect) as client:
            self.assertEqual(client.config(DAHUA_DAY_NIGHT_CONFIG), table)
        connect.assert_called_once_with(("192.168.1.244", 37777), timeout=10)
        self.assertEqual(connection.sent[0], DAHUA_REALM_REQUEST)
        random = "1186367205"
        proof = ("admin&&" + md5(f"admin:{random}:{md5('admin:Login to 2EB0B884E54CBCCD:admin')}")
                 + md5(f"admin:{random}:6QNMIQGe")).encode()
        self.assertEqual(connection.sent[1][32:], proof)
        self.assertEqual(struct.unpack("<I", connection.sent[1][4:8])[0], len(proof))
        request = json.loads(connection.sent[2][32:])
        self.assertEqual(request, {"method": "configManager.getConfig", "params": {"name": DAHUA_DAY_NIGHT_CONFIG},
                                   "id": 1, "session": SESSION})
        self.assertEqual(struct.unpack("<I", connection.sent[2][24:28])[0], SESSION)
        self.assertEqual(json.loads(connection.sent[3][32:])["method"], "global.logout")
        self.assertTrue(connection.closed)

    def test_legacy_password_hash_matches_the_known_dahua_value(self) -> None:
        self.assertEqual(dahua_legacy_hash("admin"), "6QNMIQGe")

    def test_rejected_password_is_reported_without_a_session(self) -> None:
        connection = FakeConnection(login_reply(CHALLENGE), login_reply(result=b"\x01\x00", session=0))
        with self.assertRaises(DahuaError) as raised:
            with DahuaClient("192.168.1.244", "admin", "wrong", connect=Mock(return_value=connection)):
                pass
        self.assertEqual(str(raised.exception), "The camera rejected its device password.")
        self.assertEqual(len(connection.sent), 2)
        self.assertTrue(connection.closed)

    def test_missing_challenge_stops_before_sending_a_password_proof(self) -> None:
        connection = FakeConnection(login_reply(b"Realm:\r\nRandom:\r\n\r\n"))
        with self.assertRaises(DahuaError) as raised:
            with DahuaClient("192.168.1.244", "admin", "secret", connect=Mock(return_value=connection)):
                pass
        self.assertEqual(connection.sent, [DAHUA_REALM_REQUEST])

    def test_unreachable_camera_is_reported_as_a_camera_error(self) -> None:
        with self.assertRaises(DahuaError) as raised:
            with DahuaClient("192.168.1.244", "admin", "secret", connect=Mock(side_effect=TimeoutError())):
                pass
        self.assertEqual(str(raised.exception), "The camera configuration service is unreachable.")

    def test_interrupted_configuration_reply_is_a_camera_error(self) -> None:
        connection = FakeConnection(login_reply(CHALLENGE), login_reply())
        with DahuaClient("192.168.1.244", "admin", "secret", connect=Mock(return_value=connection)) as client:
            connection.recv = Mock(side_effect=TimeoutError())
            with self.assertRaises(DahuaError) as raised:
                client.config(DAHUA_DAY_NIGHT_CONFIG)
        self.assertEqual(str(raised.exception), "The camera configuration connection failed.")
        self.assertTrue(connection.closed)


class ForcedNightVisionTest(unittest.TestCase):
    def client(self, *modes: str) -> Mock:
        client = Mock()
        client.config.return_value = [[{"Delay": 6, "Mode": mode, "Sensitivity": 2} for mode in modes]]
        return client

    def test_forcing_sets_every_profile_of_the_channel_to_black_and_white(self) -> None:
        client = self.client("Brightness", "Color")
        self.assertTrue(set_forced_night_vision(client, 0, True))
        client.set_config.assert_called_once_with(DAHUA_DAY_NIGHT_CONFIG, [[
            {"Delay": 6, "Mode": "BlackWhite", "Sensitivity": 2}, {"Delay": 6, "Mode": "BlackWhite", "Sensitivity": 2},
        ]])

    def test_release_returns_only_black_and_white_profiles_to_automatic(self) -> None:
        client = self.client("BlackWhite", "Color")
        self.assertTrue(set_forced_night_vision(client, 0, False))
        client.set_config.assert_called_once_with(DAHUA_DAY_NIGHT_CONFIG, [[
            {"Delay": 6, "Mode": "Brightness", "Sensitivity": 2}, {"Delay": 6, "Mode": "Color", "Sensitivity": 2},
        ]])

    def test_matching_mode_is_not_written_again(self) -> None:
        for modes, forced in ((("Brightness",), False), (("BlackWhite",), True)):
            with self.subTest(modes=modes):
                client = self.client(*modes)
                self.assertFalse(set_forced_night_vision(client, 0, forced))
                client.set_config.assert_not_called()

    def test_missing_channel_is_an_error(self) -> None:
        with self.assertRaises(DahuaError):
            set_forced_night_vision(self.client("Brightness"), 1, True)


class NightVisionScheduleTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        if self.window.tray is None:
            self.window.create_tray()
        self.start = patch.object(NightVisionWorker, "start").start()
        self.clock = patch("app.QTime.currentTime", return_value=QTime(12, 0)).start()
        self.camera = replace(imou_account_camera(ACCOUNT, DEVICE), local_connection=CONNECTION)
        self.window.devices = [self.camera]
        self.setting = f"{NIGHT_VISION_SETTING}/{self.camera.uid}"

    def tearDown(self) -> None:
        self.window.night_vision_workers.clear()
        self.window.quit_requested = True
        self.window.close()
        patch.stopall()
        self.settings.sync()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def apply_at(self, value: str) -> None:
        self.clock.return_value = QTime.fromString(value, "HH:mm")
        self.window.apply_night_vision()

    def finish(self, forced: bool, error: str = "") -> None:
        self.window.on_night_vision_completed(self.camera.uid, forced, not error, error)
        self.window.on_night_vision_finished(self.camera.uid)

    def test_window_crossing_midnight_and_window_inside_one_day(self) -> None:
        evening = QTime(18, 0), QTime(8, 0)
        for value, expected in (("17:59", False), ("18:00", True), ("23:30", True), ("00:00", True),
                                ("07:59", True), ("08:00", False), ("12:00", False)):
            with self.subTest(value=value):
                self.assertEqual(night_vision_forced(evening, QTime.fromString(value, "HH:mm")), expected)
        day = QTime(9, 0), QTime(17, 0)
        for value, expected in (("08:59", False), ("09:00", True), ("16:59", True), ("17:00", False)):
            with self.subTest(value=value):
                self.assertEqual(night_vision_forced(day, QTime.fromString(value, "HH:mm")), expected)

    def test_only_valid_distinct_times_form_a_schedule(self) -> None:
        for value, expected in (("18:00-08:00", (QTime(18, 0), QTime(8, 0))), ("", None), ("18:00-18:00", None),
                                ("18:00", None), ("25:00-08:00", None)):
            with self.subTest(value=value):
                self.settings.setValue(self.setting, value)
                self.assertEqual(night_vision_schedule(self.settings, self.camera.uid), expected)

    def test_only_imou_cameras_with_local_address_are_scheduled(self) -> None:
        local = RtspCamera("rtsp:garage", "Garage", imou_rtsp_url("192.168.1.108", 554, 2), "tcp", IMOU_PROVIDER, "admin")
        self.assertEqual(night_vision_target(local), ("192.168.1.108", 1, "admin"))
        self.assertEqual(night_vision_target(self.camera), ("192.168.1.244", 0, "admin"))
        self.assertIsNone(night_vision_target(imou_account_camera(ACCOUNT, DEVICE)))
        self.assertIsNone(night_vision_target(RtspCamera("rtsp:door", "Door", "rtsp://192.168.1.20/stream")))
        self.assertIsNone(night_vision_target(SimpleNamespace(uid="garden", name="Jardin")))
        self.window.devices = [local, self.camera, imou_account_camera(ACCOUNT, ImouDevice("other", "Cuisine", "0"))]
        self.settings.setValue(self.setting, "18:00-08:00")
        self.window.update_night_vision_menu()
        self.assertEqual([action.text() for action in self.window.night_vision_menu.actions()],
                         ["Garage...", "Salon · 18:00 to 08:00..."])

    def test_schedule_forces_black_and_white_at_night_and_releases_it_in_the_morning(self) -> None:
        self.settings.setValue(self.setting, "18:00-08:00")
        self.apply_at("17:59")
        worker = self.window.night_vision_workers[self.camera.uid]
        self.assertFalse(worker.forced)
        self.assertEqual(worker.target, ("192.168.1.244", 0, "admin"))
        self.finish(False)
        self.apply_at("17:59")
        self.assertEqual(self.window.night_vision_workers, {})
        self.apply_at("18:00")
        self.assertTrue(self.window.night_vision_workers[self.camera.uid].forced)
        self.finish(True)
        self.apply_at("23:00")
        self.assertEqual(self.window.night_vision_workers, {})
        self.apply_at("08:00")
        self.assertFalse(self.window.night_vision_workers[self.camera.uid].forced)
        self.assertEqual(self.start.call_count, 3)

    def test_schedule_is_verified_again_after_the_check_interval(self) -> None:
        self.settings.setValue(self.setting, "18:00-08:00")
        with patch("app.time.monotonic", return_value=1000.0):
            self.apply_at("23:00")
            self.finish(True)
        with patch("app.time.monotonic", return_value=1000.0 + NIGHT_VISION_CHECK_SECONDS - 1):
            self.apply_at("23:14")
        self.assertEqual(self.window.night_vision_workers, {})
        with patch("app.time.monotonic", return_value=1000.0 + NIGHT_VISION_CHECK_SECONDS):
            self.apply_at("23:15")
        self.assertTrue(self.window.night_vision_workers[self.camera.uid].forced)

    def test_failure_is_reported_once_and_retried_after_the_check_interval(self) -> None:
        self.settings.setValue(self.setting, "18:00-08:00")
        with patch.object(self.window, "show_notice") as notice:
            with patch("app.time.monotonic", return_value=1000.0):
                self.apply_at("23:00")
                self.finish(True, "The camera configuration service is unreachable.")
                self.apply_at("23:01")
            self.assertEqual(self.window.night_vision_workers, {})
            with patch("app.time.monotonic", return_value=1000.0 + NIGHT_VISION_CHECK_SECONDS):
                self.apply_at("23:15")
                self.finish(True, "The camera configuration service is unreachable.")
        notice.assert_called_once_with("Salon: The camera configuration service is unreachable.")
        self.assertEqual(self.start.call_count, 2)

    def test_disabling_the_schedule_returns_the_camera_to_automatic_once(self) -> None:
        self.settings.setValue(self.setting, "18:00-08:00")

        def disable(dialog: NightVisionDialog) -> int:
            dialog.enabled.setChecked(False)
            return QDialog.DialogCode.Accepted

        self.clock.return_value = QTime(23, 0)
        with patch.object(self.window, "exec_camera_dialog", side_effect=disable):
            self.window.configure_night_vision(self.camera)
        self.assertFalse(self.settings.contains(self.setting))
        self.assertFalse(self.window.night_vision_workers[self.camera.uid].forced)
        self.finish(False)
        self.apply_at("23:30")
        self.assertEqual(self.window.night_vision_workers, {})
        self.assertEqual(self.start.call_count, 1)

    def test_saved_schedule_is_stored_and_applied_at_once(self) -> None:
        def schedule(dialog: NightVisionDialog) -> int:
            dialog.enabled.setChecked(True)
            dialog.start.setTime(QTime(19, 30))
            dialog.end.setTime(QTime(7, 0))
            return QDialog.DialogCode.Accepted

        self.clock.return_value = QTime(20, 0)
        with patch.object(self.window, "exec_camera_dialog", side_effect=schedule):
            self.window.configure_night_vision(self.camera)
        self.assertEqual(self.settings.value(self.setting), "19:30-07:00")
        self.assertTrue(self.window.night_vision_workers[self.camera.uid].forced)

    def test_dialog_requires_different_times_and_disables_them_when_unchecked(self) -> None:
        dialog = NightVisionDialog(self.window, "Salon", None)
        self.assertFalse(dialog.enabled.isChecked())
        self.assertFalse(dialog.start.isEnabled())
        self.assertIsNone(dialog.schedule())
        dialog.enabled.setChecked(True)
        self.assertEqual(dialog.schedule(), (QTime(18, 0), QTime(8, 0)))
        dialog.end.setTime(QTime(18, 0))
        dialog.submit()
        self.assertEqual(dialog.message.text(), NIGHT_VISION_SAME_TIME_MESSAGE)
        self.assertNotEqual(dialog.result(), QDialog.DialogCode.Accepted)
        dialog.enabled.setChecked(False)
        self.assertEqual(dialog.message.text(), "")
        dialog.deleteLater()

    def test_closing_waits_for_a_running_night_vision_change(self) -> None:
        self.settings.setValue(self.setting, "18:00-08:00")
        self.apply_at("23:00")
        self.window.quit_requested = True
        self.window.close()
        self.assertTrue(self.window.close_pending)
        self.window.on_night_vision_completed(self.camera.uid, True, True, "")
        with patch.object(self.window, "close") as close:
            self.window.on_night_vision_finished(self.camera.uid)
        close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
