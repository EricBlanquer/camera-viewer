import os
import queue
import threading
import unittest
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from icam365 import (
    STREAM_HEAD_MAX_BYTES, TS_PACKET_SIZE, TS_PAT_PID, TS_SYNC_BYTE, TS_UNIT_START_FLAG, TS_VIDEO_PID,
    StreamHead, broadcast_media,
)


def packet(pid: int, unit_start: bool = False, filler: int = 0) -> bytes:
    second = (TS_UNIT_START_FLAG if unit_start else 0) | ((pid >> 8) & 0x1F)
    return bytes([TS_SYNC_BYTE, second, pid & 0xFF, 0x10]) + bytes([filler]) * (TS_PACKET_SIZE - 4)


def table() -> bytes:
    return packet(TS_PAT_PID, unit_start=True)


def video(filler: int = 0) -> bytes:
    return packet(TS_VIDEO_PID, filler=filler)


class StreamHeadTest(unittest.TestCase):
    def test_a_fresh_head_offers_nothing_before_it_finds_a_packet_boundary(self):
        head = StreamHead()
        self.assertEqual(head.snapshot(), b"")
        head.feed(b"\x11\x22\x33")
        self.assertEqual(head.snapshot(), b"")

    def test_the_snapshot_starts_at_the_program_table(self):
        head = StreamHead()
        head.feed(table() + video(1) + video(2))
        self.assertEqual(head.snapshot(), table() + video(1) + video(2))

    def test_a_new_table_drops_the_previous_group_of_pictures(self):
        head = StreamHead()
        head.feed(table() + video(1))
        head.feed(table() + video(2))
        self.assertEqual(head.snapshot(), table() + video(2))

    def test_video_received_before_any_table_is_not_offered_to_a_new_player(self):
        head = StreamHead()
        head.feed(video(1) + video(2))
        self.assertEqual(head.snapshot(), b"")

    def test_a_mid_packet_start_resynchronises_on_the_next_table(self):
        head = StreamHead()
        head.feed(video(1)[100:] + table() + video(2))
        self.assertEqual(head.snapshot(), table() + video(2))

    def test_the_snapshot_joins_the_following_bytes_without_a_gap(self):
        head = StreamHead()
        head.feed(table() + video(1))
        prologue = head.snapshot()
        later = video(2) + video(3)
        head.feed(later)
        self.assertEqual(prologue + later, head.snapshot())

    def _fill_past_the_cap(self, head):
        head.feed(table() + video(1))
        self.assertTrue(head.snapshot())
        chunk = video(7) * 64
        for _ in range(STREAM_HEAD_MAX_BYTES // len(chunk) + 2):
            head.feed(chunk)
            if not head.snapshot():
                return
        self.fail("The head kept offering video that carries no program table.")

    def test_video_without_a_table_stops_being_offered_past_the_cap(self):
        head = StreamHead()
        self._fill_past_the_cap(head)
        self.assertEqual(head.snapshot(), b"")

    def test_the_head_recovers_after_the_cap_discarded_it(self):
        head = StreamHead()
        self._fill_past_the_cap(head)
        head.feed(table() + video(9))
        self.assertEqual(head.snapshot(), table() + video(9))

    def test_a_lost_sync_byte_discards_the_head_until_the_next_table(self):
        head = StreamHead()
        head.feed(table() + video(1))
        head.feed(b"\x00" * TS_PACKET_SIZE)
        self.assertEqual(head.snapshot(), b"")
        head.feed(table() + video(2))
        self.assertEqual(head.snapshot(), table() + video(2))


class ParameterSetRepeatTest(unittest.TestCase):
    def test_every_live_bridge_writes_parameter_sets_before_each_keyframe(self):
        import v380
        from icam365 import LIVE_MEDIA_OUTPUT_OPTIONS
        for options in (LIVE_MEDIA_OUTPUT_OPTIONS, v380.LIVE_MEDIA_OUTPUT_OPTIONS):
            self.assertIn("-bsf:v", options)
            self.assertEqual(options[options.index("-bsf:v") + 1], "dump_extra=freq=keyframe")
            self.assertLess(options.index("-bsf:v"), options.index("-f"))


class BroadcastHeadTest(unittest.TestCase):
    def _broadcast(self, chunks, head):
        subscriber: queue.Queue = queue.Queue(maxsize=256)
        stopped = threading.Event()
        remaining = list(chunks)

        def read(_size):
            return remaining.pop(0) if remaining else b""

        process = SimpleNamespace(stdout=SimpleNamespace(read=read))
        broadcast_media(process, stopped, {subscriber}, threading.Lock(), head)
        received = b""
        while not subscriber.empty():
            received += subscriber.get_nowait()
        return received

    def test_the_broadcaster_feeds_the_head_it_was_given(self):
        head = StreamHead()
        received = self._broadcast([table() + video(1), video(2)], head)
        self.assertEqual(received, table() + video(1) + video(2))
        self.assertEqual(head.snapshot(), received)

    def test_the_broadcaster_still_runs_without_a_head(self):
        self.assertEqual(self._broadcast([table() + video(1)], None), table() + video(1))


if __name__ == "__main__":
    unittest.main()
