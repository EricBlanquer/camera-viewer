import struct
import threading
import unittest
from unittest.mock import Mock, patch

import v380


class V380ProtocolTest(unittest.TestCase):
    def test_password_blob_is_random_key_plus_three_encrypted_blocks(self) -> None:
        blob = v380.password_blob("CamSecret123")
        self.assertEqual(len(blob), v380.RANDOM_KEY_LENGTH + v380.PASSWORD_LENGTH)
        self.assertNotEqual(v380.password_blob("CamSecret123")[16:], blob[16:])

    def test_media_key_layout(self) -> None:
        key = v380.media_key(5555)
        ticket, middle, tail = struct.unpack("<IQI", key)
        self.assertEqual((ticket, middle, tail), (5555, v380.MEDIA_KEY_MIDDLE, v380.MEDIA_KEY_TAIL))

    def test_decrypt_media_leaves_trailing_bytes_untouched(self) -> None:
        key = v380.media_key(1)
        payload = bytes(range(80)) + b"tail"
        decrypted = v380.decrypt_media(key, payload)
        self.assertEqual(decrypted[-4:], b"tail")
        self.assertEqual(len(decrypted), len(payload))

    def test_parameter_set_detection(self) -> None:
        vps = b"\x00\x00\x01\x40\x01"
        trail = b"\x00\x00\x01\x02\x01"
        self.assertTrue(v380._has_parameter_set(vps))
        self.assertFalse(v380._has_parameter_set(trail))


class V380ConfigTest(unittest.TestCase):
    def test_config_round_trip(self) -> None:
        config = v380.V380Config(did=99933817, address="192.168.1.200", username="admin")
        self.assertEqual(v380.V380Config.from_record(config.record()), config)

    def test_invalid_config_rejected(self) -> None:
        with self.assertRaises(OSError):
            v380.V380Config.from_record({"did": "not-a-number"})

    def test_relay_url_round_trip_and_validation(self) -> None:
        config = v380.V380Config(did=99933817, address="192.168.1.200", relay_url="rtsp://192.168.1.251:8554/maison")
        self.assertEqual(v380.V380Config.from_record(config.record()), config)
        self.assertNotIn("relay_url", v380.V380Config(did=1, address="192.168.1.200").record())
        for url in ("rtsp://8.8.8.8:8554/maison", "http://192.168.1.251/maison", "rtsp://user:pw@192.168.1.251/maison",
                    "rtsp://192.168.1.251:8554/maison?x=1", "rtsp://192.168.1.251:8554/mai son",
                    "rtsp://127.0.0.1:8554/maison", "rtsp://192.168.1.251:99999/maison", "rtsp://192.168.1.251"):
            self.assertFalse(v380.valid_relay_url(url), url)
            with self.assertRaises(OSError):
                v380.V380Config.from_record({"did": 1, "relay_url": url})

    def test_non_local_address_rejected(self) -> None:
        with self.assertRaises(OSError):
            v380.V380Config.from_record({"did": 1, "address": "8.8.8.8", "username": "admin"})
        with self.assertRaises(OSError):
            v380.V380Config.from_record({"did": 1, "address": "127.0.0.1", "username": "admin"})

    def test_local_address_check(self) -> None:
        self.assertTrue(v380._is_local_address("192.168.1.200"))
        self.assertFalse(v380._is_local_address("8.8.8.8"))
        self.assertFalse(v380._is_local_address("127.0.0.1"))
        self.assertFalse(v380._is_local_address("not-an-ip"))

    def test_ptz_and_light_commands_defined(self) -> None:
        self.assertEqual(set(v380.PTZ_COMMANDS), {"Up", "Down", "Left", "Right"})
        self.assertEqual(set(v380.LIGHT_COMMANDS), {"on", "off", "auto"})
        for command in (*v380.PTZ_COMMANDS.values(), *v380.LIGHT_COMMANDS.values(), v380.PTZ_STOP):
            self.assertEqual(len(command), 16)


