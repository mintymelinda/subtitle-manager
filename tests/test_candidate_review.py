import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    list_candidates, set_candidate_decision, connect,
)


class CandidateReviewDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "review.sqlite3")
        initialize()
        self.media_path = "Movies/example.mkv"
        self.size = 1000
        self.modified = 1234.5
        record_scan(self.media_path, self.size, self.modified, {
            "has_verified_english_subtitle": False,
            "audio_streams": [{"codec": "aac", "language": "eng"}],
            "subtitle_streams": [],
        })

    def tearDown(self):
        settings.database_path = self.old_path
        self.temp.cleanup()

    def candidate(self, file_id):
        return {
            "file_id": file_id,
            "subtitle_id": str(file_id),
            "file_name": "candidate.srt",
            "language": "en",
            "format": "srt",
            "release": "Example.2024",
            "moviehash_match": True,
            "download_count": 20,
            "match_score": 90,
            "match_reasons": ["Exact file fingerprint match"],
        }

    def stage(self, file_id):
        row = save_candidate(self.media_path, self.size, self.modified, self.candidate(file_id))
        mark_candidate_staged(row["id"], str(Path(self.temp.name) / f"{file_id}.srt"), 10,
                              {"content_status": "english", "content_language": "en",
                               "content_confidence": 0.99})
        return row["id"]

    def test_unchanged_candidate_does_not_issue_an_update(self):
        candidate = self.candidate(17)
        with connect() as db:
            db.execute("CREATE TABLE candidate_update_audit (candidate_id INTEGER)")
            db.execute("""
                CREATE TRIGGER audit_candidate_update
                AFTER UPDATE ON subtitle_candidates
                BEGIN
                    INSERT INTO candidate_update_audit(candidate_id) VALUES (NEW.id);
                END
            """)

        save_candidate(self.media_path, self.size, self.modified, candidate)
        save_candidate(self.media_path, self.size, self.modified, candidate)
        with connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM candidate_update_audit").fetchone()[0],
                0,
            )

        changed = {**candidate, "match_score": candidate["match_score"] + 1}
        save_candidate(self.media_path, self.size, self.modified, changed)
        with connect() as db:
            self.assertEqual(
                db.execute("SELECT COUNT(*) FROM candidate_update_audit").fetchone()[0],
                1,
            )

    def test_review_decision_persists_and_is_visible_with_media_details(self):
        candidate_id = self.stage(11)
        result = set_candidate_decision(candidate_id, "approved")
        self.assertEqual(result["review_decision"], "approved")
        row = list_candidates()[0]
        self.assertEqual(row["review_decision"], "approved")
        self.assertEqual(row["inspection"]["audio_streams"][0]["codec"], "aac")
        self.assertFalse(row["is_stale"])

    def test_rejected_candidate_stays_rejected_when_same_file_is_scanned_again(self):
        candidate_id = self.stage(12)
        set_candidate_decision(candidate_id, "rejected")
        save_candidate(self.media_path, self.size, self.modified, self.candidate(12))
        self.assertEqual(list_candidates()[0]["review_decision"], "rejected")

    def test_changed_media_resets_previous_decision_to_pending(self):
        candidate_id = self.stage(13)
        set_candidate_decision(candidate_id, "approved")
        new_size = self.size + 1
        record_scan(self.media_path, new_size, self.modified + 1, {})
        save_candidate(self.media_path, new_size, self.modified + 1, self.candidate(13))
        row = list_candidates()[0]
        self.assertEqual(row["review_decision"], "pending")
        self.assertTrue(row["is_stale"] is False)

    def test_only_one_candidate_can_be_approved_per_media_file(self):
        first = self.stage(14)
        second = self.stage(15)
        set_candidate_decision(first, "approved")
        with self.assertRaisesRegex(ValueError, "Another candidate is already approved"):
            set_candidate_decision(second, "approved")

    def test_non_staged_candidate_cannot_be_approved(self):
        row = save_candidate(self.media_path, self.size, self.modified, self.candidate(16))
        with self.assertRaisesRegex(ValueError, "Only successfully staged"):
            set_candidate_decision(row["id"], "approved")


if __name__ == "__main__":
    unittest.main()
