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
import httpx

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


def _make_asgi_app_with_scope(base_app, client_host="127.0.0.1", server_host="127.0.0.1"):
    """
    Test helper wrapping an ASGI app to simulate specific client and server addresses
    in request.scope, verifying ASGI-level network access control enforcement.
    Note: ASGITransport scope tests verify ASGI middleware/route inspection logic.
    Actual Uvicorn socket behaviour at the kernel layer binds network sockets directly.
    """
    async def asgi_wrapper(scope, receive, send):
        if scope["type"] == "http":
            scope = dict(scope)
            if client_host is not None:
                scope["client"] = (client_host, 12345)
            else:
                scope["client"] = None
            if server_host is not None:
                scope["server"] = (server_host, 8000)
            else:
                scope["server"] = None
        await base_app(scope, receive, send)
    return asgi_wrapper


class TestCentralClientAuthentication(unittest.TestCase):
    """Tests client API token enforcement and fail-closed security behavior."""

    def setUp(self):
        self._saved_dependency_overrides = dict(app.dependency_overrides)
        self._saved_query_cache = get_query_cache()
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
        app.dependency_overrides.update(self._saved_dependency_overrides)
        set_query_cache(self._saved_query_cache)
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

    def test_default_authentication_required_on_loopback(self):
        """When no tokens are configured and ALLOW_UNAUTHENTICATED_LOOPBACK is false (default), requests return 401."""
        env = dict(os.environ)
        env.pop("CLIENT_API_TOKEN", None)
        env.pop("CLIENT_API_TOKENS", None)
        env.pop("ALLOW_UNAUTHENTICATED_LOOPBACK", None)
        with patch.dict(os.environ, env, clear=True):
            reload_settings()
            # /query fails closed
            resp = self.client.post("/query", json={"prompt": "Hello"})
            self.assertEqual(resp.status_code, 401)
            self.assertIn("Unauthorized: Invalid or missing client API token", resp.text)

            # /api/v1/query fails closed
            resp_v1 = self.client.post("/api/v1/query", json={"prompt": "Hello"})
            self.assertEqual(resp_v1.status_code, 401)
            self.assertIn("Unauthorized: Invalid or missing client API token", resp_v1.text)

            # Upstream provider was never called
            self.assertEqual(self.mock_groq.chat.completions.create.call_count, 0)

    @patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
    def test_allow_unauthenticated_loopback_valid_access(self):
        """When ALLOW_UNAUTHENTICATED_LOOPBACK is explicitly true and connections are loopback, requests succeed."""
        reload_settings()
        resp = self.client.post("/query", json={"prompt": "Hello"})
        self.assertEqual(resp.status_code, 200)

        # Works on /api/v1/query as well
        resp_v1 = self.client.post("/api/v1/query", json={"prompt": "Hello v1"})
        self.assertEqual(resp_v1.status_code, 200)

    @patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
    def test_allow_unauthenticated_loopback_rejects_forwarded_headers(self):
        """Unauthenticated loopback access is rejected if forwarded headers are present."""
        reload_settings()
        # X-Forwarded-For
        resp_xff = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"X-Forwarded-For": "203.0.113.1"},
        )
        self.assertEqual(resp_xff.status_code, 401)
        self.assertIn("forwarded headers are present", resp_xff.text)

        # Forwarded
        resp_fwd = self.client.post(
            "/query",
            json={"prompt": "Hello"},
            headers={"Forwarded": "for=203.0.113.1;proto=https"},
        )
        self.assertEqual(resp_fwd.status_code, 401)
        self.assertIn("forwarded headers are present", resp_fwd.text)

    @patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
    def test_allow_unauthenticated_loopback_rejects_external_peer(self):
        """When direct client peer IP is external (non-loopback), unauthenticated access is rejected."""
        reload_settings()
        wrapped_app = _make_asgi_app_with_scope(app, client_host="192.168.1.100", server_host="127.0.0.1")
        test_client = TestClient(wrapped_app)
        resp = test_client.post("/query", json={"prompt": "Hello"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Direct connection peer is not a valid loopback address", resp.text)

    @patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
    def test_allow_unauthenticated_loopback_rejects_non_loopback_server(self):
        """When ASGI server bind address is non-loopback (e.g. 0.0.0.0 or external), unauthenticated access is rejected."""
        reload_settings()
        # 0.0.0.0 is not a loopback address
        wrapped_app_0 = _make_asgi_app_with_scope(app, client_host="127.0.0.1", server_host="0.0.0.0")
        client_0 = TestClient(wrapped_app_0)
        resp_0 = client_0.post("/query", json={"prompt": "Hello"})
        self.assertEqual(resp_0.status_code, 401)
        self.assertIn("Server bind address is not a valid loopback address", resp_0.text)

        # 10.0.0.1 is not a loopback address
        wrapped_app_ext = _make_asgi_app_with_scope(app, client_host="127.0.0.1", server_host="10.0.0.1")
        client_ext = TestClient(wrapped_app_ext)
        resp_ext = client_ext.post("/query", json={"prompt": "Hello"})
        self.assertEqual(resp_ext.status_code, 401)
        self.assertIn("Server bind address is not a valid loopback address", resp_ext.text)

    @patch.dict(os.environ, {
        "HOST": "0.0.0.0",
        "ALLOW_UNAUTHENTICATED_LOOPBACK": "true",
    }, clear=False)
    def test_fail_closed_beyond_loopback_without_client_token(self):
        """Central deployment binding beyond loopback fails closed if client credentials are unset."""
        reload_settings()

        resp = self.client.post("/query", json={"prompt": "Test beyond loopback"})
        self.assertEqual(resp.status_code, 401)
        self.assertIn("Server host setting is not a valid loopback address", resp.text)

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

    @patch.dict(os.environ, {"CLIENT_API_TOKEN": "valid-token-xyz"}, clear=False)
    def test_api_v1_query_enforces_authentication_identically(self):
        """POST /api/v1/query enforces authentication identically to POST /query."""
        reload_settings()
        # 1. Missing token on /api/v1/query -> 401
        resp_miss = self.client.post("/api/v1/query", json={"prompt": "Hello"})
        self.assertEqual(resp_miss.status_code, 401)

        # 2. Invalid token on /api/v1/query -> 401
        resp_bad = self.client.post(
            "/api/v1/query",
            json={"prompt": "Hello"},
            headers={"X-Client-Token": "bad-token"},
        )
        self.assertEqual(resp_bad.status_code, 401)

        # 3. Valid token on /api/v1/query -> 200
        resp_good = self.client.post(
            "/api/v1/query",
            json={"prompt": "Hello"},
            headers={"X-Client-Token": "valid-token-xyz"},
        )
        self.assertEqual(resp_good.status_code, 200)


class TestMultiClientSharingAndNamespaces(unittest.TestCase):
    """Tests multi-client cache reuse and server-enforced namespace separation."""

    def setUp(self):
        self._saved_dependency_overrides = dict(app.dependency_overrides)
        self._saved_query_cache = get_query_cache()
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
        app.dependency_overrides.update(self._saved_dependency_overrides)
        set_query_cache(self._saved_query_cache)
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
        "CLIENT_API_TOKEN": "attacker-token-123",
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
            headers={"X-Client-Token": "attacker-token-123"},
        )
        self.assertEqual(resp.status_code, 200)

        # Entry must be stored in 'trusted_server_ns', not 'attacker_namespace'
        async def verify():
            entries_attacker = await self.cache.stats(namespace="attacker_namespace")
            entries_trusted = await self.cache.stats(namespace="trusted_server_ns")
            self.assertEqual(entries_attacker.total_entries, 0)
            self.assertEqual(entries_trusted.total_entries, 1)

        asyncio.run(verify())

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": '{"tok_alpha": "ns_team_alpha", "tok_beta": "ns_team_alpha", "tok_gamma": "ns_team_gamma"}',
    }, clear=False)
    def test_multi_token_json_configuration_and_sharing(self):
        """Tokens tok_alpha and tok_beta share ns_team_alpha; tok_gamma is in ns_team_gamma."""
        reload_settings()
        headers_a = {"X-Client-Token": "tok_alpha"}
        headers_b = {"X-Client-Token": "tok_beta"}
        headers_c = {"X-Client-Token": "tok_gamma"}

        # Client A requests -> Miss -> 1 provider call
        resp_a = self.client.post("/query", json={"prompt": "Multi-token test"}, headers=headers_a)
        self.assertEqual(resp_a.status_code, 200)
        self.assertFalse(resp_a.json()["cached"])
        self.assertEqual(self.api_call_count, 1)

        # Client B requests same query -> Hit -> 0 extra provider calls
        resp_b = self.client.post("/query", json={"prompt": "Multi-token test"}, headers=headers_b)
        self.assertEqual(resp_b.status_code, 200)
        self.assertTrue(resp_b.json()["cached"])
        self.assertEqual(self.api_call_count, 1)

        # Client C requests same query -> Miss because it's in ns_team_gamma -> 1 extra provider call
        resp_c = self.client.post("/query", json={"prompt": "Multi-token test"}, headers=headers_c)
        self.assertEqual(resp_c.status_code, 200)
        self.assertFalse(resp_c.json()["cached"])
        self.assertEqual(self.api_call_count, 2)

    @patch.dict(os.environ, {
        "CLIENT_API_TOKENS": "tok1:ns_one,tok2:ns_one,tok3:ns_two",
    }, clear=False)
    def test_multi_token_delimiter_configuration(self):
        """Tokens formatted as comma-separated token:namespace pairs parse correctly."""
        settings = reload_settings()
        token_map = settings.get_client_token_map()
        self.assertEqual(token_map["tok1"], "ns_one")
        self.assertEqual(token_map["tok2"], "ns_one")
        self.assertEqual(token_map["tok3"], "ns_two")

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


