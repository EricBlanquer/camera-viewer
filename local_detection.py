from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import queue
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.request
import uuid
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable


LOG = logging.getLogger("okam-linux.local_detection")
MODEL_URL = "https://huggingface.co/opencv/opencv_zoo/resolve/main/models/object_detection_yolox/object_detection_yolox_2022nov.onnx"
MODEL_SHA256 = "c5c2d13e59ae883e6af3b45daea64af4833a4951c92d116ec270d9ddbe998063"
MODEL_SIZE = 35858002
PERSON_CLASS = "person"
ANIMAL_CLASS = "animal"
CLASS_NAMES = {0: PERSON_CLASS, 14: ANIMAL_CLASS, 15: ANIMAL_CLASS, 16: ANIMAL_CLASS}
LEGACY_ANIMAL_CLASSES = frozenset({"bird", "cat", "dog"})
SCORE_THRESHOLD = 0.55
SAMPLE_WIDTH = 640
SAMPLE_HEIGHT = 360
SAMPLE_BYTES = SAMPLE_WIDTH * SAMPLE_HEIGHT * 3
EVENT_MARGIN_SECONDS = 5
EVENT_GAP_SECONDS = 3
EVENT_CONFIRM_SECONDS = 2
EVENT_MAX_SECONDS = 120
TRACK_MAX_AGE = timedelta(seconds=60)
TRACK_NEARBY_AGE = timedelta(seconds=20)
TRACK_OVERLAP_THRESHOLD = 0.25
TRACK_DISTANCE_FACTOR = 1.1
TRACK_AREA_RATIO_LIMIT = 2.5
PERSON_MOVEMENT_FACTOR = 0.25
PERSON_SCALE_LIMIT = 1.25
MODEL_DOWNLOAD_LIMIT = MODEL_SIZE + 1
PLAYBACK_PERIOD = timedelta(hours=24)
RECORDING_RETENTION = timedelta(hours=24)
EVENT_DAY_FORMAT = "%Y-%m-%d"
METADATA_FOLDER = ".metadata"
COVER_NAME = "cover_land.jpg"
COVER_MIME_TYPE = "image/jpeg"
COVER_JPEG_QUALITY = 90
COVER_BOX_COLOR = (40, 60, 230)
COVER_BOX_THICKNESS = 2
COVER_TEMPORARY_PREFIX = "intraswitch_camera_cover_"


def model_path() -> Path:
    return Path.home() / ".cache/camera-viewer/models/yolox-s.onnx"


def install_model(path: Path | None = None) -> Path:
    destination = path or model_path()
    if destination.is_file() and destination.stat().st_size == MODEL_SIZE:
        with destination.open("rb") as source:
            if hashlib.file_digest(source, "sha256").hexdigest() == MODEL_SHA256:
                return destination
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix="intraswitch_camera_model_", dir=destination.parent)
    try:
        digest = hashlib.sha256()
        total = 0
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(MODEL_URL, timeout=30) as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > MODEL_DOWNLOAD_LIMIT:
                    raise OSError("The detection model is larger than expected.")
                digest.update(chunk)
                output.write(chunk)
        if total != MODEL_SIZE or digest.hexdigest() != MODEL_SHA256:
            raise OSError("The detection model checksum does not match the published model.")
        os.replace(temporary, destination)
        return destination
    finally:
        Path(temporary).unlink(missing_ok=True)


class YoloXDetector:
    def __init__(self, path: Path, threshold: float = SCORE_THRESHOLD) -> None:
        import cv2
        import numpy as np
        cv2.setNumThreads(2)
        self.cv2 = cv2
        self.np = np
        self.net = cv2.dnn.readNet(str(path))
        self.threshold = threshold
        grids = []
        strides = []
        for stride in (8, 16, 32):
            side = 640 // stride
            x, y = np.meshgrid(np.arange(side), np.arange(side))
            grids.append(np.stack((x, y), axis=2).reshape(-1, 2))
            strides.append(np.full((side * side, 1), stride))
        self.grids = np.concatenate(grids)
        self.strides = np.concatenate(strides)

    def infer(self, frame: bytes) -> list[DetectionHit]:
        cv2, np = self.cv2, self.np
        source = np.frombuffer(frame, np.uint8).reshape(SAMPLE_HEIGHT, SAMPLE_WIDTH, 3)
        image = np.full((640, 640, 3), 114, np.float32)
        image[:SAMPLE_HEIGHT] = source
        self.net.setInput(cv2.dnn.blobFromImage(image, swapRB=True))
        output = self.net.forward().reshape(-1, 85)
        scores = output[:, 4:5] * output[:, 5:]
        classes = np.argmax(scores, axis=1)
        best = scores[np.arange(len(scores)), classes]
        found = []
        for name in sorted(set(CLASS_NAMES.values())):
            indices = [index for index, label in CLASS_NAMES.items() if label == name]
            matches = np.where(np.isin(classes, indices) & (best >= self.threshold))[0]
            if not len(matches):
                continue
            centers = (output[matches, :2] + self.grids[matches]) * self.strides[matches]
            sizes = np.exp(output[matches, 2:4]) * self.strides[matches]
            boxes = np.concatenate((centers - sizes / 2, sizes), axis=1)
            kept = cv2.dnn.NMSBoxes(boxes.tolist(), best[matches].tolist(), self.threshold, 0.5)
            if len(kept):
                for row in np.asarray(kept).flatten():
                    position = int(row)
                    box = tuple(float(value) for value in boxes[position])
                    if box[1] < SAMPLE_HEIGHT:
                        found.append(DetectionHit(name, float(best[matches[position]]), box))
        return found


