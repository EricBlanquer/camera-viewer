import http.client
import json
import os
import queue
import struct
import subprocess
import tempfile
import threading
import time
import unittest
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app import CameraPreview, Icam365ReplayWorker, LocalReplayPane, MainWindow, RtspCamera, vendor_connection_available
from icam365 import (
    AUDIO_CODEC, GENERIC_RESPONSE_COMMAND, LIST_RECORDINGS_COMMAND, LIST_RECORDINGS_RESPONSE, NO_RECORDING_MESSAGE,
    PLAY_CONTROL_COMMAND, PLAY_CONTROL_RESPONSE, PLAY_FORWARD, PLAY_START, PLAY_STOP, RECORDING_LIST_ERROR, VIDEO_CODEC,
    MediaFrames, NativeRecording, NativeReplay, PlaybackStream, decode_recording_list, play_control, recording_list_request,
    time_day,
)


SYNC_TIME = datetime(2026, 10, 14, 11, 59, 56)
FRAME_INTERVAL = 0.08
AUDIO_CHUNK = b"\xd5" * 320
AAC_FRAMING_SECONDS = 2 * 1024 / 8000


def media_frame(codec, flags, timestamp, payload=b""):
    return bytes([codec, 0, flags, 0]) + b"\0" * 4 + struct.pack("<II", len(payload), timestamp) + payload


def sync_frame(moment):
    return struct.pack("<HHIII", 0, 0, 0, 0, int(moment.timestamp()))


def recording_page(entries, total, last, entry_type=0, padding=b""):
    extra = b"\0" * 4 if entry_type else b""
    body = b"".join(time_day(start) + struct.pack("<IBB2x", duration, event, 0) + extra for start, duration, event in entries)
    return struct.pack("<IIBBBB", 0, total, 0, int(last), len(entries), entry_type) + body + padding


def wait_for(predicate, timeout=3):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class FakeReplaySession:
    def __init__(self):
        self.batches = queue.Queue()
        self.sent = []
        self.stop_commands = ()
        self.closed = False

    def receive(self):
        try:
            return self.batches.get(timeout=0.01)
        except queue.Empty:
            return [], []

    def send(self, command, payload=b""):
        self.sent.append((command, payload))

    def controls(self):
        return [struct.unpack_from("<III", payload) + (payload[12:20],)
                for command, payload in list(self.sent) if command == PLAY_CONTROL_COMMAND]

    def close(self):
        self.closed = True


class FakeStream:
    def __init__(self, replay, output=None, until=None):
        self.until = until.timestamp() if until is not None else None
        self.added = []
        self.finished = False
        self.error = None
        self.stopped = threading.Event()

    def add(self, video, timestamp, content, payload, keyframe=False):
        self.added.append((video, timestamp, content, payload, keyframe))

    def finish(self):
        self.finished = True

    def fail(self, message):
        self.error = message
        self.stopped.set()


