import tempfile
import unittest
from pathlib import Path

from app.config import settings
from app.database import initialize, list_media_files, record_media_patch, record_scan


class AllScannedFilesDatabaseTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_path = settings.database_path
        settings.database_path = str(Path(self.temp.name) / "all-files.sqlite3")
        initialize()

    def tearDown(self):
        settings.database_path = self.old_path
        self.temp.cleanup()

    def test_inventory_includes_files_without_subtitle_candidates(self):
        record_scan("Movies/no-candidate.mkv", 123, 456.5, {
            "audio_streams": [{"codec": "aac", "language": "eng"}],
            "subtitle_streams": [{"index": 2, "codec": "subrip", "language": "fra"}],
            "has_verified_english_subtitle": False,
        })
        files = list_media_files()
        self.assertEqual(len(files), 1)
        self.assertEqual(files[0]["relative_path"], "Movies/no-candidate.mkv")
        self.assertEqual(files[0]["inspection"]["subtitle_streams"][0]["index"], 2)

    def test_existing_track_edits_are_recorded_without_candidate_identity(self):
        record_media_patch("Movies/no-candidate.mkv", "patched", {
            "existing_tracks_only": True,
            "existing_subtitle_streams_kept": [2],
            "existing_subtitle_streams_removed": [3],
        }, "/media/.movie.recovery-test.bak")
        from app.database import connect
        with connect() as db:
            row = db.execute("SELECT * FROM patch_history").fetchone()
        self.assertIsNone(row["candidate_id"])
        self.assertEqual(row["status"], "patched")
        self.assertIn('"existing_tracks_only": true', row["details"])


if __name__ == "__main__":
    unittest.main()
