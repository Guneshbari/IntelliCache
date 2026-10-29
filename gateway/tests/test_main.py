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
        self.cache = get_query_cache()
        asyncio.run(self.cache.clear())

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
            self.assertIn("default", resp_valid.json()["message"])
            self.assertEqual(resp_valid.json()["namespace"], "default")

            # 4. Valid Bearer Authorization token -> 200
            resp_bearer = self.client.post("/cache/clear", headers={"Authorization": "Bearer secret-adm-pass-123"})
            self.assertEqual(resp_bearer.status_code, 200)
            self.assertEqual(resp_bearer.json()["status"], "ok")
            self.assertEqual(resp_bearer.json()["namespace"], "default")

    def test_cache_clear_scopes_strictly_to_configured_namespace(self):
        """Verify /cache/clear only purges the server-configured namespace and preserves others."""
        from app.cache.interfaces import CacheEntry
        from app.core.contracts import UsageMetrics

        with patch.dict(os.environ, {
            "ADMIN_API_TOKEN": "secret-adm-pass-123",
            "CACHE_NAMESPACE": "finance",
        }, clear=False):
            reload_settings()

            # Seed entries in two separate namespaces
            entry_fin = CacheEntry("Finance Data", "model", UsageMetrics(), 10.0)
            entry_hr = CacheEntry("HR Data", "model", UsageMetrics(), 10.0)
            asyncio.run(self.cache.set("fin_fp", entry_fin, namespace="finance"))
            asyncio.run(self.cache.set("hr_fp", entry_hr, namespace="hr"))

            # Clear cache via admin endpoint
            resp = self.client.post("/cache/clear", headers={"X-Admin-Token": "secret-adm-pass-123"})
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertEqual(data["status"], "ok")
            self.assertEqual(data["namespace"], "finance")
            self.assertIn("finance", data["message"])

            # Verify 'finance' is cleared, but 'hr' is intact
            self.assertIsNone(asyncio.run(self.cache.get("fin_fp", namespace="finance")))
            res_hr = asyncio.run(self.cache.get("hr_fp", namespace="hr"))
            self.assertIsNotNone(res_hr)
            self.assertEqual(res_hr.response, "HR Data")

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