@dataclass(frozen=True)
class DetectionHit:
    name: str
    score: float
    box: tuple[float, float, float, float]


@dataclass
class DetectionTrack:
    label: str
    box: tuple[float, float, float, float]
    last: datetime
    anchor: tuple[float, float, float, float]
    moved: bool = False


def box_overlap(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    left = max(first[0], second[0])
    top = max(first[1], second[1])
    right = min(first[0] + first[2], second[0] + second[2])
    bottom = min(first[1] + first[3], second[1] + second[3])
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    union = first[2] * first[3] + second[2] * second[3] - intersection
    return intersection / union if union > 0 else 0.0


def box_proximity(first: tuple[float, float, float, float], second: tuple[float, float, float, float]) -> float:
    first_area = first[2] * first[3]
    second_area = second[2] * second[3]
    if min(first_area, second_area) <= 0 or max(first_area, second_area) / min(first_area, second_area) > TRACK_AREA_RATIO_LIMIT:
        return 0.0
    distance_limit = max(first[2], first[3], second[2], second[3]) * TRACK_DISTANCE_FACTOR
    horizontal = first[0] + first[2] / 2 - second[0] - second[2] / 2
    vertical = first[1] + first[3] / 2 - second[1] - second[3] / 2
    distance_squared = horizontal * horizontal + vertical * vertical
    return max(0.0, 1 - distance_squared / (distance_limit * distance_limit))


def box_moved(anchor: tuple[float, float, float, float], box: tuple[float, float, float, float]) -> bool:
    anchor_area = anchor[2] * anchor[3]
    box_area = box[2] * box[3]
    if min(anchor_area, box_area) <= 0 or max(anchor_area, box_area) / min(anchor_area, box_area) > PERSON_SCALE_LIMIT:
        return True
    horizontal = anchor[0] + anchor[2] / 2 - box[0] - box[2] / 2
    vertical = anchor[1] + anchor[3] / 2 - box[1] - box[3] / 2
    return math.hypot(horizontal, vertical) > max(anchor[2], anchor[3]) * PERSON_MOVEMENT_FACTOR


def track_match_score(hit: DetectionHit, track: DetectionTrack, when: datetime) -> float:
    if track.label != hit.name or when - track.last > TRACK_MAX_AGE:
        return 0.0
    overlap = box_overlap(hit.box, track.box)
    if overlap >= TRACK_OVERLAP_THRESHOLD:
        return 2 + overlap
    if timedelta(0) < when - track.last <= TRACK_NEARBY_AGE:
        return box_proximity(hit.box, track.box)
    return 0.0


@dataclass(frozen=True)
class DetectionEvent:
    uid: str
    camera: str
    first: datetime
    last: datetime
    classes: tuple[str, ...]
    score: float
    clip: Path | None = None
    clip_start: datetime | None = None
    cover: bytes | None = field(default=None, repr=False, compare=False)


def encode_cover(frame: bytes, boxes: tuple[tuple[float, float, float, float], ...]) -> bytes:
    import cv2
    import numpy as np
    image = np.frombuffer(frame, np.uint8).reshape(SAMPLE_HEIGHT, SAMPLE_WIDTH, 3).copy()
    for left, top, width, height in boxes:
        cv2.rectangle(image, (round(left), round(top)), (round(left + width), round(top + height)),
                      COVER_BOX_COLOR, COVER_BOX_THICKNESS)
    encoded, data = cv2.imencode(".jpg", image, [cv2.IMWRITE_JPEG_QUALITY, COVER_JPEG_QUALITY])
    if not encoded:
        raise ValueError("The detection image could not be encoded.")
    return data.tobytes()


class EventTracker:
    def __init__(self, uid: str, camera: str) -> None:
        self.uid = uid
        self.camera = camera
        self.candidate: deque[tuple[datetime, dict[int, float]]] = deque()
        self.first: datetime | None = None
        self.last: datetime | None = None
        self.tracks: dict[int, DetectionTrack] = {}
        self.next_track_id = 0
        self.active_hits: dict[int, int] = {}
        self.score = 0.0
        self.cover_source: tuple[float, bytes, tuple[tuple[float, float, float, float], ...]] | None = None

    def _match(self, when: datetime, hits: list[DetectionHit]) -> dict[int, float]:
        for identifier, track in list(self.tracks.items()):
            if when - track.last > TRACK_MAX_AGE and identifier not in self.active_hits:
                del self.tracks[identifier]
        found = {}
        for hit in sorted(hits, key=lambda item: item.score, reverse=True):
            matches = (
                (track_match_score(hit, track, when), identifier)
                for identifier, track in self.tracks.items()
            )
            match_score, identifier = max(matches, default=(0.0, -1))
            if match_score <= 0:
                identifier = self.next_track_id
                self.next_track_id += 1
                self.tracks[identifier] = DetectionTrack(hit.name, hit.box, when, hit.box)
            track = self.tracks[identifier]
            track.box = hit.box
            track.last = when
            track.moved = track.moved or box_moved(track.anchor, hit.box)
            found[identifier] = max(found.get(identifier, 0.0), hit.score)
        return found

    def _relevant(self, identifier: int) -> bool:
        track = self.tracks.get(identifier)
        return track is not None and (track.label != PERSON_CLASS or track.moved)

    def _keep_cover(self, frame: bytes | None, relevant: dict[int, float]) -> None:
        score = max(relevant.values())
        if frame is not None and (self.cover_source is None or score > self.cover_source[0]):
            self.cover_source = (score, frame, tuple(self.tracks[identifier].box for identifier in relevant))

    def observe(self, when: datetime, hits: list[DetectionHit], frame: bytes | None = None) -> list[DetectionEvent]:
        completed = []
        if self.last is not None and (when - self.last).total_seconds() > EVENT_GAP_SECONDS:
            completed.append(self.finish())
        found = self._match(when, hits)
        relevant = {identifier: score for identifier, score in found.items() if self._relevant(identifier)}
        if self.first is not None:
            if not relevant:
                return completed
            self.last = when
            for identifier in relevant:
                self.active_hits[identifier] = self.active_hits.get(identifier, 0) + 1
            self.score = max(self.score, *relevant.values())
            self._keep_cover(frame, relevant)
            if (when - self.first).total_seconds() >= EVENT_MAX_SECONDS:
                completed.append(self.finish())
            return completed
        if not found:
            return completed
        while self.candidate and (when - self.candidate[0][0]).total_seconds() > EVENT_CONFIRM_SECONDS:
            self.candidate.popleft()
        for start, earlier in self.candidate:
            confirmed = {identifier: score for identifier, score in earlier.items() if self._relevant(identifier)}
            if confirmed.keys() & relevant.keys():
                self.first = start
                self.last = when
                self.active_hits = {identifier: int(identifier in confirmed) + int(identifier in relevant)
                                    for identifier in confirmed.keys() | relevant.keys()}
                self.score = max(*confirmed.values(), *relevant.values())
                self._keep_cover(frame, relevant)
                self.candidate.clear()
                return completed
        self.candidate.append((when, found))
        return completed

    def finish(self) -> DetectionEvent:
        assert self.first is not None and self.last is not None
        classes = tuple(sorted({self.tracks[identifier].label for identifier, count in self.active_hits.items()
                                if count >= 2 and identifier in self.tracks}))
        cover = None
        if self.cover_source is not None:
            try:
                cover = encode_cover(*self.cover_source[1:])
            except (ImportError, ValueError) as ex:
                LOG.warning("Could not keep the detection image for %s: %s", self.camera, ex)
        event = DetectionEvent(self.uid, self.camera, self.first, self.last, classes, self.score, cover=cover)
        self.first = None
        self.last = None
        self.active_hits.clear()
        self.score = 0.0
        self.cover_source = None
        self.candidate.clear()
        return event


def event_directory() -> Path:
    from PyQt6.QtCore import QStandardPaths
    videos = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MoviesLocation)
    directory = (Path(videos) if videos else Path.home() / "Videos") / "O-KAM Linux/Detections"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    return directory


