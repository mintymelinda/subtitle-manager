import tempfile
import unittest
from pathlib import Path

from app.scanner import scan_media


class ScannerTests(unittest.TestCase):
    def test_finds_supported_media_recursively(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "nested").mkdir()
            (root / "nested" / "movie.MKV").write_bytes(b"x")
            (root / "notes.txt").write_text("not media")
            found = list(scan_media(root))
            self.assertEqual([p.name for p in found], ["movie.MKV"])

    def test_rejects_missing_root(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(FileNotFoundError):
                list(scan_media(Path(directory) / "missing"))


if __name__ == "__main__":
    unittest.main()
