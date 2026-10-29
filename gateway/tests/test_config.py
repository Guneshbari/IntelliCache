"""
Unit tests for configuration management in IntelliCache Gateway.
"""

import os
import unittest
from unittest.mock import patch

from app.config import Settings, get_settings


class TestConfig(unittest.TestCase):
    """Test configuration loading, validation, and singleton behaviors."""

    def test_default_settings(self):
        """Verify default settings instantiation and defaults."""
        settings = Settings()
        self.assertIsNotNone(settings.groq_model)
        self.assertEqual(settings.port, 8000)
        self.assertTrue(settings.cache_enabled)
        self.assertEqual(settings.cache_ttl_seconds, 3600)
        self.assertEqual(settings.cache_max_entries, 1000)
        self.assertEqual(settings.default_temperature, 0.7)
        self.assertEqual(settings.max_prompt_chars, 131072)
        self.assertEqual(settings.max_messages_count, 100)
        self.assertEqual(settings.max_message_chars, 131072)
        self.assertEqual(settings.max_system_prompt_chars, 65536)

    @patch.dict(os.environ, {
        "GROQ_MODEL": "test-model-custom",
        "PORT": "9090",
        "DEBUG": "true",
        "CACHE_ENABLED": "false",
        "CACHE_TTL_SECONDS": "1800",
        "CORS_ORIGINS": "http://localhost:3000, https://example.com",
        "MAX_PROMPT_CHARS": "50000",
        "MAX_MESSAGES_COUNT": "50",
    }, clear=False)
    def test_env_overrides(self):
        """Verify environment variables correctly override default settings."""
        settings = Settings()
        self.assertEqual(settings.groq_model, "test-model-custom")
        self.assertEqual(settings.port, 9090)
        self.assertTrue(settings.debug)
        self.assertFalse(settings.cache_enabled)
        self.assertEqual(settings.cache_ttl_seconds, 1800)
        self.assertEqual(settings.cors_origins, ["http://localhost:3000", "https://example.com"])
        self.assertEqual(settings.max_prompt_chars, 50000)
        self.assertEqual(settings.max_messages_count, 50)

    def test_singleton_get_settings(self):
        """Verify get_settings returns consistent singleton instance."""
        s1 = get_settings()
        s2 = get_settings()
        self.assertIs(s1, s2)


if __name__ == "__main__":
    unittest.main()
