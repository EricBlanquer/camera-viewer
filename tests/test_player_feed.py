import os
import select
import time
import unittest

from app import PlayerFeed

FRAME = b"x" * 65536


class PlayerFeedTest(unittest.TestCase):
    def setUp(self) -> None:
        reader, writer = os.pipe()
        self.reader = os.fdopen(reader, "rb", buffering=0)
        self.output = os.fdopen(writer, "wb", buffering=0)
        self.feed = PlayerFeed(self.output)

    def tearDown(self) -> None:
        self.feed.close()
        self.output.close()
        if not self.reader.closed:
            self.reader.close()

    def read(self, size: int) -> bytes:
        received = b""
        deadline = time.monotonic() + 5
        while len(received) < size and time.monotonic() < deadline:
            if select.select([self.reader], [], [], 0.2)[0]:
                received += os.read(self.reader.fileno(), size - len(received))
        return received

    def test_frames_reach_the_player_in_order(self) -> None:
        frames = [bytes([index]) * 100000 for index in range(10)]
        for frame in frames:
            self.assertTrue(self.feed.write(frame))
            self.assertEqual(self.read(len(frame)), frame)

    def test_stalled_player_refuses_frames_without_blocking_and_accepts_them_again(self) -> None:
        started = time.monotonic()
        accepted = [self.feed.write(FRAME) for _ in range(100)]
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(accepted[-1])
        self.assertIn(sum(accepted), (32, 33, 34))
        self.assertEqual(len(self.read(sum(accepted) * len(FRAME))), sum(accepted) * len(FRAME))
        self.assertTrue(self.feed.write(b"next"))
        self.assertEqual(self.read(4), b"next")

    def test_closed_player_is_reported(self) -> None:
        self.reader.close()
        with self.assertRaises(BrokenPipeError):
            for _ in range(200):
                self.feed.write(b"frame")
                time.sleep(0.01)

    def test_close_returns_while_the_player_is_stalled_and_restores_blocking_writes(self) -> None:
        for _ in range(40):
            self.feed.write(FRAME)
        self.assertFalse(os.get_blocking(self.output.fileno()))
        started = time.monotonic()
        self.feed.close()
        self.assertLess(time.monotonic() - started, 1)
        self.assertFalse(self.feed.thread.is_alive())
        self.assertTrue(os.get_blocking(self.output.fileno()))

    def test_unused_feed_leaves_the_player_input_untouched(self) -> None:
        self.feed.close()
        self.assertTrue(os.get_blocking(self.output.fileno()))


if __name__ == "__main__":
    unittest.main()