APPROVED_TEST_DATABASES = {"intellicache_test"}
_extra_allowed = os.getenv("ALLOWED_TEST_DATABASES", "")
if _extra_allowed:
    for _item in _extra_allowed.split(","):
        if _item.strip():
            APPROVED_TEST_DATABASES.add(_item.strip())


def _redact_db_url(url: str) -> str:
    """Removes user credentials from a database URL for safe reporting."""
    try:
        from urllib.parse import urlsplit, urlunsplit
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            netloc = parsed.hostname or ""
            if parsed.port:
                netloc = f"{netloc}:{parsed.port}"
            return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
        return url
    except Exception:
        return "<redacted_database_url>"


class TestPostgresCentralStorage(unittest.IsolatedAsyncioTestCase):
    """
    Tests persistent PostgreSQL exact-cache implementation.
    Requires an explicitly configured TEST_DATABASE_URL and approved database name.
    Skips cleanly when TEST_DATABASE_URL is not set.
    """

    async def asyncSetUp(self):
        explicit_url = os.getenv("TEST_DATABASE_URL")
        if not explicit_url:
            self.skipTest(
                "TEST_DATABASE_URL environment variable is not set. "
                "PostgreSQL integration tests require an explicitly configured TEST_DATABASE_URL."
            )
        self.db_url = explicit_url
        safe_url = _redact_db_url(self.db_url)

        # Safety check: enforce explicit allowlist
        from urllib.parse import urlsplit
        parsed = urlsplit(self.db_url)
        db_name = (parsed.path or "").lstrip("/")
        if db_name not in APPROVED_TEST_DATABASES:
            self.fail(
                f"Safety check failed: Database '{db_name}' is not in the approved test database allowlist: "
                f"{sorted(APPROVED_TEST_DATABASES)}. Refusing to run tests against non-allowlisted database."
            )

        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=10)
        except Exception as e:
            redacted_err = str(e)
            if parsed.password and parsed.password in redacted_err:
                redacted_err = redacted_err.replace(parsed.password, "***")
            if parsed.username and parsed.username in redacted_err:
                redacted_err = redacted_err.replace(parsed.username, "***")
            self.fail(
                f"PostgreSQL integration test setup failed for explicitly configured "
                f"TEST_DATABASE_URL ({safe_url}): {redacted_err}"
            )

        # Register cleanup immediately
        self.addAsyncCleanup(self.pool.close)
        self.addAsyncCleanup(self._cleanup_stores)

        await run_migrations(self.pool)
        self.store_a = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_a")
        self.store_b = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_a")
        self.store_other_ns = PostgresCache(self.pool, max_entries=5, ttl_seconds=3600, default_namespace="test_ns_b")
        await self.store_a.clear()
        await self.store_other_ns.clear()

    async def _cleanup_stores(self):
        if hasattr(self, "store_a"):
            await self.store_a.clear()
        if hasattr(self, "store_other_ns"):
            await self.store_other_ns.clear()

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

    async def test_provider_metadata_persistence_and_retrieval(self):
        """Verify provider metadata is saved, retrieved, and updated on conflict in PostgreSQL."""
        entry = CacheEntry(
            response="Anthropic Response",
            model="claude-3-5-sonnet",
            usage=UsageMetrics(10, 20, 30),
            latency_ms=35.0,
            provider="anthropic",
        )
        await self.store_a.set("provider_fp", entry)

        retrieved = await self.store_b.get("provider_fp")
        self.assertIsNotNone(retrieved)
        self.assertEqual(retrieved.provider, "anthropic")
        self.assertEqual(retrieved.model, "claude-3-5-sonnet")
        self.assertEqual(retrieved.response, "Anthropic Response")

        # Update entry with a different provider and verify conflict update saves new provider
        entry_updated = CacheEntry(
            response="Updated Groq Response",
            model="qwen/qwen3.8-27b",
            usage=UsageMetrics(15, 25, 40),
            latency_ms=25.0,
            provider="groq",
        )
        await self.store_a.set("provider_fp", entry_updated)

        retrieved_updated = await self.store_b.get("provider_fp")
        self.assertIsNotNone(retrieved_updated)
        self.assertEqual(retrieved_updated.provider, "groq")
        self.assertEqual(retrieved_updated.response, "Updated Groq Response")

    async def test_concurrent_writes_capacity_enforcement(self):
        """Concurrently inserting many distinct fingerprints into a small cache never exceeds max_entries."""
        small_store = PostgresCache(
            self.pool,
            max_entries=5,
            ttl_seconds=3600,
            default_namespace="test_concurrent_cap",
        )
        await small_store.clear()

        # Insert 30 distinct keys concurrently across 30 tasks
        tasks = [
            small_store.set(
                f"fp_concurrent_{i}",
                CacheEntry(f"Response {i}", "model", UsageMetrics(), 10.0),
            )
            for i in range(30)
        ]
        await asyncio.gather(*tasks)

        # Database count must strictly equal max_entries (5)
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                "test_concurrent_cap",
            )
        self.assertEqual(count, 5)

        stats = await small_store.stats(namespace="test_concurrent_cap")
        self.assertEqual(stats.total_entries, 5)
        await small_store.clear()

    async def test_independent_namespace_concurrency(self):
        """Concurrent writes into separate namespaces execute independently and maintain isolated caps."""
        store_x = PostgresCache(self.pool, max_entries=4, ttl_seconds=3600, default_namespace="test_ns_x")
        store_y = PostgresCache(self.pool, max_entries=4, ttl_seconds=3600, default_namespace="test_ns_y")
        await store_x.clear()
        await store_y.clear()

        # Concurrently write 10 entries to ns_x and 10 to ns_y
        tasks_x = [
            store_x.set(f"fp_x_{i}", CacheEntry(f"X {i}", "model", UsageMetrics(), 1.0))
            for i in range(10)
        ]
        tasks_y = [
            store_y.set(f"fp_y_{i}", CacheEntry(f"Y {i}", "model", UsageMetrics(), 1.0))
            for i in range(10)
        ]
        await asyncio.gather(*tasks_x, *tasks_y)

        stats_x = await store_x.stats(namespace="test_ns_x")
        stats_y = await store_y.stats(namespace="test_ns_y")
        self.assertEqual(stats_x.total_entries, 4)
        self.assertEqual(stats_y.total_entries, 4)

        await store_x.clear()
        await store_y.clear()

    async def test_cache_clear_scoped_to_namespace_in_postgres(self):
        """Clearing one namespace in PostgreSQL does not delete entries from other namespaces."""
        entry_a = CacheEntry("Answer A", "model", UsageMetrics(), 5.0)
        entry_b = CacheEntry("Answer B", "model", UsageMetrics(), 5.0)

        await self.store_a.set("fp_a", entry_a)
        await self.store_other_ns.set("fp_b", entry_b)

        # Both entries exist
        self.assertIsNotNone(await self.store_a.get("fp_a"))
        self.assertIsNotNone(await self.store_other_ns.get("fp_b"))

        # Clear only test_ns_a
        await self.store_a.clear(namespace="test_ns_a")

        # test_ns_a is cleared
        self.assertIsNone(await self.store_a.get("fp_a"))
        # test_ns_b is preserved
        res_b = await self.store_other_ns.get("fp_b")
        self.assertIsNotNone(res_b)
        self.assertEqual(res_b.response, "Answer B")


