import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication

from app import (
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
        self.assertIsNone(self.worker._next_request())

    def test_listing_runs_first_and_only_latest_download_is_kept(self) -> None:
        older = ReplayBuffer(recording("20260926192758_100.mp4"))
        newer = ReplayBuffer(recording("20260926231604_011.mp4"))
        self.worker.download(older)
        self.worker.list_day("20260926")
        self.worker.download(newer)
        self.assertEqual(self.worker._next_request(), (REPLAY_LIST_REQUEST, "20260926"))
        self.assertEqual(self.worker._next_request(), (REPLAY_DOWNLOAD_REQUEST, newer))
        self.assertTrue(older.ended)
        self.assertIsNone(self.worker._next_request())


CLOSED_PLAYBACK_FAILURE_SCRIPT = """
import gc, os, sys, threading
os.environ["QT_QPA_PLATFORM"] = "offscreen"
sys.path.insert(0, sys.argv[1])
from datetime import datetime
from types import SimpleNamespace
from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import QApplication
from app import ReplayController, TimelineWidget
application = QApplication([])
messages = []
for index in range(30):
    failed = threading.Event()
    timeline = TimelineWidget()
    controller = ReplayController(SimpleNamespace(), "", timeline)
    if not index:
        controller.status_changed.connect(messages.append)
    controller.worker.failed.connect(lambda message, done=failed: done.set(), Qt.ConnectionType.DirectConnection)
    controller.load_visible_days(datetime.now(), datetime.now())
    if not failed.wait(10):
        raise SystemExit("The replay worker did not fail.")
    if not index:
        application.processEvents()
    controller.stop()
    del controller, timeline
    gc.collect()
application.processEvents()
print(messages)
"""


class ReplayFailureTest(unittest.TestCase):
    def test_worker_failure_reaches_status_and_survives_closed_playback(self) -> None:
        result = subprocess.run(
            [sys.executable, "-c", CLOSED_PLAYBACK_FAILURE_SCRIPT, str(Path(__file__).resolve().parents[1])],
            capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(result.stdout.strip(), "['The camera recording could not be read.']")


class ReplayLoadingTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.application = QApplication.instance() or QApplication([])

    def test_loading_counts_up_to_the_requested_time(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            loading: list[str] = []
            controller.status_changed.connect(loading.append)
            target = recording("20260926192758_100.mp4")
            controller.recordings["20260926"] = [target]
            controller.seek(datetime(2026, 9, 26, 19, 30, 28))
            controller.on_progress(target.name, 0.25)
            controller.on_progress(target.name, 0.6)
            controller.on_position(camera_timestamp(datetime(2026, 9, 26, 19, 30, 28)))
            self.assertEqual(
                loading,
                [
                    "Loading 26/09/2026 19:30:28 0%",
                    "Loading 26/09/2026 19:30:28 50%",
                    "Loading 26/09/2026 19:30:28 99%",
                    "Playback 26/09/2026 19:30:28",
                ],
            )
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
            controller.timeline.center = datetime(2026, 9, 26, 12, 0, 30)
            controller.jump_to_detection(-1)
            self.assertEqual(controller.current.recording.name, "20260926100000_011.mp4")
        finally:
            controller.stop()

    def test_overlapping_detections_form_one_event(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            first = recording("20260926230955_011.mp4")
            second = recording("20260926231004_011.mp4")
            earlier = recording("20260926192809_011.mp4")
            controller.recordings["20260926"] = [earlier, first, second]
            controller.seek(second.start, second)
            controller.timeline.center = datetime(2026, 9, 26, 23, 10, 20)
            controller.jump_to_detection(-1)
            self.assertEqual(controller.current.recording, earlier)
            controller.timeline.center = datetime(2026, 9, 26, 19, 28, 20)
            controller.jump_to_detection(1)
            self.assertEqual(controller.current.recording, first)
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
            self.assertEqual(downloads, [first.name, detection.name])
            controller.on_finished(first.name)
            self.assertEqual(controller.current.recording, detection)
            self.assertEqual(controller.timeline.center, first.end)
            self.assertEqual(downloads, [first.name, detection.name])
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
            self.assertEqual(controller.current.recording, detection)
            self.assertEqual(controller.timeline.center, continuous.end)
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

    def test_repeated_cut_short_recording_stops_after_retry_limit(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            downloads: list[str] = []
            controller.worker.download = lambda buffer: downloads.append(buffer.recording.name)
            target = recording("20260926042458_100.mp4")
            controller.recordings["20260926"] = [target]
            controller.seek(target.start)
            for _ in range(3):
                controller.on_position(camera_timestamp(target.start))
                controller.on_finished(target.name)
            self.assertEqual(downloads, [target.name] * 3)
            self.assertEqual(controller.reloads, 2)
            self.assertFalse(controller.playing)
        finally:
            controller.stop()


    def test_previous_detection_waits_for_a_day_already_requested(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            listed: list[str] = []
            controller.worker.list_day = listed.append
            controller.recordings["20260926"] = [recording("20260926120000_100.mp4")]
            controller.requested_days.update({"20260926", "20260925"})
            messages: list[str] = []
            controller.status_changed.connect(messages.append)
            controller.timeline.center = datetime(2026, 9, 26, 12, 1, 0)
            controller.jump_to_detection(-1)
            self.assertEqual(messages[-1], "Looking for an earlier detection...")
            self.assertEqual(listed, [])
            controller.on_day_listed("20260925", [recording("20260925080000_011.mp4")])
            self.assertEqual(controller.current.recording.name, "20260925080000_011.mp4")
        finally:
            controller.stop()


    def test_previous_detection_skips_a_short_detection_that_just_ended(self) -> None:
        controller = ReplayController(SimpleNamespace(), "", TimelineWidget())
        try:
            controller.worker.download = lambda buffer: None
            earlier = recording("20260925010000_011.mp4")
            short = CardRecording("20260925032041_011.mp4", datetime(2026, 9, 25, 3, 20, 41), 10, 100)
            following = recording("20260925031904_100.mp4")
            controller.recordings["20260925"] = [earlier, following, short]
            controller.seek(short.start, short)
            controller.seek(datetime(2026, 9, 25, 3, 20, 52), following)
            controller.timeline.center = datetime(2026, 9, 25, 3, 20, 53)
            controller.jump_to_detection(-1)
            self.assertEqual(controller.current.recording, earlier)
        finally:
            controller.stop()


if __name__ == "__main__":
    unittest.main()