def _camera_key(uid: str) -> str:
    return hashlib.sha256(uid.encode("utf-8")).hexdigest()[:16]


def _private_folder(root: Path, *parts: str) -> Path:
    folder = root
    for part in parts:
        folder = folder / part
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        folder.chmod(0o700)
    return folder


def _event_folder(root: Path, uid: str, day: datetime) -> Path:
    return _private_folder(root, day.strftime(EVENT_DAY_FORMAT), METADATA_FOLDER, _camera_key(uid))


def _current_classes(names: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    return tuple(sorted({ANIMAL_CLASS if name in LEGACY_ANIMAL_CLASSES else name for name in names}))


def _event_type(classes: tuple[str, ...]) -> str:
    names = set(classes)
    if not names or not names.issubset(CLASS_NAMES.values()):
        raise ValueError("Unknown local detection type.")
    return "_".join(sorted(names))


def _category_folder(root: Path, day: datetime, category: str) -> Path:
    return _private_folder(root, day.strftime(EVENT_DAY_FORMAT), category)


def _remove_empty_folders(root: Path, folder: Path) -> None:
    while folder != root and folder.is_relative_to(root):
        try:
            folder.rmdir()
        except OSError:
            return
        folder = folder.parent


def _move_clip(source: Path, destination: Path) -> None:
    if source.is_file():
        if destination.exists():
            raise FileExistsError(destination)
        source.replace(destination)
    elif not destination.is_file():
        raise FileNotFoundError(source)


def _clip_path(directory: Path, name: str) -> Path:
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Invalid local detection clip path.")
    return directory / relative


def _write_event_document(path: Path, document: dict) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix="intraswitch_camera_event_", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as output:
            json.dump(document, output, ensure_ascii=False)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save_event(path: Path, event: DetectionEvent, status: str) -> None:
    document = {
        "uid": event.uid, "camera": event.camera, "first": event.first.isoformat(),
        "last": event.last.isoformat(), "classes": event.classes, "score": event.score,
        "clip": event.clip.relative_to(event_directory()).as_posix() if event.clip is not None else None,
        "clip_start": event.clip_start.isoformat() if event.clip_start is not None else None,
        "cover": COVER_NAME if event.clip is not None and event.cover is not None else None,
        "status": status,
    }
    _write_event_document(path, document)


def organize_events() -> None:
    root = event_directory()
    documents = [
        *root.glob("*/*.json"), *root.glob(f"{METADATA_FOLDER}/*/*.json"),
        *root.glob(f"*/{METADATA_FOLDER}/*/*.json"),
    ]
    for metadata in documents:
        try:
            _organize_event(root, metadata)
        except (OSError, ValueError, KeyError, TypeError) as ex:
            LOG.warning("Could not organize local detection %s: %s", metadata, ex)


def _organize_event(root: Path, metadata: Path) -> None:
    document = json.loads(metadata.read_text(encoding="utf-8"))
    if metadata.parent.name != _camera_key(document["uid"]):
        raise ValueError("Invalid local detection camera folder.")
    day = datetime.fromisoformat(document["first"])
    classes = list(_current_classes(document["classes"]))
    changed = classes != document["classes"]
    document["classes"] = classes
    name = document.get("clip")
    if document["status"] == "ready" and name:
        source = _clip_path(metadata.parent if Path(name).parent == Path(".") else root, name)
        category = _event_type(tuple(classes))
        destination = _category_folder(root, day, category) / f"{category}_{source.name.removeprefix(f'{source.parent.name}_')}"
        if destination != source:
            _move_clip(source, destination)
            _remove_empty_folders(root, source.parent)
            document["clip"] = destination.relative_to(root).as_posix()
            changed = True
    if changed:
        _write_event_document(metadata, document)
    target = _event_folder(root, document["uid"], day) / metadata.name
    if target != metadata:
        if target.exists():
            raise FileExistsError(target)
        metadata.replace(target)
        _remove_empty_folders(root, metadata.parent)


def load_events(uid: str, now: datetime | None = None) -> list[DetectionEvent]:
    root = event_directory()
    current = now or datetime.now()
    cutoff = current - PLAYBACK_PERIOD
    days = [cutoff + timedelta(days=offset) for offset in range((current.date() - cutoff.date()).days + 1)]
    events = []
    for path in (path for day in days
                 for path in (root / day.strftime(EVENT_DAY_FORMAT) / METADATA_FOLDER / _camera_key(uid)).glob("*.json")):
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
            first = datetime.fromisoformat(document["first"])
            clip = _clip_path(root, document["clip"]) if document["clip"] else None
            if document["uid"] == uid and first >= cutoff and document["status"] == "ready" and clip is not None and clip.is_file():
                events.append(DetectionEvent(
                    uid, document["camera"], first, datetime.fromisoformat(document["last"]),
                    _current_classes(document["classes"]), float(document["score"]), clip,
                    datetime.fromisoformat(document["clip_start"]),
                ))
        except (OSError, ValueError, KeyError, TypeError):
            LOG.warning("Invalid local detection event %s", path)
    return sorted(events, key=lambda event: event.first)


def _run(
    command: list[str], timeout: int = 60, cancel: threading.Event | None = None,
) -> subprocess.CompletedProcess:
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    deadline = time.monotonic() + timeout
    try:
        while True:
            if cancel is not None and cancel.is_set():
                raise OSError("Local detection export interrupted.")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise subprocess.TimeoutExpired(command, timeout)
            try:
                stdout, stderr = process.communicate(timeout=min(0.5, remaining))
                if process.returncode:
                    raise subprocess.CalledProcessError(process.returncode, command, stdout, stderr)
                return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
            except subprocess.TimeoutExpired:
                continue
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)


