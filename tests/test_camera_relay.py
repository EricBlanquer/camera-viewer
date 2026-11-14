import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ["QT_QPA_PLATFORM"] = "offscreen"

import app
from app import CameraPreview, FrameRelay, RtspStreamWorker, SourceRelay, StreamWorker, camera_relay_url

RELAY_BASE = "srt://192.168.1.251:8890/cams/"
ENTREE = "srt://192.168.1.251:8890?streamid=publish:cams/entree:relay:s3cr3t-Token_x&pkt_size=1316"
JARDIN = "srt://192.168.1.251:8890?streamid=publish:cams/jardin:relay:s3cr3t-Token_x&pkt_size=1316"
KEYFRAME = b"\x00\x00\x00\x01\x67\x42\x00\x1f" + b"\x00\x00\x00\x01\x65\x88"
FRAME = b"\x00\x00\x00\x01\x41\x9a"


class FakeProcess:
    def __init__(self, command: list[str], stdin=None, bufsize: int = -1, **_: object) -> None:
        self.command = command
        self.bufsize = bufsize
        self.returncode: int | None = None
        self.terminated = False
        self.stdin = None
        self.read_end = None
        if stdin == subprocess.PIPE:
            read_end, write_end = os.pipe()
            self.read_end = read_end
            self.stdin = os.fdopen(write_end, "wb", buffering=0)

    def poll(self) -> int | None:
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True
        self.returncode = -15

    def wait(self, timeout: float | None = None) -> int:
        return self.returncode if self.returncode is not None else 0

    def kill(self) -> None:
        self.returncode = -9

    def received(self) -> bytes:
        os.set_blocking(self.read_end, False)
        try:
            return os.read(self.read_end, 65536)
        except BlockingIOError:
            return b""


class CameraRelayUrlTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.settings = Path(self.directory.name) / "relay.json"

    def tearDown(self) -> None:
        self.directory.cleanup()

    def write(self, url: object, **credentials: str) -> None:
        self.settings.write_text(json.dumps({"url": url, **({"user": "relay", "password": "s3cr3t-Token_x"} if not credentials else credentials)}))
        self.settings.chmod(0o600)

    def test_camera_path_comes_from_its_name_in_ascii(self) -> None:
        self.write(RELAY_BASE)
        self.assertEqual(camera_relay_url("Entrée", self.settings), (ENTREE, "cams/entree"))
        self.assertEqual(camera_relay_url("Jardin côté rue", self.settings),
                         ("srt://192.168.1.251:8890?streamid=publish:cams/jardin-cote-rue:relay:s3cr3t-Token_x&pkt_size=1316",
                          "cams/jardin-cote-rue"))

    def test_no_settings_means_no_relay(self) -> None:
        self.assertIsNone(camera_relay_url("Entrée", self.settings))

    def test_no_relay_without_credentials(self) -> None:
        self.write(RELAY_BASE, user="relay")
        self.assertIsNone(camera_relay_url("Entrée", self.settings))

    def test_relay_outside_the_local_network_or_with_credentials_is_refused(self) -> None:
        for url in ("srt://8.8.8.8:8890/cams/", "srt://user:secret@192.168.1.251:8890/cams/", "srt://192.168.1.251/cams/",
                    "rtsp://192.168.1.251:8554/cams/", "srt://192.168.1.251:8890/cams/?latency=1", 42):
            self.write(url)
            self.assertIsNone(camera_relay_url("Entrée", self.settings), url)

    def test_a_name_without_letters_or_digits_has_no_relay(self) -> None:
        self.write(RELAY_BASE)
        self.assertIsNone(camera_relay_url("***", self.settings))


class SourceRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.processes: list[FakeProcess] = []
        patcher = mock.patch.object(app.subprocess, "Popen", side_effect=self.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.relay = SourceRelay(ENTREE, "cams/entree", "Entrée")

    def spawn(self, command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        self.processes.append(process)
        return process

    def test_publishes_the_live_source_without_reencoding_while_displayed(self) -> None:
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 0.0)
        command = self.processes[0].command
        self.assertEqual(command[:1], ["ffmpeg"])
        self.assertIn("http://127.0.0.1:4000/live", command)
        self.assertEqual(command[-7:], ["-map", "0:v:0", "-c", "copy", "-f", "mpegts", ENTREE])
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 1.0)
        self.assertEqual(len(self.processes), 1)

    def test_never_logs_the_password(self) -> None:
        with self.assertLogs("okam-linux", level="INFO") as logs:
            self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 0.0)
            self.relay.update(False, lambda: None, 1.0)
        self.assertTrue(any("cams/entree" in line for line in logs.output))
        self.assertFalse(any("s3cr3t" in line for line in logs.output))

    def test_stops_when_the_camera_is_no_longer_displayed(self) -> None:
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 0.0)
        self.relay.update(False, lambda: ["-i", "http://127.0.0.1:4000/live"], 1.0)
        self.assertTrue(self.processes[0].terminated)
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 2.0)
        self.assertEqual(len(self.processes), 2)

    def test_restarts_a_relay_that_stopped_after_a_delay(self) -> None:
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 0.0)
        self.processes[0].returncode = 1
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 1.0)
        self.assertEqual(len(self.processes), 1)
        self.relay.update(True, lambda: ["-i", "http://127.0.0.1:4000/live"], 1.0 + app.CAMERA_RELAY_RETRY_SECONDS * 2)
        self.assertEqual(len(self.processes), 2)

    def test_waits_for_a_live_source(self) -> None:
        self.relay.update(True, lambda: None, 0.0)
        self.assertEqual(self.processes, [])


