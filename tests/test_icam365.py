import json
import os
import queue
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication
from cs2pppp import PpppSession
from app import CameraPreview, RtspCamera, mpv_rtsp_command
from icam365 import MAX_FRAME_SIZE, PTZ_COMMAND, PTZ_POSITION_COMMAND, MediaFrames, NativeBridge, NativeConfig, NativeSession, OrderedChannel, decode_presets, load_config


class FakeTransport:
    def __init__(self):
        self.peer = SimpleNamespace(ip="127.0.0.1", port=32100)
        self.packets = []
        self.sent = []
        self.closed = False
        self.sock = self
        self.received_ack_counts = []
        self._via = "direct"
        self._uid = b"test-camera".ljust(20, b"\0")
        self.punch_targets = []
        self.sent_addresses = []

    def settimeout(self, timeout):
        pass

    def recvfrom(self, size):
        if not self.packets:
            raise socket.timeout()
        self.received_ack_counts.append(len(self.sent))
        return self.packets.pop(0)

    def _send(self, packet, peer):
        self.sent.append(packet)
        self.sent_addresses.append(peer)

    def _drw_packet(self, data, channel, idx):
        body = bytes([0xD1, channel]) + struct.pack(">H", idx) + data
        return b"\xf1\xd0" + struct.pack(">H", len(body)) + body

    def alive(self):
        pass

    def close(self):
        self.closed = True