def _duration(path: Path, cancel: threading.Event | None = None) -> float:
    if path.name.endswith(".mkv.h264"):
        return max(0.0, path.stat().st_mtime - _segment_start(path).timestamp())
    result = _run([
        "ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
        "packet=pts_time,duration_time", "-of", "csv=p=0", str(path),
    ], timeout=30, cancel=cancel)
    positions = []
    for line in result.stdout.splitlines():
        try:
            positions.append(float(line.split(",")[0]))
        except ValueError:
            continue
    return max(0.0, max(positions) - min(positions)) if positions else 0.0


def _segment_start(path: Path) -> datetime:
    return datetime.strptime(path.name.rsplit("_", 2)[-2] + "_" + path.name.rsplit("_", 2)[-1][:6], "%Y%m%d_%H%M%S")


def _source_segments(
    directory: Path, prefix: str, first: datetime, last: datetime,
    cancel: threading.Event | None = None,
) -> list[tuple[Path, datetime, float]]:
    choices = {}
    for path in directory.glob(f"{prefix}_*.mkv*"):
        if not (path.name.endswith(".mkv") or path.name.endswith(".mkv.h264")):
            continue
        try:
            start = _segment_start(path)
            if start > last or start < first - timedelta(minutes=11) or path.stat().st_size == 0:
                continue
        except (OSError, ValueError):
            continue
        key = path.name.removesuffix(".h264")
        if key not in choices or path.suffix == ".mkv":
            choices[key] = path
    segments = []
    for path in choices.values():
        try:
            duration = _duration(path, cancel)
            start = _segment_start(path)
            if start + timedelta(seconds=duration) > first and start < last:
                segments.append((path, start, duration))
        except (OSError, ValueError, subprocess.SubprocessError):
            LOG.warning("Could not inspect recording %s", path)
    return sorted(segments, key=lambda item: item[1])


