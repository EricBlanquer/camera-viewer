import hashlib
import io
import os
import json
import subprocess
import tempfile
import time
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtWidgets import QApplication, QWidget

from app import LocalReplayPane, MainWindow, RtspCamera, measured_video_rate, update_local_detector
from local_detection import (
    DetectionEvent, DetectionHit, DetectionPipeline, EventTracker, export_event, install_model, load_events,
    organize_events, prune_events, save_event,
)


class EventTrackerTest(unittest.TestCase):
    @staticmethod
    def hit(name, score, box=(10, 10, 20, 20)):
        return DetectionHit(name, score, box)

    def test_two_nearby_hits_confirm_one_event_and_ignored_frames_close_it(self):
        start = datetime(2026, 9, 29, 17, 32, 30)
        tracker = EventTracker("camera", "Garden")
        self.assertEqual(tracker.observe(start, [self.hit("person", .7)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1), []), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=2), [self.hit("person", .8)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=4), []), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=5), [self.hit("person", .9)]), [])
        completed = tracker.observe(start + timedelta(seconds=9), [])
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].first, completed[0].last), (start, start + timedelta(seconds=5)))
        self.assertEqual(completed[0].classes, ("person",))
        self.assertEqual(completed[0].score, .9)

    def test_different_single_classes_do_not_confirm_false_event(self):
        start = datetime(2026, 9, 29, 17)
        tracker = EventTracker("camera", "Garden")
        for seconds, found in ((0, [self.hit("person", .6)]), (1, [self.hit("bird", .6)]), (4, [])):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds), found), [])

    def test_sustained_event_splits_before_unbounded_clip(self):
        start = datetime(2026, 9, 29, 17)
        tracker = EventTracker("camera", "Garden")
        events = []
        for seconds in range(121):
            events += tracker.observe(start + timedelta(seconds=seconds), [self.hit("dog", .8)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].classes, ("dog",))
        self.assertLessEqual((events[0].last - events[0].first).total_seconds(), 120)

    def test_one_cat_keeps_its_identity_when_the_model_calls_it_dog_or_bird(self):
        start = datetime(2026, 9, 29, 20, 14)
        tracker = EventTracker("camera", "Garden")
        dog_box = (422, 248, 64, 46)
        cat_box = (423, 250, 65, 43)
        bird_box = (418, 226, 65, 68)
        for seconds, hit in ((0, self.hit("dog", .64, dog_box)),
                             (1, self.hit("dog", .69, dog_box)),
                             (2, self.hit("cat", .62, cat_box)),
                             (3, self.hit("dog", .74, dog_box))):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds), [hit]), [])
        first = tracker.observe(start + timedelta(seconds=7), [])
        self.assertEqual(first[0].classes, ("cat",))
        self.assertEqual(tracker.observe(start + timedelta(seconds=21), [self.hit("bird", .59, bird_box)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=22), [self.hit("bird", .61, bird_box)]), [])
        second = tracker.observe(start + timedelta(seconds=26), [])
        self.assertEqual(second[0].classes, ("cat",))
        self.assertEqual(tracker.observe(start + timedelta(seconds=100), [self.hit("bird", .7, bird_box)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=101), [self.hit("bird", .7, bird_box)]), [])
        third = tracker.observe(start + timedelta(seconds=105), [])
        self.assertEqual(third[0].classes, ("bird",))

    def test_separate_cat_and_dog_remain_two_types(self):
        start = datetime(2026, 9, 29, 20, 14)
        tracker = EventTracker("camera", "Garden")
        animals = [self.hit("cat", .8, (10, 10, 20, 20)), self.hit("dog", .9, (100, 100, 20, 20))]
        self.assertEqual(tracker.observe(start, animals), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1), animals), [])
        completed = tracker.observe(start + timedelta(seconds=5), [])
        self.assertEqual(completed[0].classes, ("cat", "dog"))


