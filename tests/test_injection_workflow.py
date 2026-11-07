import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    set_candidate_decision, claim_candidate_patch, finish_candidate_patch,
    get_candidate,
)


class InjectionApprovalDatabaseTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
