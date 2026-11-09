import os
import unittest
from unittest.mock import patch

from app.config import Settings


class SettingsDefaultsTests(unittest.TestCase):
    def test_live_mode_is_the_default(self):
        with patch.dict(os.environ, {}, clear=True):
            settings = Settings(_env_file=None)
        self.assertFalse(settings.test_mode)

    def test_test_mode_can_be_enabled_explicitly(self):
        with patch.dict(os.environ, {"TEST_MODE": "true"}, clear=True):
            settings = Settings(_env_file=None)
        self.assertTrue(settings.test_mode)

    def test_live_mode_can_be_selected_explicitly(self):
        with patch.dict(os.environ, {"TEST_MODE": "false"}, clear=True):
            settings = Settings(_env_file=None)
        self.assertFalse(settings.test_mode)


if __name__ == "__main__":
    unittest.main()
