import json
import subprocess
import tempfile
import unittest
from pathlib import Path

from app import VideoRecorder


ACCESS_UNIT_DELIMITER = b"\x00\x00\x00\x01\x09"
FRAME_COUNT = 30
FRAME_SECONDS = 0.1


def encoded_frames() -> list[bytes]:
    stream = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            f"testsrc=size=320x180:rate=15:duration={FRAME_COUNT / 15}",
            "-c:v",
            "libx264",
            "-bf",
            "0",
            "-g",
            "10",
            "-x264-params",
            "aud=1",
            "-f",
            "h264",
            "-",
        ],
        capture_output=True,
        check=True,
    ).stdout
    return [ACCESS_UNIT_DELIMITER + part for part in stream.split(ACCESS_UNIT_DELIMITER) if part]


def probe_duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "json", str(path)],
        capture_output=True,
        check=True,
        text=True,
    )
    return float(json.loads(result.stdout)["format"]["duration"])


class VideoRecorderTest(unittest.TestCase):
    def test_recording_starts_on_keyframe_and_keeps_real_duration(self) -> None:
        frames = encoded_frames()
        self.assertEqual(len(frames), FRAME_COUNT)
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            path = Path(directory) / "recording.mkv"
            recorder = VideoRecorder(path)
            self.assertFalse(recorder.write(frames[1], False, 0.0))
            self.assertFalse(recorder.started)
            for index, frame in enumerate(frames):
                recorder.write(frame, index % 10 == 0, index * FRAME_SECONDS)
            self.assertEqual(recorder.finish(), path)
            self.assertAlmostEqual(probe_duration(path), FRAME_COUNT * FRAME_SECONDS, delta=0.15)
            self.assertFalse(recorder.raw_path.exists())

    def test_recording_without_video_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory(prefix="intraswitch_okam_") as directory:
            recorder = VideoRecorder(Path(directory) / "recording.mkv")
            with self.assertRaises(OSError):
                recorder.finish()


if __name__ == "__main__":
    unittest.main()
