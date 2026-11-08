import tempfile
import unittest
from pathlib import Path

from fastapi import HTTPException

from app.config import settings
from app.main import media_directories


class MediaDirectoryBrowserTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.old_media_root = settings.media_root
        self.root = Path(self.temp.name) / "media"
        self.root.mkdir()
        (self.root / "Movies").mkdir()
        (self.root / "TV Shows").mkdir()
        (self.root / "Movies" / "2024").mkdir()
        (self.root / "movie.mkv").write_bytes(b"not a folder")
        settings.media_root = str(self.root)

    def tearDown(self):
        settings.media_root = self.old_media_root
        self.temp.cleanup()

    def test_lists_only_direct_child_directories_in_stable_order(self):
        result = media_directories("")
        self.assertEqual(result["display_path"], "/media")
        self.assertEqual([item["name"] for item in result["directories"]],
                         ["Movies", "TV Shows"])
        self.assertNotIn("movie.mkv", [item["name"] for item in result["directories"]])

    def test_lists_nested_directories_and_parent_navigation(self):
        result = media_directories("Movies")
        self.assertEqual(result["display_path"], "/media/Movies")
        self.assertEqual(result["parent_path"], "")
        self.assertEqual(result["directories"], [{"name": "2024", "path": "Movies/2024"}])

    def test_rejects_parent_directory_traversal(self):
        with self.assertRaises(HTTPException) as raised:
            media_directories("../")
        self.assertEqual(raised.exception.status_code, 400)

    def test_rejects_symlink_directories(self):
        link = self.root / "Outside"
        outside = Path(self.temp.name) / "outside"
        outside.mkdir()
        try:
            link.symlink_to(outside, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("Directory symlinks are unavailable on this platform")
        result = media_directories("")
        self.assertNotIn("Outside", [item["name"] for item in result["directories"]])
        with self.assertRaises(HTTPException):
            media_directories("Outside")


if __name__ == "__main__":
    unittest.main()
