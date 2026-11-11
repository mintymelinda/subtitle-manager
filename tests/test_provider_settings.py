import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import provider_settings
from app.main import _load_scan_provider_settings


class ProviderSettingsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "private" / "opensubtitles.json"
        self.env = patch.dict(os.environ, {"SUBTITLE_SETTINGS_PATH": str(self.path)})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.temp.cleanup()

    def test_save_persists_settings_with_restricted_permissions(self):
        result = provider_settings.save_provider_settings(
            api_key="secret-key", username="viewer", password="secret-password",
            max_candidates_per_file=4,
        )
        self.assertTrue(result["configured"])
        self.assertTrue(result["has_account_credentials"])
        self.assertEqual(result["max_candidates_per_file"], 4)
        self.assertEqual(result["log_level"], "INFO")
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(provider_settings.load_provider_settings()["api_key"], "secret-key")

    def test_blank_fields_keep_existing_secrets(self):
        provider_settings.save_provider_settings(
            api_key="secret-key", username="viewer", password="secret-password",
            max_candidates_per_file=2,
        )
        provider_settings.save_provider_settings(
            api_key="", username="", password="", max_candidates_per_file=3,
        )
        saved = provider_settings.load_provider_settings()
        self.assertEqual(saved["api_key"], "secret-key")
        self.assertEqual(saved["username"], "viewer")
        self.assertEqual(saved["password"], "secret-password")
        self.assertEqual(saved["max_candidates_per_file"], 3)

    def test_account_credentials_must_be_a_pair(self):
        with self.assertRaisesRegex(ValueError, "both"):
            provider_settings.save_provider_settings(
                api_key="secret-key", username="viewer", password="",
                max_candidates_per_file=2,
            )

    def test_candidate_limit_is_bounded(self):
        with self.assertRaisesRegex(ValueError, "between 1 and 5"):
            provider_settings.save_provider_settings(
                api_key="secret-key", username="", password="",
                max_candidates_per_file=6,
            )

    def test_account_credentials_can_be_cleared_explicitly(self):
        provider_settings.save_provider_settings(
            api_key="secret-key", username="viewer", password="secret-password",
            max_candidates_per_file=2,
        )
        provider_settings.save_provider_settings(
            api_key="", username="", password="", max_candidates_per_file=2,
            clear_account_credentials=True,
        )
        saved = provider_settings.load_provider_settings()
        self.assertEqual(saved["username"], "")
        self.assertEqual(saved["password"], "")

    def test_logging_level_persists_and_is_returned_publicly(self):
        result = provider_settings.save_provider_settings(
            api_key="secret-key", username="", password="",
            max_candidates_per_file=2, log_level="DEBUG",
        )
        self.assertEqual(result["log_level"], "DEBUG")
        self.assertEqual(provider_settings.load_provider_settings()["log_level"], "DEBUG")

    def test_logging_level_can_be_saved_before_provider_is_configured(self):
        result = provider_settings.save_provider_settings(
            api_key="", username="", password="",
            max_candidates_per_file=2, log_level="DEBUG",
        )
        self.assertFalse(result["configured"])
        self.assertEqual(result["log_level"], "DEBUG")

    def test_invalid_logging_level_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Logging level must be"):
            provider_settings.save_provider_settings(
                api_key="secret-key", username="", password="",
                max_candidates_per_file=2, log_level="TRACE",
            )

    def test_invalid_saved_logging_level_falls_back_to_info(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"api_key": "secret-key", "log_level": "TRACE"}))
        self.assertEqual(provider_settings.load_provider_settings()["log_level"], "INFO")

    def test_scan_api_key_override_is_scoped_and_does_not_mutate_saved_settings(self):
        provider_settings.save_provider_settings(
            api_key="saved-key", username="viewer", password="secret-password",
            max_candidates_per_file=3,
        )

        effective = _load_scan_provider_settings("temporary-key")

        self.assertEqual(effective["api_key"], "temporary-key")
        self.assertEqual(effective["username"], "viewer")
        self.assertEqual(effective["password"], "secret-password")
        self.assertEqual(effective["max_candidates_per_file"], 3)
        self.assertEqual(provider_settings.load_provider_settings()["api_key"], "saved-key")

    def test_scan_provider_settings_use_saved_key_without_override(self):
        provider_settings.save_provider_settings(
            api_key="saved-key", username="", password="", max_candidates_per_file=2,
        )

        effective = _load_scan_provider_settings()

        self.assertEqual(effective["api_key"], "saved-key")
        self.assertEqual(provider_settings.load_provider_settings()["api_key"], "saved-key")

    def test_public_settings_never_expose_secrets(self):
        provider_settings.save_provider_settings(
            api_key="secret-key", username="viewer", password="secret-password",
            max_candidates_per_file=2,
        )
        public = provider_settings.public_provider_settings()
        encoded = json.dumps(public)
        self.assertNotIn("secret-key", encoded)
        self.assertNotIn("secret-password", encoded)
        self.assertNotIn("viewer", encoded)


if __name__ == "__main__":
    unittest.main()
