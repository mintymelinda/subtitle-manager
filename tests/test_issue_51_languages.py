import unittest

from app.languages import LANGUAGE_CODES, normalize_languages, language_matches
from app.opensubtitles import OpenSubtitlesClient
from app.patcher import _validate_streams
from pathlib import Path
from unittest.mock import patch


class TargetLanguageTests(unittest.TestCase):
    def test_language_catalog_contains_common_languages(self):
        self.assertTrue({"en", "fr", "de", "es", "ja", "zh"}.issubset(LANGUAGE_CODES))

    def test_normalize_languages_requires_valid_nonempty_unique_codes(self):
        self.assertEqual(normalize_languages(["FR", "en", "fr"]), ["fr", "en"])
        with self.assertRaises(ValueError):
            normalize_languages([])
        with self.assertRaises(ValueError):
            normalize_languages(["xx"])

    def test_language_match_accepts_iso_639_2_and_regional_tags(self):
        self.assertTrue(language_matches("fra", "fr"))
        self.assertTrue(language_matches("zh-CN", "zh"))
        self.assertFalse(language_matches("de", "fr"))

    def test_patcher_verifies_the_selected_non_english_language(self):
        before = {"format": {"format_name": "matroska"}, "streams": []}
        after = {"format": {"format_name": "matroska"}, "streams": [
            {"codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "fra", "language_ietf": "fr"}},
        ]}
        with patch("app.patcher._subtitle_has_content", return_value=True):
            _validate_streams(before, after, Path("film.mkv"), [], "fr")
        wrong_language = {"format": {"format_name": "matroska"}, "streams": [
            {"codec_type": "subtitle", "codec_name": "subrip",
             "tags": {"language": "deu", "language_ietf": "de"}},
        ]}
        with self.assertRaisesRegex(RuntimeError, "selected target language"):
            _validate_streams(before, wrong_language, Path("film.mkv"), [], "fr")

    def test_search_queries_and_filters_multiple_target_languages(self):
        captured = []

        class Response:
            headers = {}
            def __enter__(self):
                return self
            def __exit__(self, *_args):
                return False
            def read(self, _limit=-1):
                return (b'{"data": ['
                        b'{"attributes":{"language":"fr","files":[{"file_id":1,"file_name":"fr.srt"}]}},'
                        b'{"attributes":{"language":"de","files":[{"file_id":2,"file_name":"de.srt"}]}},'
                        b'{"attributes":{"language":"es","files":[{"file_id":3,"file_name":"es.srt"}]}}]}')

        def opener(request, timeout=30):
            captured.append(request)
            return Response()

        from pathlib import Path
        import tempfile
        import urllib.parse
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "film.mkv"
            media.write_bytes(b"small media")
            client = OpenSubtitlesClient("test-key", opener=opener, sleeper=lambda _s: None)
            results = client.search(media, languages=["fr", "de"])
        self.assertEqual([item["language"] for item in results], ["fr", "de"])
        query = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0].full_url).query)
        self.assertEqual(query["languages"], ["de,fr"])


if __name__ == "__main__":
    unittest.main()
