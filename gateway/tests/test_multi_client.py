"""
Multi-client and centralized PostgreSQL integration tests for IntelliCache Gateway.
Tests client authentication, namespace isolation, multi-client cache sharing,
and PostgreSQL persistence when a database connection is available.
"""

import asyncio
from datetime import datetime, timezone
import os
import unittest
from unittest.mock import AsyncMock, patch

import asyncpg
from fastapi.testclient import TestClient

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import Settings, get_settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, UsageMetrics
from app.main import app
from app.routes.query import (
    get_groq_client_factory,
    get_query_cache,
    set_query_cache,
)


class MockCompletion:
    def __init__(self, content: str = "Central Mocked Response"):
        self.choices = [
            type("Choice", (), {"message": type("Message", (), {"content": content})})
        ]
        self.usage = type(
            "Usage", (), {"prompt_tokens": 12, "completion_tokens": 18, "total_tokens": 30}
        )


class TestCentralClientAuthentication(unittest.TestCase):
    """Tests client API token enforcement and fail-closed security behavior."""

    def setUp(self):
        self.client = TestClient(app)
        self.cache = ExactCache()
        set_query_cache(self.cache)

        # Mock Groq provider
        self.mock_groq = AsyncMock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Auth Test Response")
        self.mock_groq.close = AsyncMock()
        app.dependency_overrides[get_groq_client_factory] = lambda: lambda: self.mock_groq

    def tearDown(self):
        app.dependency_overrides.clear()
        reload_settings()

    @patch.dict(os.environ, {"CLIENT_API_TOKEN": "secret-client-token-123"}, clear=False)
    def test_client_token_enforcement(self):
        """Query endpoints reject missing or invalid tokens when CLIENT_API_TOKEN is set."""
        reload_settings()

        # 1. Missing token -> 401 Unauthorized
        resp_missing = self.client.post("/query", json={"prompt": "Hello"})
        self.assertEqual(resp_missing.status_code, 401)
        self.assertIn("Unauthorized: Invalid or missing client API token", resp_missing.text)

        # 2. Invalid token -> 401 Unauthorized
        resp_bad = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"X-Client-Token": "wrong-token"},
        )
        self.assertEqual(resp_bad.status_code, 401)

        # 3. Valid X-Client-Token header -> 200 OK
        resp_good_header = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"X-Client-Token": "secret-client-token-123"},
        )
        self.assertEqual(resp_good_header.status_code, 200)

        # 4. Valid Authorization Bearer header -> 200 OK
        resp_good_bearer = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"Authorization": "Bearer secret-client-token-123"},
        )
        self.assertEqual(resp_good_bearer.status_code, 200)

    @patch.dict(os.environ, {
        "HOST": "0.0.0.0",
        "CLIENT_API_TOKEN": "",
    }, clear=False)
    def test_fail_closed_beyond_loopback_without_client_token(self):
        """Central deployment binding beyond loopback fails closed if CLIENT_API_TOKEN is unset."""
        reload_settings()

        resp = self.client.post("/query", json={"prompt": "Test beyond loopback"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Central deployment bound beyond loopback requires CLIENT_API_TOKEN", resp.text)

    @patch.dict(os.environ, {
        "CLIENT_API_TOKEN": "client-token-only",
        "ADMIN_API_TOKEN": "admin-token-only",
    }, clear=False)
    def test_client_and_admin_token_separation(self):
        """CLIENT_API_TOKEN cannot access /cache/clear, and ADMIN_API_TOKEN cannot access /query."""
        reload_settings()

        # Client token cannot access admin endpoint
        admin_resp = self.client.post(
            "/cache/clear",
            headers={"X-Admin-Token": "client-token-only"},
        )
        self.assertEqual(admin_resp.status_code, 403)

        # Admin token cannot access query endpoint
        query_resp = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"X-Client-Token": "admin-token-only"},
        )
        self.assertEqual(query_resp.status_code, 401)


