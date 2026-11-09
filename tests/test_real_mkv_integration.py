"""Integration tests using real FFmpeg, MKVToolNix, and ffprobe binaries."""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from app.patcher import patch_mkv, _probe


TOOLS = ("ffmpeg", "ffprobe", "mkvmerge")


@unittest.skipUnless(all(shutil.which(tool) for tool in TOOLS),
                     "real media integration tools are not installed")
class RealMkvInjectionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "fixture.mkv"
        self.original_subtitle = self.root / "existing.srt"
        self.new_subtitle = self.root / "candidate.srt"
        self.original_subtitle.write_text(
            "1\n00:00:00,100 --> 00:00:00,700\nExisting subtitle\n",
            encoding="utf-8",
        )
        self.new_subtitle.write_text(
            "1\n00:00:00,200 --> 00:00:00,800\nNew English subtitle\n",
            encoding="utf-8",
        )
        raw = self.root / "raw.mkv"
        subprocess.run([
            "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
            "-f", "lavfi", "-i", "color=c=black:s=160x90:r=24:d=1",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
            "-shortest", "-c:v", "mpeg4", "-c:a", "aac", str(raw),
        ], check=True, capture_output=True, timeout=60)
        subprocess.run([
            "mkvmerge", "-o", str(self.source), str(raw),
            "--language", "0:eng", str(self.original_subtitle),
        ], check=True, capture_output=True, timeout=60)
        self.original_bytes = self.source.read_bytes()
        self.before = _probe(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def assert_media_tracks_preserved(self):
        after = _probe(self.source)
        before_streams = self.before["streams"]
        after_streams = after["streams"]
        signature = lambda stream: (
            stream.get("codec_type"), stream.get("codec_name"),
            stream.get("width"), stream.get("height"),
            stream.get("channels"), stream.get("sample_rate"),
            stream.get("channel_layout"),
        )
        before_av = [signature(s) for s in before_streams
                     if s.get("codec_type") in {"video", "audio"}]
        after_av = [signature(s) for s in after_streams
                    if s.get("codec_type") in {"video", "audio"}]
        self.assertEqual(after_av, before_av)
        before_subs = [s for s in before_streams if s.get("codec_type") == "subtitle"]
        after_subs = [s for s in after_streams if s.get("codec_type") == "subtitle"]
        self.assertGreaterEqual(len(after_subs), len(before_subs) + 1)
        self.assertTrue(any((s.get("tags") or {}).get("language") in {"eng", "en"}
                            for s in after_subs))
        self.assertIn("matroska", after.get("format", {}).get("format_name", ""))

    def test_real_mkv_empty_subtitle_is_rejected_and_original_is_preserved(self):
        self.new_subtitle.write_text("", encoding="utf-8")
        stat = self.source.stat()
        with self.assertRaisesRegex(RuntimeError, "contains no subtitle cues"):
            patch_mkv(
                self.source, self.new_subtitle, approved=True, test_mode=True,
                expected_size=stat.st_size, expected_mtime=stat.st_mtime,
            )
        self.assertEqual(self.source.read_bytes(), self.original_bytes)
        self.assertEqual(len(list(self.root.glob(".*.recovery-*.bak"))), 1)

    def test_real_mkv_injection_preserves_streams_and_test_mode_backup(self):
        stat = self.source.stat()
        result = patch_mkv(
            self.source, self.new_subtitle, approved=True, test_mode=True,
            expected_size=stat.st_size, expected_mtime=stat.st_mtime,
        )
        self.assertEqual(result["status"], "patched")
        self.assertTrue(result["test_mode"])
        backup = Path(result["backup"])
        self.assertTrue(backup.is_file())
        self.assertEqual(backup.read_bytes(), self.original_bytes)
        self.assert_media_tracks_preserved()

    def test_real_mkv_normal_mode_removes_backup_after_verification(self):
        stat = self.source.stat()
        result = patch_mkv(
            self.source, self.new_subtitle, approved=True, test_mode=False,
            expected_size=stat.st_size, expected_mtime=stat.st_mtime,
        )
        self.assertIsNone(result["backup"])
        self.assert_media_tracks_preserved()
        self.assertEqual(list(self.root.glob(".*.recovery-*.bak")), [])


if __name__ == "__main__":
    unittest.main()
