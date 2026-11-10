"""Opt-in real-media injection profiler for issue #66.

Run with SUBTITLE_MANAGER_RUN_INJECTION_PROFILE=1 after placing the media fixture
at tests/fixtures/injection/test.mkv. This deliberately stays out of normal CI.
"""
import json
import logging
import os
import tempfile
import time
import unittest
from pathlib import Path

from app.patcher import patch_mkv


FIXTURE = Path(__file__).parent / "fixtures" / "injection" / "test.mkv"
PROFILE_ENABLED = os.environ.get("SUBTITLE_MANAGER_RUN_INJECTION_PROFILE") == "1"


class StageTimingCapture(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.INFO)
        self.events = []

    def emit(self, record):
        if getattr(record, "event", None) == "stage_timing":
            self.events.append({
                "stage": getattr(record, "stage", "unknown"),
                "elapsed_ms": getattr(record, "elapsed_ms", None),
                "bytes_processed": getattr(record, "bytes_processed", None),
                "file_size_bytes": getattr(record, "file_size_bytes", None),
                "tool": getattr(record, "tool", None),
            })


@unittest.skipUnless(PROFILE_ENABLED and FIXTURE.is_file(),
                     "set SUBTITLE_MANAGER_RUN_INJECTION_PROFILE=1 and add tests/fixtures/injection/test.mkv")
class InjectionPerformanceProfileTests(unittest.TestCase):
    def test_profile_real_mkv_injection_stages(self):
        source_size = FIXTURE.stat().st_size
        capture = StageTimingCapture()
        logger = logging.getLogger("subtitle_manager")
        previous_level = logger.level
        logger.setLevel(logging.INFO)
        logger.addHandler(capture)

        try:
            with tempfile.TemporaryDirectory(prefix="subtitle-injection-profile-") as directory:
                workdir = Path(directory)
                source = workdir / "test.mkv"
                subtitle = workdir / "profile.srt"
                source.write_bytes(FIXTURE.read_bytes())
                subtitle.write_text(
                    "1\n00:00:00,000 --> 00:00:01,000\n"
                    "Subtitle injection profiling cue.\n",
                    encoding="utf-8",
                )

                started = time.perf_counter()
                result = patch_mkv(
                    source,
                    subtitle,
                    approved=True,
                    test_mode=True,
                    target_language="en",
                )
                total_elapsed_ms = round((time.perf_counter() - started) * 1000, 2)

                self.assertEqual(result["status"], "patched")
                self.assertTrue(result["test_mode"])
                self.assertTrue(result["backup"])
                self.assertTrue(Path(result["backup"]).is_file())

                stages = {event["stage"] for event in capture.events}
                required_stages = {
                    "injection_recovery_copy",
                    "injection_mkvmerge_inspection",
                    "injection_remux",
                    "injection_candidate_validation",
                    "injection_packet_validation",
                    "injection_subtitle_decode_validation",
                    "injection_post_replace_validation",
                }
                self.assertTrue(
                    required_stages.issubset(stages),
                    f"Missing timing stages: {sorted(required_stages - stages)}; "
                    f"captured: {capture.events}",
                )

                report = {
                    "fixture": str(FIXTURE),
                    "fixture_size_bytes": source_size,
                    "total_injection_elapsed_ms": total_elapsed_ms,
                    "stage_timings": capture.events,
                }
                print("INJECTION_PROFILE " + json.dumps(report, sort_keys=True))
        finally:
            logger.removeHandler(capture)
            logger.setLevel(previous_level)


if __name__ == "__main__":
    unittest.main()