def export_event(
    event: DetectionEvent, recordings: Path, prefix: str,
    cancel: threading.Event | None = None,
) -> DetectionEvent:
    first = event.first - timedelta(seconds=EVENT_MARGIN_SECONDS)
    last = event.last + timedelta(seconds=EVENT_MARGIN_SECONDS)
    segments = _source_segments(recordings, prefix, first, last, cancel)
    if not segments:
        raise OSError("No continuous recording covers the local detection.")
    category = _event_type(event.classes)
    clip_start = max(first, segments[0][1])
    folder = _category_folder(event_directory(), event.first, category)
    clip = folder / f"{category}_{prefix}_{clip_start:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}.mkv"
    with tempfile.TemporaryDirectory(prefix="intraswitch_camera_event_") as temporary:
        parts = []
        available_end = clip_start
        for path, start, duration in segments:
            left = max(first, start, available_end)
            right = min(last, start + timedelta(seconds=duration))
            if right <= left:
                continue
            part = Path(temporary) / f"part-{len(parts):03}.mkv"
            command = ["ffmpeg", "-nostdin", "-v", "error", "-y"]
            if path.name.endswith(".mkv.h264"):
                fps = max(1.0, min(60.0, _frame_count(path, cancel) / max(duration, 1)))
                command += ["-framerate", str(fps), "-f", "h264", "-i", str(path),
                            "-ss", str((left - start).total_seconds())]
            else:
                command += ["-ss", str((left - start).total_seconds()), "-i", str(path)]
            command += [
                "-t", str((right - left).total_seconds()), "-an", "-vf", "scale=1280:-2",
                "-r", "15", "-c:v", "libx264", "-preset", "ultrafast", "-crf", "25",
                "-threads", "2", str(part),
            ]
            _run(command, timeout=90, cancel=cancel)
            if not _has_video_packets(part, cancel):
                raise OSError("The local detection excerpt is empty.")
            parts.append(part)
            available_end = right
        if not parts or available_end < event.last:
            raise OSError("The continuous recording does not contain the detected event.")
        target = Path(temporary) / "event.mkv"
        if len(parts) == 1:
            shutil.copyfile(parts[0], target)
        else:
            playlist = Path(temporary) / "parts.txt"
            playlist.write_text("".join(f"file '{part}'\n" for part in parts))
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
                  "-i", str(playlist), "-c", "copy", str(target)], timeout=30, cancel=cancel)
        if not _has_video_packets(target, cancel):
            raise OSError("The local detection excerpt is empty.")
        covered = Path(temporary) / "covered.mkv"
        try:
            cover = attach_cover(target, covered, event.cover, _cover_offset(event.first, event.last, clip_start), cancel)
            target = covered
        except (OSError, ValueError, subprocess.SubprocessError) as ex:
            LOG.warning("Could not add the detection image to %s: %s", clip.name, ex)
            cover = None
        shutil.move(target, clip)
        clip.chmod(0o600)
    return DetectionEvent(event.uid, event.camera, event.first, event.last, event.classes, event.score, clip, clip_start,
                          cover)


