import json
import queue
import subprocess
import tempfile
import threading
import time
import unittest
from pathlib import Path

from icam365 import NativeBridge


FRAME_COUNT = 48
FRAME_INTERVAL = 0.05
AUDIO_CHUNK = b"\xd5" * 400
MPEG_TIMESTAMP_WRAP = (1 << 33) / 90000
AAC_PRIMING_PACKETS = 2


class NativeMuxerClockTest(unittest.TestCase):
    def test_audio_samples_follow_the_live_timestamps(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.hevc"
            subprocess.run([
                "ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc2=size=128x128:rate=8",
                "-frames:v", str(FRAME_COUNT), "-c:v", "libx265", "-preset", "ultrafast",
                "-x265-params", "pools=1:frame-threads=1:keyint=1:log-level=error",
                "-f", "hevc", str(source),
            ], check=True, capture_output=True, timeout=15)
            packets = self.read_packets(source, "packet=pos,size")
            data = source.read_bytes()
            bridge = NativeBridge.__new__(NativeBridge)
            bridge.stopped = threading.Event()
            bridge.error = None
            bridge.video_queue = queue.Queue(maxsize=128)
            bridge.audio_queue = queue.Queue(maxsize=512)
            bridge.subscriber_lock = threading.Lock()
            output = queue.Queue()
            bridge.subscribers = {output}
            threads = bridge._start_muxer()
            try:
                started = time.monotonic()
                for index, packet in enumerate(packets):
                    remaining = started + index * FRAME_INTERVAL - time.monotonic()
                    if remaining > 0:
                        time.sleep(remaining)
                    offset = int(packet["pos"])
                    bridge.video_queue.put(data[offset:offset + int(packet["size"])])
                    bridge.audio_queue.put(AUDIO_CHUNK)
                bridge.video_queue.put(None)
                bridge.audio_queue.put(None)
                bridge.process.wait(timeout=10)
                for thread in threads:
                    thread.join(timeout=3)
                self.assertEqual(bridge.process.returncode, 0)
                captured = Path(directory) / "captured.ts"
                chunks = []
                while not output.empty():
                    chunks.append(output.get_nowait())
                captured.write_bytes(b"".join(chunks))
                media = self.read_packets(captured, "packet=codec_type,pts_time,duration_time")
                video = [packet for packet in media if packet["codec_type"] == "video"]
                audio = [packet for packet in media if packet["codec_type"] == "audio"]
                self.assertEqual(len(video), FRAME_COUNT)
                self.assertGreater(len(audio), 10)
                steady_audio = audio[AAC_PRIMING_PACKETS:]
                for previous, current in zip(steady_audio, steady_audio[1:]):
                    interval = (float(current["pts_time"]) - float(previous["pts_time"])) % MPEG_TIMESTAMP_WRAP
                    self.assertAlmostEqual(interval, float(previous["duration_time"]), delta=0.0001)
            finally:
                bridge.stopped.set()
                if bridge.process.poll() is None:
                    bridge.process.kill()
                    bridge.process.wait(timeout=5)
                for thread in threads:
                    thread.join(timeout=3)
                bridge.process.stdout.close()

    @staticmethod
    def read_packets(path, entries):
        result = subprocess.run([
            "ffprobe", "-v", "error", "-show_packets", "-show_entries", entries,
            "-of", "json", str(path),
        ], check=True, capture_output=True, timeout=10)
        return json.loads(result.stdout)["packets"]


if __name__ == "__main__":
    unittest.main()