class ModelInstallationTest(unittest.TestCase):
    def test_model_checksum_must_match_before_replacing_cached_copy(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            destination = Path(directory) / "model.onnx"
            destination.write_bytes(b"old")
            with patch("local_detection.MODEL_SIZE", 4), \
                    patch("local_detection.MODEL_SHA256", hashlib.sha256(b"good").hexdigest()), \
                    patch("local_detection.urllib.request.urlopen", return_value=io.BytesIO(b"evil")):
                with self.assertRaisesRegex(OSError, "checksum"):
                    install_model(destination)
            self.assertEqual(destination.read_bytes(), b"old")
            with patch("local_detection.MODEL_SIZE", 4), \
                    patch("local_detection.MODEL_SHA256", hashlib.sha256(b"good").hexdigest()), \
                    patch("local_detection.urllib.request.urlopen", return_value=io.BytesIO(b"good")):
                self.assertEqual(install_model(destination), destination)
            self.assertEqual(destination.read_bytes(), b"good")


class RecordingExcerptTest(unittest.TestCase):
    def test_input_rate_is_measured_from_arriving_camera_frames(self):
        from collections import deque
        self.assertIsNone(measured_video_rate(deque(index / 15 for index in range(20))))
        self.assertEqual(measured_video_rate(deque(index / 15 for index in range(45))), 15)
        self.assertEqual(measured_video_rate(deque(index / 8 for index in range(24))), 8)

    def test_live_h264_decoder_produces_frames_without_display(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            source = Path(directory) / "source.h264"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=8",
                "-frames:v", "32", "-c:v", "libx264", "-preset", "ultrafast", "-g", "8",
                "-threads", "1", "-f", "h264", str(source),
            ], check=True, capture_output=True, timeout=15)
            packets = json.loads(subprocess.run([
                "ffprobe", "-v", "error", "-show_packets", "-show_entries", "packet=pos,size,flags",
                "-of", "json", "-f", "h264", str(source),
            ], check=True, capture_output=True, timeout=10).stdout)["packets"]
            frames = []
            engine = SimpleNamespace(
                recover=lambda pipeline: None, remove=lambda pipeline: None,
                submit=lambda pipeline, frame, when: frames.append((frame, when)),
            )
            pipeline = DetectionPipeline(
                engine, "camera", "Garden", "Garden", ["-r", "8", "-f", "h264", "-i", "pipe:0"],
                lambda event: None, lambda error: self.fail(error), raw_input=True,
            )
            try:
                data = source.read_bytes()
                for packet in packets:
                    offset = int(packet["pos"])
                    pipeline.feed(data[offset:offset + int(packet["size"])], "K" in packet["flags"])
                    time.sleep(.125)
                deadline = time.monotonic() + 2
                while not frames and time.monotonic() < deadline:
                    time.sleep(.05)
                self.assertGreaterEqual(len(frames), 1)
                self.assertEqual(len(frames[0][0]), 640 * 360 * 3)
            finally:
                pipeline.close()

    def test_excerpt_includes_both_sides_of_a_segment_boundary(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory)
            recordings = root / "Continuous"
            recordings.mkdir()
            start = datetime.now().replace(microsecond=0) - timedelta(minutes=1)
            for index in range(2):
                path = recordings / f"Garden_{start + timedelta(seconds=index * 10):%Y%m%d_%H%M%S}.mkv"
                subprocess.run([
                    "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=8",
                    "-t", "10", "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(path),
                ], check=True, capture_output=True, timeout=15)
            event = DetectionEvent("camera", "Garden", start + timedelta(seconds=9),
                                   start + timedelta(seconds=11), ("person",), .9)
            with patch("local_detection.event_directory", return_value=root / "Detections"):
                finished = export_event(event, recordings, "Garden")
                self.assertIsNotNone(finished.clip)
                self.assertEqual(finished.clip_start, start + timedelta(seconds=4))
                self.assertEqual(finished.clip.parent.name, "person")
                self.assertTrue(finished.clip.name.startswith("person_Garden_"))
                metadata = root / "Detections" / ".metadata" / hashlib.sha256(b"camera").hexdigest()[:16] / "event.json"
                metadata.parent.mkdir(parents=True)
                save_event(metadata, finished, "ready")
                self.assertEqual(load_events("camera", start + timedelta(seconds=20)), [finished])
                duration = subprocess.run([
                    "ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1",
                    str(finished.clip),
                ], check=True, capture_output=True, text=True, timeout=10)
                self.assertAlmostEqual(float(duration.stdout), 12, delta=1)

    def test_excerpt_from_open_raw_segment_contains_decodable_video(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory)
            recordings = root / "Continuous"
            recordings.mkdir()
            start = datetime.now().replace(microsecond=0) - timedelta(minutes=1)
            source = recordings / f"Garden_{start:%Y%m%d_%H%M%S}.mkv.h264"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=8",
                "-t", "30", "-c:v", "libx264", "-preset", "ultrafast", "-g", "8",
                "-threads", "1", "-f", "h264", str(source),
            ], check=True, capture_output=True, timeout=15)
            os.utime(source, (start.timestamp() + 30, start.timestamp() + 30))
            event = DetectionEvent("camera", "Garden", start + timedelta(seconds=20),
                                   start + timedelta(seconds=22), ("cat",), .8)
            with patch("local_detection.event_directory", return_value=root / "Detections"):
                finished = export_event(event, recordings, "Garden")
                self.assertEqual(finished.clip_start, start + timedelta(seconds=15))
                packets = subprocess.run([
                    "ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(finished.clip),
                ], check=True, capture_output=True, text=True, timeout=10)
                self.assertGreater(int(packets.stdout.strip()), 0)

    def test_existing_excerpts_are_sorted_by_type_and_remain_playable(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory) / "Detections"
            camera_folder = root / hashlib.sha256(b"camera").hexdigest()[:16]
            camera_folder.mkdir(parents=True)
            clip = camera_folder / "Garden_20260929_173226_12345678.mkv"
            clip.write_bytes(b"existing video")
            first = datetime.now().replace(microsecond=0)
            event = DetectionEvent("camera", "Garden", first, first + timedelta(seconds=4),
                                   ("cat", "person"), .9, clip, first - timedelta(seconds=5))
            metadata = camera_folder / "event.json"
            with patch("local_detection.event_directory", return_value=root):
                save_event(metadata, event, "ready")
                document = json.loads(metadata.read_text(encoding="utf-8"))
                document["clip"] = clip.name
                metadata.write_text(json.dumps(document), encoding="utf-8")
                pending = camera_folder / "pending.json"
                save_event(pending, DetectionEvent("camera", "Garden", first, first,
                                                   ("dog",), .7), "pending")
                organize_events()
                organize_events()
                relocated = root / "cat_person" / f"cat_person_{clip.name}"
                relocated_metadata = root / ".metadata" / camera_folder.name / metadata.name
                relocated_pending = relocated_metadata.with_name(pending.name)
                self.assertEqual(relocated.read_bytes(), b"existing video")
                self.assertFalse(clip.exists())
                self.assertFalse(metadata.exists())
                self.assertEqual(json.loads(relocated_pending.read_text(encoding="utf-8"))["status"], "pending")
                self.assertEqual(json.loads(relocated_metadata.read_text(encoding="utf-8"))["clip"],
                                 f"cat_person/{relocated.name}")
                self.assertEqual(load_events("camera", first + timedelta(hours=1))[0].clip, relocated)
                prune_events(first + timedelta(hours=25))
                self.assertFalse(relocated.exists())
                self.assertFalse(relocated_metadata.exists())
                self.assertFalse(relocated_pending.exists())

    def test_disabled_continuous_recording_does_not_start_decoder(self):
        worker = SimpleNamespace(
            local_detection_enabled=True, local_detector=None, continuous=None,
            continuous_enabled=SimpleNamespace(is_set=lambda: False),
        )
        with patch("app.local_detection_engine") as engine:
            self.assertIsNone(update_local_detector(worker, RtspCamera("camera", "Garden", "rtsp://example.invalid"), lambda: []))
            engine.assert_not_called()


class LocalReplayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def test_detection_is_marked_on_timeline_and_opens_its_excerpt(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            clip = Path(directory) / "excerpt.mkv"
            clip.write_bytes(b"media")
            first = datetime.now().replace(microsecond=0) - timedelta(minutes=1)
            event = DetectionEvent("camera", "Garden", first, first + timedelta(seconds=4),
                                   ("person",), .8, clip, first - timedelta(seconds=5))
            parent = QWidget()
            with patch("app.camera_recordings", return_value=[]), \
                    patch("app.load_events", return_value=[event]), \
                    patch.object(LocalReplayPane, "play_selected") as play_selected:
                pane = LocalReplayPane(RtspCamera("camera", "Garden", "rtsp://example.invalid"), parent, initial_detection=event)
                pane.refresh_recordings()
                self.assertEqual(pane.recordings, [clip])
                self.assertTrue(pane.timeline.recordings[0].detection)
                play_selected.assert_called_once_with(0, 5.0)
                pane.stop()
            parent.close()

    def test_notification_opens_the_affected_camera_excerpt(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            from PyQt6.QtCore import QSettings
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            with patch("app.QSettings", return_value=settings), patch("app.QTimer.singleShot"):
                window = MainWindow()
            camera = RtspCamera("camera", "Garden", "rtsp://example.invalid")
            event = DetectionEvent(camera.uid, camera.name, datetime.now(), datetime.now(),
                                   ("person",), .8, Path(directory) / "event.mkv", datetime.now())
            window.devices = [camera]
            window.selected_device = camera
            window.latest_local_detection = event
            with patch.object(window, "show_window"), patch.object(window, "open_local_replay") as open_replay:
                window.open_detection_notification()
                open_replay.assert_called_once_with(camera, event)
            window.quit_requested = True
            window.close()


if __name__ == "__main__":
    unittest.main()
