import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

from app import ContinuousRecorder, RtspCamera, camera_recordings, continuous_prefix, main, prune_continuous_recordings, recover_continuous_recordings, segment_time

from test_video_recorder import FRAME_SECONDS, encoded_frames, probe_duration


class ContinuousRecorderTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.path = Path(self.directory.name)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def wait_for_files(self, count: int) -> list[Path]:
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            files = sorted(self.path.glob("*.mkv"))
            if len(files) == count and not list(self.path.glob("*.h264")):
                return files
            time.sleep(0.1)
        self.fail(f"expected {count} recordings, found {sorted(self.path.iterdir())}")

    def test_segments_rotate_on_a_keyframe(self) -> None:
        frames = encoded_frames()
        recorder = ContinuousRecorder(self.path, "Jardin")
        with mock.patch("app.CONTINUOUS_SEGMENT_SECONDS", 1.4):
            for index, frame in enumerate(frames[:20]):
                recorder.write(frame, index % 10 == 0, index * FRAME_SECONDS)
            time.sleep(1.1)
            for index, frame in enumerate(frames[20:], start=20):
                recorder.write(frame, index % 10 == 0, index * FRAME_SECONDS)
        recorder.close()
        files = self.wait_for_files(2)
        self.assertAlmostEqual(probe_duration(files[0]), 2.0, delta=0.15)
        self.assertAlmostEqual(probe_duration(files[1]), 1.0, delta=0.15)

    def test_recordings_older_than_the_retention_are_deleted(self) -> None:
        now = datetime(2026, 9, 27, 12, 0, 0)
        old = self.path / "Jardin_20260926_115959.mkv"
        recent = self.path / "Jardin_20260926_120100.mkv"
        other = self.path / "notes.txt"
        for path in (old, recent, other):
            path.write_bytes(b"x")
        prune_continuous_recordings(self.path, now, timedelta(hours=24))
        self.assertEqual(sorted(path.name for path in self.path.iterdir()), [recent.name, other.name])

    def test_local_replay_lists_only_its_camera_within_last_day(self) -> None:
        camera = RtspCamera("rtsp:27b7804d", "Entrée", "rtsp://192.0.2.10:8001/0")
        now = datetime.now()
        recent = self.path / f"{continuous_prefix(camera)}_{now:%Y%m%d_%H%M%S}.mkv"
        old = self.path / f"{continuous_prefix(camera)}_{now - timedelta(hours=25):%Y%m%d_%H%M%S}.mkv"
        other = self.path / f"Other_27b7804d_{now:%Y%m%d_%H%M%S}.mkv"
        active = self.path / f"{continuous_prefix(camera)}_{now - timedelta(minutes=1):%Y%m%d_%H%M%S}.mkv"
        for path in (recent, old, other):
            path.write_bytes(b"video")
            os.utime(path, (time.time() - 10, time.time() - 10))
        active.write_bytes(b"incomplete video")
        self.assertIsNone(segment_time(self.path / "Entrée_27b7804d_20261340_999999.mkv"))
        with mock.patch("app.continuous_directory", return_value=self.path):
            self.assertEqual(camera_recordings(camera), [recent])
            self.assertEqual(camera_recordings(camera, {recent}), [])

    def test_interrupted_segment_is_recovered(self) -> None:
        frames = encoded_frames()
        start = (datetime.now() - timedelta(seconds=3)).replace(microsecond=0)
        raw = self.path / f"Jardin_{start:%Y%m%d_%H%M%S}.mkv.h264"
        raw.write_bytes(b"".join(frames))
        mtime = start.timestamp() + 3
        os.utime(raw, (mtime, mtime))
        recover_continuous_recordings(self.path)
        output = self.path / f"Jardin_{start:%Y%m%d_%H%M%S}.mkv"
        self.assertFalse(raw.exists())
        self.assertAlmostEqual(probe_duration(output), 3.0, delta=0.4)

    def test_second_instance_does_not_recover_recordings(self) -> None:
        with mock.patch("app.sys.argv", ["app.py"]), mock.patch("app.QApplication"), mock.patch(
            "app.configure_logging"
        ), mock.patch("app.continuous_directory", return_value=self.path) as directory, mock.patch(
            "app.QLocalSocket"
        ) as socket, mock.patch("app.threading.Thread") as thread:
            socket.return_value.waitForConnected.return_value = True
            self.assertEqual(main(), 0)
            directory.assert_not_called()
            thread.assert_not_called()

    def test_recovery_only_touches_files_present_before_live_start(self) -> None:
        frames = encoded_frames()
        start = (datetime.now() - timedelta(seconds=3)).replace(microsecond=0)
        stale = self.path / f"Stale_{start:%Y%m%d_%H%M%S}.mkv.h264"
        active = self.path / f"Active_{start:%Y%m%d_%H%M%S}.mkv.h264"
        stale.write_bytes(b"".join(frames))
        os.utime(stale, (start.timestamp() + 3, start.timestamp() + 3))
        startup_files = tuple(self.path.glob("*.mkv.h264"))
        active.write_bytes(b"".join(frames))
        recover_continuous_recordings(self.path, startup_files)
        self.assertTrue(active.exists())
        self.assertFalse(stale.exists())


if __name__ == "__main__":
    unittest.main()
