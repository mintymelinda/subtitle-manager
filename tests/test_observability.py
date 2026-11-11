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
            "candidate filtered api_key=must-not-appear", (), None,
        )
        record.event = "opensubtitles_candidate_filtered"
        record.phase = "candidate_selection"
        record.filter_reason = "unsupported_language"
        record.provider_count = 13
        record.error_code = "download_rate_limited"
        record.retryable = True
        record.http_status = 429
        record.error_message = "Provider rate limit; api_key=must-not-appear"
        record.provider_error_detail = "Invalid file_id; token=must-not-appear"
        payload = json.loads(JsonFormatter().format(record))
        self.assertEqual(payload["filter_reason"], "unsupported_language")
        self.assertEqual(payload["provider_count"], 13)
        self.assertEqual(payload["error_code"], "download_rate_limited")
        self.assertIs(payload["retryable"], True)
        self.assertEqual(payload["http_status"], 429)
        self.assertEqual(payload["error_message"], "Provider rate limit; api_key=[REDACTED]")
        self.assertEqual(payload["provider_error_detail"], "Invalid file_id; token=[REDACTED]")
        self.assertNotIn("must-not-appear", json.dumps(payload))
        self.assertIn("api_key=[REDACTED]", payload["message"])


if __name__ == "__main__":
    unittest.main()
