from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from local_detection import SAMPLE_HEIGHT, SAMPLE_WIDTH, camera_key, private_folder

IMAGE_WIDTH = 320
IMAGE_HEIGHT = 180
CLOCK_WIDTH = 160
CLOCK_HEIGHT = 16
NIGHT_COLOR_LIMIT = 10.0
DAY = "day"
NIGHT = "night"
SHIFT_LIMIT = 0.05
STILL_SHIFT_LIMIT = 0.01
STILL_RESPONSE = 0.5
MATCH_RESPONSE = 0.2
MATCH_FEATURES = 40
FEATURE_COUNT = 800
FEATURE_THRESHOLD = 10
FEATURE_RATIO = 0.8
FEATURE_TOLERANCE = 4.0
SETTLE_SECONDS = 3.0
CONFIRM_SECONDS = 12.0
CHECK_SECONDS = 60.0
PENDING = "pending"
SUSPECTED = "suspected"
MATCHED = "matched"
SAVED = "saved"
MISPLACED = "misplaced"
EXPIRED = "expired"
CORRECTION_STATUS_FIELD = "center_status"
CORRECTION_ACTIVE = "1"
CORRECTION_STATUS_SECONDS = 2.0
CORRECTION_START_SECONDS = 8.0
CORRECTION_FALLBACK_SECONDS = 30.0
CORRECTION_LIMIT_SECONDS = 120.0
REFERENCE_SUFFIX = ".png"
TEMPORARY_SUFFIX = ".tmp.png"


@dataclass(frozen=True)
class Offset:
    horizontal: float
    vertical: float
    response: float

    def within(self, limit: float) -> bool:
        return abs(self.horizontal) <= limit and abs(self.vertical) <= limit


def position_image(frame: bytes) -> np.ndarray:
    source = np.frombuffer(frame, np.uint8).reshape(SAMPLE_HEIGHT, SAMPLE_WIDTH, 3)
    image = cv2.resize(cv2.cvtColor(source, cv2.COLOR_BGR2GRAY), (IMAGE_WIDTH, IMAGE_HEIGHT), interpolation=cv2.INTER_AREA)
    image[:CLOCK_HEIGHT, :CLOCK_WIDTH] = 0
    return image


def frame_lighting(frame: bytes) -> str:
    source = np.frombuffer(frame, np.uint8).reshape(SAMPLE_HEIGHT, SAMPLE_WIDTH, 3).astype(np.int16)
    color = np.abs(source[..., 0] - source[..., 1]).mean() + np.abs(source[..., 1] - source[..., 2]).mean()
    return NIGHT if color < NIGHT_COLOR_LIMIT else DAY


def _edges(image: np.ndarray) -> np.ndarray:
    values = image.astype(np.float32)
    return cv2.magnitude(cv2.Sobel(values, cv2.CV_32F, 1, 0), cv2.Sobel(values, cv2.CV_32F, 0, 1))


def image_offset(reference: np.ndarray, current: np.ndarray) -> Offset:
    window = cv2.createHanningWindow((IMAGE_WIDTH, IMAGE_HEIGHT), cv2.CV_32F)
    (horizontal, vertical), response = cv2.phaseCorrelate(_edges(reference), _edges(current), window)
    return Offset(horizontal / IMAGE_WIDTH, vertical / IMAGE_HEIGHT, response)


def _features_match(reference: np.ndarray, current: np.ndarray) -> bool:
    mask = np.full(reference.shape, 255, np.uint8)
    mask[:CLOCK_HEIGHT, :CLOCK_WIDTH] = 0
    features = cv2.ORB_create(nfeatures=FEATURE_COUNT, fastThreshold=FEATURE_THRESHOLD)
    reference_points, reference_descriptors = features.detectAndCompute(reference, mask)
    current_points, current_descriptors = features.detectAndCompute(current, mask)
    if reference_descriptors is None or current_descriptors is None:
        return False
    pairs = cv2.BFMatcher(cv2.NORM_HAMMING).knnMatch(reference_descriptors, current_descriptors, k=2)
    good = [pair[0] for pair in pairs if len(pair) == 2 and pair[0].distance < FEATURE_RATIO * pair[1].distance]
    if len(good) < MATCH_FEATURES:
        return False
    source = np.float32([reference_points[match.queryIdx].pt for match in good])
    target = np.float32([current_points[match.trainIdx].pt for match in good])
    matrix, inliers = cv2.estimateAffinePartial2D(source, target, method=cv2.RANSAC, ransacReprojThreshold=FEATURE_TOLERANCE)
    return (
        matrix is not None and int(inliers.sum()) >= MATCH_FEATURES
        and abs(matrix[0, 2]) / IMAGE_WIDTH <= SHIFT_LIMIT and abs(matrix[1, 2]) / IMAGE_HEIGHT <= SHIFT_LIMIT
    )


