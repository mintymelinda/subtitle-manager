import json
import os
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app.patcher import _validate_streams, patch_mkv


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
        if "-J" in command:
            return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"tracks": []}), stderr="")
        output = Path(command[command.index("-o") + 1])
        output.write_bytes(b"muxed media bytes")

    def run_patch(self, probes, test_mode=True, has_content=True):
        with patch("app.patcher._probe", side_effect=probes), patch(
            "app.patcher._subtitle_has_content", return_value=has_content
        ), patch("app.patcher.subprocess.run", side_effect=self.fake_mux):
            return patch_mkv(self.source, self.subtitle, approved=True, test_mode=test_mode)

    def test_validation_accepts_only_the_selected_existing_subtitle_tracks(self):
        before = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "ass", "jpn"), "index": 3},
        ]}
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "subrip", "eng"), "index": 3},
        ]}
        with patch("app.patcher._subtitle_has_content", return_value=True):
            _validate_streams(before, after, self.source, [2])

    def test_validation_rejects_existing_subtitle_tracks_marked_for_removal(self):
        before = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "ass", "jpn"), "index": 3},
        ]}
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "ass", "jpn"), "index": 3},
            {**stream("subtitle", "subrip", "eng"), "index": 4},
        ]}
        with self.assertRaisesRegex(RuntimeError, "unexpected existing subtitle tracks"):
            _validate_streams(before, after, self.source, [2])


    def test_existing_track_only_validation_accepts_exactly_selected_tracks(self):
        before = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "ass", "jpn"), "index": 3},
        ]}
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "ass", "jpn"), "index": 2},
        ]}
        _validate_streams(before, after, self.source, [3], subtitle_added=False)

    def test_existing_track_only_validation_rejects_any_unrequested_new_track(self):
        before = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
        ]}
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
            {**stream("subtitle", "subrip", "eng"), "index": 3},
        ]}
        with self.assertRaisesRegex(RuntimeError, "track count"):
            _validate_streams(before, after, self.source, [2], subtitle_added=False)

    def test_existing_track_only_validation_can_remove_every_subtitle(self):
        before = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
            {**stream("subtitle", "subrip", "fra"), "index": 2},
        ]}
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac"),
        ]}
        _validate_streams(before, after, self.source, [], subtitle_added=False)

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

    def test_empty_added_subtitle_is_rejected_before_replacement(self):
        with self.assertRaisesRegex(RuntimeError, "contains no subtitle cues"):
            self.run_patch([self.before, self.valid_after], has_content=False)
        self.assertEqual(self.source.read_bytes(), self.original)
        backups = list(self.root.glob(".*.recovery-*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_bytes(), self.original)

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



    def test_existing_track_only_remux_never_supplies_a_new_subtitle(self):
        after = {"format": {"format_name": "matroska"}, "streams": [
            stream("video", "h264"), stream("audio", "aac")
        ]}
        commands = []

        def mux(command, **_kwargs):
            commands.append(command)
            if "-J" in command:
                return subprocess.CompletedProcess(command, 0, stdout=json.dumps({"tracks": []}), stderr="")
            output = Path(command[command.index("-o") + 1])
            output.write_bytes(b"remuxed without subtitles")
            return subprocess.CompletedProcess(command, 0, stdout="", stderr="")

        with patch("app.patcher._probe", side_effect=[self.before, after, after]), patch(
            "app.patcher.subprocess.run", side_effect=mux
        ):
            result = patch_mkv(self.source, None, approved=True, keep_subtitle_indices=[])

        self.assertTrue(result["existing_tracks_only"])
        self.assertFalse(result["target_language_subtitle_added"])
        self.assertIn("--no-subtitles", commands[-1])
        self.assertNotIn(str(self.subtitle), commands[-1])

if __name__ == "__main__":
    unittest.main()
