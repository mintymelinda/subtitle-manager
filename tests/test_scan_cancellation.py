import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.config import settings
from app.database import initialize, create_job, finish_job, get_job
from app.main import cancel_job, _scan_worker


class ScanCancellationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_db = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "jobs.sqlite3")
        initialize()

    def tearDown(self):
        settings.database_path = self.old_db
        self.temp.cleanup()

    def test_queued_scan_can_be_cancelled(self):
        job = create_job("scan", total=3)
        result = cancel_job(job["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(get_job(job["id"])["status"], "cancelled")

    def test_running_scan_can_be_cancelled(self):
        job = create_job("scan", total=3)
        from app.database import start_job
        start_job(job["id"])
        result = cancel_job(job["id"])
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(get_job(job["id"])["status"], "cancelled")

    def test_non_scan_jobs_cannot_be_cancelled(self):
        job = create_job("inject", total=1)
        with self.assertRaises(HTTPException) as raised:
            cancel_job(job["id"])
        self.assertEqual(raised.exception.status_code, 409)
        self.assertEqual(get_job(job["id"])["status"], "queued")

    def test_terminal_scan_cannot_be_cancelled_again(self):
        job = create_job("scan", total=1)
        finish_job(job["id"], "completed")
        with self.assertRaises(HTTPException) as raised:
            cancel_job(job["id"])
        self.assertEqual(raised.exception.status_code, 409)

    def test_worker_does_not_start_a_cancelled_queued_scan(self):
        job = create_job("scan", total=1)
        finish_job(job["id"], "cancelled", "Cancelled by user.")
        media_root = Path(self.temp.name) / "media"
        media_root.mkdir()
        media_file = media_root / "movie.mkv"
        media_file.write_bytes(b"fixture")
        with patch("app.main.inspect_media") as inspect:
            _scan_worker(job["id"], media_root, [media_file])
        inspect.assert_not_called()
        self.assertEqual(get_job(job["id"])["status"], "cancelled")


if __name__ == "__main__":
    unittest.main()
