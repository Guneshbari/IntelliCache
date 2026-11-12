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
        self.env_patcher = patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
        self.env_patcher.start()
        self.client = TestClient(app)
        reload_settings()
        self.cache = get_query_cache()
        asyncio.run(self.cache.clear())

    def tearDown(self):
        self.env_patcher.stop()
        reload_settings()

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


class TestLifespanStartupAndShutdown(unittest.IsolatedAsyncioTestCase):
    """Verifies application lifespan across memory and postgres configurations."""

    async def asyncTearDown(self):
        reload_settings()

    async def test_lifespan_memory_semantic_cache_startup_and_shutdown(self):
        """Memory cache with in-memory semantic cache starts up and shuts down cleanly."""
        from app.main import create_app, lifespan
        from app.vector_store import InMemoryVectorStore
        from app.routes.query import get_query_cache, get_semantic_cache, get_vector_store

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "SEMANTIC_CACHE_ENABLED": "true",
            "EMBEDDING_PROVIDER": "fastembed",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            async with lifespan(app):
                # Verify state and singletons
                self.assertIsNotNone(app.state.cache)
                self.assertIsInstance(app.state.vector_store, InMemoryVectorStore)
                self.assertIsNotNone(app.state.semantic_cache)
                self.assertIs(get_vector_store(), app.state.vector_store)
                self.assertIs(get_semantic_cache(), app.state.semantic_cache)

            # After shutdown, singletons reset
            self.assertIsNone(get_semantic_cache())
            self.assertIsNone(get_vector_store())

    async def test_lifespan_postgres_semantic_cache_startup_and_shutdown(self):
        """Postgres cache with pgvector semantic cache starts up, runs migrations, and cleans up."""
        from unittest.mock import AsyncMock, MagicMock
        from app.main import create_app, lifespan
        from app.vector_store import PostgresVectorStore
        from app.routes.query import get_query_cache, get_semantic_cache, get_vector_store

        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://test_user:pass@localhost:5432/test_db",
            "SEMANTIC_CACHE_ENABLED": "true",
            "EMBEDDING_PROVIDER": "ollama",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
                 patch("app.main.run_migrations", new_callable=AsyncMock) as mock_migrations, \
                 patch("app.main.run_vector_migrations", new_callable=AsyncMock) as mock_vec_migrations, \
                 patch("app.main.EmbeddingProviderFactory.create") as mock_factory:

                mock_provider = MagicMock()
                mock_provider.close = AsyncMock()
                mock_factory.return_value = mock_provider

                async with lifespan(app):
                    mock_migrations.assert_awaited_once_with(mock_pool)
                    mock_vec_migrations.assert_awaited_once_with(mock_pool, dimensions=768)
                    self.assertIsInstance(app.state.vector_store, PostgresVectorStore)
                    self.assertIsNotNone(app.state.semantic_cache)
                    self.assertIs(get_vector_store(), app.state.vector_store)
                    self.assertIs(get_semantic_cache(), app.state.semantic_cache)

                # Shutdown cleans up
                mock_pool.close.assert_awaited_once()
                self.assertIsNone(get_semantic_cache())
                self.assertIsNone(get_vector_store())

    async def test_semantic_cache_startup_error_fails_fast(self):
        """Startup errors in semantic cache are not swallowed by fail-open logic."""
        from app.main import create_app, lifespan
        from app.embeddings.errors import EmbeddingConfigurationError

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "memory",
            "SEMANTIC_CACHE_ENABLED": "true",
            "EMBEDDING_PROVIDER": "unknown-provider",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with self.assertRaises(EmbeddingConfigurationError):
                async with lifespan(app):
                    pass

    async def test_startup_db_migration_failure_closes_pool(self):
        """If database migration fails during startup, the PostgreSQL pool is closed and singletons reset."""
        from unittest.mock import AsyncMock, MagicMock
        from app.main import create_app, lifespan
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://test_user:pass@localhost:5432/test_db",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
                 patch("app.main.run_migrations", new_callable=AsyncMock, side_effect=RuntimeError("Migration failed")):

                with self.assertRaises(RuntimeError) as ctx:
                    async with lifespan(app):
                        pass
                self.assertIn("Migration failed", str(ctx.exception))

            mock_pool.close.assert_awaited_once()
            self.assertIsNone(routes._cache_singleton)
            self.assertIsNone(get_vector_store())
            self.assertIsNone(get_semantic_cache())
            self.assertIsNone(app.state.db_pool)
            self.assertIsNone(app.state.cache)

    async def test_startup_vector_migration_failure_closes_pool_and_releases_resources(self):
        """If vector migration fails, the PostgreSQL pool is closed and singletons reset."""
        from unittest.mock import AsyncMock, MagicMock
        from app.main import create_app, lifespan
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://test_user:pass@localhost:5432/test_db",
            "SEMANTIC_CACHE_ENABLED": "true",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
                 patch("app.main.run_migrations", new_callable=AsyncMock), \
                 patch("app.main.run_vector_migrations", new_callable=AsyncMock, side_effect=RuntimeError("Vector migration error")):

                with self.assertRaises(RuntimeError) as ctx:
                    async with lifespan(app):
                        pass
                self.assertIn("Vector migration error", str(ctx.exception))

            mock_pool.close.assert_awaited_once()
            self.assertIsNone(routes._cache_singleton)
            self.assertIsNone(get_vector_store())
            self.assertIsNone(get_semantic_cache())

    async def test_startup_embedding_factory_failure_postgres_closes_pool(self):
        """If embedding provider factory fails in PostgreSQL mode, pool is closed cleanly."""
        from unittest.mock import AsyncMock, MagicMock
        from app.main import create_app, lifespan
        from app.embeddings.errors import EmbeddingConfigurationError
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://test_user:pass@localhost:5432/test_db",
            "SEMANTIC_CACHE_ENABLED": "true",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
                 patch("app.main.run_migrations", new_callable=AsyncMock), \
                 patch("app.main.run_vector_migrations", new_callable=AsyncMock), \
                 patch("app.main.EmbeddingProviderFactory.create", side_effect=EmbeddingConfigurationError("Factory error")):

                with self.assertRaises(EmbeddingConfigurationError):
                    async with lifespan(app):
                        pass

            mock_pool.close.assert_awaited_once()
            self.assertIsNone(routes._cache_singleton)
            self.assertIsNone(get_vector_store())
            self.assertIsNone(get_semantic_cache())

    async def test_startup_embedding_factory_failure_memory_clears_resources(self):
        """If embedding provider factory fails in memory mode, partial memory resources are cleared."""
        from app.main import create_app, lifespan
        from app.embeddings.errors import EmbeddingConfigurationError
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "SEMANTIC_CACHE_ENABLED": "true",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("app.main.EmbeddingProviderFactory.create", side_effect=EmbeddingConfigurationError("Mem factory fail")):
                with self.assertRaises(EmbeddingConfigurationError):
                    async with lifespan(app):
                        pass

            self.assertIsNone(routes._cache_singleton)
            self.assertIsNone(get_vector_store())
            self.assertIsNone(get_semantic_cache())

    async def test_shutdown_semantic_cache_error_still_closes_exact_cache_and_pool(self):
        """If semantic-cache cleanup raises, exact-cache and PostgreSQL pool are still closed."""
        from unittest.mock import AsyncMock, MagicMock
        from app.main import create_app, lifespan
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        mock_pool = MagicMock()
        mock_pool.close = AsyncMock()

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": "postgresql://test_user:pass@localhost:5432/test_db",
            "SEMANTIC_CACHE_ENABLED": "true",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with patch("asyncpg.create_pool", new_callable=AsyncMock, return_value=mock_pool), \
                 patch("app.main.run_migrations", new_callable=AsyncMock), \
                 patch("app.main.run_vector_migrations", new_callable=AsyncMock), \
                 patch("app.main.EmbeddingProviderFactory.create") as mock_factory:

                mock_provider = MagicMock()
                mock_provider.close = AsyncMock()
                mock_factory.return_value = mock_provider

                with self.assertRaises(RuntimeError) as ctx:
                    async with lifespan(app):
                        # Mock semantic_cache.close to raise an error during shutdown
                        app.state.semantic_cache.close = AsyncMock(side_effect=RuntimeError("Semantic close exploded"))
                self.assertIn("Semantic close exploded", str(ctx.exception))

                # Pool should STILL be closed even though semantic cache close exploded!
                mock_pool.close.assert_awaited_once()
                # Global singletons must still be reset
                self.assertIsNone(get_semantic_cache())
                self.assertIsNone(get_vector_store())
                self.assertIsNone(routes._cache_singleton)

    async def test_shutdown_vector_store_error_in_lifespan_still_closes_exact_cache(self):
        """If vector-store cleanup raises during unowned shutdown, exact-cache is still closed."""
        from unittest.mock import AsyncMock
        from app.main import create_app, lifespan
        import app.api.routes as routes
        from app.routes.query import get_semantic_cache, get_vector_store

        with patch.dict(os.environ, {
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "SEMANTIC_CACHE_ENABLED": "true",
            "GROQ_API_KEY": "gsk_test",
        }, clear=False):
            reload_settings()
            app = create_app()

            with self.assertRaises(RuntimeError) as ctx:
                async with lifespan(app):
                    # Detach semantic_cache so vector_store is closed independently, and mock vector_store.close to raise
                    app.state.semantic_cache = None
                    app.state.vector_store.close = AsyncMock(side_effect=RuntimeError("Vector store close exploded"))
            self.assertIn("Vector store close exploded", str(ctx.exception))

            # Singletons must be reset
            self.assertIsNone(get_semantic_cache())
            self.assertIsNone(get_vector_store())
            self.assertIsNone(routes._cache_singleton)


if __name__ == "__main__":
    unittest.main()
