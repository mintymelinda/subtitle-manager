import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.config import settings
from app.database import (
    create_job,
    get_cached_scan,
    get_job,
    initialize,
    record_scan,
)
from app.main import _scan_worker


class InspectionCacheTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        self.old_db = settings.database_path
        self.old_media_root = settings.media_root
        settings.database_path = str(Path(self.temp.name) / "cache.sqlite3")
        settings.media_root = str(self.root)
        initialize()

    def tearDown(self):
        settings.database_path = self.old_db
        settings.media_root = self.old_media_root
        self.temp.cleanup()

    def test_cached_inspection_requires_exact_filesystem_identity(self):
        media = self.root / "movie.mkv"
        media.write_bytes(b"original media")
        original_stat = media.stat()
        inspection = {"has_verified_english_subtitle": True, "subtitle_streams": []}
        record_scan(
            "movie.mkv", original_stat.st_size, original_stat.st_mtime, inspection,
            modified_ns=original_stat.st_mtime_ns,
            device=original_stat.st_dev,
            inode=original_stat.st_ino,
        )

        self.assertEqual(get_cached_scan("movie.mkv", original_stat), inspection)

        os.utime(
            media,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns + 1_000_000_000),
        )
        changed_stat = media.stat()
        self.assertIsNone(get_cached_scan("movie.mkv", changed_stat))

    def test_replaced_file_with_same_size_and_timestamp_is_a_cache_miss(self):
        media = self.root / "replacement.mkv"
        media.write_bytes(b"same-size")
        original_stat = media.stat()
        record_scan(
            "replacement.mkv", original_stat.st_size, original_stat.st_mtime,
            {"has_verified_english_subtitle": False},
            modified_ns=original_stat.st_mtime_ns,
            device=original_stat.st_dev,
            inode=original_stat.st_ino,
        )

        replacement = self.root / "replacement.tmp"
        replacement.write_bytes(b"same-size")
        os.utime(
            replacement,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        os.replace(replacement, media)
        replaced_stat = media.stat()

        self.assertEqual(replaced_stat.st_size, original_stat.st_size)
        self.assertEqual(replaced_stat.st_mtime_ns, original_stat.st_mtime_ns)
        self.assertNotEqual(replaced_stat.st_ino, original_stat.st_ino)
        self.assertIsNone(get_cached_scan("replacement.mkv", replaced_stat))

    def test_legacy_rows_without_filesystem_identity_are_cache_misses(self):
        media = self.root / "legacy.mkv"
        media.write_bytes(b"legacy")
        stat_result = media.stat()
        record_scan(
            "legacy.mkv", stat_result.st_size, stat_result.st_mtime,
            {"has_verified_english_subtitle": False},
        )
        self.assertIsNone(get_cached_scan("legacy.mkv", stat_result))

    def test_scan_worker_reuses_unchanged_inspection_but_still_completes_scan(self):
        media = self.root / "movie.mkv"
        media.write_bytes(b"unchanged")
        stat_result = media.stat()
        inspection = {
            "has_verified_english_subtitle": False,
            "metadata": {},
            "subtitle_streams": [],
        }
        record_scan(
            "movie.mkv", stat_result.st_size, stat_result.st_mtime, inspection,
            modified_ns=stat_result.st_mtime_ns,
            device=stat_result.st_dev,
            inode=stat_result.st_ino,
        )
        job = create_job("scan", total=1)

        with patch("app.main.inspect_media") as inspect:
            _scan_worker(job["id"], self.root, [media])

        inspect.assert_not_called()
        self.assertEqual(get_job(job["id"])["status"], "completed")
        self.assertEqual(get_job(job["id"])["succeeded"], 1)

    def test_scan_worker_reinspects_when_file_changes(self):
        media = self.root / "movie.mkv"
        media.write_bytes(b"old")
        old_stat = media.stat()
        record_scan(
            "movie.mkv", old_stat.st_size, old_stat.st_mtime,
            {"has_verified_english_subtitle": False},
            modified_ns=old_stat.st_mtime_ns,
            device=old_stat.st_dev,
            inode=old_stat.st_ino,
        )
        media.write_bytes(b"new content with different size")
        job = create_job("scan", total=1)
        inspection = {
            "has_verified_english_subtitle": True,
            "metadata": {},
            "subtitle_streams": [],
        }

        with patch("app.main.inspect_media", return_value=inspection) as inspect:
            _scan_worker(job["id"], self.root, [media])

        inspect.assert_called_once_with(media)
        self.assertEqual(get_job(job["id"])["status"], "completed")
        self.assertEqual(get_cached_scan("movie.mkv", media.stat()), inspection)


if __name__ == "__main__":
    unittest.main()
