import io
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from local_detection import (
    SAMPLE_BYTES, SAMPLE_HEIGHT, SAMPLE_WIDTH, DetectionPipeline, detection_video_filter,
    hardware_decode_input_args,
)

RENDER_NODE = "/dev/dri/renderD128"
CROP_THREE = (
    "[s0]crop=iw:ih/3:0:0*ih/3[l0];[s1]crop=iw:ih/3:0:1*ih/3[l1];[s2]crop=iw:ih/3:0:2*ih/3[l2]"
)
STACK_TAIL = (
    "[l0][l1][l2]xstack=inputs=3:layout=0_0|w0_0|0_h0:fill=black,"
    "pad=iw*2/2:ih*2/2:0:0:black,scale=640:360:flags=fast_bilinear"
)
SOFTWARE_PREAMBLE = [
    "ffmpeg", "-nostdin", "-v", "error", "-threads", "2", "-probesize", "65536",
    "-analyzeduration", "1000000",
]
HARDWARE_PREAMBLE = SOFTWARE_PREAMBLE + [
    "-hwaccel", "vaapi", "-hwaccel_device", RENDER_NODE, "-hwaccel_output_format", "vaapi",
]
OUTPUT_TAIL = ["-an", "-sn", "-vf"]


class _FakeProcess:
    """Enough of Popen for DetectionPipeline to start, read nothing and close."""

    def __init__(self) -> None:
        self.stdin = None
        self.stdout = io.BytesIO()
        self.terminated = False

    def poll(self) -> None:
        return None

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.terminated = True

    def wait(self, timeout: float | None = None) -> int:
        return 0


def _captured_command(input_args: list[str], device: str | None, lenses: int) -> list[str]:
    recorded: list[list[str]] = []

    def popen(command, **kwargs):
        recorded.append(list(command))
        return _FakeProcess()

    engine = SimpleNamespace(
        recover=lambda pipeline: None, remove=lambda pipeline: None,
        submit=lambda pipeline, frame, when: None,
    )
    with patch("local_detection.hardware_decode_device", return_value=device), \
            patch("local_detection.subprocess.Popen", side_effect=popen):
        pipeline = DetectionPipeline(
            engine, "camera", "House", "House", list(input_args),
            lambda event: None, lambda error: None, stacked_lenses=lenses,
        )
    pipeline.close()
    assert len(recorded) == 1, recorded
    return recorded[0]


