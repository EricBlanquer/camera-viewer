import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

from app import (
    LOADING_DONE,
    REPLAY_DOWNLOAD_REQUEST,
    REPLAY_LIST_REQUEST,
    CardRecording,
    ReplayBuffer,
    ReplayController,
    ReplayWorker,
    TimelineWidget,
    camera_timestamp,
)


def recording(name: str) -> CardRecording:
    return CardRecording(name, datetime.strptime(name[:14], "%Y%m%d%H%M%S"), 300, 1000)


class ReplayWorkerQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.worker = ReplayWorker(SimpleNamespace(), "", Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_download_is_handed_out_once(self) -> None:
        buffer = ReplayBuffer(recording("20260926192758_100.mp4"))
        self.worker.download(buffer)
        self.assertEqual(self.worker._next_request(), (REPLAY_DOWNLOAD_REQUEST, buffer))
        self.assertTrue(self.worker.requests.empty())

    def test_listing_runs_first_and_only_latest_download_is_kept(self) -> None:
        older = ReplayBuffer(recording("20260926192758_100.mp4"))
        newer = ReplayBuffer(recording("20260926231604_011.mp4"))
        self.worker.download(older)
        self.worker.list_day("20260926")
        self.worker.download(newer)
        self.assertEqual(self.worker._next_request(), (REPLAY_LIST_REQUEST, "20260926"))
        self.assertTrue(older.ended)
        self.assertEqual(self.worker._next_request(), (REPLAY_DOWNLOAD_REQUEST, newer))
        self.assertTrue(self.worker.requests.empty())


class ReplayLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_loading_counts_up_to_the_requested_time(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            loading: list[int] = []
            controller.loading_changed.connect(loading.append)
            target = recording("20260926192758_100.mp4")
            controller.recordings["20260926"] = [target]
            controller.seek(datetime(2026, 9, 26, 19, 30, 28))
            controller.on_progress(target.name, 0.25)
            controller.on_progress(target.name, 0.6)
            controller.on_position(camera_timestamp(datetime(2026, 9, 26, 19, 30, 28)))
            self.assertEqual(loading, [0, 50, 99, LOADING_DONE])
        finally:
            controller.stop()


    def test_detection_buttons_jump_between_detections(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            controller.recordings["20260926"] = [
                recording("20260926095900_100.mp4"),
                recording("20260926100000_011.mp4"),
                recording("20260926120000_011.mp4"),
                recording("20260926140000_011.mp4"),
            ]
            controller.timeline.center = datetime(2026, 9, 26, 12, 0, 0)
            controller.jump_to_detection(1)
            self.assertEqual(controller.current.recording.name, "20260926140000_011.mp4")
            controller.timeline.center = datetime(2026, 9, 26, 12, 0, 0)
            controller.jump_to_detection(-1)
            self.assertEqual(controller.current.recording.name, "20260926100000_011.mp4")
        finally:
            controller.stop()


    def test_next_recording_is_preloaded_and_reused(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            downloads: list[str] = []
            controller.worker.download = lambda buffer: downloads.append(buffer.recording.name)
            first = recording("20260926192758_100.mp4")
            detection = recording("20260926193000_011.mp4")
            second = recording("20260926193258_100.mp4")
            controller.recordings["20260926"] = [first, detection, second]
            controller.seek(first.start)
            controller.on_downloaded(first.name, "/nonexistent/first.mkv")
            self.assertEqual(downloads, [first.name, second.name])
            controller.on_finished(first.name)
            self.assertEqual(controller.current.recording, second)
            self.assertEqual(downloads, [first.name, second.name])
        finally:
            controller.stop()


    def test_detection_inside_a_file_does_not_loop_back(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            continuous = recording("20260926042458_100.mp4")
            detection = recording("20260926042744_011.mp4")
            following = recording("20260926042958_100.mp4")
            controller.recordings["20260926"] = [continuous, detection, following]
            controller.saved_clips[continuous.name] = Path("/nonexistent/continuous.mkv")
            controller.seek(continuous.start, continuous)
            controller.on_finished(continuous.name)
            self.assertEqual(controller.current.recording, following)
        finally:
            controller.stop()

    def test_recording_cut_short_is_reloaded_from_last_position(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            downloads: list[str] = []
            controller.worker.download = lambda buffer: downloads.append(buffer.recording.name)
            continuous = recording("20260926042458_100.mp4")
            controller.recordings["20260926"] = [continuous]
            controller.seek(continuous.start, continuous)
            controller.on_position(camera_timestamp(datetime(2026, 9, 26, 4, 27, 53)))
            controller.on_finished(continuous.name)
            self.assertEqual(downloads, [continuous.name, continuous.name])
            self.assertEqual(controller.timeline.center, datetime(2026, 9, 26, 4, 27, 53))
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