def position_matches(reference: np.ndarray, current: np.ndarray) -> bool:
    offset = image_offset(reference, current)
    return offset.response >= MATCH_RESPONSE and offset.within(SHIFT_LIMIT) or _features_match(reference, current)


def images_still(previous: np.ndarray, current: np.ndarray) -> bool:
    offset = image_offset(previous, current)
    return offset.response >= STILL_RESPONSE and offset.within(STILL_SHIFT_LIMIT)


class PositionReferences:
    def __init__(self, root: Path) -> None:
        self.root = root

    def _name(self, preset: str, lighting: str) -> str:
        return f"{preset.lower().replace(' ', '_')}_{lighting}"

    def load(self, uid: str, preset: str, lighting: str) -> np.ndarray | None:
        path = self.root / camera_key(uid) / f"{self._name(preset, lighting)}{REFERENCE_SUFFIX}"
        if not path.is_file():
            return None
        image = cv2.imread(str(path), cv2.IMREAD_GRAYSCALE)
        return image if image is not None and image.shape == (IMAGE_HEIGHT, IMAGE_WIDTH) else None

    def save(self, uid: str, preset: str, lighting: str, image: np.ndarray) -> None:
        folder = private_folder(self.root, camera_key(uid))
        name = self._name(preset, lighting)
        temporary = folder / f"{name}{TEMPORARY_SUFFIX}"
        if not cv2.imwrite(str(temporary), image):
            raise OSError(f"Unable to write {temporary}")
        os.replace(temporary, folder / f"{name}{REFERENCE_SUFFIX}")


class PositionCheck:
    def __init__(self, uid: str, preset: str, started: float, trusted: bool, references: PositionReferences) -> None:
        self.uid = uid
        self.preset = preset
        self.started = started
        self.trusted = trusted
        self.references = references
        self.previous: np.ndarray | None = None
        self.previous_at = started + SETTLE_SECONDS
        self.still_since: float | None = None
        self.result = PENDING

    def observe(self, sample: tuple[bytes, float] | None, now: float) -> str:
        if now - self.started > CHECK_SECONDS:
            return EXPIRED
        if sample is None or sample[1] <= self.previous_at:
            return self.result
        frame, captured = sample
        image = position_image(frame)
        previous, self.previous, self.previous_at = self.previous, image, captured
        if previous is None or not images_still(previous, image):
            self.still_since = None
            self.result = PENDING
            return self.result
        if self.still_since is None:
            self.still_since = captured
        lighting = frame_lighting(frame)
        reference = None if self.trusted else self.references.load(self.uid, self.preset, lighting)
        if reference is None:
            self.references.save(self.uid, self.preset, lighting, image)
            self.result = SAVED
        elif position_matches(reference, image):
            self.result = MATCHED
        else:
            self.result = MISPLACED if captured - self.still_since >= CONFIRM_SECONDS else SUSPECTED
        return self.result


class PositionCorrection:
    def __init__(self, preset: str | None, automatic: bool, started: float) -> None:
        self.preset = preset
        self.automatic = automatic
        self.started = started
        self.next_status_at = started + CORRECTION_STATUS_SECONDS
        self.active_seen = False

    def finished(self, status: dict[str, str] | None, now: float) -> bool:
        self.next_status_at = now + CORRECTION_STATUS_SECONDS
        elapsed = now - self.started
        if elapsed >= CORRECTION_LIMIT_SECONDS:
            return True
        value = (status or {}).get(CORRECTION_STATUS_FIELD)
        if value is None:
            return elapsed >= CORRECTION_FALLBACK_SECONDS
        if value == CORRECTION_ACTIVE:
            self.active_seen = True
            return False
        return self.active_seen or elapsed >= CORRECTION_START_SECONDS