def _cover_offset(first: datetime, last: datetime, clip_start: datetime) -> float:
    return max(0.0, (first + (last - first) / 2 - clip_start).total_seconds())


def attach_cover(
    clip: Path, output: Path, cover: bytes | None, offset: float, cancel: threading.Event | None = None,
) -> bytes:
    image = output.with_name(COVER_NAME)
    if cover is None:
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{offset:.3f}", "-i", str(clip),
              "-frames:v", "1", "-vf", f"scale={SAMPLE_WIDTH}:-2", "-q:v", "3", str(image)],
             timeout=30, cancel=cancel)
        cover = image.read_bytes()
        if not cover:
            raise OSError("The local detection excerpt has no image at the detection time.")
    else:
        image.write_bytes(cover)
    _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(clip), "-map", "0:V", "-c", "copy",
          "-attach", str(image), "-metadata:s:t:0", f"mimetype={COVER_MIME_TYPE}",
          "-metadata:s:t:0", f"filename={COVER_NAME}", str(output)], timeout=30, cancel=cancel)
    if not _has_video_packets(output, cancel):
        raise OSError("The local detection excerpt with its image is empty.")
    return cover


def add_missing_covers(cancel: threading.Event | None = None) -> None:
    root = event_directory()
    for temporary in root.glob(f"*/{METADATA_FOLDER}/{COVER_TEMPORARY_PREFIX}*"):
        shutil.rmtree(temporary, ignore_errors=True)
    for metadata in sorted(root.glob(f"*/{METADATA_FOLDER}/*/*.json")):
        if cancel is not None and cancel.is_set():
            return
        try:
            document = json.loads(metadata.read_text(encoding="utf-8"))
            if document["status"] != "ready" or not document["clip"] or document.get("cover"):
                continue
            clip = _clip_path(root, document["clip"])
            if not clip.is_file():
                continue
            offset = _cover_offset(datetime.fromisoformat(document["first"]), datetime.fromisoformat(document["last"]),
                                   datetime.fromisoformat(document["clip_start"]))
            with tempfile.TemporaryDirectory(prefix=COVER_TEMPORARY_PREFIX, dir=metadata.parent.parent) as temporary:
                output = Path(temporary) / clip.name
                attach_cover(clip, output, None, offset, cancel)
                output.chmod(0o600)
                os.replace(output, clip)
            document["cover"] = COVER_NAME
            _write_event_document(metadata, document)
            LOG.info("Added the detection image to %s", clip.name)
        except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as ex:
            LOG.warning("Could not add the detection image to %s: %s", metadata, ex)


def _frame_count(path: Path, cancel: threading.Event | None = None) -> int:
    result = _run([
        "ffprobe", "-v", "error", "-count_packets", "-show_entries", "stream=nb_read_packets",
        "-of", "csv=p=0", "-f", "h264", str(path),
    ], timeout=30, cancel=cancel)
    return int(result.stdout.strip().split(",")[0])


def _has_video_packets(path: Path, cancel: threading.Event | None = None) -> bool:
    if not path.is_file() or path.stat().st_size == 0:
        return False
    try:
        result = _run([
            "ffprobe", "-v", "error", "-count_packets", "-select_streams", "v:0",
            "-show_entries", "stream=nb_read_packets", "-of", "csv=p=0", str(path),
        ], timeout=30, cancel=cancel)
        return int(result.stdout.strip().split(",")[0]) > 0
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        return False


