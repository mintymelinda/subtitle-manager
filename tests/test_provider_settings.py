import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from app import provider_settings


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
