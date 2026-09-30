import http.client
import socket
import struct
import threading
import unittest
from datetime import datetime
from unittest.mock import Mock, patch

from rtsp_tunnel import ImouReplayTunnel, RtspWebSocketTunnel, rewrite_rtsp_message, rtsp_message_length


class RtspTunnelTest(unittest.TestCase):
    def test_partial_messages_and_interleaved_packets_keep_their_boundaries(self):
        packet = b"$\x00\x00\x05abcde"
        self.assertIsNone(rtsp_message_length(packet[:3]))
        self.assertEqual(rtsp_message_length(packet), 9)
        response = b"RTSP/1.0 200 OK\r\nContent-Length: 5\r\n\r\nabcde"
        self.assertIsNone(rtsp_message_length(response[:10]))
        self.assertEqual(rtsp_message_length(response[:-1]), len(response))
        self.assertEqual(rtsp_message_length(response + packet), len(response))
        for length in (b"-1", b"not-a-number", b"999999999"):
            with self.assertRaises(OSError):
                rtsp_message_length(b"RTSP/1.0 200 OK\r\nContent-Length: " + length + b"\r\n\r\n")

    def test_uri_rewriting_preserves_body_lengths_and_seek_headers(self):
        upstream = b"rtsp://gateway.example.com:8556/private?digest=secret"
        local = b"rtsp://127.0.0.1:40000/recording"
        body = b"a=control:" + upstream + b"/trackID=0\r\n"
        response = b"RTSP/1.0 200 OK\r\nContent-Base: " + upstream + b"/\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n" + body
        rewritten = rewrite_rtsp_message(response, upstream, local)
        self.assertNotIn(upstream, rewritten)
        self.assertEqual(rtsp_message_length(rewritten), len(rewritten))
        request = b"PLAY " + local + b" RTSP/1.0\r\nCSeq: 1\r\nRange: npt=12-\r\nScale: 1.0\r\n\r\n"
        self.assertIn(b"Range: npt=12-", rewrite_rtsp_message(request, local, upstream))

    def test_native_recordings_use_camera_local_time_and_sdk_rate_commands(self):
        tunnel = ImouReplayTunnel("gateway.example.com:8556/private?digest=secret"
                                  "&beginTime=2026-09-30 02:00:00&endTime=2026-09-30 02:05:00",
                                  datetime(2026, 9, 30, 2), datetime(2026, 9, 30, 2, 5), "0")
        try:
            self.assertNotIn("secret", tunnel.url)
            command = tunnel._command("PLAY")
            self.assertIn(b"/vod/playback.xav?channel=1&subtype=0&starttime=2026_09_30_02_00_00", command)
            self.assertIn(b"?sourceId=private?digest=secret", command)
            self.assertNotIn(b"&beginTime", command)
            self.assertIn(b"Accept-Sdp: Private", command)
            self.assertIn(b"method=1", tunnel._command("PAUSE"))
            self.assertIn(b"Speed: 2", tunnel._command("SCALE", 2))
            self.assertIn(b"Scale: 8", tunnel._command("SCALE", 8))
            self.assertNotIn(b"Range:", tunnel._command("SCALE", 8))
            packet = b"$\x00\x00\x00\x00\x05abcde"
            self.assertIsNone(rtsp_message_length(packet[:5], 6))
            self.assertEqual(rtsp_message_length(packet, 6), len(packet))
            with self.assertRaises(OSError):
                rtsp_message_length(b"$\x00\xff\xff\xff\xff", 6)
            response = b"HTTP/1.1 200 OK\r\nPrivate-Length: 5\r\n\r\nabcde"
            self.assertEqual(rtsp_message_length(response, 6), len(response))
        finally:
            tunnel.close()

    def test_native_media_is_framed_once_and_exposed_only_on_loopback(self):
        media = b"DHAVprivate-media"
        response = b"HTTP/1.1 200 OK\r\nPrivate-Length: 5\r\n\r\nabcde"
        packet = b"$\x00" + len(media).to_bytes(4, "big") + media
        connection = Mock()
        connection.recv.side_effect = [response[:20], response[20:] + packet[:8], packet[8:],
                                       b"RTSP/1.0 500 OffLine: File Over\r\n\r\n"]
        with patch("rtsp_tunnel.websocket.create_connection", return_value=connection) as connect:
            tunnel = ImouReplayTunnel("gateway.example.com:8556/private?digest=secret",
                                      datetime(2026, 9, 30, 2), datetime(2026, 9, 30, 2, 5), "0")
            try:
                client = socket.create_connection(tunnel.listener.getsockname(), timeout=2)
                client.sendall(b"GET /recording HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.getheader("Transfer-Encoding"), "chunked")
                self.assertEqual(response.read(), media)
                client.close()
                tunnel.thread.join(timeout=2)
                self.assertFalse(tunnel.error)
                self.assertTrue(tunnel.finishing)
                connect.assert_called_once_with("wss://gateway.example.com:8556/httpprivateoverwebsocket", timeout=15)
            finally:
                tunnel.close()

    def test_repeated_audio_frames_are_removed_without_discarding_a_counter_wrap(self):
        def frame(number, timestamp):
            media = bytearray(24)
            media[:5] = b"DHAV\xf0"
            struct.pack_into("<IIIH", media, 8, number, 24, 123456789, timestamp)
            return bytes(media)

        frames = [frame(1, 65500), frame(2, 65530), frame(1, 65500), frame(2, 65530), frame(3, 25)]
        response = b"HTTP/1.1 200 OK\r\n\r\n"
        connection = Mock()
        connection.recv.side_effect = [response, *[b"$\x00" + len(media).to_bytes(4, "big") + media
                                                  for media in frames], b"OffLine:File Over"]
        with patch("rtsp_tunnel.websocket.create_connection", return_value=connection):
            tunnel = ImouReplayTunnel("gateway.example.com:8556/private", datetime(2026, 9, 30, 2),
                                      datetime(2026, 9, 30, 2, 5), "0")
            try:
                client = socket.create_connection(tunnel.listener.getsockname(), timeout=2)
                client.sendall(b"GET /recording HTTP/1.1\r\nHost: localhost\r\n\r\n")
                response = http.client.HTTPResponse(client)
                response.begin()
                self.assertEqual(response.read(), frames[0] + frames[1] + frames[4])
                client.close()
                tunnel.thread.join(timeout=2)
                self.assertFalse(tunnel.error)
            finally:
                tunnel.close()

    def test_only_a_secure_gateway_and_loopback_listener_are_used(self):
        for url in ("http://gateway.example.com", "rtsp://user:password@gateway.example.com:8556/",
                    "rtsp://gateway.example.com:8554/", "rtsp://gateway.example.com:8556/\nsecret",
                    "rtsp://gateway.example.com:invalid/", "rtsp://[invalid/"):
            with self.assertRaises(OSError):
                RtspWebSocketTunnel(url)
        with patch("rtsp_tunnel.websocket.create_connection", side_effect=OSError("connection refused")) as connect:
            tunnel = RtspWebSocketTunnel("rtsp://gateway.example.com:8556/private?digest=secret")
            try:
                self.assertNotIn("secret", tunnel.url)
                client = socket.create_connection(tunnel.listener.getsockname(), timeout=1)
                tunnel.thread.join(timeout=2)
                client.close()
                self.assertIn("connection failed", tunnel.error)
                connect.assert_called_once_with("wss://gateway.example.com:8556", timeout=15)
                self.assertEqual(tunnel.listener.getsockname() if tunnel.listener.fileno() >= 0 else None, None)
            finally:
                tunnel.close()
        self.assertFalse(tunnel.thread.is_alive())
        self.assertEqual(tunnel.upstream, b"")

    def test_binary_recording_end_is_eof_and_camera_offline_is_an_error(self):
        for notification in (b"OffLine:File Over", "OffLine:FileOver", b"OffLine:Device Offline",
                             b"RTSP/1.0 500 OffLine: File Over\r\n\r\n",
                             b"HTTP/1.1 200 OK\r\nis_session_end: true\r\n\r\n"):
            with self.subTest(notification=notification):
                tunnel = RtspWebSocketTunnel.__new__(RtspWebSocketTunnel)
                tunnel.stopped = threading.Event()
                tunnel.finishing = False
                tunnel.error = ""
                tunnel.connection = Mock()
                tunnel.connection.recv.return_value = notification
                tunnel.client = Mock()
                tunnel.listener = Mock()
                tunnel._receive()
                self.assertTrue(tunnel.stopped.is_set())
                self.assertEqual(bool(tunnel.error), notification == b"OffLine:Device Offline")
                tunnel.client.sendall.assert_not_called()
