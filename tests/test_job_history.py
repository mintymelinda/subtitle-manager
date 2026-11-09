import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.database import (
    initialize, create_job, start_job, update_job_progress, record_job_item,
    finish_job, get_job, list_jobs, recover_interrupted_jobs,
)
from app.observability import redact


class JobHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.temp.name) / "jobs.sqlite3")
        self.override = patch.object(settings, "database_path", self.db_path)
        self.override.start()
        initialize()

    def tearDown(self):
        self.override.stop()
        self.temp.cleanup()

    def test_job_progress_and_file_outcome_persist(self):
        job = create_job("scan", total=3, metadata={"scan_root": "/media"})
        start_job(job["id"])
        update_job_progress(job["id"], phase="discovery", current_file="Film/movie.mkv",
                            completed=1, succeeded=1, failed=0)
        record_job_item(job["id"], media_path="Film/movie.mkv", status="completed",
                        phase="discovery", details={"staged": 2})
        finish_job(job["id"], "completed")
        saved = get_job(job["id"])
        self.assertEqual(saved["status"], "completed")
        self.assertEqual(saved["completed"], 1)
        self.assertEqual(saved["phase"], "completed")
        self.assertIsNone(saved["current_file"])
        self.assertEqual(saved["items"][0]["details"], {"staged": 2})
        self.assertEqual(list_jobs()[0]["id"], job["id"])

    def test_restart_marks_queued_and_running_jobs_interrupted(self):
        queued = create_job("scan")
        running = create_job("inject")
        start_job(running["id"])
        record_job_item(running["id"], media_path="Movies/title.mkv", status="running", phase="muxing")
        self.assertEqual(recover_interrupted_jobs(), 2)
        self.assertEqual(get_job(queued["id"])["status"], "interrupted")
        self.assertEqual(get_job(running["id"])["status"], "interrupted")
        self.assertEqual(get_job(running["id"])["items"][0]["status"], "interrupted")
        self.assertEqual(get_job(running["id"])["phase"], "interrupted")
        self.assertIsNone(get_job(running["id"])["current_file"])
        self.assertEqual(recover_interrupted_jobs(), 0)

    def test_failed_job_retains_actionable_error(self):
        job = create_job("inject", total=1)
        start_job(job["id"])
        finish_job(job["id"], "failed", "MKV validation failed")
        saved = get_job(job["id"])
        self.assertEqual(saved["status"], "failed")
        self.assertIn("validation", saved["error"])

    def test_secret_values_are_redacted_recursively(self):
        value = redact({
            "api_key": "top-secret",
            "message": "password=hunter2 Bearer abc.def",
            "nested": [{"access_token": "hidden"}],
        })
        encoded = json.dumps(value)
        self.assertNotIn("top-secret", encoded)
        self.assertNotIn("hunter2", encoded)
        self.assertNotIn("abc.def", encoded)
        self.assertNotIn("hidden", encoded)
        self.assertIn("[REDACTED]", encoded)


if __name__ == "__main__":
    unittest.main()