class DetectionEngine:
    def __init__(self, recordings: Path) -> None:
        self.recordings = recordings
        self.condition = threading.Condition()
        self.latest: OrderedDict[str, tuple[DetectionPipeline, bytes, datetime]] = OrderedDict()
        self.trackers: dict[str, EventTracker] = {}
        self.processed: dict[str, int] = {}
        self.exports: queue.Queue[tuple[Path, DetectionEvent, str, Callable[[DetectionEvent], None]]] = queue.Queue(maxsize=32)
        self.scheduled_paths: set[Path] = set()
        self.stopped = threading.Event()
        self.inference = threading.Thread(target=self._infer, daemon=True)
        self.exporter = threading.Thread(target=self._export, daemon=True)
        self.inference.start()
        self.exporter.start()

    def submit(self, pipeline: DetectionPipeline, frame: bytes, when: datetime) -> None:
        with self.condition:
            if self.stopped.is_set():
                return
            self.latest[pipeline.uid] = (pipeline, frame, when)
            self.latest.move_to_end(pipeline.uid)
            self.condition.notify()

    def remove(self, pipeline: DetectionPipeline) -> None:
        with self.condition:
            current = self.latest.get(pipeline.uid)
            if current is not None and current[0] is pipeline:
                del self.latest[pipeline.uid]
            tracker = self.trackers.pop(pipeline.uid, None)
        if tracker is not None and tracker.first is not None:
            self.schedule(tracker.finish(), pipeline.prefix, pipeline.on_event)

    def schedule(self, event: DetectionEvent, prefix: str, callback: Callable[[DetectionEvent], None]) -> None:
        directory = _event_folder(event_directory(), event.uid, event.first)
        identifier = f"{event.first:%Y%m%d_%H%M%S}_{uuid.uuid4().hex[:8]}"
        path = directory / f"{identifier}.json"
        save_event(path, event, "pending")
        self._enqueue(path, event, prefix, callback)

    def _enqueue(self, path: Path, event: DetectionEvent, prefix: str, callback: Callable[[DetectionEvent], None]) -> None:
        with self.condition:
            if path in self.scheduled_paths:
                return
            self.scheduled_paths.add(path)
        try:
            self.exports.put_nowait((path, event, prefix, callback))
        except queue.Full:
            with self.condition:
                self.scheduled_paths.discard(path)
            LOG.warning("Local detection export queue is full; event remains pending: %s", path)

    def recover(self, pipeline: DetectionPipeline) -> None:
        for path in event_directory().glob(f"*/{METADATA_FOLDER}/{_camera_key(pipeline.uid)}/*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                if data["status"] != "pending" or data["uid"] != pipeline.uid:
                    continue
                event = DetectionEvent(
                    pipeline.uid, data["camera"], datetime.fromisoformat(data["first"]),
                    datetime.fromisoformat(data["last"]), _current_classes(data["classes"]), float(data["score"]),
                )
                if datetime.now() - event.last > RECORDING_RETENTION:
                    path.unlink()
                    LOG.info("Discarded local detection %s because its recording is no longer kept", path)
                    continue
                self._enqueue(path, event, pipeline.prefix, pipeline.on_event)
            except (OSError, ValueError, KeyError):
                LOG.warning("Could not recover local detection %s", path)

    def _infer(self) -> None:
        try:
            detector = YoloXDetector(model_path())
        except (OSError, ImportError, ValueError) as ex:
            LOG.error("Local detection model is unavailable: %s", ex)
            self.stopped.set()
            return
        while not self.stopped.is_set():
            with self.condition:
                while not self.latest and not self.stopped.is_set():
                    self.condition.wait(timeout=1)
                if self.stopped.is_set():
                    break
                _, (pipeline, frame, when) = self.latest.popitem(last=False)
            if pipeline.stopped.is_set():
                continue
            try:
                found = detector.infer(frame)
            except Exception as ex:
                LOG.error("Local detection inference stopped: %s", ex)
                pipeline.on_error(f"Local detection stopped: {ex}")
                self.stopped.set()
                return
            with self.condition:
                tracker = self.trackers.setdefault(pipeline.uid, EventTracker(pipeline.uid, pipeline.camera))
                completed = tracker.observe(when, found, frame)
            for event in completed:
                self.schedule(event, pipeline.prefix, pipeline.on_event)
            self.processed[pipeline.uid] = self.processed.get(pipeline.uid, 0) + 1
            if self.processed[pipeline.uid] % 30 == 0:
                LOG.info("Local detection processed %d frames for %s", self.processed[pipeline.uid], pipeline.camera)

    def _export(self) -> None:
        while not self.stopped.is_set():
            try:
                path, event, prefix, callback = self.exports.get(timeout=1)
            except queue.Empty:
                continue
            try:
                for attempt in range(4):
                    if self.stopped.is_set():
                        break
                    try:
                        finished = export_event(event, self.recordings, prefix, self.stopped)
                        save_event(path, finished, "ready")
                        LOG.info("Saved local detection %s", finished.clip.name)
                        try:
                            callback(finished)
                        except RuntimeError:
                            pass
                        break
                    except (OSError, ValueError, subprocess.SubprocessError) as ex:
                        LOG.warning("Could not export local detection %s (attempt %d): %s", path, attempt + 1, ex)
                        if attempt < 3:
                            self.stopped.wait(3)
            finally:
                with self.condition:
                    self.scheduled_paths.discard(path)

    def close(self) -> None:
        self.stopped.set()
        with self.condition:
            self.condition.notify_all()
        self.inference.join(timeout=2)
        self.exporter.join(timeout=2)


class DetectionPipeline:
    def __init__(
        self, engine: DetectionEngine, uid: str, camera: str, prefix: str,
        input_args: list[str], on_event: Callable[[DetectionEvent], None],
        on_error: Callable[[str], None], input_descriptor: int | None = None,
        raw_input: bool = False,
    ) -> None:
        self.engine = engine
        self.uid = uid
        self.camera = camera
        self.prefix = prefix
        self.on_event = on_event
        self.on_error = on_error
        self.stopped = threading.Event()
        self.pending: queue.Queue[bytes | None] | None = queue.Queue(maxsize=16) if raw_input else None
        self.needs_keyframe = raw_input
        command = [
            "ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-probesize", "65536",
            "-analyzeduration", "1000000", *input_args, "-an", "-sn", "-vf",
            "fps=1,scale=640:360:flags=fast_bilinear", "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
        ]
        try:
            self.process = subprocess.Popen(
                command, stdin=subprocess.PIPE if raw_input else subprocess.DEVNULL,
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
                pass_fds=(input_descriptor,) if input_descriptor is not None else (),
            )
        finally:
            if input_descriptor is not None:
                os.close(input_descriptor)
        self.reader = threading.Thread(target=self._read, daemon=True)
        self.writer = threading.Thread(target=self._write, daemon=True) if raw_input else None
        self.reader.start()
        if self.writer is not None:
            self.writer.start()
        self.engine.recover(self)

    def feed(self, frame: bytes, keyframe: bool) -> None:
        pending = self.pending
        if pending is None or self.stopped.is_set():
            return
        if self.needs_keyframe and not keyframe:
            return
        try:
            pending.put_nowait(frame)
            self.needs_keyframe = False
        except queue.Full:
            self.needs_keyframe = True
            while True:
                try:
                    pending.get_nowait()
                except queue.Empty:
                    break
            if keyframe:
                pending.put_nowait(frame)
                self.needs_keyframe = False

    def _write(self) -> None:
        assert self.pending is not None and self.process.stdin is not None
        try:
            while not self.stopped.is_set():
                try:
                    frame = self.pending.get(timeout=0.5)
                except queue.Empty:
                    continue
                if frame is None:
                    break
                remaining = memoryview(frame)
                while remaining:
                    written = self.process.stdin.write(remaining)
                    if written is None or written == 0:
                        raise OSError("The local detection decoder stopped accepting video.")
                    remaining = remaining[written:]
        except (BrokenPipeError, OSError):
            if not self.stopped.is_set():
                self.on_error(f"Local detection decoder stopped for {self.camera}.")

    def _read(self) -> None:
        assert self.process.stdout is not None
        try:
            while not self.stopped.is_set():
                remaining = SAMPLE_BYTES
                parts = []
                while remaining:
                    data = self.process.stdout.read(remaining)
                    if not data:
                        return
                    parts.append(data)
                    remaining -= len(data)
                self.engine.submit(self, b"".join(parts), datetime.now())
        except OSError as ex:
            if not self.stopped.is_set():
                LOG.warning("Local detection decoder failed for %s: %s", self.camera, ex)

    def close(self) -> None:
        self.stopped.set()
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        self.reader.join(timeout=2)
        if self.writer is not None:
            self.writer.join(timeout=2)
        if self.process.stdin is not None:
            self.process.stdin.close()
        if self.process.stdout is not None:
            self.process.stdout.close()
        self.engine.remove(self)


if __name__ == "__main__":
    import sys
    if sys.argv[1:] != ["--install-model"]:
        raise SystemExit("Usage: python local_detection.py --install-model")
    print(install_model())
