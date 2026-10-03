import unittest

from okam_native.cs2 import CS2Error, CS2Timeout

from app import CameraFrameReader


class FakeSession:
    def __init__(self, responses: list[bytes | Exception]) -> None:
        self.responses = responses
        self.sizes: list[int] = []

    def read_exact(self, channel: int, size: int, *, timeout: float) -> bytes:
        self.sizes.append(size)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def header(frame_type: int, length: int) -> bytes:
    result = bytearray(32)
    result[:4] = b"\x55\xaa\x15\xa8"
    result[4] = frame_type
    result[16:20] = length.to_bytes(4, "little")
    return bytes(result)


class CameraFrameReaderTest(unittest.TestCase):
    def test_payload_timeout_keeps_frame_header(self) -> None:
        session = FakeSession([header(1, 4), CS2Timeout("late packet"), b"abcd"])
        reader = CameraFrameReader()
        with self.assertRaises(CS2Timeout):
            reader.read(session)
        self.assertEqual(reader.read(session), (b"abcd", 1))
        self.assertEqual(session.sizes, [32, 4, 4])

    def test_header_timeout_retries_header(self) -> None:
        session = FakeSession([CS2Timeout("late header"), header(12, 3), b"abc"])
        reader = CameraFrameReader()
        with self.assertRaises(CS2Timeout):
            reader.read(session)
        self.assertEqual(reader.read(session), (b"abc", 12))
        self.assertEqual(session.sizes, [32, 32, 3])

    def test_frame_keeps_the_camera_timestamp_of_its_header(self) -> None:
        stamped = bytearray(header(1, 2))
        stamped[6:8] = (250).to_bytes(2, "little")
        stamped[8:12] = (1791070198).to_bytes(4, "little")
        reader = CameraFrameReader()
        self.assertIsNone(reader.timestamp)
        reader.read(FakeSession([bytes(stamped), b"ab"]))
        self.assertEqual(reader.timestamp, 1791070198.25)

    def test_invalid_header_is_rejected(self) -> None:
        session = FakeSession([bytes(32)])
        with self.assertRaises(CS2Error):
            CameraFrameReader().read(session)


if __name__ == "__main__":
    unittest.main()
