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
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication, QMenu, QWidget

from app import (
    LOCAL_DETECTION_CAMERAS_SETTING, LocalReplayPane, MainWindow, RtspCamera,
    measured_video_rate, update_local_detector,
)
from local_detection import (
    COVER_NAME, SAMPLE_BYTES, SCORE_THRESHOLD, DetectionEngine, DetectionEvent, DetectionHit, DetectionPipeline,
    EventTracker, YoloXDetector, add_missing_covers, encode_cover, export_event, install_model, load_events,
    organize_events, save_event,
)


def media_streams(path):
    return json.loads(subprocess.run([
        "ffprobe", "-v", "error", "-show_entries",
        "stream=index,codec_name:stream_disposition=attached_pic:stream_tags=filename,mimetype",
        "-of", "json", str(path),
    ], check=True, capture_output=True, text=True, timeout=10).stdout)["streams"]


def attached_image(path):
    return subprocess.run([
        "ffmpeg", "-v", "error", "-i", str(path), "-map", "0:1", "-c", "copy", "-f", "image2", "pipe:1",
    ], check=True, capture_output=True, timeout=10).stdout


def decoded_image(data):
    import cv2
    import numpy as np
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def make_test_video(path, seconds):
    subprocess.run([
        "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x72:rate=8",
        "-t", str(seconds), "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1", str(path),
    ], check=True, capture_output=True, timeout=15)


