import struct
import unittest

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

    def test_ptz_and_light_commands_defined(self) -> None:
        self.assertEqual(set(v380.PTZ_COMMANDS), {"Up", "Down", "Left", "Right"})
        self.assertEqual(set(v380.LIGHT_COMMANDS), {"on", "off", "auto"})
        for command in (*v380.PTZ_COMMANDS.values(), *v380.LIGHT_COMMANDS.values(), v380.PTZ_STOP):
            self.assertEqual(len(command), 16)


if __name__ == "__main__":
    unittest.main()
