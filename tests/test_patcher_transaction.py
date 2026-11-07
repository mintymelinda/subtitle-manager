import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.patcher import patch_mkv


def stream(kind, codec, language=None):
    item = {"codec_type": kind, "codec_name": codec}
    if language:
        item["tags"] = {"language": language}
    return item


class TransactionalPatcherTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "movie.mkv"
        self.subtitle = self.root / "subtitle.srt"
        self.original = b"original media bytes"
        self.source.write_bytes(self.original)
        self.subtitle.write_text("1\n00:00:01,000 --> 00:00:02,000\nHello there, this is a subtitle.\n", encoding="utf-8")
        os.chmod(self.source, 0o640)
        self.before = {"format": {"format_name": "matroska"}, "streams": [stream("video", "h264"), stream("audio", "aac")]}
        self.valid_after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"), stream("subtitle", "subrip", "eng")
        ]}

    def tearDown(self):
        self.temp.cleanup()

    def fake_mux(self, command, **_kwargs):
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"muxed media bytes")

    def run_patch(self, probes, test_mode=True):
        with patch("app.patcher._probe", side_effect=probes), patch(
            "app.patcher.subprocess.run", side_effect=self.fake_mux
        ):
            return patch_mkv(self.source, self.subtitle, approved=True, test_mode=test_mode)

    def test_test_mode_patches_one_file_and_retains_recovery_copy(self):
        result = self.run_patch([self.before, self.valid_after, self.valid_after])
        self.assertEqual(result["status"], "patched")
        self.assertTrue(result["test_mode"])
        self.assertTrue(Path(result["backup"]).is_file())
        self.assertEqual(Path(result["backup"]).read_bytes(), self.original)
        self.assertEqual(stat.S_IMODE(self.source.stat().st_mode), 0o640)
        self.assertTrue(result["metadata_preserved"])

    def test_normal_mode_deletes_recovery_only_after_verification(self):
        result = self.run_patch([self.before, self.valid_after, self.valid_after], test_mode=False)
        self.assertIsNone(result["backup"])
        self.assertFalse(list(self.root.glob("*.recovery-*.bak")))
        self.assertNotEqual(self.source.read_bytes(), self.original)

    def test_pre_replacement_validation_failure_leaves_original_and_backup(self):
        invalid = {"format": {"format_name": "matroska"}, "streams": [stream("video", "h264"), stream("audio", "aac")]}
        with self.assertRaisesRegex(RuntimeError, "new subtitle stream is missing"):
            self.run_patch([self.before, invalid])
        self.assertEqual(self.source.read_bytes(), self.original)
        backups = list(self.root.glob(".*.recovery-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_post_replacement_failure_rolls_back_and_retains_recovery_copy(self):
        invalid_final = {"format": {"format_name": "matroska"}, "streams": [stream("video", "h264"), stream("audio", "aac")]}
        with self.assertRaisesRegex(RuntimeError, "new subtitle stream is missing"):
            self.run_patch([self.before, self.valid_after, invalid_final])
        self.assertEqual(self.source.read_bytes(), self.original)
        backups = list(self.root.glob(".*.recovery-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

    def test_rejects_media_that_does_not_match_approved_snapshot(self):
        with patch("app.patcher._probe") as probe:
            with self.assertRaisesRegex(RuntimeError, "size no longer matches"):
                patch_mkv(
                    self.source, self.subtitle, approved=True,
                    expected_size=self.source.stat().st_size + 1,
                    expected_mtime=self.source.stat().st_mtime,
                )
            probe.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_requires_explicit_approval_before_any_probe_or_write(self):
        with patch("app.patcher._probe") as probe:
            with self.assertRaises(PermissionError):
                patch_mkv(self.source, self.subtitle, approved=False)
            probe.assert_not_called()
        self.assertEqual(self.source.read_bytes(), self.original)


if __name__ == "__main__":
    unittest.main()
