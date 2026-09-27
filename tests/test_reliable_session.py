import unittest
from unittest import mock

from okam_native.cs2 import CS2Error

from app import ReliableCS2Session, open_camera_session


def data_packet(sequence: int, payload: bytes, channel: int = 4) -> bytes:
    body = b"\xd1" + bytes([channel]) + sequence.to_bytes(2, "big") + payload
    return b"\xf1\xd0" + len(body).to_bytes(2, "big") + body


class RecordingSession(ReliableCS2Session):
    def __init__(self) -> None:
        self._peer = ("192.0.2.1", 32100)
        self._incoming_sequence = [0] * 8
        self._out_of_order = [dict() for _ in range(8)]
        self._channel_buffers = [bytearray() for _ in range(8)]
        self._counters = {}
        self.acknowledged: list[int] = []

    def _send_clear(self, packet: bytes, address: tuple[str, int]) -> None:
        self.acknowledged.append(int.from_bytes(packet[8:10], "big"))


class ReliableSessionTest(unittest.TestCase):
    def test_out_of_order_packets_are_delivered_in_sequence(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(1, b"B"))
        session._handle_data(data_packet(0, b"A"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"AB")
        self.assertEqual(session.acknowledged, [1, 0])

    def test_packet_dropped_for_lack_of_room_is_not_acknowledged(self) -> None:
        session = RecordingSession()
        with mock.patch("app.MAX_OUT_OF_ORDER_PACKETS", 2):
            session._handle_data(data_packet(1, b"B"))
            session._handle_data(data_packet(2, b"C"))
            session._handle_data(data_packet(3, b"D"))
            self.assertEqual(session.acknowledged, [1, 2])
            session._handle_data(data_packet(0, b"A"))
            session._handle_data(data_packet(3, b"D"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"ABCD")
        self.assertEqual(session.acknowledged, [1, 2, 0, 3])

    def test_duplicate_of_delivered_packet_is_acknowledged_again(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(0, b"A"))
        session._handle_data(data_packet(0, b"A"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"A")
        self.assertEqual(session.acknowledged, [0, 0])


def remember_path(session: ReliableCS2Session, *args: object, prefer_relay: bool = True) -> None:
    session.prefer_relay = prefer_relay


class SessionPathTest(unittest.TestCase):
    def fake_connect(self, direct_works: bool):
        attempts: list[bool] = []

        def connect(session: ReliableCS2Session, *, timeout: float) -> None:
            attempts.append(session.prefer_relay)
            if not session.prefer_relay and not direct_works:
                raise CS2Error("camera did not establish a native P2P session")
            session._peer = ("192.168.1.17", 25717)

        return attempts, connect

    def test_direct_session_is_tried_first(self) -> None:
        attempts, connect = self.fake_connect(True)
        with mock.patch.object(ReliableCS2Session, "__init__", remember_path), mock.patch.object(
            ReliableCS2Session, "connect", connect
        ):
            session = open_camera_session("uid", "parameter")
        self.assertFalse(session.prefer_relay)
        self.assertEqual(attempts, [False])

    def test_relay_is_used_when_direct_fails(self) -> None:
        attempts, connect = self.fake_connect(False)
        with mock.patch.object(ReliableCS2Session, "__init__", remember_path), mock.patch.object(
            ReliableCS2Session, "connect", connect
        ):
            session = open_camera_session("uid", "parameter")
        self.assertTrue(session.prefer_relay)
        self.assertEqual(attempts, [False, True])


if __name__ == "__main__":
    unittest.main()