class FrameRelayTest(unittest.TestCase):
    def setUp(self) -> None:
        self.processes: list[FakeProcess] = []
        patcher = mock.patch.object(app.subprocess, "Popen", side_effect=self.spawn)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.relay = FrameRelay(JARDIN, "cams/jardin", "Jardin")
        self.addCleanup(self.relay.stop)

    def spawn(self, command: list[str], **kwargs: object) -> FakeProcess:
        process = FakeProcess(command, **kwargs)
        self.processes.append(process)
        return process

    def wait_for(self, process: FakeProcess, expected: bytes) -> bytes:
        received = b""
        for _ in range(100):
            received += process.received()
            if received == expected:
                break
            app.time.sleep(0.01)
        return received

    def test_starts_on_a_keyframe_and_forwards_the_frames(self) -> None:
        self.relay.feed(FRAME, False, 15.0, 0.0)
        self.assertEqual(self.processes, [])
        self.relay.feed(KEYFRAME, True, 15.0, 0.1)
        self.relay.feed(FRAME, False, 15.0, 0.2)
        command = self.processes[0].command
        self.assertEqual(command[command.index("-r") + 1], "15.0")
        self.assertEqual(command[command.index("-f") + 1], "h264")
        self.assertEqual(command[-1], JARDIN)
        self.assertEqual(self.wait_for(self.processes[0], KEYFRAME + FRAME), KEYFRAME + FRAME)

    def test_feeds_ffmpeg_through_an_unbuffered_pipe(self) -> None:
        self.relay.feed(KEYFRAME, True, 15.0, 0.0)
        self.assertEqual(self.processes[0].bufsize, 0)

    def test_a_writer_that_broke_restarts_the_relay(self) -> None:
        self.relay.feed(KEYFRAME, True, 15.0, 0.0)
        self.relay.feed_writer.thread = mock.Mock(is_alive=mock.Mock(return_value=False))
        self.relay.feed(FRAME, False, 15.0, 0.1)
        self.assertTrue(self.processes[0].terminated)
        self.relay.feed(KEYFRAME, True, 15.0, 0.2)
        self.assertEqual(len(self.processes), 1)
        self.relay.feed(KEYFRAME, True, 15.0, 0.1 + app.CAMERA_RELAY_RETRY_SECONDS * 2)
        self.assertEqual(len(self.processes), 2)

    def test_stop_ends_the_relay_until_the_next_keyframe(self) -> None:
        self.relay.feed(KEYFRAME, True, 15.0, 0.0)
        self.relay.stop()
        self.assertTrue(self.processes[0].terminated)
        self.relay.feed(FRAME, False, 15.0, 0.1)
        self.assertEqual(len(self.processes), 1)
        self.relay.feed(KEYFRAME, True, 15.0, 0.2)
        self.assertEqual(len(self.processes), 2)


class DisplayGatesTheRelayTest(unittest.TestCase):
    def test_rtsp_worker_follows_the_display(self) -> None:
        worker = mock.Mock(display_enabled=app.threading.Event())
        RtspStreamWorker.set_display(worker, True)
        self.assertTrue(worker.display_enabled.is_set())
        RtspStreamWorker.set_display(worker, False)
        self.assertFalse(worker.display_enabled.is_set())

    def test_rtsp_worker_relays_its_live_source(self) -> None:
        camera = app.RtspCamera("rtsp:entree", "Entrée", "rtsp://192.168.1.18:8001/0/av0")
        worker = mock.Mock(camera=camera)
        worker._live_url.return_value = "http://127.0.0.1:4000/live"
        self.assertEqual(RtspStreamWorker._relay_input(worker), ["-i", "http://127.0.0.1:4000/live"])
        worker._live_url.return_value = camera.url
        self.assertEqual(RtspStreamWorker._relay_input(worker), ["-rtsp_transport", "tcp", "-i", camera.url])
        worker._live_url.return_value = None
        self.assertIsNone(RtspStreamWorker._relay_input(worker))

    def test_preview_forwards_the_display_to_both_worker_kinds(self) -> None:
        for kind in (StreamWorker, RtspStreamWorker):
            preview = CameraPreview.__new__(CameraPreview)
            preview.overlay_timer = mock.Mock()
            preview.hide_overlay = mock.Mock()
            preview.status_overlay = mock.Mock()
            preview.worker = mock.Mock(spec=kind)
            CameraPreview.set_display(preview, False)
            preview.worker.set_display.assert_called_once_with(False)


if __name__ == "__main__":
    unittest.main()
