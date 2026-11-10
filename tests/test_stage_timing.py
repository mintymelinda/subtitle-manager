import json
import unittest

from app.observability import JsonFormatter, timed_stage


class StageTimingTests(unittest.TestCase):
    def test_stage_timing_logs_elapsed_time_and_safe_context(self):
        with self.assertLogs("subtitle_manager", level="INFO") as captured:
            with timed_stage("media_probe", media_path="example.mkv", bytes_processed=123):
                pass

        event = json.loads(JsonFormatter().format(captured.records[0]))
        self.assertEqual(event["event"], "stage_timing")
        self.assertEqual(event["stage"], "media_probe")
        self.assertGreaterEqual(event["elapsed_ms"], 0)
        self.assertEqual(event["media_path"], "example.mkv")
        self.assertEqual(event["bytes_processed"], 123)

    def test_stage_timing_logs_even_when_stage_raises(self):
        with self.assertLogs("subtitle_manager", level="INFO") as captured:
            with self.assertRaisesRegex(RuntimeError, "expected"):
                with timed_stage("failing_stage"):
                    raise RuntimeError("expected")

        event = json.loads(JsonFormatter().format(captured.records[0]))
        self.assertEqual(event["stage"], "failing_stage")
        self.assertGreaterEqual(event["elapsed_ms"], 0)


if __name__ == "__main__":
    unittest.main()
