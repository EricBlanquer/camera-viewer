import tempfile
import unittest
from pathlib import Path

import cv2
import numpy as np

from camera_position import (
    CHECK_SECONDS, CONFIRM_SECONDS, CORRECTION_FALLBACK_SECONDS, CORRECTION_LIMIT_SECONDS, CORRECTION_START_SECONDS,
    DAY, EXPIRED, IMAGE_HEIGHT, IMAGE_WIDTH, MATCHED, MISPLACED, NIGHT, PENDING, SAVED, SETTLE_SECONDS, SUSPECTED,
    PositionCheck, PositionCorrection, PositionReferences, frame_lighting, position_image, position_matches,
)
from local_detection import SAMPLE_HEIGHT, SAMPLE_WIDTH, camera_key

SCENE_WIDTH = 1400


def scene(seed: int) -> np.ndarray:
    generator = np.random.default_rng(seed)
    image = np.full((SAMPLE_HEIGHT, SCENE_WIDTH), 90, np.uint8)
    for _ in range(160):
        x, y = int(generator.integers(0, SCENE_WIDTH)), int(generator.integers(0, SAMPLE_HEIGHT))
        width, height = int(generator.integers(10, 90)), int(generator.integers(10, 90))
        cv2.rectangle(image, (x, y), (x + width, y + height), int(generator.integers(0, 255)), -1)
    return image


