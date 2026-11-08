import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import (
    initialize, create_job, start_job, finish_job, record_scan, save_candidate,
    mark_candidate_staged, set_candidate_decision, finish_candidate_patch, list_candidates,
    clear_pending_review_queue, clear_saved_data, list_jobs, list_media_files,
)


class Issue26CleanupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "issue26.sqlite3")
        initialize()
        self.media = "Movies/example.mkv"
        record_scan(self.media, 100, 1234.5, {"audio_streams": []})

    def tearDown(self):
        settings.database_path = self.old_path
        self.temp.cleanup()

    def candidate(self, file_id):
        return {
            "file_id": file_id, "subtitle_id": str(file_id),
            "file_name": f"{file_id}.srt", "language": "en", "format": "srt",
            "release": "Example", "match_score": 90, "match_reasons": ["Exact match"],
        }

    def stage(self, file_id, patch_status=None):
        row = save_candidate(self.media, 100, 1234.5, self.candidate(file_id))
        mark_candidate_staged(row["id"], str(Path(self.temp.name) / f"{file_id}.srt"))
        if patch_status == "running":
            from app.database import claim_candidate_patch
            set_candidate_decision(row["id"], "approved")
            claim_candidate_patch(row["id"])
        return row["id"]

    def test_new_scan_removes_all_prior_candidates_except_active_recovery(self):
        self.stage(1)
        approved = self.stage(2)
        set_candidate_decision(approved, "approved")
        patched = self.stage(3)
        finish_candidate_patch(patched, "patched", {"test": True})
        paths = clear_pending_review_queue()
        self.assertEqual(
            set(paths),
            {str(Path(self.temp.name) / "1.srt"),
             str(Path(self.temp.name) / "2.srt"),
             str(Path(self.temp.name) / "3.srt")},
        )
        self.assertEqual(list_candidates(), [])

    def test_new_scan_retains_only_active_injection_recovery_record(self):
        active = self.stage(3, patch_status="running")
        old = self.stage(4)
        paths = clear_pending_review_queue()
        rows = list_candidates()
        self.assertEqual([row["id"] for row in rows], [active])
        self.assertEqual(len(paths), 1)

    def test_clear_saved_data_removes_jobs_results_candidates_and_history(self):
        self.stage(5)
        job = create_job("scan", total=1)
        start_job(job["id"])
        finish_job(job["id"], "completed")
        self.assertTrue(clear_saved_data())
        self.assertEqual(list_candidates(), [])
        self.assertEqual(list_jobs(), [])
        self.assertEqual(list_media_files(), [])

    def test_clear_saved_data_refuses_to_run_while_job_is_active(self):
        job = create_job("scan", total=1)
        start_job(job["id"])
        with self.assertRaisesRegex(ValueError, "active jobs"):
            clear_saved_data()
        self.assertEqual(len(list_jobs()), 1)

    def test_clear_saved_data_refuses_to_run_during_injection_even_without_job_row(self):
        self.stage(6, patch_status="running")
        with self.assertRaisesRegex(ValueError, "active injections"):
            clear_saved_data()


class Issue26DashboardTests(unittest.TestCase):
    def test_dashboard_has_clear_control_and_hides_empty_track_placeholders(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('id="clear-saved-data"', source)
        self.assertIn('fetch("/api/data/clear"', source)
        self.assertNotIn('return "None reported"', source)
        self.assertIn('if(audio)line(card,"Audio tracks",audio)', source)
        self.assertIn('if(subs)line(card,"Existing subtitle tracks",subs)', source)

    def test_new_scan_clears_visible_candidates_before_request(self):
        source = (Path(__file__).parents[1] / "app" / "main.py").read_text(encoding="utf-8")
        self.assertIn('candidateList.replaceChildren(el("p","Previous scan candidates cleared.', source)
        self.assertIn('for staged_path in clear_pending_review_queue():', source)


if __name__ == "__main__":
    unittest.main()