class NativeRecordingProtocolTest(unittest.TestCase):
    def test_recording_pages_skip_padding_and_empty_or_oversized_entries(self):
        start = datetime(2026, 10, 14, 0, 0, 12)
        page = recording_page([(start, 63, 0), (start + timedelta(seconds=63), 0, 0),
                               (start + timedelta(seconds=63), 146, 1), (start, 90000, 0)], 699, False, padding=b"\xaa" * 19)
        recordings, count, total, last = decode_recording_list(page)
        self.assertEqual(recordings, [NativeRecording(start, 63, 0), NativeRecording(start + timedelta(seconds=63), 146, 1)])
        self.assertEqual((count, total, last), (4, 699, False))
        self.assertEqual(recordings[1].end, start + timedelta(seconds=209))
        self.assertTrue(recordings[1].detection)
        self.assertFalse(recordings[0].detection)
        self.assertNotEqual(recordings[0].key, recordings[1].key)
        timed, _, _, last = decode_recording_list(recording_page([(start, 60, 2)], 1, True, entry_type=1))
        self.assertEqual(timed, [NativeRecording(start, 60, 2)])
        self.assertTrue(last)
        self.assertEqual(decode_recording_list(recording_page([], 0, False)), ([], 0, 0, False))

    def test_invalid_recording_pages_are_rejected(self):
        start = datetime(2026, 10, 14, 0, 0, 12)
        valid = recording_page([(start, 60, 0)], 1, True)
        invalid_date = valid[:12] + struct.pack("<HBBBBBB", 2026, 13, 1, 0, 0, 0, 0) + valid[20:]
        for page in (valid[:11], valid[:-1], recording_page([(start, 60, 0)], 1, True, entry_type=2), invalid_date):
            with self.subTest(page=page.hex()), self.assertRaises(ValueError):
                decode_recording_list(page)

    def test_requests_use_the_camera_local_time_and_weekday(self):
        sunday = datetime(2026, 10, 18, 23, 59, 59)
        self.assertEqual(time_day(sunday), struct.pack("<HBBBBBB", 2026, 10, 18, 0, 23, 59, 59))
        monday = datetime(2026, 10, 19, 0, 0, 0)
        self.assertEqual(recording_list_request(monday, sunday),
                         time_day(monday) + time_day(sunday) + b"\0" * 8)
        self.assertEqual(play_control(5, PLAY_START, 0, monday),
                         struct.pack("<III", 5, PLAY_START, 0) + time_day(monday) + b"\0" * 4)
        self.assertEqual(play_control(3, PLAY_STOP), struct.pack("<III", 3, PLAY_STOP, 0) + b"\0" * 12)

    def test_timed_frames_report_clock_messages_and_skip_their_extra_data(self):
        parser = MediaFrames((VIDEO_CODEC,))
        message = struct.pack("<HHIII", 0, 7, 0, 3, 0) + b"abc"
        video = media_frame(VIDEO_CODEC, 1, 40, b"\0\0\0\1\x40\x01")
        frames = parser.feed_timed(sync_frame(SYNC_TIME) + message + video)
        self.assertEqual([(frame.codec, frame.flags, frame.timestamp) for frame in frames],
                         [(0, 0, int(SYNC_TIME.timestamp())), (0, 7, 0), (VIDEO_CODEC, 1, 40)])
        self.assertEqual(MediaFrames((VIDEO_CODEC,)).feed(message + video), [(VIDEO_CODEC, 1, b"\0\0\0\1\x40\x01")])