class TestCentralMultiClientPostgresValidation(unittest.IsolatedAsyncioTestCase):
    """
    Step 10: Validates that multiple independently authenticated clients can use a
    centrally deployed IntelliCache gateway and share authorised cached responses
    through PostgreSQL, while maintaining strict namespace isolation between unrelated clients.
    """

    async def asyncSetUp(self):
        explicit_url = os.getenv("TEST_DATABASE_URL")
        if not explicit_url:
            self.skipTest(
                "TEST_DATABASE_URL environment variable is not set. "
                "Central multi-client integration tests require an explicitly configured TEST_DATABASE_URL."
            )
        self.db_url = explicit_url
        safe_url = _redact_db_url(self.db_url)

        from urllib.parse import urlsplit
        parsed = urlsplit(self.db_url)
        db_name = (parsed.path or "").lstrip("/")
        if db_name not in APPROVED_TEST_DATABASES:
            self.fail(
                f"Safety check failed: Database '{db_name}' is not in the approved test database allowlist: "
                f"{sorted(APPROVED_TEST_DATABASES)}. Refusing to run tests against non-allowlisted database."
            )

        self.ns_shared = "mc_test_shared_team"
        self.ns_isolated = "mc_test_isolated_team"
        self.token_a = "client_a_token_secret_111"
        self.token_b = "client_b_token_secret_222"
        self.token_c = "client_c_token_secret_333"

        # Capture initial global state for deterministic restoration
        self._saved_dependency_overrides = dict(app.dependency_overrides)
        self._saved_query_cache = get_query_cache()

        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
        except Exception as e:
            redacted_err = str(e)
            if parsed.password and parsed.password in redacted_err:
                redacted_err = redacted_err.replace(parsed.password, "***")
            if parsed.username and parsed.username in redacted_err:
                redacted_err = redacted_err.replace(parsed.username, "***")
            self.fail(
                f"PostgreSQL integration test setup failed for explicitly configured "
                f"TEST_DATABASE_URL ({safe_url}): {redacted_err}"
            )

        # Register cleanup immediately upon acquiring the pool
        self.addAsyncCleanup(self._cleanup_database_and_pool)
        self.addAsyncCleanup(self._restore_app_state)

        await run_migrations(self.pool)

        # Clean test namespaces before running
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)",
                self.ns_shared,
                self.ns_isolated,
            )

        # Configure PostgreSQL cache on gateway
        self.pg_cache = PostgresCache(
            pool=self.pool,
            max_entries=100,
            ttl_seconds=3600,
            default_namespace=self.ns_shared,
        )
        set_query_cache(self.pg_cache)

        # Mock Groq provider with invocation counter
        self.provider_call_count = 0
        self.mock_groq = AsyncMock()

        def side_effect(*args, **kwargs):
            self.provider_call_count += 1
            return MockCompletion(f"Central Response #{self.provider_call_count}")

        self.mock_groq.chat.completions.create.side_effect = side_effect
        self.mock_groq.close = AsyncMock()

        app.dependency_overrides[get_groq_client_factory] = lambda: lambda: self.mock_groq

        # Configure server environment with multi-client token mapping
        import json
        tokens_json = json.dumps({
            self.token_a: self.ns_shared,
            self.token_b: self.ns_shared,
            self.token_c: self.ns_isolated,
        })
        self.env_patcher = patch.dict(os.environ, {
            "CLIENT_API_TOKENS": tokens_json,
            "CACHE_BACKEND": "postgres",
            "DATABASE_URL": self.db_url,
            "CACHE_ENABLED": "true",
        }, clear=False)
        self.env_patcher.start()
        reload_settings()

    async def _cleanup_database_and_pool(self):
        if hasattr(self, "pool") and self.pool is not None:
            try:
                if not self.pool.is_closing():
                    async with self.pool.acquire() as conn:
                        await conn.execute(
                            "DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)",
                            self.ns_shared,
                            self.ns_isolated,
                        )
            finally:
                await self.pool.close()

    async def _restore_app_state(self):
        if hasattr(self, "env_patcher"):
            self.env_patcher.stop()
        if hasattr(self, "_saved_dependency_overrides"):
            app.dependency_overrides.clear()
            app.dependency_overrides.update(self._saved_dependency_overrides)
        reload_settings()
        if hasattr(self, "_saved_query_cache"):
            set_query_cache(self._saved_query_cache)

    async def test_multi_client_cache_sharing_same_namespace_via_gateway(self):
        """
        Phase 2: Client A and Client B authenticate with separate tokens mapped to the same
        namespace. Client A triggers LLM generation; Client B reuses cached response from PostgreSQL.
        """
        prompt = "Explain WAL (Write-Ahead Logging) in PostgreSQL"
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:

            # 1. Client A queries gateway -> Cache miss -> 1 LLM call -> Stored in PostgreSQL
            resp_a = await client_a.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(resp_a.status_code, 200)
            data_a = resp_a.json()
            self.assertEqual(data_a["response"], "Central Response #1")
            self.assertFalse(data_a["cached"])
            self.assertEqual(data_a["cache_type"], "none")
            self.assertEqual(self.provider_call_count, 1)

            # Verify entry exists in PostgreSQL directly
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    "SELECT response, hit_count FROM cache_entries WHERE cache_namespace = $1",
                    self.ns_shared,
                )
                self.assertIsNotNone(row)
                self.assertEqual(row["response"], "Central Response #1")
                self.assertEqual(row["hit_count"], 0)

            # 2. Client B sends identical request using its own token and separate client instance
            resp_b = await client_b.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(resp_b.status_code, 200)
            data_b = resp_b.json()
            self.assertEqual(data_b["response"], "Central Response #1")
            self.assertTrue(data_b["cached"])
            self.assertEqual(data_b["cache_type"], "exact")
            self.assertEqual(data_b["cache_hit_count"], 1)
            # Upstream provider call count MUST remain unchanged
            self.assertEqual(self.provider_call_count, 1)

        # 3. Third independent client instance also accesses same prompt -> Cache hit
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b_new:
            resp_b_new = await client_b_new.post(
                "/query",
                json={"prompt": prompt},
                headers={"Authorization": f"Bearer {self.token_b}"},
            )
            self.assertEqual(resp_b_new.status_code, 200)
            self.assertTrue(resp_b_new.json()["cached"])
            self.assertEqual(resp_b_new.json()["cache_hit_count"], 2)
            self.assertEqual(self.provider_call_count, 1)

    async def test_namespace_isolation_prevents_cross_client_leakage(self):
        """
        Phase 3: Client C has a valid token mapped to a different namespace.
        Submitting the identical query results in a cache miss and separate provider call.
        """
        prompt = "How does B-Tree indexing work in relational databases?"
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_c:

            # 1. Client A queries gateway in shared namespace
            resp_a = await client_a.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(resp_a.status_code, 200)
            self.assertFalse(resp_a.json()["cached"])
            self.assertEqual(self.provider_call_count, 1)

            # 2. Client C sends identical query in its isolated namespace
            # MUST NOT access Client A's entry; must trigger its own provider invocation
            resp_c = await client_c.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_c},
            )
            self.assertEqual(resp_c.status_code, 200)
            data_c = resp_c.json()
            self.assertFalse(data_c["cached"])
            self.assertEqual(data_c["cache_type"], "none")
            self.assertEqual(data_c["response"], "Central Response #2")
            self.assertEqual(self.provider_call_count, 2)

        # 3. Verify PostgreSQL has 1 record in shared namespace and 1 record in isolated namespace
        async with self.pool.acquire() as conn:
            count_shared = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                self.ns_shared,
            )
            count_isolated = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                self.ns_isolated,
            )
            self.assertEqual(count_shared, 1)
            self.assertEqual(count_isolated, 1)

        # 4. Client B (in shared namespace) can still reuse Client A's response
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:
            resp_b = await client_b.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(resp_b.status_code, 200)
            self.assertTrue(resp_b.json()["cached"])
            self.assertEqual(resp_b.json()["response"], "Central Response #1")
            self.assertEqual(self.provider_call_count, 2)

    async def test_client_payload_cannot_override_authenticated_namespace(self):
        """
        Phase 3: Attempting to supply a different namespace through the request body
        is ignored. The server strictly uses the namespace derived from authenticated token.
        """
        prompt = "Explain ACID transaction properties"
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_c:
            # Client C (authorized for ns_isolated) attempts to write into ns_shared via payload
            resp_c = await client_c.post(
                "/query",
                json={
                    "prompt": prompt,
                    "namespace": self.ns_shared,
                    "cache_namespace": self.ns_shared,
                },
                headers={"X-Client-Token": self.token_c},
            )
            self.assertEqual(resp_c.status_code, 200)
            self.assertEqual(self.provider_call_count, 1)

        # Database verification: Entry must be stored in ns_isolated, NOT ns_shared
        async with self.pool.acquire() as conn:
            count_shared = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                self.ns_shared,
            )
            count_isolated = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                self.ns_isolated,
            )
            self.assertEqual(count_shared, 0)
            self.assertEqual(count_isolated, 1)

        # Client A queries for the same prompt in ns_shared -> Cache miss (proving ns_shared is clean)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a:
            resp_a = await client_a.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(resp_a.status_code, 200)
            self.assertFalse(resp_a.json()["cached"])
            self.assertEqual(self.provider_call_count, 2)

    async def test_cache_persistence_across_new_database_connections(self):
        """
        Phase 2: Cache entries stored in PostgreSQL persist and are reusable
        when a new connection pool / separate application instance attaches to the same database.
        """
        prompt = "Explain MVCC concurrency in PostgreSQL"
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a:
            # Client A populates cache in PostgreSQL
            resp_a = await client_a.post(
                "/query",
                json={"prompt": prompt},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(resp_a.status_code, 200)
            self.assertEqual(self.provider_call_count, 1)

        # Simulate fresh application connection to PostgreSQL
        new_pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
        try:
            fresh_cache = PostgresCache(
                pool=new_pool,
                max_entries=100,
                ttl_seconds=3600,
                default_namespace=self.ns_shared,
            )
            set_query_cache(fresh_cache)

            # Client B accesses via fresh pool -> Cache hit without provider call
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:
                resp_b = await client_b.post(
                    "/query",
                    json={"prompt": prompt},
                    headers={"X-Client-Token": self.token_b},
                )
                self.assertEqual(resp_b.status_code, 200)
                self.assertTrue(resp_b.json()["cached"])
                self.assertEqual(resp_b.json()["response"], "Central Response #1")
                self.assertEqual(self.provider_call_count, 1)
        finally:
            await new_pool.close()
            # Restore original cache for cleanup
            set_query_cache(self.pg_cache)

    async def test_invalid_authentication_fails_closed(self):
        """
        Invalid or missing tokens are rejected with 401 Unauthorized, avoiding provider calls and DB writes.
        """
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
            # 1. Missing token on /query
            resp_none = await client.post("/query", json={"prompt": "test"})
            self.assertEqual(resp_none.status_code, 401)

            # 2. Unknown header token on /query
            resp_bad = await client.post(
                "/query",
                json={"prompt": "test"},
                headers={"X-Client-Token": "unauthorized_malicious_token"},
            )
            self.assertEqual(resp_bad.status_code, 401)

            # 3. Unknown Bearer token on /query
            resp_bad_bearer = await client.post(
                "/query",
                json={"prompt": "test"},
                headers={"Authorization": "Bearer unknown_bearer_token"},
            )
            self.assertEqual(resp_bad_bearer.status_code, 401)

            # 4. Same authentication enforcement on /api/v1/query
            resp_v1_none = await client.post("/api/v1/query", json={"prompt": "test"})
            self.assertEqual(resp_v1_none.status_code, 401)
            resp_v1_bad = await client.post(
                "/api/v1/query",
                json={"prompt": "test"},
                headers={"X-Client-Token": "unauthorized_malicious_token"},
            )
            self.assertEqual(resp_v1_bad.status_code, 401)

        # Provider calls MUST be zero
        self.assertEqual(self.provider_call_count, 0)

        # Zero entries written to PostgreSQL
        async with self.pool.acquire() as conn:
            cnt = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace IN ($1, $2)",
                self.ns_shared,
                self.ns_isolated,
            )
            self.assertEqual(cnt, 0)

    async def test_request_parameter_fingerprint_consistency(self):
        """
        Varying request parameters (e.g. temperature) produces distinct fingerprints
        and results in separate cache entries even within the same shared namespace.
        """
        prompt = "Generate Fibonacci sequence in Python"
        transport = httpx.ASGITransport(app=app)

        async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_a, \
                   httpx.AsyncClient(transport=transport, base_url="http://testserver") as client_b:

            # Client A with temperature 0.2
            resp_a = await client_a.post(
                "/query",
                json={"prompt": prompt, "temperature": 0.2},
                headers={"X-Client-Token": self.token_a},
            )
            self.assertEqual(resp_a.status_code, 200)
            self.assertFalse(resp_a.json()["cached"])
            self.assertEqual(self.provider_call_count, 1)

            # Client B with temperature 0.8 (different fingerprint) -> Cache miss
            resp_b_diff = await client_b.post(
                "/query",
                json={"prompt": prompt, "temperature": 0.8},
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(resp_b_diff.status_code, 200)
            self.assertFalse(resp_b_diff.json()["cached"])
            self.assertEqual(self.provider_call_count, 2)

            # Client B with temperature 0.2 (matching fingerprint) -> Cache hit
            resp_b_match = await client_b.post(
                "/query",
                json={"prompt": prompt, "temperature": 0.2},
                headers={"X-Client-Token": self.token_b},
            )
            self.assertEqual(resp_b_match.status_code, 200)
            self.assertTrue(resp_b_match.json()["cached"])
            self.assertEqual(resp_b_match.json()["response"], resp_a.json()["response"])
            self.assertEqual(self.provider_call_count, 2)

