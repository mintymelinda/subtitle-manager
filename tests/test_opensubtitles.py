import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

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

    def test_search_uses_filename_query_instead_of_container_series_title(self):
        captured = []

        def opener(request, timeout=30):
            captured.append(request)
            return FakeResponse(b'{"data": []}')

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "Bob's.Burgers.S01E12.Lobsterfest.WEBDL-1080p.mkv"
            path.write_bytes(b"small test file")
            client = OpenSubtitlesClient("test-secret", opener=opener, sleeper=lambda _s: None)
            client.search(path, {
                "title": "Bob's Burgers",
                "year": "2011",
                "imdb_id": "tt1234567",
            })

        params = urllib.parse.parse_qs(urllib.parse.urlparse(captured[0].full_url).query)
        self.assertEqual(
            params["query"][0],
            "bob's burgers s01e12 lobsterfest webdl-1080p",
        )
        self.assertEqual(params["parent_imdb_id"], ["1234567"])
        self.assertEqual(params["season_number"], ["1"])
        self.assertEqual(params["episode_number"], ["12"])
        self.assertNotIn("imdb_id", params)

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
            with self.assertLogs("subtitle_manager", level="DEBUG") as captured_logs:
                results = client.search(path, {"title": "The Matrix", "year": "1999", "imdb_id": "tt0133093"})

        self.assertEqual({item["file_id"] for item in results}, {2, 3})
        self.assertEqual(next(item for item in results if item["file_id"] == 3)["format"], "")
        diagnostic_output = "\\n".join(captured_logs.output)
        self.assertIn("opensubtitles_candidate_filtered", diagnostic_output)
        self.assertIn("unsupported_language", diagnostic_output)
        self.assertIn("machine_translated", diagnostic_output)
        self.assertNotIn("unsupported_format", diagnostic_output)
        self.assertIn("opensubtitles_candidate_accepted", diagnostic_output)
        self.assertIn("accepted_count", diagnostic_output)
        self.assertTrue(results[0]["moviehash_match"])
        query = urllib.parse.urlparse(captured[0].full_url).query
        self.assertEqual(query, "imdb_id=133093&languages=en&moviehash=0000000000020000&query=the+matrix+1999")
        self.assertEqual(captured[0].get_header("Api-key"), "test-secret")
        self.assertEqual(captured[0].get_header("User-agent"), "SubtitleManager v0.1.0")



    def test_search_preserves_extensionless_provider_files_for_all_13_episodes(self):
        """Regression for the Bob's Burgers S1 log: provider names end in .eng."""
        next_file_id = 100

        def opener(request, timeout=30):
            nonlocal next_file_id
            next_file_id += 1
            payload = {
                "data": [{
                    "attributes": {
                        "language": "en",
                        "release": "Bobs Burgers",
                        "files": [{
                            "file_id": next_file_id,
                            "file_name": "Bobs Burgers - S01E01 - Episode.eng",
                        }],
                    }
                }]
            }
            return FakeResponse(__import__("json").dumps(payload).encode())

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            client = OpenSubtitlesClient(
                "test-secret", opener=opener, sleeper=lambda _s: None
            )
            discovered = []
            for episode in range(1, 14):
                media = root / (
                    f"Bobs.Burgers.S01E{episode:02d}.Episode.WEBDL-1080p.mkv"
                )
                media.write_bytes(b"small test media")
                discovered.extend(client.search(media))

        self.assertEqual(len(discovered), 13)
        self.assertEqual(len({item["file_id"] for item in discovered}), 13)
        self.assertTrue(all(item["format"] == "" for item in discovered))
        self.assertTrue(all(item["file_id"] > 0 for item in discovered))

    def test_subtitle_payload_language_assessment(self):
        english = ("This is a long English subtitle sentence with enough words to "
                   "identify the language reliably. " * 5).encode()
        french = ("Ceci est un texte français suffisamment long pour identifier "
                  "la langue du sous-titre avec une certaine confiance. " * 5).encode()
        self.assertEqual(assess_subtitle_payload("candidate.srt", english)["content_status"], "english")
        self.assertEqual(assess_subtitle_payload("candidate.srt", french)["content_status"], "other")
        self.assertEqual(assess_subtitle_payload("candidate.srt", b"Hi")["content_status"], "unknown")
        self.assertEqual(assess_subtitle_payload("candidate.sup", b"binary")["content_status"], "unknown")

    def test_initial_download_quota_probe_is_serialized(self):
        client = OpenSubtitlesClient("test-secret")
        active = 0
        peak = 0
        lock = threading.Lock()

        def download(file_id):
            nonlocal active, peak
            with lock:
                active += 1
                peak = max(peak, active)
            time.sleep(0.02)
            with lock:
                active -= 1
            return f"{file_id}.srt", b"subtitle", None

        with patch.object(client, "download", side_effect=download):
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(client.download_if_available, range(4)))

        self.assertEqual(len(results), 4)
        self.assertEqual(peak, 1)

    def test_concurrent_download_reservations_never_exceed_known_quota(self):
        client = OpenSubtitlesClient("test-secret")
        client.remaining_downloads = 2
        with patch.object(
            client, "download",
            side_effect=lambda file_id: (f"{file_id}.srt", b"subtitle", 1),
        ) as download:
            with ThreadPoolExecutor(max_workers=4) as executor:
                results = list(executor.map(client.download_if_available, range(4)))

        self.assertEqual(download.call_count, 2)
        self.assertEqual(sum(result is not None for result in results), 2)
        self.assertEqual(client.remaining_downloads, 0)

    def test_provider_http_errors_expose_safe_retry_diagnostics(self):
        cases = (
            (403, "authentication_rejected", False),
            (429, "rate_limited", True),
            (503, "provider_unavailable", True),
            (422, "provider_request_rejected", False),
        )
        for status, expected_code, expected_retryable in cases:
            with self.subTest(status=status):
                calls = []

                def opener(request, timeout=30):
                    calls.append(request)
                    raise urllib.error.HTTPError(
                        request.full_url, status, "provider response", {}, None
                    )

                client = OpenSubtitlesClient(
                    "test-secret", opener=opener, sleeper=lambda _seconds: None
                )
                with self.assertRaises(OpenSubtitlesError) as raised:
                    client._json_request("/subtitles")

                error = raised.exception
                self.assertEqual(error.error_code, expected_code)
                self.assertEqual(error.retryable, expected_retryable)
                self.assertEqual(error.http_status, status)
                self.assertNotIn("provider response", str(error))
                self.assertEqual(len(calls), 2 if status == 429 else 1)

    def test_download_link_http_errors_are_classified_without_exposing_url(self):
        def opener(request, timeout=60):
            raise urllib.error.HTTPError(
                request.full_url, 403, "signed URL expired", {}, None
            )

        client = OpenSubtitlesClient("test-secret", opener=opener)
        with patch.object(client, "_json_request", return_value={
            "link": "https://www.opensubtitles.com/download/temporary-token",
            "file_name": "candidate.srt",
        }):
            with self.assertRaises(OpenSubtitlesError) as raised:
                client.download(123)

        error = raised.exception
        self.assertEqual(error.error_code, "download_link_rejected")
        self.assertTrue(error.retryable)
        self.assertEqual(error.http_status, 403)
        self.assertNotIn("temporary-token", str(error))

    def test_download_406_quota_response_exposes_reset_time(self):
        body = __import__("json").dumps({
            "message": "Daily limit quota exceeded",
            "reset_at": "2026-11-12T00:00:00Z",
        }).encode()
        def opener(request, timeout=30):
            raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {},
                __import__("io").BytesIO(body))
        client = OpenSubtitlesClient("test-secret", opener=opener)
        with self.assertRaises(OpenSubtitlesError) as raised:
            client._json_request("/download", body={"file_id": 123})
        error = raised.exception
        self.assertEqual(error.error_code, "download_quota_reached")
        self.assertTrue(error.retryable)
        self.assertEqual(error.http_status, 406)
        self.assertEqual(error.retry_at, "2026-11-12T00:00:00+00:00")
        self.assertNotIn("Daily limit quota exceeded", str(error))
        self.assertEqual(client.quota_retry_at, error.retry_at)

    def test_download_406_without_quota_message_is_not_misclassified(self):
        body = __import__("json").dumps({"message": "Invalid file_id"}).encode()
        def opener(request, timeout=30):
            raise urllib.error.HTTPError(request.full_url, 406, "Not Acceptable", {},
                __import__("io").BytesIO(body))
        client = OpenSubtitlesClient("test-secret", opener=opener)
        with self.assertRaises(OpenSubtitlesError) as raised:
            client._json_request("/download", body={"file_id": 123})
        self.assertEqual(raised.exception.error_code, "provider_request_rejected")
        self.assertFalse(raised.exception.retryable)
        self.assertIsNone(client.quota_retry_at)

    def test_download_406_exposes_only_sanitized_bounded_provider_detail(self):
        detail = "Invalid file_id; api_key=provider-secret https://example.test/download/signed-token"
        body = __import__("json").dumps({"message": detail}).encode()

        def opener(request, timeout=30):
            raise urllib.error.HTTPError(
                request.full_url, 406, "Not Acceptable", {},
                __import__("io").BytesIO(body),
            )

        client = OpenSubtitlesClient("test-secret", opener=opener)
        with self.assertRaises(OpenSubtitlesError) as raised:
            client._json_request("/download", body={"file_id": 123})

        error = raised.exception
        self.assertEqual(error.error_code, "provider_request_rejected")
        self.assertFalse(error.retryable)
        self.assertIn("Invalid file_id", error.provider_error_detail)
        self.assertIn("api_key=[REDACTED]", error.provider_error_detail)
        self.assertIn("[URL REDACTED]", error.provider_error_detail)
        self.assertNotIn("provider-secret", error.provider_error_detail)
        self.assertNotIn("signed-token", error.provider_error_detail)
        self.assertLessEqual(len(error.provider_error_detail), 240)
        self.assertNotIn("provider-secret", str(error))
        self.assertIsNone(client.quota_retry_at)

    def test_download_406_malformed_body_has_no_provider_detail(self):
        def opener(request, timeout=30):
            raise urllib.error.HTTPError(
                request.full_url, 406, "Not Acceptable", {},
                __import__("io").BytesIO(b"<html>upstream failure</html>"),
            )

        client = OpenSubtitlesClient("test-secret", opener=opener)
        with self.assertRaises(OpenSubtitlesError) as raised:
            client._json_request("/download", body={"file_id": 123})

        self.assertEqual(raised.exception.error_code, "provider_request_rejected")
        self.assertIsNone(raised.exception.provider_error_detail)

    def test_quota_block_prevents_further_download_requests(self):
        client = OpenSubtitlesClient("test-secret")
        client.quota_retry_at = "2026-11-12T00:00:00+00:00"
        with patch.object(client, "download") as download:
            self.assertIsNone(client.download_if_available(123))
        download.assert_not_called()

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
