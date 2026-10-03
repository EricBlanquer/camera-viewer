import unittest
from unittest import mock

from okam_native.cs2 import CS2Error, decrypt_packet

from app import ReliableCS2Session, open_camera_session


def data_packet(sequence: int, payload: bytes, channel: int = 4) -> bytes:
    body = b"\xd1" + bytes([channel]) + sequence.to_bytes(2, "big") + payload
    return b"\xf1\xd0" + len(body).to_bytes(2, "big") + body


class RecordingSocket:
    def __init__(self) -> None:
        self.sent: list[bytes] = []

    def sendto(self, data: bytes, address: tuple[str, int]) -> None:
        self.sent.append(data)


class RecordingSession(ReliableCS2Session):
    def __init__(self) -> None:
        self.key = "camera"
        self._socket = RecordingSocket()
        self._peer = ("192.0.2.1", 32100)
        self._incoming_sequence = [0] * 8
        self._out_of_order = [dict() for _ in range(8)]
        self._channel_buffers = [bytearray() for _ in range(8)]
        self._pending = {}
        self._counters = {}
        self._unacknowledged = {}
        self._acknowledge_at = None

    def acknowledgements(self) -> list[bytes]:
        if self._acknowledge_at is not None:
            self._send_acknowledgements()
        return [decrypt_packet(self.key, data) for data in self._socket.sent]

    def acknowledged(self) -> list[list[int]]:
        return [
            [int.from_bytes(packet[offset:offset + 2], "big") for offset in range(8, len(packet), 2)]
            for packet in self.acknowledgements()
        ]


class ReliableSessionTest(unittest.TestCase):
    def test_out_of_order_packets_are_delivered_in_sequence(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(1, b"B"))
        session._handle_data(data_packet(0, b"A"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"AB")
        self.assertEqual(session.acknowledged(), [[1, 0]])

    def test_packet_dropped_for_lack_of_room_is_not_acknowledged(self) -> None:
        session = RecordingSession()
        with mock.patch("app.MAX_OUT_OF_ORDER_PACKETS", 2):
            session._handle_data(data_packet(1, b"B"))
            session._handle_data(data_packet(2, b"C"))
            session._handle_data(data_packet(3, b"D"))
            self.assertEqual(session.acknowledged(), [[1, 2]])
            session._handle_data(data_packet(0, b"A"))
            session._handle_data(data_packet(3, b"D"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"ABCD")
        self.assertEqual(session.acknowledged(), [[1, 2], [0, 3]])

    def test_duplicate_of_delivered_packet_is_acknowledged_again(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(0, b"A"))
        session._handle_data(data_packet(0, b"A"))
        self.assertEqual(session.acknowledged(), [[0]])
        session._handle_data(data_packet(0, b"A"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"A")
        self.assertEqual(session.acknowledged(), [[0], [0]])

    def test_acknowledgements_are_encrypted_and_grouped_by_channel(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(0, b"A"))
        session._handle_data(data_packet(1, b"B"))
        session._handle_data(data_packet(0, b"C", channel=1))
        self.assertEqual(session._socket.sent, [])
        self.assertEqual(session.acknowledgements(), [
            b"\xf1\xd1\x00\x08\xd1\x04\x00\x02\x00\x00\x00\x01",
            b"\xf1\xd1\x00\x06\xd1\x01\x00\x01\x00\x00",
        ])
        self.assertTrue(all(not data.startswith(b"\xf1") for data in session._socket.sent))

    def test_full_group_is_acknowledged_without_waiting(self) -> None:
        session = RecordingSession()
        with mock.patch("app.MAX_ACKNOWLEDGED_PACKETS", 2):
            session._handle_data(data_packet(0, b"A"))
            self.assertEqual(session._socket.sent, [])
            session._handle_data(data_packet(1, b"B"))
        self.assertEqual(len(session._socket.sent), 1)
        self.assertEqual(session.acknowledged(), [[0, 1]])

    def test_waiting_acknowledgements_are_sent_when_the_delay_ends_without_new_packets(self) -> None:
        session = RecordingSession()
        with mock.patch("app.time.monotonic", return_value=100.0):
            session._handle_data(data_packet(0, b"A"))
        with mock.patch("app.time.monotonic", return_value=100.004), mock.patch.object(
            RecordingSession, "_receive_clear", return_value=None
        ), mock.patch("app.select.select", return_value=([session._socket], [], [])) as wait:
            session._pump()
            self.assertEqual(session._socket.sent, [])
            wait.return_value = ([], [], [])
            session._pump()
        self.assertEqual([call.args[0] for call in wait.call_args_list], [[session._socket]] * 2)
        self.assertAlmostEqual(wait.call_args.args[3], 0.006)
        self.assertEqual(session.acknowledged(), [[0]])

    def test_overdue_acknowledgements_are_sent_before_reading(self) -> None:
        session = RecordingSession()
        with mock.patch("app.time.monotonic", return_value=100.0):
            session._handle_data(data_packet(0, b"A"))
        with mock.patch("app.time.monotonic", return_value=100.01), mock.patch.object(
            RecordingSession, "_receive_clear", return_value=None
        ), mock.patch("app.select.select") as wait:
            session._pump()
        wait.assert_not_called()
        self.assertEqual(len(session._socket.sent), 1)

    def test_discard_skips_acknowledged_out_of_order_packets(self) -> None:
        session = RecordingSession()
        session._handle_data(data_packet(1, b"old"))
        session.discard_channel(4, 0, 0)
        session._handle_data(data_packet(2, b"new"))
        session._handle_data(data_packet(0, b"late"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"new")
        self.assertEqual(session._incoming_sequence[4], 3)

    def test_discard_advances_across_sequence_wrap(self) -> None:
        session = RecordingSession()
        session._incoming_sequence[4] = 65534
        session._handle_data(data_packet(0, b"old 0"))
        session._handle_data(data_packet(65535, b"old 65535"))
        session.discard_channel(4, 0, 0)
        session._handle_data(data_packet(1, b"new"))
        self.assertEqual(bytes(session._channel_buffers[4]), b"new")
        self.assertEqual(session._incoming_sequence[4], 2)


def remember_path(session: ReliableCS2Session, *args: object, prefer_relay: bool = True) -> None:
    session.prefer_relay = prefer_relay


class SessionPathTest(unittest.TestCase):
    def fake_connect(self, direct_works: bool):
        attempts: list[bool] = []

        def connect(session: ReliableCS2Session, *, timeout: float) -> None:
            attempts.append(session.prefer_relay)
            if not session.prefer_relay and not direct_works:
                raise CS2Error("camera did not establish a native P2P session")
            session._peer = ("192.0.2.17", 25717)

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