def frame(image: np.ndarray, left: int, night: bool = True, gain: float = 1.0) -> bytes:
    view = np.clip(image[:, left:left + SAMPLE_WIDTH].astype(np.float32) * gain, 0, 255).astype(np.uint8)
    color = cv2.cvtColor(view, cv2.COLOR_GRAY2BGR)
    if not night:
        color[..., 0] = np.clip(color[..., 0].astype(np.int16) - 40, 0, 255).astype(np.uint8)
        color[..., 2] = np.clip(color[..., 2].astype(np.int16) + 40, 0, 255).astype(np.uint8)
    cv2.putText(color, "2026-10-28 19:01:37", (10, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2)
    return color.tobytes()


class CameraPositionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_position_test_")
        self.references = PositionReferences(Path(self.directory.name))
        self.garden = scene(1)

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_frames_from_the_same_position_match_despite_lighting_changes(self) -> None:
        reference = position_image(frame(self.garden, 300))
        self.assertTrue(position_matches(reference, position_image(frame(self.garden, 300, gain=0.6))))
        self.assertTrue(position_matches(reference, position_image(frame(self.garden, 304))))

    def test_frames_from_another_position_or_scene_do_not_match(self) -> None:
        reference = position_image(frame(self.garden, 300))
        self.assertFalse(position_matches(reference, position_image(frame(self.garden, 500))))
        self.assertFalse(position_matches(reference, position_image(frame(scene(2), 300))))

    def test_infrared_frames_are_night_frames(self) -> None:
        self.assertEqual(frame_lighting(frame(self.garden, 0)), NIGHT)
        self.assertEqual(frame_lighting(frame(self.garden, 0, night=False)), DAY)
        self.assertEqual(position_image(frame(self.garden, 0)).shape, (IMAGE_HEIGHT, IMAGE_WIDTH))

    def test_first_still_view_becomes_the_reference(self) -> None:
        check = PositionCheck("garden", "Preset 4", 100, False, self.references)
        self.assertEqual(check.observe((frame(self.garden, 300), 100 + SETTLE_SECONDS - 1), 103), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 104), 104), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 105), 105), SAVED)
        folder = Path(self.directory.name) / camera_key("garden")
        self.assertEqual(sorted(path.name for path in folder.iterdir()), ["preset_4_night.png"])
        self.assertEqual(folder.stat().st_mode & 0o777, 0o700)
        self.assertIsNone(self.references.load("garden", "Preset 4", DAY))
        self.assertIsNotNone(self.references.load("garden", "Preset 4", NIGHT))

    def test_still_view_matching_the_reference_ends_the_check(self) -> None:
        self.references.save("garden", "Preset 4", NIGHT, position_image(frame(self.garden, 300)))
        check = PositionCheck("garden", "Preset 4", 100, False, self.references)
        self.assertEqual(check.observe((frame(self.garden, 100), 104), 104), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 105), 105), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 106), 106), MATCHED)

    def test_wrong_view_is_reported_after_it_stays_still(self) -> None:
        self.references.save("garden", "Preset 4", NIGHT, position_image(frame(self.garden, 300)))
        check = PositionCheck("garden", "Preset 4", 100, False, self.references)
        wrong = frame(self.garden, 700)
        self.assertEqual(check.observe((wrong, 104), 104), PENDING)
        self.assertEqual(check.observe((wrong, 105), 105), SUSPECTED)
        self.assertEqual(check.observe((wrong, 105), 106), SUSPECTED)
        self.assertEqual(check.observe((wrong, 105 + CONFIRM_SECONDS - 1), 117), SUSPECTED)
        self.assertEqual(check.observe((frame(self.garden, 500), 117.5), 118), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 118), 118), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 119), 119), MATCHED)
        check = PositionCheck("garden", "Preset 4", 200, False, self.references)
        self.assertEqual(check.observe((wrong, 204), 204), PENDING)
        self.assertEqual(check.observe((wrong, 205), 205), SUSPECTED)
        self.assertEqual(check.observe((wrong, 205 + CONFIRM_SECONDS), 217), MISPLACED)

    def test_view_after_a_correction_replaces_the_reference(self) -> None:
        self.references.save("garden", "Preset 4", NIGHT, position_image(frame(self.garden, 700)))
        check = PositionCheck("garden", "Preset 4", 100, True, self.references)
        self.assertEqual(check.observe((frame(self.garden, 300), 104), 104), PENDING)
        self.assertEqual(check.observe((frame(self.garden, 300), 105), 105), SAVED)
        reference = self.references.load("garden", "Preset 4", NIGHT)
        self.assertTrue(position_matches(reference, position_image(frame(self.garden, 300))))

    def test_check_without_a_still_view_expires(self) -> None:
        check = PositionCheck("garden", "Preset 4", 100, False, self.references)
        self.assertEqual(check.observe(None, 100 + CHECK_SECONDS), PENDING)
        self.assertEqual(check.observe(None, 100 + CHECK_SECONDS + 1), EXPIRED)

    def test_unreadable_reference_is_ignored(self) -> None:
        folder = Path(self.directory.name) / camera_key("garden")
        folder.mkdir()
        (folder / "preset_4_night.png").write_bytes(b"invalid")
        cv2.imwrite(str(folder / "preset_5_night.png"), np.zeros((10, 10), np.uint8))
        self.assertIsNone(self.references.load("garden", "Preset 4", NIGHT))
        self.assertIsNone(self.references.load("garden", "Preset 5", NIGHT))

    def test_correction_ends_when_the_camera_reports_it_finished(self) -> None:
        correction = PositionCorrection("Preset 4", False, 100)
        self.assertFalse(correction.finished({"center_status": "0"}, 102))
        self.assertEqual(correction.next_status_at, 104)
        self.assertFalse(correction.finished({"center_status": "1"}, 104))
        self.assertFalse(correction.finished({"center_status": "1"}, 120))
        self.assertTrue(correction.finished({"center_status": "0"}, 122))
        correction = PositionCorrection(None, True, 100)
        self.assertFalse(correction.finished({"center_status": "0"}, 100 + CORRECTION_START_SECONDS - 1))
        self.assertTrue(correction.finished({"center_status": "0"}, 100 + CORRECTION_START_SECONDS))

    def test_correction_without_reported_progress_waits_a_fixed_time(self) -> None:
        correction = PositionCorrection(None, True, 100)
        self.assertFalse(correction.finished(None, 100 + CORRECTION_FALLBACK_SECONDS - 1))
        self.assertTrue(correction.finished({}, 100 + CORRECTION_FALLBACK_SECONDS))
        correction = PositionCorrection(None, True, 100)
        self.assertTrue(correction.finished({"center_status": "1"}, 100 + CORRECTION_LIMIT_SECONDS))


if __name__ == "__main__":
    unittest.main()