class TestMultiClientSharingAndNamespaces(unittest.TestCase):
    """Tests multi-client cache reuse and server-enforced namespace separation."""

    def setUp(self):
        self.client = TestClient(app)
        self.cache = ExactCache()
        set_query_cache(self.cache)

        self.mock_groq = AsyncMock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Shared Knowledge")
        self.mock_groq.close = AsyncMock()
        self.api_call_count = 0

        def factory():
            self.api_call_count += 1
            return self.mock_groq

        app.dependency_overrides[get_groq_client_factory] = lambda: factory

    def tearDown(self):
        app.dependency_overrides.clear()
        reload_settings()

    @patch.dict(os.environ, {
        "CLIENT_API_TOKEN": "org-token",
        "CACHE_NAMESPACE": "engineering_team",
    }, clear=False)
    def test_multi_client_cache_sharing(self):
        """Client A generates a response; Client B accesses the identical query and gets a cache hit."""
        reload_settings()
        headers_client_a = {"X-Client-Token": "org-token"}
        headers_client_b = {"X-Client-Token": "org-token"}

        # Client A executes query -> Cache miss -> 1 LLM generation call
        resp_a = self.client.post(
            "/query",
            json={"prompt": "Explain database indexing in PostgreSQL"},
            headers=headers_client_a,
        )
        self.assertEqual(resp_a.status_code, 200)
        data_a = resp_a.json()
        self.assertEqual(data_a["response"], "Shared Knowledge")
        self.assertFalse(data_a["cached"])
        self.assertEqual(self.api_call_count, 1)

        # Client B sends the exact same query -> Cache hit -> 0 additional LLM calls
        resp_b = self.client.post(
            "/query",
            json={"prompt": "Explain database indexing in PostgreSQL"},
            headers=headers_client_b,
        )
        self.assertEqual(resp_b.status_code, 200)
        data_b = resp_b.json()
        self.assertEqual(data_b["response"], "Shared Knowledge")
        self.assertTrue(data_b["cached"])
        self.assertEqual(data_b["cache_hit_count"], 1)
        self.assertEqual(self.api_call_count, 1)  # Provider was NOT invoked again!

    @patch.dict(os.environ, {
        "CACHE_NAMESPACE": "trusted_server_ns",
    }, clear=False)
    def test_request_body_cannot_override_server_namespace(self):
        """Untrusted request payload cannot override the server's configured cache namespace."""
        reload_settings()

        # Attacker tries to inject a custom namespace in the body
        resp = self.client.post(
            "/query",
            json={
                "prompt": "Test prompt",
                "namespace": "attacker_namespace",
            },
        )
        self.assertEqual(resp.status_code, 200)

        # Entry must be stored in 'trusted_server_ns', not 'attacker_namespace'
        async def verify():
            entries_attacker = await self.cache.stats(namespace="attacker_namespace")
            entries_trusted = await self.cache.stats(namespace="trusted_server_ns")
            self.assertEqual(entries_attacker.total_entries, 0)
            self.assertEqual(entries_trusted.total_entries, 1)

        asyncio.run(verify())

    def test_namespace_isolation_at_storage_layer(self):
        """Different namespaces store entries independently and never collide."""
        cache = ExactCache()

        async def run_isolation():
            entry1 = CacheEntry("Ans 1", "m", UsageMetrics(), 10.0)
            entry2 = CacheEntry("Ans 2", "m", UsageMetrics(), 10.0)

            await cache.set("fp_same", entry1, namespace="ns_alpha")
            await cache.set("fp_same", entry2, namespace="ns_beta")

            res_alpha = await cache.get("fp_same", namespace="ns_alpha")
            res_beta = await cache.get("fp_same", namespace="ns_beta")

            self.assertEqual(res_alpha.response, "Ans 1")
            self.assertEqual(res_beta.response, "Ans 2")

            # Non-existent namespace returns None
            self.assertIsNone(await cache.get("fp_same", namespace="ns_gamma"))

        asyncio.run(run_isolation())


