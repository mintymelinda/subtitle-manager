import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from app.config import settings
from app.database import (
    initialize, record_scan, save_candidate, mark_candidate_staged,
    set_candidate_decision, get_candidate,
)
from app.main import (
    InjectRequest, MediaEditRequest, candidates, dashboard, edit_scanned_media, inject_candidate,
)


class InjectionApiSafetyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.old = {
            "database_path": settings.database_path,
            "media_root": settings.media_root,
            "subtitle_staging_path": settings.subtitle_staging_path,
            "test_mode": settings.test_mode,
        }
        settings.database_path = str(self.root / "db.sqlite3")
        settings.media_root = str(self.root / "media")
        settings.subtitle_staging_path = str(self.root / "staging")
        settings.test_mode = True
        self.media_root = Path(settings.media_root)
        self.staging_root = Path(settings.subtitle_staging_path)
        self.media_root.mkdir()
        self.staging_root.mkdir()
        initialize()
        self.source = self.media_root / "movie.mkv"
        self.source.write_bytes(b"original mkv placeholder")
        self.subtitle = self.staging_root / "candidate.srt"
        self.subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello world\n", encoding="utf-8")
        stat = self.source.stat()
        record_scan("movie.mkv", stat.st_size, stat.st_mtime, {})
        row = save_candidate("movie.mkv", stat.st_size, stat.st_mtime, {
            "file_id": 999, "file_name": "candidate.srt", "language": "en",
            "format": "srt", "match_score": 99, "match_reasons": ["exact hash"],
        })
        self.candidate_id = row["id"]
        mark_candidate_staged(self.candidate_id, str(self.subtitle), 10, {
            "content_status": "english", "content_language": "en", "content_confidence": 0.99,
        })

    def tearDown(self):
        settings.database_path = self.old["database_path"]
        settings.media_root = self.old["media_root"]
        settings.subtitle_staging_path = self.old["subtitle_staging_path"]
        settings.test_mode = self.old["test_mode"]
        self.temp.cleanup()

    def test_injection_requires_second_confirmation(self):
        set_candidate_decision(self.candidate_id, "approved")
        with self.assertRaises(HTTPException) as error:
            inject_candidate(self.candidate_id, InjectRequest(confirm=False))
        self.assertEqual(error.exception.status_code, 400)
        self.assertEqual(get_candidate(self.candidate_id)["patch_status"], "not_started")

    def test_injection_requires_explicit_candidate_approval(self):
        with patch("app.main.patch_mkv") as patcher:
            with self.assertRaises(HTTPException) as error:
                inject_candidate(self.candidate_id, InjectRequest(confirm=True))
            self.assertEqual(error.exception.status_code, 409)
            patcher.assert_not_called()

    def test_approved_candidate_injects_exact_staged_file_and_records_success(self):
        set_candidate_decision(self.candidate_id, "approved")
        with patch("app.main.patch_mkv", return_value={
            "status": "patched", "backup": "/tmp/recovery.bak", "test_mode": True,
        }) as patcher:
            result = inject_candidate(self.candidate_id, InjectRequest(confirm=True))
        patcher.assert_called_once_with(self.source, self.subtitle, approved=True, test_mode=True, expected_size=self.source.stat().st_size, expected_mtime=self.source.stat().st_mtime, keep_subtitle_indices=[], target_language="en")
        self.assertEqual(result["status"], "patched")
        self.assertEqual(get_candidate(self.candidate_id)["patch_status"], "patched")

    def test_changed_media_is_rejected_before_patch(self):
        set_candidate_decision(self.candidate_id, "approved")
        self.source.write_bytes(b"changed media content with a different size")
        with patch("app.main.patch_mkv") as patcher:
            with self.assertRaises(HTTPException) as error:
                inject_candidate(self.candidate_id, InjectRequest(confirm=True))
            self.assertEqual(error.exception.status_code, 409)
            patcher.assert_not_called()

    def test_parent_traversal_media_path_is_rejected(self):
        outside = self.root / "outside.mkv"
        outside.write_bytes(b"outside")
        stat = outside.stat()
        record_scan("../outside.mkv", stat.st_size, stat.st_mtime, {})
        row = save_candidate("../outside.mkv", stat.st_size, stat.st_mtime, {
            "file_id": 1000, "file_name": "candidate.srt", "language": "en",
            "format": "srt", "match_score": 90, "match_reasons": [],
        })
        mark_candidate_staged(row["id"], str(self.subtitle), 10, {"content_status": "english"})
        set_candidate_decision(row["id"], "approved")
        with patch("app.main.patch_mkv") as patcher:
            with self.assertRaises(HTTPException) as error:
                inject_candidate(row["id"], InjectRequest(confirm=True))
            self.assertEqual(error.exception.status_code, 400)
            patcher.assert_not_called()

    def test_symlinked_media_file_is_rejected(self):
        outside = self.root / "outside.mkv"
        outside.write_bytes(b"outside media")
        link = self.media_root / "linked.mkv"
        link.symlink_to(outside)
        stat = outside.stat()
        record_scan("linked.mkv", stat.st_size, stat.st_mtime, {})
        row = save_candidate("linked.mkv", stat.st_size, stat.st_mtime, {
            "file_id": 1001, "file_name": "candidate.srt", "language": "en",
            "format": "srt", "match_score": 90, "match_reasons": [],
        })
        mark_candidate_staged(row["id"], str(self.subtitle), 10, {"content_status": "english"})
        set_candidate_decision(row["id"], "approved")
        with patch("app.main.patch_mkv") as patcher:
            with self.assertRaises(HTTPException) as error:
                inject_candidate(row["id"], InjectRequest(confirm=True))
            self.assertEqual(error.exception.status_code, 400)
            patcher.assert_not_called()



    def test_all_scanned_files_option_includes_non_candidate_inventory(self):
        other = self.media_root / "other.mkv"
        other.write_bytes(b"other scanned media")
        stat = other.stat()
        record_scan("other.mkv", stat.st_size, stat.st_mtime, {
            "audio_streams": [{"index": 1, "codec": "aac"}],
            "subtitle_streams": [{"index": 2, "codec": "subrip", "language": "fra"}],
        })
        default_result = candidates()
        all_result = candidates(all=True)
        self.assertNotIn("other.mkv", [item["media_relative_path"] for item in default_result["candidates"]])
        scanned = next(item for item in all_result["candidates"] if item["media_relative_path"] == "other.mkv")
        self.assertTrue(scanned["is_non_candidate"])
        self.assertEqual(scanned["inspection"]["subtitle_streams"][0]["index"], 2)

    def test_non_candidate_edit_uses_track_only_patcher_and_records_history(self):
        other = self.media_root / "other.mkv"
        other.write_bytes(b"other scanned media")
        stat = other.stat()
        inspection = {
            "audio_streams": [{"index": 1, "codec": "aac"}],
            "subtitle_streams": [{"index": 2, "codec": "subrip", "language": "fra"}],
            "has_verified_english_subtitle": False,
        }
        record_scan("other.mkv", stat.st_size, stat.st_mtime, inspection)
        with patch("app.main.patch_mkv", return_value={
            "status": "patched", "backup": "/tmp/recovery.bak", "existing_tracks_only": True,
        }) as patcher, patch("app.main.inspect_media", return_value=inspection):
            result = edit_scanned_media(MediaEditRequest(
                confirm=True, media_relative_path="other.mkv", keep_subtitle_indices=[2],
            ))
        patcher.assert_called_once_with(
            other, None, approved=True, test_mode=True,
            expected_size=stat.st_size, expected_mtime=stat.st_mtime,
            keep_subtitle_indices=[2],
        )
        self.assertEqual(result["status"], "completed")
        from app.database import connect
        with connect() as db:
            history = db.execute(
                "SELECT candidate_id, status FROM patch_history WHERE relative_path='other.mkv'"
            ).fetchone()
        self.assertIsNone(history["candidate_id"])
        self.assertEqual(history["status"], "patched")

    def test_non_candidate_edit_rejects_unscanned_paths(self):
        with patch("app.main.patch_mkv") as patcher:
            with self.assertRaises(HTTPException) as error:
                edit_scanned_media(MediaEditRequest(
                    confirm=True, media_relative_path="not-scanned.mkv", keep_subtitle_indices=[],
                ))
        self.assertEqual(error.exception.status_code, 404)
        patcher.assert_not_called()


    def test_dashboard_supports_multi_select_non_candidate_batch_edits(self):
        html = dashboard().body.decode("utf-8")
        self.assertIn('id="show-all-scanned-files"', html)
        self.assertIn('id="batch-run" disabled>Approve &amp; patch selected</button>', html)
        self.assertIn('id="select-all-candidates"', html)
        self.assertIn("const selectedMediaPaths = new Set();", html)
        self.assertIn('class="candidate-select media-edit-select"', html)
        self.assertIn("selectedMediaPaths.has(c.media_relative_path)", html)
        self.assertIn('fetch("/api/media-files/edit"', html)

    def test_dashboard_retains_media_target_selection_across_renders(self):
        html = dashboard().body.decode("utf-8")
        self.assertIn("for (const path of [...selectedMediaPaths])", html)
        self.assertIn("selectedMediaPaths.add(c.media_relative_path)", html)
        self.assertIn("selectedMediaPaths.clear()", html)
        self.assertIn("const mediaPaths = [...selectedMediaPaths]", html)

if __name__ == "__main__":
    unittest.main()
