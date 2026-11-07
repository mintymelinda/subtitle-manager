import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    set_candidate_decision, claim_candidate_patch, finish_candidate_patch,
    get_candidate, recover_interrupted_patches,
)


class InjectionApprovalDatabaseTests(unittest.TestCase):
    def test_initialize_migrates_existing_issue_two_database(self):
        with tempfile.TemporaryDirectory() as directory:
            old_path = settings.database_path
            database_path = Path(directory) / "legacy.sqlite3"
            settings.database_path = str(database_path)
            try:
                with sqlite3.connect(database_path) as db:
                    db.executescript("""
                        CREATE TABLE media_files (
                            id INTEGER PRIMARY KEY, relative_path TEXT UNIQUE,
                            size INTEGER, modified_at REAL, status TEXT,
                            last_scanned_at TEXT
                        );
                        CREATE TABLE subtitle_candidates (
                            id INTEGER PRIMARY KEY, media_relative_path TEXT,
                            status TEXT
                        );
                        CREATE TABLE patch_history (
                            id INTEGER PRIMARY KEY, relative_path TEXT,
                            subtitle_name TEXT, status TEXT, details TEXT,
                            created_at TEXT
                        );
                    """)
                initialize()
                with sqlite3.connect(database_path) as db:
                    candidate_columns = {row[1] for row in db.execute("PRAGMA table_info(subtitle_candidates)")}
                    history_columns = {row[1] for row in db.execute("PRAGMA table_info(patch_history)")}
                self.assertTrue({"review_decision", "patch_status", "patch_started_at", "patch_completed_at"} <= candidate_columns)
                self.assertTrue({"candidate_id", "backup_path"} <= history_columns)
            finally:
                settings.database_path = old_path

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "injection.sqlite3")
        initialize()
        self.path = "Films/test.mkv"
        self.size = 512
        self.mtime = 123.0
        record_scan(self.path, self.size, self.mtime, {"has_verified_english_subtitle": False})

    def tearDown(self):
        settings.database_path = self.old_path
        self.temp.cleanup()

    def create_candidate(self):
        candidate = {
            "file_id": 1234, "file_name": "test.srt", "language": "en",
            "format": "srt", "release": "test", "moviehash_match": True,
            "match_score": 100, "match_reasons": ["Exact hash match"],
        }
        row = save_candidate(self.path, self.size, self.mtime, candidate)
        mark_candidate_staged(row["id"], str(Path(self.temp.name) / "test.srt"), 10,
                              {"content_status": "english", "content_language": "en",
                               "content_confidence": 0.99})
        return row["id"]

    def test_test_mode_allows_only_one_concurrent_injection(self):
        first = self.create_candidate()
        second_path = "Films/other.mkv"
        record_scan(second_path, self.size, self.mtime, {})
        second_row = save_candidate(second_path, self.size, self.mtime, {
            "file_id": 1235, "file_name": "alternate.srt", "language": "en",
            "format": "srt", "match_score": 90, "match_reasons": [],
        })
        mark_candidate_staged(second_row["id"], str(Path(self.temp.name) / "alternate.srt"), 10,
                              {"content_status": "english", "content_language": "en",
                               "content_confidence": 0.99})
        set_candidate_decision(first, "approved")
        set_candidate_decision(second_row["id"], "approved")
        claim_candidate_patch(first, test_mode=True)
        with self.assertRaisesRegex(ValueError, "one injection to run at a time"):
            claim_candidate_patch(second_row["id"], test_mode=True)

    def test_unapproved_candidate_cannot_be_claimed_for_injection(self):
        candidate_id = self.create_candidate()
        with self.assertRaisesRegex(ValueError, "explicitly approved"):
            claim_candidate_patch(candidate_id)
        self.assertEqual(get_candidate(candidate_id)["patch_status"], "not_started")

    def test_only_explicitly_approved_candidate_can_be_claimed_and_recorded(self):
        candidate_id = self.create_candidate()
        set_candidate_decision(candidate_id, "approved")
        claimed = claim_candidate_patch(candidate_id)
        self.assertEqual(claimed["patch_status"], "running")
        with self.assertRaisesRegex(ValueError, "already running"):
            claim_candidate_patch(candidate_id)
        finish_candidate_patch(candidate_id, "patched", {"status": "patched"}, "/tmp/recovery.bak")
        row = get_candidate(candidate_id)
        self.assertEqual(row["patch_status"], "patched")
        with self.assertRaisesRegex(ValueError, "already been injected"):
            claim_candidate_patch(candidate_id)

    def test_failed_patch_can_be_retried_but_success_cannot(self):
        candidate_id = self.create_candidate()
        set_candidate_decision(candidate_id, "approved")
        claim_candidate_patch(candidate_id)
        finish_candidate_patch(candidate_id, "failed", {"error": "validation failed"})
        self.assertEqual(claim_candidate_patch(candidate_id)["patch_status"], "running")

    def test_restart_marks_running_patch_interrupted_and_blocks_retry(self):
        candidate_id = self.create_candidate()
        set_candidate_decision(candidate_id, "approved")
        claim_candidate_patch(candidate_id)
        self.assertEqual(recover_interrupted_patches(), 1)
        self.assertEqual(get_candidate(candidate_id)["patch_status"], "interrupted")
        with self.assertRaisesRegex(ValueError, "was interrupted"):
            claim_candidate_patch(candidate_id)

    def test_new_media_version_resets_previously_patched_candidate_state(self):
        candidate_id = self.create_candidate()
        set_candidate_decision(candidate_id, "approved")
        claim_candidate_patch(candidate_id)
        finish_candidate_patch(candidate_id, "patched", {"status": "patched"})
        updated = self.size + 1
        record_scan(self.path, updated, self.mtime + 1, {})
        save_candidate(self.path, updated, self.mtime + 1, {
            "file_id": 1234, "file_name": "test.srt", "language": "en",
            "format": "srt", "release": "test", "moviehash_match": True,
            "match_score": 100, "match_reasons": ["Exact hash match"],
        })
        row = get_candidate(candidate_id)
        self.assertEqual(row["patch_status"], "not_started")
        self.assertEqual(row["review_decision"], "pending")


if __name__ == "__main__":
    unittest.main()
