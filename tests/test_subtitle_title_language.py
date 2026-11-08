import json
import subprocess
import unittest
from pathlib import Path
from unittest.mock import patch

from app.media import _subtitle_title_language, inspect_media


class SubtitleTitleLanguageTests(unittest.TestCase):
    def test_recognizes_language_names_codes_and_release_descriptors(self):
        for title in ("German", "German SDH", "Deutsch Forced", "ger", "deu", "de"):
            with self.subTest(title=title):
                self.assertEqual(_subtitle_title_language(title), "de")

    def test_does_not_match_language_names_inside_other_words(self):
        self.assertIsNone(_subtitle_title_language("Forced narrative"))

    def test_ambiguous_multiple_languages_are_not_used_as_a_hint(self):
        self.assertIsNone(_subtitle_title_language("English / German"))


class SubtitleTitleOverridesMetadataTests(unittest.TestCase):
    def inspect_with(self, title, content):
        probe = {
            "format": {"format_name": "matroska", "duration": "60", "size": "1000"},
            "streams": [{
                "index": 2, "codec_type": "subtitle", "codec_name": "subrip",
                "tags": {"language": "eng", "title": title},
            }],
        }
        completed = subprocess.CompletedProcess([], 0, json.dumps(probe), "")
        with patch("app.media.subprocess.run", return_value=completed), patch(
            "app.media._subtitle_content_language", return_value=content
        ):
            return inspect_media(Path("fixture.mkv"))

    def test_non_english_title_overrides_english_metadata_and_detection(self):
        result = self.inspect_with(
            "German SDH", {"status": "english", "language": "en", "confidence": 0.99}
        )
        self.assertFalse(result["has_verified_english_subtitle"])
        stream = result["subtitle_streams"][0]
        self.assertEqual(stream["language_metadata"], "eng")
        self.assertEqual(stream["title_language"], "de")
        self.assertEqual(stream["content_language"], "de")
        self.assertEqual(stream["content_status"], "other")

    def test_unknown_content_is_not_verified_by_an_unrecognized_title(self):
        result = self.inspect_with(
            "Full subtitles", {"status": "unknown", "language": None, "confidence": None}
        )
        self.assertFalse(result["has_verified_english_subtitle"])

    def test_english_content_stays_verified_with_a_generic_title(self):
        result = self.inspect_with(
            "Full subtitles", {"status": "english", "language": "en", "confidence": 0.99}
        )
        self.assertTrue(result["has_verified_english_subtitle"])


if __name__ == "__main__":
    unittest.main()
