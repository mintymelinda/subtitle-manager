import tempfile
import unittest
from pathlib import Path

from app.patcher import patch_mkv


class PatcherSafetyTests(unittest.TestCase):
    def test_requires_explicit_approval_before_touching_files(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mkv"
            subtitle = Path(directory) / "movie.srt"
            media.write_bytes(b"not a real mkv")
            subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello\n", encoding="utf-8")
            with self.assertRaises(PermissionError):
                patch_mkv(media, subtitle, approved=False)
            self.assertEqual(media.read_bytes(), b"not a real mkv")

    def test_rejects_non_mkv_source_after_approval(self):
        with tempfile.TemporaryDirectory() as directory:
            media = Path(directory) / "movie.mp4"
            subtitle = Path(directory) / "movie.srt"
            media.write_bytes(b"placeholder")
            subtitle.write_text("subtitle", encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "Only MKV"):
                patch_mkv(media, subtitle, approved=True)


if __name__ == "__main__":
    unittest.main()
