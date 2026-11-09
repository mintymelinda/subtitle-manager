import json
import logging
import unittest

from app.observability import JsonFormatter, configure_logging


class ObservabilityTests(unittest.TestCase):
    def test_configure_logging_applies_supported_levels(self):
        root = logging.getLogger()
        previous = root.level
        try:
            for name, expected in (
                ("DEBUG", logging.DEBUG),
                ("INFO", logging.INFO),
                ("WARNING", logging.WARNING),
                ("ERROR", logging.ERROR),
                ("CRITICAL", logging.CRITICAL),
                ("invalid", logging.INFO),
            ):
                configure_logging(name)
                self.assertEqual(root.level, expected)
        finally:
            root.setLevel(previous)

    def test_json_formatter_includes_candidate_diagnostics_and_redacts_secrets(self):
        record = logging.LogRecord(
            "subtitle_manager", logging.DEBUG, __file__, 1,
            "candidate filtered", (), None,
        )
        record.event = "opensubtitles_candidate_filtered"
        record.phase = "candidate_selection"
        record.filter_reason = "unsupported_language"
        record.provider_count = 13
        record.api_key = "must-not-appear"
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["filter_reason"], "unsupported_language")
        self.assertEqual(payload["provider_count"], 13)
        self.assertEqual(payload["api_key"], "[REDACTED]")
        self.assertNotIn("must-not-appear", json.dumps(payload))


if __name__ == "__main__":
    unittest.main()
