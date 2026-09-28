import json
import os
import struct
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication
from app import CameraPreview, RtspCamera, mpv_rtsp_command
from icam365 import MAX_FRAME_SIZE, MediaFrames, NativeConfig, NativeSession, OrderedChannel, load_config


class FakeTransport:
    def __init__(self):
        self.peer = SimpleNamespace(ip="127.0.0.1", port=32100)
        self.packets = []
        self.sent = []
        self.closed = False

    def _send(self, packet, peer):
        self.sent.append(packet)

    def _recv(self, timeout):
        packets, self.packets = self.packets, []
        return packets

    def _drw_packet(self, data, channel, idx):
        body = bytes([0xD1, channel]) + struct.pack(">H", idx) + data
        return b"\xf1\xd0" + struct.pack(">H", len(body)) + body

    def alive(self):
        pass

    def close(self):
        self.closed = True


class NativeTransportTest(unittest.TestCase):
    def session(self):
        transport = FakeTransport()
        decoded = SimpleNamespace(lib_ok=True, servers=("127.0.0.1",))
        with patch("icam365.decode_init_string", return_value=decoded), patch("icam365.PpppSession", return_value=transport):
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

    def test_close_stops_media_before_closing_connection(self):
        session, transport = self.session()
        session.authenticated = True
        session.receive = lambda: session.pending.clear()
        session.close()
        self.assertEqual([struct.unpack_from("<I", packet, 8)[0] for packet in transport.sent], [0x2FF, 0x301])
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