class NativeReplaySessionTest(unittest.TestCase):
    def setUp(self):
        self.session = FakeReplaySession()
        patcher = patch.object(NativeReplay, "_connect", return_value=self.session)
        patcher.start()
        self.addCleanup(patcher.stop)
        stream_patcher = patch("icam365.PlaybackStream", FakeStream)
        stream_patcher.start()
        self.addCleanup(stream_patcher.stop)
        self.replay = NativeReplay("rtsp:entrance")
        self.addCleanup(self.replay.close)

    def started(self, count):
        return wait_for(lambda: len([control for control in self.session.controls() if control[1] == PLAY_START]) >= count)

    def test_each_playback_uses_a_fresh_channel_and_ignores_frames_from_the_previous_one(self):
        first_moment = datetime(2026, 10, 14, 12, 0, 0)
        first = self.replay.play(first_moment)
        self.assertTrue(self.started(1))
        self.assertEqual(self.session.controls(), [(3, PLAY_START, 0, time_day(first_moment))])
        self.assertEqual(self.session.stop_commands, ((PLAY_CONTROL_COMMAND, play_control(3, PLAY_STOP)),))
        self.session.batches.put(([
            (3, media_frame(VIDEO_CODEC, 0, 20, b"early")),
            (4, media_frame(AUDIO_CODEC, 2, 20, AUDIO_CHUNK)),
            (3, sync_frame(SYNC_TIME)),
            (3, media_frame(VIDEO_CODEC, 0, 40, b"before-key")),
            (4, media_frame(AUDIO_CODEC, 2, 40, AUDIO_CHUNK)),
            (3, media_frame(VIDEO_CODEC, 1, 120, b"key")),
            (4, media_frame(AUDIO_CODEC, 2, 160, AUDIO_CHUNK)),
            (3, media_frame(VIDEO_CODEC, 0, 200, b"next")),
        ], []))
        self.assertTrue(wait_for(lambda: len(first.added) == 3))
        base = SYNC_TIME.timestamp()
        self.assertEqual(first.added, [(True, 120, base + 0.08, b"key", True), (False, 160, base + 0.12, AUDIO_CHUNK, False),
                                       (True, 200, base + 0.16, b"next", False)])
        second_moment = datetime(2026, 10, 14, 9, 45, 30)
        second = self.replay.play(second_moment)
        self.assertTrue(self.started(2))
        self.assertEqual(self.session.controls()[1:], [(3, PLAY_STOP, 0, b"\0" * 8), (5, PLAY_START, 0, time_day(second_moment))])
        self.session.batches.put(([
            (3, media_frame(VIDEO_CODEC, 0, 280, b"late-first")),
            (5, sync_frame(second_moment)),
            (5, media_frame(VIDEO_CODEC, 1, 40, b"second-key")),
            (6, media_frame(AUDIO_CODEC, 2, 40, AUDIO_CHUNK)),
        ], []))
        self.assertTrue(wait_for(lambda: len(second.added) == 2))
        self.assertEqual(len(first.added), 3)
        self.assertEqual(second.added[0], (True, 40, second_moment.timestamp(), b"second-key", True))

    def test_speed_pause_and_close_only_control_the_current_playback(self):
        self.replay.speed_supported = True
        saved = self.replay.play(SYNC_TIME, Path("/nonexistent/clip.mkv"), SYNC_TIME + timedelta(minutes=1), 2)
        self.assertTrue(self.started(1))
        self.assertEqual(self.session.controls()[1][:3], (3, PLAY_FORWARD, 1))
        self.replay.request("close", saved)
        self.assertTrue(wait_for(lambda: (3, PLAY_STOP) in [control[:2] for control in self.session.controls()]))
        self.session.sent.clear()
        stream = self.replay.play(SYNC_TIME)
        self.assertTrue(self.started(1))
        other = FakeStream(self.replay)
        self.replay.request("speed", other, 2)
        self.replay.request("speed", stream, 2)
        self.assertTrue(wait_for(lambda: len(self.session.controls()) == 2))
        self.assertEqual(self.session.controls()[1][:3], (5, PLAY_FORWARD, 1))
        self.replay.request("pause", stream)
        self.assertTrue(wait_for(lambda: len(self.session.controls()) == 3))
        self.assertEqual(self.session.controls()[2][:2], (5, PLAY_STOP))
        self.assertEqual(self.session.stop_commands, ())
        self.replay.request("close", stream)
        time.sleep(0.1)
        self.assertEqual(len(self.session.controls()), 3)
        self.assertIsNone(stream.error)

    def test_playback_without_video_fails_and_stalled_playback_ends(self):
        with patch("icam365.PLAYBACK_START_TIMEOUT", 0.1), patch("icam365.PLAYBACK_IDLE_TIMEOUT", 0.1):
            empty = self.replay.play(SYNC_TIME)
            self.assertTrue(wait_for(lambda: empty.error is not None))
            self.assertEqual(empty.error, NO_RECORDING_MESSAGE)
            stalled = self.replay.play(SYNC_TIME)
            self.assertTrue(self.started(2))
            self.session.batches.put(([(5, sync_frame(SYNC_TIME)), (5, media_frame(VIDEO_CODEC, 1, 40, b"key"))], []))
            self.assertTrue(wait_for(lambda: stalled.finished))
        self.assertIsNone(stalled.error)
        self.assertEqual([control[:2] for control in self.session.controls()],
                         [(3, PLAY_START), (3, PLAY_STOP), (5, PLAY_START), (5, PLAY_STOP)])

    def test_saved_recording_ends_at_its_listed_end(self):
        stream = self.replay.play(SYNC_TIME, Path("/nonexistent/clip.mkv"), SYNC_TIME + timedelta(seconds=0.1))
        self.assertTrue(self.started(1))
        self.session.batches.put(([
            (3, sync_frame(SYNC_TIME)),
            (3, media_frame(VIDEO_CODEC, 1, 0, b"key")),
            (3, media_frame(VIDEO_CODEC, 0, 80, b"inside")),
            (3, media_frame(VIDEO_CODEC, 0, 160, b"after")),
        ], []))
        self.assertTrue(wait_for(lambda: stream.finished))
        self.assertEqual([added[3] for added in stream.added], [b"key", b"inside"])
        self.assertEqual(self.session.controls()[-1][:2], (3, PLAY_STOP))

    def recordings(self, responses):
        result = {}

        def request():
            try:
                result["value"] = self.replay.recordings(SYNC_TIME, SYNC_TIME + timedelta(hours=1))
            except OSError as ex:
                result["error"] = str(ex)

        thread = threading.Thread(target=request)
        thread.start()
        self.assertTrue(wait_for(lambda: any(command == LIST_RECORDINGS_COMMAND for command, _ in self.session.sent)))
        self.session.batches.put(([], responses))
        thread.join(3)
        self.session.sent.clear()
        return result

    def test_recording_lists_join_pages_and_report_refusals(self):
        start = datetime(2026, 10, 14, 12, 0, 0)
        result = self.recordings([
            (LIST_RECORDINGS_RESPONSE, recording_page([(start + timedelta(minutes=1), 60, 0)], 2, False)),
            (LIST_RECORDINGS_RESPONSE, recording_page([(start, 60, 1)], 2, True)),
        ])
        self.assertEqual(result, {"value": [NativeRecording(start, 60, 1), NativeRecording(start + timedelta(minutes=1), 60, 0)]})
        self.assertEqual(self.recordings([(LIST_RECORDINGS_RESPONSE, recording_page([], 0, False))]), {"value": []})
        self.assertEqual(self.recordings([(GENERIC_RESPONSE_COMMAND, struct.pack("<Ii", LIST_RECORDINGS_COMMAND, 3))]),
                         {"error": RECORDING_LIST_ERROR})

    def test_refused_start_and_closed_connection_fail_the_playback(self):
        stream = self.replay.play(SYNC_TIME)
        self.assertTrue(self.started(1))
        self.session.batches.put(([], [(PLAY_CONTROL_RESPONSE, struct.pack("<Ii4x", PLAY_START, -1))]))
        self.assertTrue(wait_for(lambda: stream.error is not None))
        self.assertEqual(stream.error, NO_RECORDING_MESSAGE)
        pending = self.replay.play(SYNC_TIME)
        self.assertTrue(self.started(2))
        self.replay.close()
        self.assertTrue(self.session.closed)
        self.assertIsNotNone(pending.error)
        with self.assertRaises(OSError):
            self.replay.play(SYNC_TIME)


class PlaybackStreamTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_replay_")
        source = Path(cls.directory.name) / "source.hevc"
        subprocess.run([
            "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=25/2",
            "-frames:v", "26", "-c:v", "libx265", "-preset", "ultrafast",
            "-x265-params", "pools=1:frame-threads=1:keyint=13:log-level=error", "-f", "hevc", str(source),
        ], check=True, capture_output=True, timeout=30)
        data = source.read_bytes()
        cls.frames = [(data[int(packet["pos"]):int(packet["pos"]) + int(packet["size"])], "K" in packet["flags"])
                      for packet in cls.packets(source, "packet=pos,size,flags")]

    @classmethod
    def tearDownClass(cls):
        cls.directory.cleanup()

    @staticmethod
    def packets(path, entries):
        result = subprocess.run(["ffprobe", "-v", "error", "-show_packets", "-show_entries", entries, "-of", "json", str(path)],
                                check=True, capture_output=True, timeout=10)
        return json.loads(result.stdout)["packets"]

    @classmethod
    def duration(cls, path, kind):
        packets = [packet for packet in cls.packets(path, "packet=codec_type,pts_time,duration_time") if packet["codec_type"] == kind]
        return float(packets[-1]["pts_time"]) + float(packets[-1]["duration_time"]) - float(packets[0]["pts_time"])

    def save(self, name, stamps, audio=True):
        output = Path(self.directory.name) / name
        stream = PlaybackStream(SimpleNamespace(speed_supported=True, request=Mock()), output)
        start = SYNC_TIME.timestamp()
        try:
            for (frame, keyframe), stamp in zip(self.frames, stamps):
                stream.add(True, stamp, start + stamp / 1000, frame, keyframe)
                if audio:
                    stream.add(False, stamp, start + stamp / 1000, AUDIO_CHUNK)
                    stream.add(False, stamp, start + stamp / 1000, AUDIO_CHUNK)
            stream.finish()
            self.assertTrue(stream.done.wait(15))
            self.assertEqual(stream.returncode, 0)
            self.assertIsNone(stream.error)
        finally:
            stream.close()
        return output, stream

    def test_saved_recording_keeps_the_camera_frame_times_whatever_the_delivery_pace(self):
        stamps = [40 + index * 80 for index in range(len(self.frames))]
        began = time.monotonic()
        output, stream = self.save("saved.mkv", stamps)
        self.assertLess(time.monotonic() - began, 5)
        video = sorted(float(packet["pts_time"]) for packet in self.packets(output, "packet=codec_type,pts_time")
                       if packet["codec_type"] == "video")
        self.assertEqual(len(video), len(self.frames))
        for previous, current in zip(video, video[1:]):
            self.assertAlmostEqual(current - previous, FRAME_INTERVAL, delta=0.002)
        self.assertAlmostEqual(self.duration(output, "audio"), len(self.frames) * FRAME_INTERVAL, delta=AAC_FRAMING_SECONDS)
        self.assertEqual(stream.moment(0), datetime.fromtimestamp(SYNC_TIME.timestamp() + 0.04))
        self.assertEqual(stream.moment(1.0), datetime.fromtimestamp(SYNC_TIME.timestamp() + 1.04))
        self.assertEqual(stream.moment(100), datetime.fromtimestamp(SYNC_TIME.timestamp() + stamps[-1] / 1000 + 2))

    def test_timestamp_jumps_do_not_stop_the_recording_and_missing_sound_is_silent(self):
        stamps = [40 + index * 80 + (-30000 if index >= 13 else 0) for index in range(len(self.frames))]
        output, stream = self.save("jump.mkv", stamps, audio=False)
        video = sorted(float(packet["pts_time"]) for packet in self.packets(output, "packet=codec_type,pts_time")
                       if packet["codec_type"] == "video")
        self.assertEqual(len(video), len(self.frames))
        self.assertLess(video[-1] - video[0], len(self.frames) * FRAME_INTERVAL)
        self.assertGreater(self.duration(output, "audio"), (len(self.frames) - 1) * FRAME_INTERVAL - 0.7)
        self.assertEqual(stream.moment(video[-1] - video[0]), datetime.fromtimestamp(SYNC_TIME.timestamp() + stamps[-1] / 1000))

    def test_player_stream_holds_media_until_the_player_connects_and_serves_mpeg_ts(self):
        replay = SimpleNamespace(speed_supported=False, request=Mock())
        stream = PlaybackStream(replay)
        try:
            self.assertTrue(stream.url.startswith("http://127.0.0.1:"))
            start = SYNC_TIME.timestamp()
            for (frame, keyframe), index in zip(self.frames[:5], range(5)):
                stream.add(True, index * 80, start + index * 0.08, frame, keyframe)
            self.assertTrue(stream.video_queue.empty())
            address = urllib.parse.urlsplit(stream.url)
            connection = http.client.HTTPConnection(address.hostname, address.port, timeout=10)
            connection.request("GET", address.path)
            response = connection.getresponse()
            self.assertEqual(response.status, 200)
            self.assertTrue(wait_for(lambda: stream.subscribers))
            for (frame, keyframe), index in zip(self.frames[5:], range(5, len(self.frames))):
                stream.add(True, index * 80, start + index * 0.08, frame, keyframe)
            data = response.read(188 * 4)
            self.assertEqual(data[::188], b"\x47" * 4)
            stream.set_speed(2)
            replay.speed_supported = stream.speed_supported = True
            stream.set_speed(2)
            stream.set_paused(True)
            self.assertEqual([call.args for call in replay.request.call_args_list], [("speed", stream, 2), ("pause", stream)])
            connection.close()
        finally:
            stream.close()


class NativeCameraPlaybackUiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.application = QApplication.instance() or QApplication([])

    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_native_")
        self.settings = QSettings(str(Path(self.directory.name) / "settings.ini"), QSettings.Format.IniFormat)
        with patch("app.QSettings", return_value=self.settings), patch("app.QTimer.singleShot"):
            self.window = MainWindow()
        self.camera = RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:8001/0/av0")
        self.window.devices = [self.camera]
        self.window.device_accounts = {self.camera.uid: "rtsp"}
        self.window.selected_device = self.camera
        patcher = patch("app.load_icam365_config", return_value=object())
        patcher.start()
        self.addCleanup(patcher.stop)

    def tearDown(self):
        self.window.quit_requested = True
        self.window.close()
        self.directory.cleanup()
        QApplication.setQuitOnLastWindowClosed(True)

    def pane(self):
        with patch("app.Icam365ReplayWorker"), patch("app.QTimer.singleShot"):
            pane = LocalReplayPane(self.camera, self.window, camera_recordings=True)
        self.addCleanup(pane.deleteLater)
        self.addCleanup(pane.stop)
        return pane

    def test_native_cameras_offer_camera_and_local_recordings(self):
        self.assertTrue(vendor_connection_available(self.camera))
        with patch.object(CameraPreview, "start"):
            preview = CameraPreview(self.camera, settings=self.settings)
        self.assertEqual([action.text() for action in preview.replay_button.menu().actions()],
                         ["Camera recordings", "Local recordings (24 h)"])
        preview.stop()
        with patch("app.load_icam365_config", return_value=None), patch.object(CameraPreview, "start"):
            plain = CameraPreview(self.camera, settings=self.settings)
        self.assertIsNone(plain.replay_button.menu())
        plain.stop()

    def test_camera_recordings_open_the_native_playback_in_the_camera_pane(self):
        with patch("app.Icam365ReplayWorker") as worker, patch("app.QTimer.singleShot"):
            self.window.open_camera_sd_replay(self.camera)
        pane = self.window.local_replays[self.camera.uid]
        self.assertTrue(pane.camera_recordings and pane.native_camera_recordings)
        self.assertEqual(pane.playback_speeds, (1, 2))
        worker.assert_called_once_with(self.camera)
        worker.return_value.start.assert_called_once()
        self.window.close_local_replay(self.camera.uid)

    def test_player_follows_the_camera_stream_position_across_recordings(self):
        pane = self.pane()
        start = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
        recordings = [NativeRecording(start, 60, 0), NativeRecording(start + timedelta(seconds=60), 60, 1)]
        pane.on_remote_catalog(recordings)
        pane.remote_worker.open_recording.assert_called_once_with(recordings[1], 0)
        tunnel = Mock(spec=PlaybackStream)
        tunnel.error = None
        tunnel.speed_supported = True
        tunnel.moment.return_value = start + timedelta(seconds=75)
        pane.remote_worker.tunnel = tunnel
        commands = []
        with patch("app.subprocess.Popen", side_effect=lambda command, **options: commands.append(command) or Mock(poll=Mock(return_value=None))):
            pane.on_remote_playback(recordings[1].key, "http://127.0.0.1:40000/stream", 0)
        self.assertIn("--demuxer-lavf-format=mpegts", commands[0])
        self.assertIn("--cache=yes", commands[0])
        self.assertNotIn("--cache-pause=no", commands[0])
        self.assertNotIn("--demuxer-lavf-format=dhav", commands[0])
        self.assertFalse(pane.controls.speed_button.isHidden())
        pane.current_path = Path(recordings[0].key)
        answers = {"time-pos": (True, 12.5), "audio-params": (True, {"format": "floatp"}), "eof-reached": (True, False)}
        with patch("app.mpv_request", side_effect=lambda socket, command: answers.get(command[-1], (True, None))):
            pane.update_progress()
        tunnel.moment.assert_called_with(12.5)
        self.assertEqual(pane.current_path, Path(recordings[1].key))
        self.assertEqual(pane.timeline.center, start + timedelta(seconds=75))
        self.assertTrue(pane.controls.sound_button.isEnabled())
        pane.stop_player()

    def test_pause_stops_the_camera_and_play_resumes_at_the_paused_moment(self):
        pane = self.pane()
        start = datetime.now().replace(microsecond=0) - timedelta(minutes=10)
        recording = NativeRecording(start, 120, 0)
        pane.on_remote_catalog([recording])
        tunnel = Mock(spec=PlaybackStream)
        tunnel.error = None
        tunnel.moment.return_value = start + timedelta(seconds=30)
        pane.remote_worker.tunnel = tunnel
        with patch("app.mpv_request", side_effect=lambda socket, command: (True, 4.0) if command[-1] == "time-pos" else (True, False)) as ipc:
            pane.toggle_playing()
            ipc.assert_any_call(pane.socket_path, ["set_property", "pause", True])
            tunnel.set_paused.assert_called_once_with(True)
            self.assertEqual(pane.paused_moment, start + timedelta(seconds=30))
            pane.update_progress()
            pane.remote_worker.open_recording.reset_mock()
            pane.toggle_playing()
        pane.remote_worker.open_recording.assert_called_once_with(recording, 30)
        self.assertIsNone(pane.paused_moment)

    def test_speed_accelerates_the_player_and_the_camera_delivery(self):
        pane = self.pane()
        tunnel = Mock(spec=PlaybackStream)
        pane.remote_worker.tunnel = tunnel
        with patch("app.mpv_request", return_value=(True, None)) as ipc:
            pane.change_speed()
            tunnel.set_speed.assert_called_once_with(2)
            ipc.assert_any_call(pane.socket_path, ["set_property", "speed", 2])
            ipc.assert_any_call(pane.socket_path, ["set_property", "aid", "no"])
            pane.change_speed()
        tunnel.set_speed.assert_called_with(1)
        self.assertEqual(pane.controls.speed_button.text(), "1x")

    def test_selected_camera_menus_include_camera_recordings_for_every_vendor_connection(self):
        okam = SimpleNamespace(uid="okam-camera", name="Garden")
        self.assertTrue(vendor_connection_available(okam))
        with patch("app.load_icam365_config", return_value=None):
            self.assertFalse(vendor_connection_available(self.camera))
        with patch.object(self.window, "open_camera_sd_replay") as camera_replay, patch.object(
            self.window, "open_local_replay"
        ) as local_replay:
            self.window.open_camera_replay()
            camera_replay.assert_called_once_with(self.camera)
            with patch("app.load_icam365_config", return_value=None):
                self.window.open_camera_replay()
            local_replay.assert_called_once_with(self.camera)


