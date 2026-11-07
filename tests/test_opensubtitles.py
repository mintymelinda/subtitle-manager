import tempfile
import unittest
import urllib.parse
from pathlib import Path

from app.opensubtitles import (
    OpenSubtitlesClient, OpenSubtitlesError, compute_moviehash,
    filename_query, rank_candidate, assess_subtitle_payload,
)


class FakeResponse:
    def __init__(self, payload, headers=None):
        self.payload = payload
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit=-1):
        return self.payload


class OpenSubtitlesTests(unittest.TestCase):
    def test_moviehash_uses_first_and_last_64k(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "movie.mkv"
            path.write_bytes(bytes(128 * 1024))
            self.assertEqual(compute_moviehash(path), "0000000000020000")

    def test_moviehash_skips_files_smaller_than_128k(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "small.mkv"
            path.write_bytes(b"x" * (128 * 1024 - 1))
            self.assertIsNone(compute_moviehash(path))

    def test_filename_query_normalizes_release_name(self):
        self.assertEqual(
            filename_query(Path("The.Matrix_1999.1080p.mkv")),
            "the matrix 1999 1080p",
        )

    def test_exact_hash_match_ranks_above_release_name_match(self):
        exact, reasons = rank_candidate(
            {"moviehash_match": True, "download_count": 0}, "The Matrix 1999"
        )
        name, _ = rank_candidate(
            {"release": "The.Matrix.1999.1080p"}, "The Matrix 1999"
        )
        self.assertGreater(exact, name)
        self.assertIn("Exact file fingerprint match", reasons)

    def test_search_sends_canonical_query_and_filters_results(self):
        captured = []

        def opener(request, timeout=30):
            captured.append(request)
            payload = {
                "data": [
                    {"attributes": {
                        "language": "fr", "release": "The Matrix",
                        "files": [{"file_id": 1, "file_name": "movie.srt"}],
                    }},
                    {"attributes": {
                        "language": "en", "release": "The Matrix 1999",
                        "moviehash_match": True, "download_count": 100,
                        "files": [
                            {"file_id": 2, "file_name": "movie.srt"},
                            {"file_id": 3, "file_name": "archive.zip"},
                        ],
                    }},
                    {"attributes": {
                        "language": "en", "machine_translated": True,
                        "files": [{"file_id": 4, "file_name": "machine.srt"}],
                    }},
                ]
            }
            return FakeResponse(__import__("json").dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "The.Matrix.1999.mkv"
            path.write_bytes(bytes(128 * 1024))
            client = OpenSubtitlesClient("test-secret", opener=opener, sleeper=lambda _s: None)
            results = client.search(path, {"title": "The Matrix", "year": "1999", "imdb_id": "tt0133093"})

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["file_id"], 2)
        self.assertTrue(results[0]["moviehash_match"])
        query = urllib.parse.urlparse(captured[0].full_url).query
        self.assertEqual(query, "imdb_id=133093&languages=en&moviehash=0000000000020000&query=the+matrix+1999")
        self.assertEqual(captured[0].get_header("Api-key"), "test-secret")
        self.assertEqual(captured[0].get_header("User-agent"), "SubtitleManager v0.1.0")


    def test_subtitle_payload_language_assessment(self):
        english = ("This is a long English subtitle sentence with enough words to "
                   "identify the language reliably. " * 5).encode()
        french = ("Ceci est un texte français suffisamment long pour identifier "
                  "la langue du sous-titre avec une certaine confiance. " * 5).encode()
        self.assertEqual(assess_subtitle_payload("candidate.srt", english)["content_status"], "english")
        self.assertEqual(assess_subtitle_payload("candidate.srt", french)["content_status"], "other")
        self.assertEqual(assess_subtitle_payload("candidate.srt", b"Hi")["content_status"], "unknown")
        self.assertEqual(assess_subtitle_payload("candidate.sup", b"binary")["content_status"], "unknown")

    def test_empty_api_key_is_rejected(self):
        with self.assertRaises(OpenSubtitlesError):
            OpenSubtitlesClient("")

    def test_zip_extraction_rejects_path_traversal_entry(self):
        import io
        import zipfile

        archive_bytes = io.BytesIO()
        with zipfile.ZipFile(archive_bytes, "w") as archive:
            archive.writestr("../escape.srt", "not safe")
            archive.writestr("good.srt", "1\n00:00:01,000 --> 00:00:02,000\nHello\n")
        name, content = OpenSubtitlesClient._extract_zip(archive_bytes.getvalue(), "fallback.srt")
        self.assertEqual(name, "good.srt")
        self.assertIn(b"Hello", content)


if __name__ == "__main__":
    unittest.main()