class TestPostgresCentralStorage(unittest.IsolatedAsyncioTestCase):
    """
    Tests persistent PostgreSQL exact-cache implementation.
    Uses TEST_DATABASE_URL environment variable if set, or defaults to local PostgreSQL.
    Skips cleanly if PostgreSQL is unavailable.
    """

    async def asyncSetUp(self):
        self.db_url = os.getenv(
            "TEST_DATABASE_URL",
            "postgresql://postgres@localhost:5432/intellicache_test",
        )
        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
            await run_migrations(self.pool)
            self.store_a = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_a")
            self.store_b = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_a")
            self.store_other_ns = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_b")
            await self.store_a.clear()
        except Exception as e:
            self.skipTest(f"PostgreSQL test database unavailable at {self.db_url}: {e}")

    async def asyncTearDown(self):
        if hasattr(self, "store_a"):
            await self.store_a.clear()
        if hasattr(self, "pool"):
            await self.pool.close()

    async def test_cross_instance_cache_sharing(self):
        """Entry written by store instance A is immediately readable by store instance B."""
        entry = CacheEntry(
            response="Centralized Postgres Answer",
            model="qwen/qwen3.8-27b",
            usage=UsageMetrics(prompt_tokens=20, completion_tokens=30, total_tokens=50),
            latency_ms=45.2,
            metadata={"source": "unit_test"},
        )

        await self.store_a.set("test_fp_001", entry)

        # Read through separate store instance connected to same database
        retrieved = await self.store_b.get("test_fp_001")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.response, "Centralized Postgres Answer")
        self.assertEqual(retrieved.model, "qwen/qwen3.8-27b")
        self.assertEqual(retrieved.usage.total_tokens, 50)
        self.assertEqual(retrieved.hit_count, 1)

    async def test_namespace_separation_in_database(self):
        """Entries in test_ns_a cannot be retrieved by test_ns_b."""
        entry = CacheEntry(
            response="Secret Namespace A Data",
            model="model-a",
            usage=UsageMetrics(10, 10, 20),
            latency_ms=10.0,
        )
        await self.store_a.set("shared_fp", entry)

        # store_b in same namespace sees it
        self.assertIsNotNone(await self.store_b.get("shared_fp"))

        # store_other_ns in different namespace gets cache miss
        self.assertIsNone(await self.store_other_ns.get("shared_fp"))

    async def test_conflict_safe_concurrent_upsert(self):
        """Concurrent writes to the exact same fingerprint do not duplicate records."""
        entry1 = CacheEntry("Concurrent 1", "model", UsageMetrics(), 10.0)
        entry2 = CacheEntry("Concurrent 2", "model", UsageMetrics(), 10.0)

        # Execute concurrent writes simultaneously
        await asyncio.gather(
            self.store_a.set("race_fp", entry1),
            self.store_b.set("race_fp", entry2),
        )

        # Exactly 1 record should exist in the database for this namespace & fingerprint
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1 AND fingerprint = $2",
                "test_ns_a",
                "race_fp",
            )
        self.assertEqual(count, 1)

    async def test_lru_capacity_bounded(self):
        """Cache capacity in PostgreSQL stays bounded to max_entries."""
        # store_a has max_entries=5
        for i in range(8):
            e = CacheEntry(f"Response {i}", "model", UsageMetrics(), 5.0)
            await self.store_a.set(f"fp_{i}", e)

        stats = await self.store_a.stats(namespace="test_ns_a")
        self.assertLessEqual(stats.total_entries, 5)

    async def test_expired_entry_is_not_returned_as_hit(self):
        """Entries past their expiration date return None and are purged."""
        short_ttl_store = PostgresCache(
            self.pool,
            max_entries=5,
            ttl_seconds=1,  # 1 second TTL
            default_namespace="ttl_ns",
        )
        entry = CacheEntry("Expires soon", "model", UsageMetrics(), 5.0)
        await short_ttl_store.set("fp_expire", entry)

        # Wait for TTL to elapse
        await asyncio.sleep(1.2)

        # Must return None on retrieval
        expired_result = await short_ttl_store.get("fp_expire")
        self.assertIsNone(expired_result)
