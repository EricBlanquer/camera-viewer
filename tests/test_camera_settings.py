import struct
import unittest
from datetime import datetime

from okam_native.cs2 import CS2Timeout

from app import (
    CARD_CHANNEL,
    CARD_END_FRAME_TYPE,
    CARD_PLAY_RESPONSE_COMMAND,
    CARD_START_FRAME_TYPE,
    RECORD_LIST_RESPONSE_COMMAND,
    TRANSPARENT_RESPONSE_COMMAND,
    WHITE_LIGHT_COMMAND,
    WHITE_LIGHT_STATUS_COMMAND,
    CardRecording,
    available_qualities,
    camera_timestamp,
    download_card_recording,
    is_detection_recording,
    list_detections,
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


class RecordingCommandSession(CommandSession):
    def __init__(self, packets: list[bytes]) -> None:
        super().__init__(packets)
        self.written: list[bytes] = []

    def write(self, channel: int, data: bytes, *, timeout: float = 0) -> None:
        self.written.append(data)


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


class DetectionListTest(unittest.TestCase):
    def test_only_alarm_recordings_are_detections(self) -> None:
        self.assertTrue(is_detection_recording("20260926143210_011.mp4"))
        self.assertTrue(is_detection_recording("20260926143210_010.mp4"))
        self.assertFalse(is_detection_recording("20260926143210_100.mp4"))
        self.assertFalse(is_detection_recording("../20260926143210_011.mp4"))

    def test_paged_listing_returns_sorted_detections(self) -> None:
        first = (
            b'result= 0;\r\nvar result="ok";\r\nvar record_filenum=3;\r\n'
            b'record_name[0]="20260926235404_100.mp4";\r\nrecord_size[0]=12;\r\n'
            b'record_name[1]="20260926143210_011.mp4";\r\nrecord_size[1]=12;\r\n'
            b"var totol_page=2;\r\nvar current_page=1;\r\n"
        )
        second = (
            b'result= 0;\r\nrecord_name[0]="20260926091500_011.mp4";\r\n'
            b"var totol_page=2;\r\nvar current_page=2;\r\n"
        )
        session = RecordingCommandSession(
            command_packet(RECORD_LIST_RESPONSE_COMMAND, first)
            + command_packet(RECORD_LIST_RESPONSE_COMMAND, second)
        )
        self.assertEqual(
            list_detections(session, "admin", "secret", "20260926"),
            ["20260926091500_011.mp4", "20260926143210_011.mp4"],
        )
        self.assertEqual(len(session.written), 1)
        self.assertIn(b"get_record_file.cgi?GetType=file&dirname=20260926&", session.written[0])


class ChannelSession:
    def __init__(self, channels: dict[int, list[bytes]]) -> None:
        self.channels = channels

    def write(self, channel: int, data: bytes, *, timeout: float = 0) -> None:
        pass

    def read_exact(self, channel: int, size: int, *, timeout: float) -> bytes:
        packets = self.channels.get(channel, [])
        if not packets:
            raise CS2Timeout("no packet")
        packet = packets.pop(0)
        if len(packet) != size:
            raise AssertionError(f"expected {size} bytes, got {len(packet)}")
        return packet

    def _count_kind(self, prefix: str, kind: str) -> None:
        pass


CLIP_START = datetime(2026, 9, 26, 23, 16, 4)


def card_frame(frame_type: int, body: bytes, moment: datetime = CLIP_START) -> list[bytes]:
    header = bytearray(32)
    header[:4] = b"\x55\xaa\x15\xa8"
    header[4] = frame_type
    header[8:12] = int(camera_timestamp(moment)).to_bytes(4, "little")
    header[16:20] = len(body).to_bytes(4, "little")
    return [bytes(header), body] if body else [bytes(header)]


class CardDownloadTest(unittest.TestCase):
    def test_download_skips_stale_frames_and_serves_requests_between_frames(self) -> None:
        stale = datetime(2026, 9, 26, 19, 3, 55)
        frames = card_frame(CARD_START_FRAME_TYPE, b"") + card_frame(1, b"old", stale)
        frames += card_frame(CARD_END_FRAME_TYPE, b"", stale)
        frames += card_frame(0, b"I") + card_frame(1, b"P") + card_frame(CARD_END_FRAME_TYPE, b"")
        session = ChannelSession(
            {0: command_packet(CARD_PLAY_RESPONSE_COMMAND, b"result= 0;\r\n"), CARD_CHANNEL: frames}
        )
        received: list[bytes] = []
        idle_calls: list[int] = []
        complete = download_card_recording(
            session,
            "admin",
            "secret",
            CardRecording("20260926231604_011.mp4", CLIP_START, 180, 100),
            lambda frame_type, timestamp, body: received.append(body),
            lambda fraction: None,
            lambda: False,
            lambda: idle_calls.append(len(received)),
        )
        self.assertTrue(complete)
        self.assertEqual(received, [b"I", b"P"])
        self.assertEqual(idle_calls, [1, 2])


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