class NativeRecordingSaveTest(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_camera_viewer_save_")
        self.target = Path(self.directory.name) / "clip.mkv"
        self.target.write_bytes(b"previous complete recording")
        self.recording = NativeRecording(SYNC_TIME, 60, 1)
        self.worker = Icam365ReplayWorker(RtspCamera("rtsp:entrance", "Entrance", "rtsp://192.0.2.10:8001/0/av0"))

    def tearDown(self):
        self.directory.cleanup()

    def save(self, returncode, error=None):
        streams = []

        def play(moment, output, until, speed):
            self.assertEqual(speed, 2)
            output.write_bytes(b"camera recording")
            stream = SimpleNamespace(done=threading.Event(), returncode=returncode, error=error, close=Mock())
            stream.done.set()
            streams.append((moment, output, until, stream))
            return stream

        with patch("app.NativeReplay") as replay:
            replay.return_value.play.side_effect = play
            saved = Mock()
            self.worker.saved.connect(saved)
            try:
                self.worker._save_download(self.recording, self.target)
            finally:
                self.worker.saved.disconnect(saved)
        moment, output, until, stream = streams[0]
        self.assertEqual((moment, until), (self.recording.start, self.recording.end))
        self.assertFalse(output.exists())
        stream.close.assert_called_once()
        replay.return_value.close.assert_called_once()
        return saved

    def test_completed_camera_recording_replaces_the_target_atomically(self):
        saved = self.save(0)
        saved.assert_called_once_with(str(self.target))
        self.assertEqual(self.target.read_bytes(), b"camera recording")
        self.assertEqual([path.name for path in Path(self.directory.name).iterdir()], ["clip.mkv"])

    def test_failed_camera_recording_keeps_the_previous_file(self):
        with self.assertRaisesRegex(OSError, "camera link lost"):
            self.save(0, "camera link lost")
        with self.assertRaises(OSError):
            self.save(1)
        self.assertEqual(self.target.read_bytes(), b"previous complete recording")
        self.assertEqual([path.name for path in Path(self.directory.name).iterdir()], ["clip.mkv"])


if __name__ == "__main__":
    unittest.main()