class NativeTransportTest(unittest.TestCase):
    def test_saved_positions_preserve_camera_identifiers_and_coordinates(self):
        payload = struct.pack("<HBBHHfff32s", 1, 2, 0, 2, 1, 0.415686, 0.36875, 0.01, b"Lieu1")
        presets = decode_presets(payload)
        preset = presets["Preset 1"]
        self.assertEqual(preset.name, "Lieu1")
        self.assertEqual(preset.payload(), payload[8:20] + struct.pack("<ii", 0, 1))
        bridge = NativeBridge.__new__(NativeBridge)
        bridge.ptz_supported = True
        bridge.pan_supported = True
        bridge.presets = presets
        bridge.control_requests = queue.Queue(maxsize=1)
        self.assertTrue(bridge.move_camera("Preset 1"))
        self.assertEqual(bridge.control_requests.get_nowait(), (PTZ_POSITION_COMMAND, preset.payload()))
        self.assertFalse(bridge.move_camera("Preset 2"))
        self.assertTrue(bridge.move_camera("Left"))
        self.assertEqual(bridge.control_requests.get_nowait(), (PTZ_COMMAND, bytes([3, 0, 0, 0, 0, 6, 0, 0])))
        self.assertTrue(bridge.move_camera("Right"))
        self.assertFalse(bridge.move_camera("Up"))
        self.assertEqual(bridge.control_requests.get_nowait(), (PTZ_COMMAND, bytes([6, 0, 0, 0, 0, 6, 0, 0])))
        bridge.pan_supported = False
        self.assertFalse(bridge.move_camera("Left"))

    def test_presets_reject_truncated_and_unknown_payloads(self):
        for payload in (b"", struct.pack("<HBB", 1, 2, 0), struct.pack("<HBB", 0, 5, 0)):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                decode_presets(payload)
        payload = struct.pack("<HBBHH", 1, 3, 0, 4, 7)
        self.assertEqual(decode_presets(payload), {})
        payload = struct.pack("<HBBHH", 1, 3, 0, 0, 7)
        self.assertEqual(decode_presets(payload)["Preset 7"].number, 7)

    def session(self):
        transport = FakeTransport()
        decoded = SimpleNamespace(lib_ok=True, servers=("127.0.0.1",))
        with patch("icam365.decode_init_string", return_value=decoded), patch("icam365.NativePpppSession", return_value=transport):
            session = NativeSession(NativeConfig("TEST-000001-ABCDE", "AA", "test-password"))
        session.last_alive = time.monotonic()
        return session, transport

    def test_media_orders_lost_and_retransmitted_packets_once(self):
        channel = OrderedChannel()
        self.assertEqual(channel.feed(1, b"second"), b"")
        self.assertEqual(channel.feed(1, b"second"), b"")
        self.assertEqual(channel.feed(0, b"first"), b"firstsecond")
        self.assertEqual(channel.feed(0, b"first"), b"")
        self.assertEqual(channel.pending_bytes, 0)
        self.assertIsNone(channel.gap_since)

    def test_packet_sequence_wraps_and_expired_gap_reconnects(self):
        channel = OrderedChannel(65535)
        self.assertEqual(channel.feed(0, b"next"), b"")
        self.assertEqual(channel.feed(65535, b"last"), b"lastnext")
        self.assertEqual(channel.expected, 1)
        channel.feed(2, b"gap")
        channel.gap_since = time.monotonic() - 9
        with self.assertRaises(OSError):
            channel.check_timeout()

    def test_recovered_gap_starts_a_new_timeout_for_next_missing_packet(self):
        channel = OrderedChannel()
        with patch("icam365.time.monotonic", return_value=100):
            channel.feed(1, b"one")
        with patch("icam365.time.monotonic", return_value=107):
            channel.feed(3, b"three")
            self.assertEqual(channel.feed(0, b"zero"), b"zeroone")
        with patch("icam365.time.monotonic", return_value=109):
            channel.check_timeout()
        with patch("icam365.time.monotonic", return_value=116):
            with self.assertRaises(OSError):
                channel.check_timeout()

    def test_out_of_order_packets_and_duplicates_do_not_extend_a_stalled_gap(self):
        channel = OrderedChannel()
        with patch("icam365.time.monotonic", return_value=100):
            channel.feed(1, b"one")
        with patch("icam365.time.monotonic", return_value=107):
            channel.feed(2, b"two")
            channel.feed(1, b"one")
        with patch("icam365.time.monotonic", return_value=109):
            with self.assertRaises(OSError):
                channel.check_timeout()

    def test_gap_recovery_timeout_survives_sequence_wrap(self):
        channel = OrderedChannel(65535)
        with patch("icam365.time.monotonic", return_value=100):
            channel.feed(0, b"zero")
        with patch("icam365.time.monotonic", return_value=107):
            channel.feed(2, b"two")
            self.assertEqual(channel.feed(65535, b"last"), b"lastzero")
            self.assertEqual(channel.expected, 1)
        with patch("icam365.time.monotonic", return_value=109):
            channel.check_timeout()
            self.assertEqual(channel.feed(1, b"one"), b"onetwo")
        self.assertIsNone(channel.gap_since)

    def test_control_audio_and_video_keep_receiving_after_successive_gaps(self):
        for channel in (0, 1, 2):
            with self.subTest(channel=channel):
                session, transport = self.session()
                payload = struct.pack("<III", 0x8003, 4, 0) if channel == 0 else b"firstsecondthirdfourth"
                chunks = [payload[index:index + 4] for index in range(0, 12, 4)] + [payload[12:]]
                received, commands = [], []
                for timestamp, indexes in ((100, (1,)), (107, (3, 0)), (109, ()), (110, (2,))):
                    transport.packets = [
                        (transport._drw_packet(chunks[index], channel, index), ("127.0.0.1", 32100))
                        for index in indexes
                    ]
                    with patch("icam365.time.monotonic", return_value=timestamp):
                        media, replies = session.receive()
                    received.extend(data for _, data in media)
                    commands.extend(replies)
                if channel == 0:
                    self.assertEqual(commands, [(0x8003, b"\0" * 4)])
                else:
                    self.assertEqual(b"".join(received), payload)
                self.assertIsNone(session.channels[channel].gap_since)

    def test_video_handles_split_frame_and_clock_sync_header(self):
        parser = MediaFrames((80,))
        sync = b"\0" * 16
        payload = b"\0\0\0\1\x40\x01\x0c"
        frame = bytes([80, 0, 1, 0]) + b"\0" * 4 + struct.pack("<II", len(payload), 1000) + payload
        self.assertEqual(parser.feed(sync + frame[:19]), [])
        self.assertEqual(parser.feed(frame[19:]), [(80, 1, payload)])
        self.assertEqual(parser.buffer, b"")

    def test_malformed_media_is_bounded(self):
        parser = MediaFrames((138,))
        with self.assertRaises(OSError):
            parser.feed(bytes([138]) + b"\0" * 7 + struct.pack("<II", MAX_FRAME_SIZE + 1, 0))
        with self.assertRaises(OSError):
            MediaFrames((80,)).feed(bytes([78]) + b"\0" * 15)

    def test_commands_retransmit_until_transport_ack(self):
        session, transport = self.session()
        session.send(0x8002, b"test")
        packet, _, retries = session.pending[0]
        session.pending[0] = (packet, time.monotonic() - 1, retries)
        session.receive()
        self.assertEqual(transport.sent, [packet, packet])
        transport.packets = [(b"\xf1\xd1\0\x06\xd1\0\0\x01\0\0", ("127.0.0.1", 32100))]
        session.receive()
        self.assertEqual(session.pending, {})

    def test_media_ack_reordering_and_command_fragmentation(self):
        session, transport = self.session()
        command = struct.pack("<III", 0x8003, 4, 0)
        transport.packets = [
            (transport._drw_packet(command[7:], 0, 1), ("127.0.0.1", 32100)),
            (transport._drw_packet(command[:7], 0, 0), ("127.0.0.1", 32100)),
            (transport._drw_packet(b"video", 2, 0), ("127.0.0.1", 32100)),
            (transport._drw_packet(b"video", 2, 0), ("127.0.0.1", 32100)),
        ]
        media, commands = session.receive()
        self.assertEqual(media, [(2, b"video")])
        self.assertEqual(commands, [(0x8003, b"\0" * 4)])
        self.assertEqual(len(transport.sent), 4)

    def test_each_datagram_is_acknowledged_before_receiving_the_next(self):
        session, transport = self.session()
        transport.packets = [
            (transport._drw_packet(b"first", 2, 0), ("127.0.0.1", 32100)),
            (transport._drw_packet(b"second", 2, 1), ("127.0.0.1", 32100)),
        ]
        media, commands = session.receive()
        self.assertEqual(media, [(2, b"first"), (2, b"second")])
        self.assertEqual(commands, [])
        self.assertEqual(transport.received_ack_counts, [0, 1])

    def test_camera_readiness_is_acknowledged_during_connection(self):
        decoded = SimpleNamespace(lib_ok=True, servers=("127.0.0.1",))
        with patch("icam365.decode_init_string", return_value=decoded):
            session = NativeSession(NativeConfig("TEST-000001-ABCDE", "AA", "test-password"))
        transport = session.session
        transport._uid = b"test-camera".ljust(20, b"\0")
        peer = ("127.0.0.1", 32100)
        packets = [(b"\xf1\x42\0\x14" + transport._uid, peer)]
        with patch.object(PpppSession, "_recv", return_value=packets), patch.object(transport, "_send") as send:
            self.assertEqual(transport._recv(), packets)
        send.assert_called_once_with(b"\xf1\x43\0\0", peer)

    def test_repeated_punch_and_readiness_are_answered_during_stream(self):
        session, transport = self.session()
        peer = ("127.0.0.1", 32100)
        transport.packets = [
            (b"\xf1\x41\0\x14" + transport._uid, peer),
            (b"\xf1\x42\0\x14" + transport._uid, peer),
            (b"\xf1\x42\0\x14" + b"other-camera".ljust(20, b"\0"), peer),
        ]
        self.assertEqual(session.receive(), ([], []))
        self.assertEqual(transport.sent, [b"\xf1\x42\0\x14" + transport._uid, b"\xf1\x43\0\0"])

    def test_media_starts_on_the_requested_stream_and_the_bridge_changes_it_in_session(self):
        for main_stream, quality in ((True, 1), (False, 5)):
            session, transport = self.session()
            session.start_media(main_stream)
            self.assertEqual(
                [struct.unpack_from("<I", packet, 8)[0] for packet in transport.sent if packet[:2] == b"\xf1\xd0"],
                [0x8024, 0x8012, 0x1FF, 0x320, 0x300],
            )
            self.assertEqual(struct.unpack_from("<II", transport.sent[3], 16), (0, quality))
        bridge = NativeBridge.__new__(NativeBridge)
        bridge.main_stream = True
        bridge.control_requests = queue.Queue(maxsize=16)
        bridge.set_main_stream(True)
        self.assertTrue(bridge.control_requests.empty())
        bridge.set_main_stream(False)
        self.assertEqual(bridge.control_requests.get_nowait(), (0x320, struct.pack("<II", 0, 5)))
        bridge.set_main_stream(True)
        self.assertEqual(bridge.control_requests.get_nowait(), (0x320, struct.pack("<II", 0, 1)))
        for _ in range(16):
            bridge.control_requests.put_nowait((0, b""))
        bridge.set_main_stream(False)
        self.assertTrue(bridge.main_stream)

    def test_close_stops_media_before_closing_connection(self):
        session, transport = self.session()
        session.authenticated = True
        session.receive = lambda control_only=False: session.pending.clear()
        session.close()
        self.assertEqual([
            struct.unpack_from("<I", packet, 8)[0] for packet in transport.sent if packet[:2] == b"\xf1\xd0"
        ], [0x2FF, 0x301])
        self.assertTrue(transport.closed)

    def test_close_retries_lost_stop_command_after_media_timeout(self):
        session, transport = self.session()
        session.authenticated = True
        session.channels[1].feed(1, b"pending audio")
        session.channels[1].gap_since = time.monotonic() - 9
        peer = ("127.0.0.1", 32100)
        transport.packets = [(transport._drw_packet(b"later audio", 1, 2), peer)]
        send = transport._send
        video_stops = []
        def acknowledge_retry(packet, destination):
            send(packet, destination)
            if packet[:2] != b"\xf1\xd0":
                return
            command = struct.unpack_from("<I", packet, 8)[0]
            if command == 0x2FF:
                video_stops.append(packet)
                if len(video_stops) == 1:
                    return
            index = packet[6:8]
            transport.packets.append((b"\xf1\xd1\0\x06\xd1\0\0\x01" + index, peer))
        transport._send = acknowledge_retry
        session.close()
        self.assertEqual(len(video_stops), 2)
        self.assertEqual(session.pending, {})
        self.assertTrue(transport.closed)

    def test_failed_rendezvous_closes_every_punched_port(self):
        session, transport = self.session()
        transport.peer = None
        transport.punch_targets = [SimpleNamespace(ip="127.0.0.1", port=32100)]
        session.close()
        self.assertEqual(set(transport.sent_addresses), {("127.0.0.1", port) for port in range(32097, 32104)})
        self.assertTrue(all(packet == b"\xf1\xf0\0\0" for packet in transport.sent))
        self.assertTrue(transport.closed)

    def test_config_rejects_public_credentials_and_invalid_types(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_") as directory:
            path = Path(directory) / "native.json"
            path.write_text(json.dumps({"test": {"p2p_id": [], "p2p_platform": "ppcs:AA", "password": "test"}}))
            path.chmod(0o644)
            with patch("icam365.CONFIG_PATH", path), self.assertRaises(OSError):
                load_config("test")
            path.chmod(0o600)
            with patch("icam365.CONFIG_PATH", path), self.assertRaises(OSError):
                load_config("test")
            with patch("icam365.CONFIG_PATH", path):
                self.assertIsNone(load_config("missing"))

    def test_failed_player_start_releases_native_session(self):
        application = QApplication.instance() or QApplication([])
        camera = RtspCamera("rtsp:test", "Entrance", "rtsp://127.0.0.1:1/stream")
        preview = CameraPreview(camera)
        bridge = SimpleNamespace(url="http://127.0.0.1:32123/stream")
        with patch("app.get_bridge", return_value=bridge), patch("app.close_bridge") as close, patch("app.subprocess.Popen", side_effect=OSError("Unavailable")):
            preview.start()
            close.assert_called_once_with(camera.uid)
        preview.stop()
        self.assertIsNone(preview.worker)
        preview.close()

    def test_native_source_replaces_unavailable_rtsp_for_player(self):
        camera = RtspCamera("rtsp:test", "Entrance", "rtsp://127.0.0.1:1/stream")
        bridge = SimpleNamespace(url="http://127.0.0.1:32123/stream")
        with patch("app.get_bridge", return_value=bridge):
            command = mpv_rtsp_command(Path("control.sock"), 1, True, camera)
        self.assertEqual(command[-1], bridge.url)
        self.assertNotIn(camera.url, command)
        with patch("app.get_bridge", return_value=None):
            self.assertEqual(mpv_rtsp_command(Path("control.sock"), 1, True, camera)[-1], camera.url)


if __name__ == "__main__":
    unittest.main()
