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
        """Verify default settings instantiation and secure defaults."""
        settings = Settings()
        self.assertIsNotNone(settings.groq_model)
        # Default host must be 127.0.0.1 for local network safety
        self.assertEqual(settings.llm_provider, "groq")
        self.assertEqual(settings.host, "127.0.0.1")
        self.assertEqual(settings.port, 8000)
        self.assertTrue(settings.cache_enabled)
        self.assertEqual(settings.cache_ttl_seconds, 3600)
        self.assertEqual(settings.cache_max_entries, 1000)
        self.assertEqual(settings.default_temperature, 0.7)
        self.assertIsNone(settings.admin_api_token)
        self.assertEqual(settings.max_prompt_chars, 131072)
        self.assertEqual(settings.max_messages_count, 100)
        self.assertEqual(settings.max_message_chars, 131072)
        self.assertEqual(settings.max_system_prompt_chars, 65536)

    @patch.dict(os.environ, {
        "LLM_PROVIDER": "custom_provider",
        "GROQ_MODEL": "test-model-custom",
        "HOST": "0.0.0.0",
        "PORT": "9090",
        "DEBUG": "true",
        "CACHE_ENABLED": "false",
        "CACHE_TTL_SECONDS": "1800",
        "CORS_ORIGINS": "http://localhost:3000, https://example.com",
        "ADMIN_API_TOKEN": "my-secret-admin-token",
        "MAX_PROMPT_CHARS": "50000",
        "MAX_MESSAGES_COUNT": "50",
    }, clear=False)
    def test_env_overrides(self):
        """Verify environment variables correctly override default settings."""
        settings = Settings()
        self.assertEqual(settings.llm_provider, "custom_provider")
        self.assertEqual(settings.groq_model, "test-model-custom")
        self.assertEqual(settings.host, "0.0.0.0")
        self.assertEqual(settings.port, 9090)
        self.assertTrue(settings.debug)
        self.assertFalse(settings.cache_enabled)
        self.assertEqual(settings.cache_ttl_seconds, 1800)
        self.assertEqual(settings.cors_origins, ["http://localhost:3000", "https://example.com"])
        self.assertEqual(settings.admin_api_token, "my-secret-admin-token")
        self.assertEqual(settings.max_prompt_chars, 50000)
        self.assertEqual(settings.max_messages_count, 50)

    def test_singleton_get_settings(self):
        """Verify get_settings returns consistent singleton instance."""
        s1 = get_settings()
        s2 = get_settings()
        self.assertIs(s1, s2)

    # -----------------------------------------------------------------------
    # Step 10.1: Client Token Configuration & Validation Tests
    # -----------------------------------------------------------------------

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"token_a":"team_alpha","token_b":"team_alpha","token_c":"team_gamma"}',
    }, clear=False)
    def test_client_tokens_valid_json(self):
        """Valid JSON mapping correctly parses and supports multiple clients sharing namespaces."""
        settings = Settings()
        token_map = settings.get_client_token_map()
        self.assertEqual(token_map, {
            "token_a": "team_alpha",
            "token_b": "team_alpha",
            "token_c": "team_gamma",
        })

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "token_a:team_alpha, token_b:team_alpha, token_c:team_gamma",
    }, clear=False)
    def test_client_tokens_valid_delimiter(self):
        """Valid comma-separated pairs parse correctly and trim surrounding whitespace."""
        settings = Settings()
        token_map = settings.get_client_token_map()
        self.assertEqual(token_map, {
            "token_a": "team_alpha",
            "token_b": "team_alpha",
            "token_c": "team_gamma",
        })

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"token_a": }',
    }, clear=False)
    def test_client_tokens_reject_malformed_json(self):
        """Malformed JSON in CLIENT_API_TOKENS fails configuration validation at initialization."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Malformed JSON in CLIENT_API_TOKENS configuration", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '["token_a", "token_b"]',
    }, clear=False)
    def test_client_tokens_reject_non_dict_json(self):
        """JSON array structure in CLIENT_API_TOKENS is rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("must be a JSON object", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"": "team_alpha"}',
    }, clear=False)
    def test_client_tokens_reject_empty_token_key(self):
        """Empty token keys in JSON mapping are rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Token key in CLIENT_API_TOKENS JSON mapping must be a non-empty string", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"token_a": ""}',
    }, clear=False)
    def test_client_tokens_reject_empty_namespace_value(self):
        """Empty namespace values in JSON mapping are rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Namespace value in CLIENT_API_TOKENS JSON mapping must be a non-empty string", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"token_a": 12345}',
    }, clear=False)
    def test_client_tokens_reject_non_string_namespace_type(self):
        """Non-string namespace types in JSON mapping are rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Namespace value in CLIENT_API_TOKENS JSON mapping must be a non-empty string", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"token_a": "ns_first", "token_a": "ns_second"}',
    }, clear=False)
    def test_client_tokens_reject_duplicate_json_keys(self):
        """Duplicate token keys in JSON mapping are strictly rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Duplicate token key detected in CLIENT_API_TOKENS configuration", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "token_a:ns1, token_a:ns2",
    }, clear=False)
    def test_client_tokens_reject_duplicate_delimiter_tokens(self):
        """Duplicate token keys in delimiter format are strictly rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Duplicate token key detected in CLIENT_API_TOKENS delimiter mapping", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "token_missing_namespace",
    }, clear=False)
    def test_client_tokens_reject_malformed_delimiter_entries(self):
        """Delimiter entries missing ':' separator are rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("Malformed delimiter entry in CLIENT_API_TOKENS", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "tok1:ns1,,tok2:ns2",
    }, clear=False)
    def test_client_tokens_reject_empty_delimiter_segment(self):
        """Empty segments in delimiter format are rejected."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("empty token-namespace segment detected", str(ctx.exception))

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "",
    }, clear=False)
    def test_client_tokens_reject_explicitly_empty_variable(self):
        """An explicitly empty CLIENT_API_TOKENS fails validation and does not fall back."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        self.assertIn("CLIENT_API_TOKENS cannot be empty when configured", str(ctx.exception))

    def test_legacy_token_fallback_when_tokens_absent(self):
        """When CLIENT_API_TOKENS is absent, legacy CLIENT_API_TOKEN is authoritative."""
        env = dict(os.environ)
        env.pop("CLIENT_API_TOKENS", None)
        env["CLIENT_API_TOKEN"] = "legacy_secret_token"
        env["CACHE_NAMESPACE"] = "legacy_team"
        with patch.dict(os.environ, env, clear=True):
            settings = Settings()
            self.assertEqual(settings.get_client_token_map(), {
                "legacy_secret_token": "legacy_team",
            })

    def test_client_tokens_authoritative_precedence_over_legacy(self):
        """When both are configured, CLIENT_API_TOKENS is authoritative; legacy token is ignored."""
        env = dict(os.environ)
        env["CLIENT_API_TOKENS"] = '{"tok_alpha": "ns_alpha"}'
        env["CLIENT_API_TOKEN"] = "old_decommissioned_token"
        with patch.dict(os.environ, env, clear=False):
            settings = Settings()
            token_map = settings.get_client_token_map()
            self.assertEqual(token_map, {"tok_alpha": "ns_alpha"})
            self.assertNotIn("old_decommissioned_token", token_map)

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"super_secret_token_123": }',
    }, clear=False)
    def test_secrets_never_exposed_in_validation_error(self):
        """Configuration validation error messages never expose actual secret token values."""
        with self.assertRaises(ValueError) as ctx:
            Settings()
        err_msg = str(ctx.exception)
        self.assertNotIn("super_secret_token_123", err_msg)


if __name__ == "__main__":
    unittest.main()