class EventTrackerTest(unittest.TestCase):
    @staticmethod
    def hit(name, score, box=(10, 10, 20, 20)):
        return DetectionHit(name, score, box)

    def test_two_nearby_hits_confirm_one_event_and_ignored_frames_close_it(self):
        start = datetime(2026, 9, 29, 17, 32, 30)
        tracker = EventTracker("camera", "Garden")
        self.assertEqual(tracker.observe(start, [self.hit("person", .7)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1), []), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=2), [self.hit("person", .8, (20, 10, 20, 20))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=4), []), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=5), [self.hit("person", .9, (30, 10, 20, 20))]), [])
        completed = tracker.observe(start + timedelta(seconds=9), [])
        self.assertEqual(len(completed), 1)
        self.assertEqual((completed[0].first, completed[0].last), (start, start + timedelta(seconds=5)))
        self.assertEqual(completed[0].classes, ("person",))
        self.assertEqual(completed[0].score, .9)

    def test_different_single_classes_do_not_confirm_false_event(self):
        start = datetime(2026, 9, 29, 17)
        tracker = EventTracker("camera", "Garden")
        for seconds, found in ((0, [self.hit("person", .6)]), (1, [self.hit("animal", .6)]), (4, [])):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds), found), [])

    def test_motionless_person_shape_does_not_confirm_an_event(self):
        start = datetime(2026, 10, 1, 6, 29, 57)
        tracker = EventTracker("camera", "Entrance")
        boxes = ((131, 133, 189, 172), (132, 132, 189, 174), (128, 132, 194, 174), (127, 133, 192, 173))
        for seconds, box in enumerate(boxes):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds), [self.hit("person", .8, box)]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=10), []), [])
        self.assertIsNone(tracker.first)

    def test_person_crossing_in_two_frames_confirms_an_event(self):
        start = datetime(2026, 10, 1, 7)
        tracker = EventTracker("camera", "Entrance")
        self.assertEqual(tracker.observe(start, [self.hit("person", .7, (100, 100, 60, 150))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1),
                                         [self.hit("person", .8, (150, 100, 60, 150))]), [])
        completed = tracker.observe(start + timedelta(seconds=5), [])
        self.assertEqual([(event.first, event.classes, event.score) for event in completed],
                         [(start, ("person",), .8)])

    def test_approaching_person_confirms_an_event(self):
        start = datetime(2026, 10, 1, 7)
        tracker = EventTracker("camera", "Entrance")
        self.assertEqual(tracker.observe(start, [self.hit("person", .7, (100, 100, 60, 150))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1),
                                         [self.hit("person", .8, (95, 95, 70, 175))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=5), [])[0].classes, ("person",))

    def test_motionless_person_shape_does_not_label_or_extend_an_animal_event(self):
        start = datetime(2026, 10, 1, 7)
        tracker = EventTracker("camera", "Entrance")
        statue = self.hit("person", .8, (131, 133, 189, 172))
        for seconds, cat_box in enumerate(((400, 250, 60, 40), (410, 250, 60, 40))):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds),
                                             [statue, self.hit("animal", .7, cat_box)]), [])
        for seconds in range(2, 5):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds), [statue]), [])
        completed = tracker.observe(start + timedelta(seconds=5), [statue])
        self.assertEqual([(event.last, event.classes) for event in completed],
                         [(start + timedelta(seconds=1), ("animal",))])

    def test_sustained_event_splits_before_unbounded_clip(self):
        start = datetime(2026, 9, 29, 17)
        tracker = EventTracker("camera", "Garden")
        events = []
        for seconds in range(121):
            events += tracker.observe(start + timedelta(seconds=seconds), [self.hit("animal", .8)])
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0].classes, ("animal",))
        self.assertLessEqual((events[0].last - events[0].first).total_seconds(), 120)

    def test_animal_keeps_one_track_after_a_short_detection_gap(self):
        start = datetime(2026, 9, 29, 20, 25)
        tracker = EventTracker("camera", "Garden")
        self.assertEqual(tracker.observe(start, [self.hit("animal", .7, (138, 127, 68, 36))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=1),
                                         [self.hit("animal", .7, (132, 128, 67, 33))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=5), [])[0].classes, ("animal",))
        self.assertEqual(tracker.observe(start + timedelta(seconds=18),
                                         [self.hit("animal", .77, (85, 112, 45, 38))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=19),
                                         [self.hit("animal", .78, (87, 111, 46, 39))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=23), [])[0].classes, ("animal",))
        self.assertEqual(len(tracker.tracks), 1)

    def test_event_cover_is_the_best_detection_frame_with_its_boxes(self):
        start = datetime(2026, 10, 2, 9, 44, 42)
        tracker = EventTracker("camera", "Kitchen")
        frames = [bytes([level]) * SAMPLE_BYTES for level in (20, 120, 220)]
        boxes = ((100, 100, 60, 150), (150, 100, 60, 150), (200, 100, 60, 150))
        for seconds, (frame, box, score) in enumerate(zip(frames, boxes, (.7, .9, .8))):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds),
                                             [self.hit("person", score, box)], frame), [])
        event = tracker.observe(start + timedelta(seconds=9), [], frames[0])[0]
        image = decoded_image(event.cover)
        self.assertEqual(image.shape, (360, 640, 3))
        self.assertAlmostEqual(float(image[20, 20].mean()), 120, delta=3)
        blue, green, red = (int(value) for value in image[100, 180])
        self.assertGreater(red, 180)
        self.assertLess(blue, 100)
        self.assertIsNone(tracker.cover_source)
        without_frames = EventTracker("camera", "Kitchen")
        for seconds, box in enumerate(boxes[:2]):
            without_frames.observe(start + timedelta(seconds=seconds), [self.hit("person", .8, box)])
        self.assertIsNone(without_frames.observe(start + timedelta(seconds=9), [])[0].cover)

    def test_motionless_animal_still_confirms_an_event(self):
        start = datetime(2026, 10, 1, 5, 41, 43)
        tracker = EventTracker("camera", "Garden")
        for seconds in range(2):
            self.assertEqual(tracker.observe(start + timedelta(seconds=seconds),
                                             [self.hit("animal", .74, (405, 170, 48, 61))]), [])
        self.assertEqual(tracker.observe(start + timedelta(seconds=5), [])[0].classes, ("animal",))