class V380RelayTest(unittest.TestCase):
    def test_relay_publishes_one_cropped_stream_per_lens(self) -> None:
        command = v380.relay_command("http://127.0.0.1:1/source", "rtsp://192.168.1.251:8554/maison")
        self.assertEqual(command[command.index("-i") + 1], "http://127.0.0.1:1/source")
        self.assertEqual([part for part in command if part.startswith("rtsp://")],
                         [f"rtsp://192.168.1.251:8554/maison{lens}" for lens in (1, 2, 3)])
        graph = command[command.index("-filter_complex") + 1]
        self.assertIn("split=3", graph)
        self.assertIn("[s2]crop=iw:ih/3:0:2*ih/3,scale=640:360[l2]", graph)

    def test_relay_runs_until_the_bridge_stops(self) -> None:
        bridge = object.__new__(v380.NativeBridge)
        bridge.stopped = threading.Event()
        bridge.url = "http://127.0.0.1:1/source"
        bridge.config = v380.V380Config(did=1, address="192.168.1.200", relay_url="rtsp://192.168.1.251:8554/maison")
        started = threading.Event()
        process = Mock()
        process.poll.return_value = None
        process.terminate.side_effect = lambda: setattr(process.poll, "return_value", 0)

        def popen(command, **_kwargs):
            started.set()
            return process

        with patch.object(v380, "RELAY_START_DELAY_SECONDS", 0.01), patch.object(v380.subprocess, "Popen", popen):
            thread = threading.Thread(target=bridge._relay)
            thread.start()
            self.assertTrue(started.wait(2))
            bridge.stopped.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())
        process.terminate.assert_called_once()


class V380StallTest(unittest.TestCase):
    def test_audio_without_video_is_reported_as_a_stall(self) -> None:
        class Session:
            width = height = ticket = 0
            version = v380.ENCRYPTED_VERSION

            def __init__(self, *_args) -> None:
                pass

            def authenticate(self) -> None:
                pass

            def open_stream(self) -> None:
                pass

            def frames(self):
                for _ in range(40):
                    yield v380.AUDIO_TYPES[0], bytes(v380.MEDIA_HEADER_SIZE + 8)

            def close(self) -> None:
                pass

        bridge = object.__new__(v380.NativeBridge)
        bridge.config = v380.V380Config(did=1, address="192.168.1.200")
        bridge.password = "secret"
        bridge.main_stream = True
        bridge.stopped = threading.Event()
        bridge.error = None
        bridge.process = None
        bridge.server = Mock()
        bridge.control_requests = v380.queue.Queue()
        bridge.audio_queue = v380.queue.Queue()
        bridge.video_queue = v380.queue.Queue()
        clock = iter(float(second) for second in range(1000))
        with patch.object(v380, "V380Session", Session), patch.object(bridge, "_start_muxer", return_value=[]), \
                patch.object(v380.time, "monotonic", lambda: next(clock)):
            bridge._run()
        self.assertEqual(bridge.error, "The V380 native stream stopped producing video.")


class V380ContinuousLensTest(unittest.TestCase):
    def _bridge(self) -> v380.NativeBridge:
        bridge = object.__new__(v380.NativeBridge)
        bridge.continuous_lens_lock = threading.Lock()
        bridge.continuous_lenses = set()
        return bridge

    def test_continuous_host_is_the_lowest_registered_lens(self) -> None:
        bridge = self._bridge()
        self.assertIsNone(bridge.continuous_host_lens())
        bridge.register_continuous_lens(1)
        bridge.register_continuous_lens(2)
        self.assertEqual(bridge.continuous_host_lens(), 1)
        bridge.register_continuous_lens(0)
        self.assertEqual(bridge.continuous_host_lens(), 0)
        bridge.unregister_continuous_lens(0)
        self.assertEqual(bridge.continuous_host_lens(), 1)
        bridge.unregister_continuous_lens(1)
        bridge.unregister_continuous_lens(2)
        self.assertIsNone(bridge.continuous_host_lens())


if __name__ == "__main__":
    unittest.main()
