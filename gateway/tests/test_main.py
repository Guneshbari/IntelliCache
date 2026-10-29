"""
Integration tests for main FastAPI application endpoints in IntelliCache Gateway.
"""

import asyncio
import os
import unittest
from unittest.mock import patch
from fastapi.testclient import TestClient

from app.config import get_settings, reload_settings
from app.main import app
from app.routes.query import get_query_cache


class TestMainEndpoints(unittest.TestCase):
    """Test suite for root, health, cache, administrative protection, and OpenAPI endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        reload_settings()
        cache = get_query_cache()
        asyncio.run(cache.clear())

    def test_root_endpoint(self):
        """Verify root endpoint returns metadata and online status."""
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertEqual(data["service"], "IntelliCache Gateway")
        self.assertEqual(data["status"], "online")
        self.assertIn("endpoints", data)
        self.assertIn("uptime_seconds", data)

    def test_health_endpoint(self):
        """Verify health check endpoint returns cache status and timestamp."""
        response = self.client.get("/health")
        self.assertEqual(response.status_code, 200)
        data = response.json()
        self.assertIn(data["status"], ["healthy", "degraded"])
        self.assertIn("cache", data)
        self.assertIn("groq_configured", data)

    def test_cache_stats_endpoint(self):
        """Verify global cache stats endpoint returns metrics structure."""
        stats_resp = self.client.get("/cache/stats")
        self.assertEqual(stats_resp.status_code, 200)
        data = stats_resp.json()
        self.assertIn("total_entries", data)
        self.assertIn("hits", data)
        self.assertIn("misses", data)
        self.assertIn("coalesced_requests", data)
        self.assertIn("total_requests", data)

    def test_cache_clear_denied_when_admin_token_unconfigured(self):
        """Verify cache clear is rejected with 403 when ADMIN_API_TOKEN is not configured."""
        with patch.dict(os.environ, {}, clear=True):
            reload_settings()
            clear_resp = self.client.post("/cache/clear")
            self.assertEqual(clear_resp.status_code, 403)
            self.assertIn("ADMIN_API_TOKEN is not configured", clear_resp.json()["detail"])

    def test_cache_clear_with_admin_token(self):
        """Verify cache clear authorization behavior with ADMIN_API_TOKEN configured."""
        with patch.dict(os.environ, {"ADMIN_API_TOKEN": "secret-adm-pass-123"}, clear=False):
            reload_settings()

            # 1. Missing token -> 403
            resp_missing = self.client.post("/cache/clear")
            self.assertEqual(resp_missing.status_code, 403)

            # 2. Invalid token -> 403
            resp_invalid = self.client.post("/cache/clear", headers={"X-Admin-Token": "wrong-token"})
            self.assertEqual(resp_invalid.status_code, 403)

            # 3. Valid X-Admin-Token -> 200
            resp_valid = self.client.post("/cache/clear", headers={"X-Admin-Token": "secret-adm-pass-123"})
            self.assertEqual(resp_valid.status_code, 200)
            self.assertEqual(resp_valid.json()["status"], "ok")

            # 4. Valid Bearer Authorization token -> 200
            resp_bearer = self.client.post("/cache/clear", headers={"Authorization": "Bearer secret-adm-pass-123"})
            self.assertEqual(resp_bearer.status_code, 200)
            self.assertEqual(resp_bearer.json()["status"], "ok")

    def test_openapi_docs_accessible(self):
        """Verify Swagger OpenAPI documentation is accessible."""
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)

    def test_cors_wildcard_disables_credentials(self):
        """Verify that wildcard CORS does not enable allow_credentials per RFC."""
        response = self.client.get("/", headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(response.headers.get("access-control-allow-credentials"), "true")


if __name__ == "__main__":
    unittest.main()
