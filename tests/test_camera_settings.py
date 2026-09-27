import struct
import unittest

from okam_native.cs2 import CS2Timeout

from app import (
    TRANSPARENT_RESPONSE_COMMAND,
    WHITE_LIGHT_COMMAND,
    WHITE_LIGHT_STATUS_COMMAND,
    available_qualities,
    read_response_fields,
    video_quality_path,
    white_light_path,
)


LIGHT_STATUS_RESPONSE = (
    b'result= 0;\r\nvar result="ok";\r\nvar cmd=2109;\r\nvar command=2;\r\n'
    b"var lightStatus=0;\r\nvar sirenStatus=0;\r\nvar alarmLedStatus=0;\r\n"
)


def command_packet(command: int, payload: bytes) -> list[bytes]:
    return [struct.pack("<HHHH", 0x0A01, command, len(payload), 0), payload]


class CommandSession:
    def __init__(self, packets: list[bytes]) -> None:
        self.packets = packets

    def read_exact(self, channel: int, size: int, *, timeout: float) -> bytes:
        if not self.packets:
            raise CS2Timeout("no packet")
        packet = self.packets.pop(0)
        if len(packet) != size:
            raise AssertionError(f"expected {size} bytes, got {len(packet)}")
        return packet

    def _count_kind(self, prefix: str, kind: str) -> None:
        pass


class WhiteLightTest(unittest.TestCase):
    def test_white_light_requests_only_switch_the_light(self) -> None:
        self.assertEqual(white_light_path(True), "trans_cmd_string.cgi?cmd=2109&command=0&light=1&")
        self.assertEqual(white_light_path(False), "trans_cmd_string.cgi?cmd=2109&command=0&light=0&")
        for path in (white_light_path(True), white_light_path(False)):
            self.assertNotIn("siren", path)
            self.assertNotIn("alarm", path.lower())
            self.assertNotIn("2108", path)

    def test_status_with_duplicate_result_fields_is_accepted(self) -> None:
        session = CommandSession(
            command_packet(0x6019, b"result=0;")
            + command_packet(TRANSPARENT_RESPONSE_COMMAND, LIGHT_STATUS_RESPONSE)
        )
        fields = read_response_fields(
            session,
            TRANSPARENT_RESPONSE_COMMAND,
            {"cmd": WHITE_LIGHT_COMMAND, "command": WHITE_LIGHT_STATUS_COMMAND},
            1,
        )
        self.assertIsNotNone(fields)
        self.assertEqual(fields["lightStatus"], "0")

    def test_rejected_status_is_ignored(self) -> None:
        session = CommandSession(
            command_packet(TRANSPARENT_RESPONSE_COMMAND, b"result=-1;\r\nvar cmd=2109;\r\nvar command=2;\r\n")
        )
        self.assertIsNone(
            read_response_fields(
                session,
                TRANSPARENT_RESPONSE_COMMAND,
                {"cmd": WHITE_LIGHT_COMMAND, "command": WHITE_LIGHT_STATUS_COMMAND},
                1,
            )
        )


class VideoQualityTest(unittest.TestCase):
    def test_quality_is_hidden_when_switching_back_needs_a_restart(self) -> None:
        self.assertEqual(available_qualities({"pixel": "300"}), [])
        self.assertEqual(available_qualities({"pixel": "200", "support_pixel_shift": "1"}), [])
        self.assertEqual(available_qualities({}), [])

    def test_super_hd_requires_pixel_shift_support(self) -> None:
        self.assertEqual(available_qualities({"pixel": "100"}), ["HD", "SD", "Low"])
        self.assertEqual(
            available_qualities({"pixel": "100", "support_pixel_shift": "1"}),
            ["Super HD", "HD", "SD", "Low"],
        )

    def test_quality_request_uses_sdk_values(self) -> None:
        self.assertEqual(video_quality_path("HD"), "camera_control.cgi?param=16&value=1&")
        self.assertEqual(video_quality_path("Super HD"), "camera_control.cgi?param=16&value=100&")


if __name__ == "__main__":
    unittest.main()
