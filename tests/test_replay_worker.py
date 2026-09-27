import os
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"

from app import REPLAY_DOWNLOAD_REQUEST, REPLAY_LIST_REQUEST, CardRecording, ReplayBuffer, ReplayWorker


def recording(name: str) -> CardRecording:
    return CardRecording(name, datetime.strptime(name[:14], "%Y%m%d%H%M%S"), 300, 1000)


class ReplayWorkerQueueTest(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory(prefix="intraswitch_okam_")
        self.worker = ReplayWorker(SimpleNamespace(), "", Path(self.directory.name))

    def tearDown(self) -> None:
        self.directory.cleanup()

    def test_download_is_handed_out_once(self) -> None:
        buffer = ReplayBuffer(recording("20260926192758_100.mp4"))
        self.worker.download(buffer)
        self.assertEqual(self.worker._next_request(), (REPLAY_DOWNLOAD_REQUEST, buffer))
        self.assertTrue(self.worker.requests.empty())

    def test_listing_runs_first_and_only_latest_download_is_kept(self) -> None:
        older = ReplayBuffer(recording("20260926192758_100.mp4"))
        newer = ReplayBuffer(recording("20260926231604_011.mp4"))
        self.worker.download(older)
        self.worker.list_day("20260926")
        self.worker.download(newer)
        self.assertEqual(self.worker._next_request(), (REPLAY_LIST_REQUEST, "20260926"))
        self.assertTrue(older.ended)
        self.assertEqual(self.worker._next_request(), (REPLAY_DOWNLOAD_REQUEST, newer))
        self.assertTrue(self.worker.requests.empty())


if __name__ == "__main__":
    unittest.main()