class DetectionDecodeArgvTest(unittest.TestCase):
    """The detection feed used to decode every frame on the processor to keep one a second."""

    def test_hardware_input_args_name_the_probed_render_node(self) -> None:
        self.assertEqual(hardware_decode_input_args(RENDER_NODE), [
            "-hwaccel", "vaapi", "-hwaccel_device", RENDER_NODE, "-hwaccel_output_format", "vaapi",
        ])

    def test_software_fallback_adds_no_input_args(self) -> None:
        self.assertEqual(hardware_decode_input_args(None), [])
        self.assertEqual(hardware_decode_input_args(""), [])

    def test_single_lens_hardware_filter_downloads_after_the_rate_drop(self) -> None:
        self.assertEqual(
            detection_video_filter(1, RENDER_NODE),
            "fps=1,hwdownload,format=nv12,scale=640:360:flags=fast_bilinear",
        )

    def test_single_lens_software_filter_is_unchanged(self) -> None:
        self.assertEqual(detection_video_filter(1, None), "fps=1,scale=640:360:flags=fast_bilinear")
        self.assertEqual(detection_video_filter(1), detection_video_filter(1, None))

    def test_three_lens_hardware_filter_keeps_the_crop_and_converts_to_yuv420p(self) -> None:
        self.assertEqual(
            detection_video_filter(3, RENDER_NODE),
            "fps=1,hwdownload,format=nv12,format=yuv420p,split=3[s0][s1][s2];"
            f"{CROP_THREE};{STACK_TAIL}",
        )

    def test_three_lens_software_filter_is_unchanged(self) -> None:
        self.assertEqual(
            detection_video_filter(3, None),
            f"fps=1,split=3[s0][s1][s2];{CROP_THREE};{STACK_TAIL}",
        )
        self.assertEqual(detection_video_filter(3), detection_video_filter(3, None))

    def test_scale_vaapi_stays_out_of_the_detection_filter(self) -> None:
        """Its kernel smooths far more than fast_bilinear, which changes what the model sees."""
        for lenses in (1, 3):
            self.assertNotIn("scale_vaapi", detection_video_filter(lenses, RENDER_NODE))

    def test_hardware_command_pins_the_whole_argv_for_a_single_lens(self) -> None:
        self.assertEqual(
            _captured_command(["-i", "http://127.0.0.1:9/feed"], RENDER_NODE, 1),
            HARDWARE_PREAMBLE + ["-i", "http://127.0.0.1:9/feed"] + OUTPUT_TAIL + [
                "fps=1,hwdownload,format=nv12,scale=640:360:flags=fast_bilinear",
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
        )

    def test_software_command_pins_the_whole_argv_for_a_single_lens(self) -> None:
        self.assertEqual(
            _captured_command(["-i", "http://127.0.0.1:9/feed"], None, 1),
            SOFTWARE_PREAMBLE + ["-i", "http://127.0.0.1:9/feed"] + OUTPUT_TAIL + [
                "fps=1,scale=640:360:flags=fast_bilinear",
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
        )

    def test_hardware_command_pins_the_whole_argv_for_three_stacked_lenses(self) -> None:
        input_args = ["-analyzeduration", "10000000", "-probesize", "50000000", "-i", "rtsp://127.0.0.1:8554/v380"]
        self.assertEqual(
            _captured_command(input_args, RENDER_NODE, 3),
            HARDWARE_PREAMBLE + input_args + OUTPUT_TAIL + [
                "fps=1,hwdownload,format=nv12,format=yuv420p,split=3[s0][s1][s2];"
                f"{CROP_THREE};{STACK_TAIL}",
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
        )

    def test_software_command_pins_the_whole_argv_for_three_stacked_lenses(self) -> None:
        input_args = ["-analyzeduration", "10000000", "-probesize", "50000000", "-i", "rtsp://127.0.0.1:8554/v380"]
        self.assertEqual(
            _captured_command(input_args, None, 3),
            SOFTWARE_PREAMBLE + input_args + OUTPUT_TAIL + [
                f"fps=1,split=3[s0][s1][s2];{CROP_THREE};{STACK_TAIL}",
                "-pix_fmt", "bgr24", "-f", "rawvideo", "pipe:1",
            ],
        )

    def test_the_hardware_argv_only_appears_once_the_probe_found_a_node(self) -> None:
        command = _captured_command(["-i", "pipe:0"], None, 1)
        self.assertNotIn("-hwaccel", command)
        self.assertNotIn("hwdownload", " ".join(command))


def _render_node() -> str | None:
    from hardware import hardware_decode_device
    return hardware_decode_device()


class DetectionDecodeFrameTest(unittest.TestCase):
    """Both paths have to hand the detector the same picture, not merely the same shape."""

    LENS_COLORS = ("red", "lime", "blue")

    @classmethod
    def setUpClass(cls) -> None:
        cls.device = _render_node()
        if cls.device is None:
            raise unittest.SkipTest("no usable VAAPI render node on this host")
        cls.directory = tempfile.TemporaryDirectory()
        cls.clips = {}
        for lenses in (1, 3):
            path = Path(cls.directory.name) / f"lenses{lenses}.mkv"
            sources = ";".join(
                f"color=c={color}:s=192x108:r=5:d=4[c{lens}]"
                for lens, color in enumerate(cls.LENS_COLORS[:lenses])
            )
            stack = "".join(f"[c{lens}]" for lens in range(lenses))
            graph = f"{sources};{stack}vstack=inputs={lenses}[out0]" if lenses > 1 else f"{sources};[c0]null[out0]"
            subprocess.run([
                "ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", graph,
                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                "-g", "5", "-threads", "1", str(path),
            ], check=True, capture_output=True, timeout=60)
            cls.clips[lenses] = path

    @classmethod
    def tearDownClass(cls) -> None:
        if getattr(cls, "directory", None) is not None:
            cls.directory.cleanup()

    def _frames(self, lenses: int, device: str | None) -> list[bytes]:
        collected: list[bytes] = []
        engine = SimpleNamespace(
            recover=lambda pipeline: None, remove=lambda pipeline: None,
            submit=lambda pipeline, frame, when: collected.append(frame),
        )
        with patch("local_detection.hardware_decode_device", return_value=device):
            pipeline = DetectionPipeline(
                engine, "camera", "House", "House", ["-i", str(self.clips[lenses])],
                lambda event: None, lambda error: self.fail(error), stacked_lenses=lenses,
            )
        self.addCleanup(pipeline.close)
        deadline = time.monotonic() + 30
        while len(collected) < 2 and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertGreaterEqual(len(collected), 1, "the decoder produced no frame")
        for frame in collected:
            self.assertEqual(len(frame), SAMPLE_BYTES)
        return collected

    def _color(self, frame: bytes, column: int, row: int) -> tuple[int, int, int]:
        x = SAMPLE_WIDTH // 4 + column * SAMPLE_WIDTH // 2
        y = SAMPLE_HEIGHT // 4 + row * SAMPLE_HEIGHT // 2
        offset = (y * SAMPLE_WIDTH + x) * 3
        return tuple(frame[offset:offset + 3])

    def _assert_close(self, actual: tuple[int, int, int], expected: tuple[int, int, int]) -> None:
        for value, target in zip(actual, expected):
            self.assertLess(abs(value - target), 40, (actual, expected))

    def test_single_lens_hardware_decode_matches_the_software_frame(self) -> None:
        hardware = self._frames(1, self.device)[0]
        software = self._frames(1, None)[0]
        self.assertEqual(hardware, software)

    def test_three_stacked_lenses_land_in_the_same_grid_on_the_graphics_card(self) -> None:
        hardware = self._frames(3, self.device)[0]
        software = self._frames(3, None)[0]
        for column, row, expected in ((0, 0, (0, 0, 255)), (1, 0, (0, 255, 0)),
                                      (0, 1, (255, 0, 0)), (1, 1, (0, 0, 0))):
            self._assert_close(self._color(hardware, column, row), expected)
            self._assert_close(self._color(hardware, column, row), self._color(software, column, row))

    def test_three_stacked_lenses_stay_within_a_pixel_level_of_the_software_frame(self) -> None:
        hardware = self._frames(3, self.device)[0]
        software = self._frames(3, None)[0]
        worst = max(abs(a - b) for a, b in zip(hardware, software))
        self.assertLessEqual(worst, 8, f"the hardware grid drifted by {worst} levels")


if __name__ == "__main__":
    unittest.main()
