"""
Integration tests for main FastAPI application endpoints in IntelliCache Gateway.
"""

import unittest
from fastapi.testclient import TestClient

from app.main import app, create_app
from app.routes.query import get_query_cache


class TestMainEndpoints(unittest.TestCase):
    """Test suite for root, health, cache, and OpenAPI endpoints."""

    def setUp(self):
        self.client = TestClient(app)
        cache = get_query_cache()
        cache.clear()

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

    def test_cache_stats_and_clear_endpoints(self):
        """Verify global cache stats and clear endpoints."""
        stats_resp = self.client.get("/cache/stats")
        self.assertEqual(stats_resp.status_code, 200)
        data = stats_resp.json()
        self.assertIn("total_entries", data)
        self.assertIn("hits", data)
        self.assertIn("misses", data)

        clear_resp = self.client.post("/cache/clear")
        self.assertEqual(clear_resp.status_code, 200)
        self.assertEqual(clear_resp.json()["status"], "ok")

    def test_openapi_docs_accessible(self):
        """Verify Swagger OpenAPI documentation is accessible."""
        response = self.client.get("/docs")
        self.assertEqual(response.status_code, 200)

    def test_cors_wildcard_disables_credentials(self):
        """Verify that wildcard CORS does not enable allow_credentials per RFC."""
        response = self.client.get("/", headers={"Origin": "https://example.com"})
        self.assertEqual(response.status_code, 200)
        # With wildcard origins and allow_credentials=False,
        # access-control-allow-credentials header must NOT be 'true'
        self.assertNotEqual(response.headers.get("access-control-allow-credentials"), "true")


if __name__ == "__main__":
    unittest.main()
