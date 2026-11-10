import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import BackgroundTasks

from app.config import settings
from app.database import create_job, finish_job, get_job, initialize
from app.main import ScanRequest, _scan_worker, scan


class ScanConcurrencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        self.old_db = settings.database_path
        self.old_media_root = settings.media_root
        settings.database_path = str(Path(self.temp.name) / "scan.sqlite3")
        settings.media_root = str(self.root)
        initialize()

    def tearDown(self):
        settings.database_path = self.old_db
        settings.media_root = self.old_media_root
        self.temp.cleanup()

    def _media_paths(self, count):
        paths = []
        for index in range(count):
            path = self.root / f"movie-{index}.mkv"
            path.write_bytes(b"fixture")
            paths.append(path)
        return paths

    @staticmethod
    def _completed_result(media_root, path):
        return {
            "media_path": path.relative_to(media_root).as_posix(),
            "status": "completed",
            "phase": "inspected",
            "summary": {
                "status": "not_configured",
                "found": 0,
                "staged": 0,
                "candidates": [],
            },
        }

    def test_scan_endpoint_queues_inventory_without_walking_media_tree(self):
        job_tasks = BackgroundTasks()
        with (
            patch("app.main.scan_media") as inventory,
            patch("app.main.clear_pending_review_queue", return_value=[]),
        ):
            response = scan(ScanRequest(paths=["/media"]), job_tasks)

        inventory.assert_not_called()
        self.assertEqual(response["status"], "queued")
        self.assertNotIn("total", response)
        self.assertEqual(len(job_tasks.tasks), 1)
        self.assertEqual(job_tasks.tasks[0].func, _scan_worker)
        self.assertEqual(job_tasks.tasks[0].args[2], [self.root])

    def test_worker_bounds_parallel_files_and_persists_input_order(self):
        paths = self._media_paths(6)
        job = create_job("scan", total=len(paths))
        lock = threading.Lock()
        active = 0
        peak = 0
        inspecting = 0
        inspection_peak = 0

        def inspect(_path):
            nonlocal inspecting, inspection_peak
            with lock:
                inspecting += 1
                inspection_peak = max(inspection_peak, inspecting)
            time.sleep(0.005)
            with lock:
                inspecting -= 1
            return inspection

        def stage(_client, path, _media_root, _stat, _metadata, _languages,
                  should_cancel=None):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.08)
            with lock:
                active -= 1
            self.assertFalse(should_cancel())
            return {
                "status": "no_candidates",
                "found": 0,
                "staged": 0,
                "candidates": [],
            }

        inspection = {
            "has_verified_english_subtitle": False,
            "metadata": {},
            "subtitle_streams": [],
        }
        with (
            patch("app.main.SCAN_WORKERS", 3),
            patch("app.main.scan_media", return_value=paths),
            patch("app.main.load_provider_settings", return_value={
                "api_key": "test-key", "username": "", "password": "",
            }),
            patch("app.main.OpenSubtitlesClient", return_value=object()),
            patch("app.main.inspect_media", side_effect=inspect),
            patch("app.main.stage_candidates", side_effect=stage),
        ):
            _scan_worker(job["id"], self.root, [self.root])

        result = get_job(job["id"])
        self.assertEqual(result["status"], "completed")
        self.assertEqual(peak, 3)
        self.assertEqual(inspection_peak, 1)
        self.assertEqual(
            [item["media_path"] for item in result["items"]],
            [path.name for path in paths],
        )
        self.assertEqual(result["completed"], len(paths))
        self.assertEqual(result["succeeded"], len(paths))

    def test_cancellation_does_not_schedule_a_later_batch(self):
        paths = self._media_paths(5)
        job = create_job("scan", total=len(paths))
        calls = []
        lock = threading.Lock()

        def process(job_id, _root, media_root, path, _client, _languages, _inspection_lock):
            with lock:
                calls.append(path.name)
            if path.name == paths[0].name:
                finish_job(job_id, "cancelled", "Cancelled by test.")
            return self._completed_result(media_root, path)

        with (
            patch("app.main.SCAN_WORKERS", 2),
            patch("app.main.scan_media", return_value=paths),
            patch("app.main.load_provider_settings", return_value={"api_key": ""}),
            patch("app.main._scan_one_file", side_effect=process),
        ):
            _scan_worker(job["id"], self.root, [self.root])

        self.assertEqual(get_job(job["id"])["status"], "cancelled")
        self.assertEqual(len(calls), 2)
        self.assertFalse(any(name in calls for name in [path.name for path in paths[2:]]))


if __name__ == "__main__":
    unittest.main()