class DetectorTest(unittest.TestCase):
    def test_cat_and_dog_predictions_of_one_animal_form_one_animal_hit(self):
        import numpy as np
        detector = YoloXDetector.__new__(YoloXDetector)
        detector.np = np
        detector.threshold = SCORE_THRESHOLD
        detector.grids = np.zeros((8400, 2))
        detector.strides = np.full((8400, 1), 8)
        output = np.zeros((8400, 85), np.float32)
        output[:, 2:4] = -10
        for row, index, score in ((0, 15, .6), (1, 16, .7), (2, 0, .9)):
            output[row, :2] = (50, 20)
            output[row, 2:4] = np.log(10)
            output[row, 4] = 1
            output[row, 5 + index] = score
        detector.net = SimpleNamespace(setInput=lambda blob: None, forward=lambda: output)
        import cv2
        detector.cv2 = cv2
        hits = detector.infer(bytes(640 * 360 * 3))
        self.assertEqual(sorted((hit.name, round(hit.score, 2)) for hit in hits), [("animal", .7), ("person", .9)])


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
                day = root / "Detections" / f"{event.first:%Y-%m-%d}"
                self.assertEqual(finished.clip.parent, day / "person")
                self.assertTrue(finished.clip.name.startswith("person_Garden_"))
                metadata = day / ".metadata" / hashlib.sha256(b"camera").hexdigest()[:16] / "event.json"
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
                                   start + timedelta(seconds=22), ("animal",), .8)
            with patch("local_detection.event_directory", return_value=root / "Detections"):
                finished = export_event(event, recordings, "Garden")
                self.assertEqual(finished.clip_start, start + timedelta(seconds=15))
                packets = subprocess.run([
                    "ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
                    "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(finished.clip),
                ], check=True, capture_output=True, text=True, timeout=10)
                self.assertGreater(int(packets.stdout.strip()), 0)

    def test_excerpt_thumbnail_is_the_detection_image(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory)
            recordings = root / "Continuous"
            recordings.mkdir()
            start = datetime.now().replace(microsecond=0) - timedelta(minutes=1)
            make_test_video(recordings / f"Garden_{start:%Y%m%d_%H%M%S}.mkv", 30)
            cover = encode_cover(bytes([90]) * SAMPLE_BYTES, ((10, 10, 50, 50),))
            detected = DetectionEvent("camera", "Garden", start + timedelta(seconds=10),
                                      start + timedelta(seconds=14), ("person",), .9, cover=cover)
            undetected_image = DetectionEvent("camera", "Garden", start + timedelta(seconds=20),
                                              start + timedelta(seconds=24), ("animal",), .8)
            with patch("local_detection.event_directory", return_value=root / "Detections"):
                finished = export_event(detected, recordings, "Garden")
                extracted = export_event(undetected_image, recordings, "Garden")
                metadata = root / "event.json"
                save_event(metadata, finished, "ready")
                self.assertEqual(json.loads(metadata.read_text(encoding="utf-8"))["cover"], COVER_NAME)
            for clip in (finished.clip, extracted.clip):
                streams = media_streams(clip)
                self.assertEqual([(stream["codec_name"], stream["disposition"]["attached_pic"]) for stream in streams],
                                 [("h264", 0), ("mjpeg", 1)])
                self.assertEqual(streams[1]["tags"], {"filename": COVER_NAME, "mimetype": "image/jpeg"})
                self.assertEqual(oct(clip.stat().st_mode & 0o777), "0o600")
            self.assertEqual(attached_image(finished.clip), cover)
            self.assertEqual(finished.cover, cover)
            self.assertEqual(attached_image(extracted.clip), extracted.cover)
            detection_frame = subprocess.run([
                "ffmpeg", "-v", "error", "-ss", "7", "-i", str(extracted.clip), "-frames:v", "1",
                "-vf", "scale=640:-2", "-f", "image2", "-c:v", "png", "pipe:1",
            ], check=True, capture_output=True, timeout=10).stdout
            first_frame = subprocess.run([
                "ffmpeg", "-v", "error", "-i", str(extracted.clip), "-frames:v", "1",
                "-vf", "scale=640:-2", "-f", "image2", "-c:v", "png", "pipe:1",
            ], check=True, capture_output=True, timeout=10).stdout
            embedded = decoded_image(extracted.cover).astype(int)
            self.assertLess(abs(embedded - decoded_image(detection_frame)).mean(), 8)
            self.assertGreater(abs(embedded - decoded_image(first_frame)).mean(), 15)

    def test_existing_excerpts_receive_their_detection_image_once(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory) / "Detections"
            clip_start = datetime(2026, 10, 2, 9, 44, 37)
            clip = root / "2026-10-02" / "person" / "person_Kitchen_20261002_094437_03e42bc9.mkv"
            clip.parent.mkdir(parents=True)
            make_test_video(clip, 12)
            clip.chmod(0o600)
            metadata_folder = root / "2026-10-02" / ".metadata" / hashlib.sha256(b"camera").hexdigest()[:16]
            metadata_folder.mkdir(parents=True)
            stale = root / "2026-10-02" / ".metadata" / "intraswitch_camera_cover_interrupted"
            stale.mkdir()
            (stale / clip.name).write_bytes(b"partial")
            archived = root / "old" / ".metadata" / metadata_folder.name
            archived.mkdir(parents=True)
            with patch("local_detection.event_directory", return_value=root):
                save_event(metadata_folder / "event.json", DetectionEvent(
                    "camera", "Kitchen", clip_start + timedelta(seconds=5), clip_start + timedelta(seconds=9),
                    ("person",), .75, clip, clip_start,
                ), "ready")
                document = json.loads((metadata_folder / "event.json").read_text(encoding="utf-8"))
                del document["cover"]
                (metadata_folder / "event.json").write_text(json.dumps(document), encoding="utf-8")
                archived_document = {**document, "clip": "person/person_Kitchen_moved.mkv"}
                (archived / "event.json").write_text(json.dumps(archived_document), encoding="utf-8")
                add_missing_covers()
                covered = clip.stat()
                streams = media_streams(clip)
                self.assertEqual([stream["disposition"]["attached_pic"] for stream in streams], [0, 1])
                self.assertEqual(oct(covered.st_mode & 0o777), "0o600")
                self.assertFalse(stale.exists())
                self.assertEqual(json.loads((metadata_folder / "event.json").read_text(encoding="utf-8"))["cover"],
                                 COVER_NAME)
                self.assertEqual(json.loads((archived / "event.json").read_text(encoding="utf-8")), archived_document)
                self.assertEqual(load_events("camera", clip_start + timedelta(hours=1))[0].clip, clip)
                add_missing_covers()
                self.assertEqual(clip.stat().st_mtime_ns, covered.st_mtime_ns)
                self.assertEqual(len(media_streams(clip)), 2)
                self.assertEqual(list((root / "2026-10-02" / ".metadata").iterdir()), [metadata_folder])

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
                relocated = root / f"{first:%Y-%m-%d}" / "animal_person" / f"animal_person_{clip.name}"
                relocated_metadata = root / f"{first:%Y-%m-%d}" / ".metadata" / camera_folder.name / metadata.name
                relocated_pending = relocated_metadata.with_name(pending.name)
                self.assertEqual(relocated.read_bytes(), b"existing video")
                self.assertFalse(camera_folder.exists())
                self.assertEqual(json.loads(relocated_pending.read_text(encoding="utf-8"))["status"], "pending")
                self.assertEqual(json.loads(relocated_metadata.read_text(encoding="utf-8"))["clip"],
                                 relocated.relative_to(root).as_posix())
                self.assertEqual(load_events("camera", first + timedelta(hours=1))[0].clip, relocated)
                self.assertEqual(load_events("camera", first + timedelta(hours=25)), [])
                self.assertTrue(relocated.exists())
                self.assertTrue(relocated_metadata.exists())

    def test_type_folders_are_sorted_by_day_without_deleting_old_excerpts(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory) / "Detections"
            metadata_folder = root / ".metadata" / hashlib.sha256(b"camera").hexdigest()[:16]
            metadata_folder.mkdir(parents=True)
            personal = root / "chambre-chat-2025-03"
            personal.mkdir()
            (personal / "kept.mkv").write_bytes(b"personal")
            clips = []
            with patch("local_detection.event_directory", return_value=root):
                for index, clip_start in enumerate((datetime(2026, 9, 30, 23, 59, 58), datetime(2026, 9, 30, 6, 29, 52))):
                    folder = root / "person"
                    folder.mkdir(exist_ok=True)
                    clip = folder / f"person_Entrance_{clip_start:%Y%m%d_%H%M%S}_{index}.mkv"
                    clip.write_bytes(b"video %d" % index)
                    first = clip_start + timedelta(seconds=5)
                    save_event(metadata_folder / f"{index}.json", DetectionEvent(
                        "camera", "Entrance", first, first + timedelta(seconds=2), ("person",), .8, clip, clip_start,
                    ), "ready")
                    clips.append(clip)
                dog_folder = root / "dog"
                dog_folder.mkdir()
                dog_clip = dog_folder / "dog_Garden_20261001_054138_cfa036ea.mkv"
                dog_clip.write_bytes(b"black cat")
                dog_start = datetime(2026, 10, 1, 5, 41, 38)
                save_event(metadata_folder / "2.json", DetectionEvent(
                    "camera", "Garden", dog_start + timedelta(seconds=5), dog_start + timedelta(seconds=26),
                    ("dog",), .71, dog_clip, dog_start,
                ), "ready")
                organize_events()
                organize_events()
                sorted_clips = [root / "2026-10-01" / "person" / clips[0].name,
                                root / "2026-09-30" / "person" / clips[1].name]
                animal_clip = root / "2026-10-01" / "animal" / "animal_Garden_20261001_054138_cfa036ea.mkv"
                documents = [root / day / ".metadata" / metadata_folder.name / f"{index}.json"
                             for index, day in enumerate(("2026-10-01", "2026-09-30", "2026-10-01"))]
                self.assertEqual([path.read_bytes() for path in sorted_clips], [b"video 0", b"video 1"])
                self.assertEqual(animal_clip.read_bytes(), b"black cat")
                self.assertFalse((root / ".metadata").exists())
                animal_document = json.loads(documents[2].read_text(encoding="utf-8"))
                self.assertEqual((animal_document["clip"], animal_document["classes"]),
                                 (animal_clip.relative_to(root).as_posix(), ["animal"]))
                self.assertFalse((root / "person").exists())
                self.assertFalse(dog_folder.exists())
                self.assertEqual((personal / "kept.mkv").read_bytes(), b"personal")
                self.assertEqual([json.loads(documents[index].read_text(encoding="utf-8"))["clip"]
                                  for index in range(2)],
                                 [path.relative_to(root).as_posix() for path in sorted_clips])
                self.assertEqual([event.clip for event in load_events("camera", datetime(2026, 10, 1, 12))],
                                 [sorted_clips[0], animal_clip])

    def test_pending_detection_without_kept_recording_is_discarded(self):
        with tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_") as directory:
            root = Path(directory) / "Detections"
            engine = SimpleNamespace(_enqueue=Mock())
            pipeline = SimpleNamespace(uid="camera", prefix="Garden", on_event=Mock())
            now = datetime.now()
            with patch("local_detection.event_directory", return_value=root):
                paths = []
                for name, last in (("expired", now - timedelta(hours=25)), ("recent", now - timedelta(hours=1))):
                    metadata = root / f"{last:%Y-%m-%d}" / ".metadata" / hashlib.sha256(b"camera").hexdigest()[:16]
                    metadata.mkdir(parents=True, exist_ok=True)
                    paths.append(metadata / f"{name}.json")
                    save_event(paths[-1], DetectionEvent(
                        "camera", "Garden", last - timedelta(seconds=5), last, ("cat",), .8,
                    ), "pending")
                DetectionEngine.recover(engine, pipeline)
            self.assertEqual([path.exists() for path in paths], [False, True])
            self.assertEqual([(call.args[0].name, call.args[1].classes) for call in engine._enqueue.call_args_list],
                             [("recent.json", ("animal",))])

    def test_disabled_continuous_recording_does_not_start_decoder(self):
        worker = SimpleNamespace(
            local_detection_enabled=True, local_detector=None, continuous=None,
            continuous_enabled=SimpleNamespace(is_set=lambda: False),
        )
        with patch("app.local_detection_engine") as engine:
            self.assertIsNone(update_local_detector(worker, RtspCamera("camera", "Garden", "rtsp://example.invalid"), lambda: []))
            engine.assert_not_called()


class CameraDetectionSettingsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_detection_test_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.garden = RtspCamera("garden", "Garden", "rtsp://example.invalid")
        self.living = RtspCamera("living", "Living room", "rtsp://example.invalid")
        self.window.devices = [self.garden, self.living]
        self.window.selected_device = self.garden
        self.window.local_detection_menu = QMenu(self.window)
        self.window.stream_worker = SimpleNamespace(local_detection_enabled=True)
        self.preview = Mock()
        self.window.previews = {self.living.uid: self.preview}

    def tearDown(self):
        self.window.stream_worker = None
        self.window.previews.clear()
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()

    def test_camera_choices_persist_and_master_toggle_preserves_independent_preferences(self):
        self.window.set_camera_local_detection(self.living.uid, False)
        self.assertTrue(self.window.stream_worker.local_detection_enabled)
        self.preview.set_local_detection.assert_called_with(False)
        with patch.object(self.window, "show_notice"):
            self.window.set_local_detection(False)
            self.assertFalse(self.window.stream_worker.local_detection_enabled)
            self.assertFalse(self.window.local_detection_menu.isEnabled())
            self.window.set_local_detection(True)
        self.assertTrue(self.window.stream_worker.local_detection_enabled)
        self.assertTrue(self.window.local_detection_menu.isEnabled())
        self.assertFalse(self.window.local_detection_enabled(self.living.uid))
        self.preview.set_local_detection.assert_called_with(False)
        reopened = QSettings(self.settings.fileName(), QSettings.Format.IniFormat)
        self.assertFalse(reopened.value(f"{LOCAL_DETECTION_CAMERAS_SETTING}/{self.living.uid}", True, bool))
        self.window.selected_device = self.living
        self.window.apply_local_detection_settings()
        self.assertFalse(self.window.stream_worker.local_detection_enabled)

    def test_menu_changes_only_the_chosen_camera(self):
        self.window.update_local_detection_menu()
        actions = self.window.local_detection_menu.actions()
        self.assertEqual([action.text() for action in actions], ["Garden", "Living room"])
        self.assertTrue(all(action.isCheckable() and action.isChecked() for action in actions))
        actions[1].trigger()
        self.assertTrue(actions[0].isChecked())
        self.assertTrue(self.window.local_detection_enabled(self.garden.uid))
        self.assertFalse(self.window.local_detection_enabled(self.living.uid))

    def test_already_queued_disabled_camera_event_updates_history_without_a_notification(self):
        self.window.set_camera_local_detection(self.living.uid, False)
        now = datetime.now()
        event = DetectionEvent(self.living.uid, self.living.name, now, now, ("person",), .8,
                               Path(self.directory.name) / "event.mkv", now)
        replay = Mock()
        self.window.local_replays[self.living.uid] = replay
        with patch.object(self.window, "show_notice") as notice, patch.object(self.window.notifier, "notify") as notify:
            self.window.on_local_detection_ready(event)
            replay.refresh_recordings.assert_called_once()
            notify.assert_not_called()
            notice.assert_not_called()
            enabled_event = DetectionEvent(self.garden.uid, self.garden.name, now, now, ("person",), .8,
                                          Path(self.directory.name) / "garden.mkv", now)
            self.window.on_local_detection_ready(enabled_event)
            notify.assert_called_once()
            notice.assert_called_once()
        self.window.local_replays.clear()


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
            other = DetectionEvent(camera.uid, camera.name, datetime.now(), datetime.now(),
                                   ("animal",), .7, Path(directory) / "other.mkv", datetime.now())
            window.devices = [camera]
            window.selected_device = camera
            actions = []
            with patch.object(window.notifier, "notify",
                              side_effect=lambda title, message, timeout, activate, fallback: actions.append(activate)), \
                    patch.object(window, "show_notice"):
                window.on_local_detection_ready(event)
                window.on_local_detection_ready(other)
            with patch.object(window, "show_window") as show_window, \
                    patch.object(window, "open_local_replay") as open_replay:
                actions[0]()
                open_replay.assert_called_once_with(camera, event)
                show_window.assert_called_once_with()
            window.quit_requested = True
            window.close()


if __name__ == "__main__":
    unittest.main()
