import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.config import settings
from app.main import stage_candidates
from app.opensubtitles import OpenSubtitlesError


class DownloadDiagnosticsTests(unittest.TestCase):
    def test_failed_candidate_is_recorded_and_later_candidate_still_stages(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            media_root = root / "media"
            media_root.mkdir()
            media_path = media_root / "episode.mkv"
            media_path.write_bytes(b"media fixture")
            staging_root = root / "staging"

            candidates = [
                {
                    "subtitle_id": "first",
                    "file_id": 101,
                    "file_name": "first.srt",
                    "language": "en",
                    "format": "srt",
                    "release": "Episode",
                    "moviehash_match": True,
                    "download_count": 10,
                    "trusted_uploader": False,
                    "hearing_impaired": False,
                    "ai_translated": False,
                    "match_score": 100,
                    "match_reasons": [],
                },
                {
                    "subtitle_id": "second",
                    "file_id": 102,
                    "file_name": "second.srt",
                    "language": "en",
                    "format": "srt",
                    "release": "Episode",
                    "moviehash_match": False,
                    "download_count": 5,
                    "trusted_uploader": False,
                    "hearing_impaired": False,
                    "ai_translated": False,
                    "match_score": 50,
                    "match_reasons": [],
                },
            ]
            client = SimpleNamespace(
                search=Mock(return_value=candidates),
                download_if_available=Mock(side_effect=[
                    OpenSubtitlesError(
                        "OpenSubtitles is temporarily unavailable.",
                        error_code="provider_unavailable",
                        retryable=True,
                        http_status=503,
                    ),
                    ("second.srt", b"subtitle payload", 4),
                ]),
            )
            saved_rows = [
                {
                    "id": 1,
                    "status": "pending",
                    "content_status": "unknown",
                    "content_language": None,
                    "content_language_confidence": None,
                },
                {
                    "id": 2,
                    "status": "pending",
                    "content_status": "unknown",
                    "content_language": None,
                    "content_language_confidence": None,
                },
            ]

            with (
                patch.object(settings, "subtitle_staging_path", str(staging_root)),
                patch("app.main.load_provider_settings", return_value={
                    "max_candidates_per_file": 2,
                }),
                patch("app.main.save_candidate", side_effect=saved_rows),
                patch("app.main.assess_subtitle_payload", return_value={
                    "content_status": "unknown",
                    "content_language": None,
                    "content_confidence": None,
                }),
                patch("app.main.mark_candidate_error") as mark_error,
                patch("app.main.mark_candidate_staged") as mark_staged,
            ):
                summary = stage_candidates(
                    client, media_path, media_root, media_path.stat(), {},
                    target_languages=["en"],
                )

            self.assertEqual(client.download_if_available.call_count, 2)
            mark_error.assert_called_once()
            self.assertEqual(mark_error.call_args.args[0:2], (
                1, "OpenSubtitles is temporarily unavailable."
            ))
            mark_staged.assert_called_once()
            self.assertEqual(summary["staged"], 1)
            self.assertEqual(
                [candidate["status"] for candidate in summary["candidates"]],
                ["download_failed", "staged"],
            )
            self.assertEqual(
                summary["candidates"][0]["error_code"], "provider_unavailable"
            )
            self.assertTrue(summary["candidates"][0]["retryable"])
            self.assertIsNone(summary["candidates"][1]["error_code"])
            self.assertTrue((staging_root / "candidate-2.srt").is_file())


if __name__ == "__main__":
    unittest.main()
