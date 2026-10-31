"""
Unit and PostgreSQL integration tests for the vector storage layer.
Tests vector contracts, immutability, dimension validation, pgvector nearest-neighbour
search, namespace isolation, model isolation, cascade delete, and failure handling.
"""

import ast
import asyncio
from datetime import datetime, timezone
import math
import os
from pathlib import Path
import random
import time
import unittest
from urllib.parse import urlsplit

import asyncpg

from app.cache.migration import run_migrations
from app.config import Settings
from app.vector_store.contracts import (
    VectorRecord,
    VectorSearchResult,
    cosine_distance_to_similarity,
)
from app.vector_store.errors import (
    VectorDimensionMismatchError,
    VectorInvalidError,
    VectorStoreConfigurationError,
    VectorStoreConnectionError,
    VectorStoreError,
)
from app.vector_store.interfaces import BaseVectorStore
from app.vector_store.migration import (
    check_pgvector_available,
    run_vector_migrations,
)
from app.vector_store.postgres_store import PostgresVectorStore


def _redact_url(url: str) -> str:
    parsed = urlsplit(url)
    if parsed.password:
        return url.replace(f":{parsed.password}@", ":***@")
    return url


class TestVectorContractsAndMath(unittest.TestCase):
    """Tests contracts, validation, immutability, and math calculations."""

    def test_valid_vector_record(self):
        """Valid inputs produce a frozen VectorRecord with immutable vector tuple."""
        vec = [0.1 * i for i in range(10)]
        rec = VectorRecord(
            cache_namespace="test_ns",
            fingerprint="fp_12345",
            query_text="What is vector search?",
            vector=vec,
            model="nomic-embed-text",
            metadata={"source": "unit_test"},
        )
        self.assertEqual(rec.cache_namespace, "test_ns")
        self.assertEqual(rec.namespace, "test_ns")
        self.assertEqual(rec.fingerprint, "fp_12345")
        self.assertEqual(rec.model, "nomic-embed-text")
        self.assertEqual(rec.dimensions, 10)
        self.assertIsInstance(rec.vector, tuple)
        self.assertEqual(len(rec.to_list()), 10)

    def test_input_mutation_does_not_mutate_vector_record(self):
        """Mutating the source list does not alter the VectorRecord's vector."""
        vec = [1.0, 2.0, 3.0]
        rec = VectorRecord(
            cache_namespace="test_ns",
            fingerprint="fp_123",
            query_text="test",
            vector=vec,
            model="nomic",
        )
        vec[0] = 999.0
        vec.append(888.0)
        self.assertEqual(rec.vector[0], 1.0)
        self.assertEqual(len(rec.vector), 3)

    def test_empty_fields_rejected(self):
        """Empty namespace, fingerprint, query_text, model, or vector are rejected."""
        valid_vec = [0.1, 0.2]
        with self.assertRaises(VectorInvalidError):
            VectorRecord(cache_namespace="", fingerprint="fp", query_text="q", vector=valid_vec, model="m")
        with self.assertRaises(VectorInvalidError):
            VectorRecord(cache_namespace="ns", fingerprint="", query_text="q", vector=valid_vec, model="m")
        with self.assertRaises(VectorInvalidError):
            VectorRecord(cache_namespace="ns", fingerprint="fp", query_text=" ", vector=valid_vec, model="m")
        with self.assertRaises(VectorInvalidError):
            VectorRecord(cache_namespace="ns", fingerprint="fp", query_text="q", vector=valid_vec, model="")
        with self.assertRaises(VectorInvalidError):
            VectorRecord(cache_namespace="ns", fingerprint="fp", query_text="q", vector=[], model="m")

    def test_non_finite_and_boolean_rejected(self):
        """NaN, Inf, and booleans in vectors are rejected."""
        for bad_val in [float("nan"), float("inf"), float("-inf"), True, False]:
            with self.assertRaises(VectorInvalidError):
                VectorRecord(
                    cache_namespace="ns",
                    fingerprint="fp",
                    query_text="q",
                    vector=[0.1, bad_val],
                    model="m",
                )

    def test_cosine_distance_to_similarity(self):
        """Validates distance to similarity calculation and clamping."""
        # Identical vectors: distance = 0.0 -> similarity = 1.0
        self.assertAlmostEqual(cosine_distance_to_similarity(0.0), 1.0)
        # Orthogonal vectors: distance = 1.0 -> similarity = 0.0
        self.assertAlmostEqual(cosine_distance_to_similarity(1.0), 0.0)
        # Opposite vectors: distance = 2.0 -> similarity = -1.0
        self.assertAlmostEqual(cosine_distance_to_similarity(2.0), -1.0)
        # Intermediate distance = 0.25 -> similarity = 0.75
        self.assertAlmostEqual(cosine_distance_to_similarity(0.25), 0.75)
        # Clamping bounds: slight floating point overshoot
        self.assertEqual(cosine_distance_to_similarity(-0.0001), 1.0)
        self.assertEqual(cosine_distance_to_similarity(2.0001), -1.0)

        # Invalid distances
        with self.assertRaises(VectorInvalidError):
            cosine_distance_to_similarity(float("nan"))
        with self.assertRaises(VectorInvalidError):
            cosine_distance_to_similarity(float("inf"))

    def test_ast_import_isolation(self):
        """Vector store package must not import FastAPI, Groq SDK, or Ollama SDK."""
        package_dir = Path(__file__).resolve().parent.parent / "app" / "vector_store"
        forbidden = ["fastapi", "starlette", "groq", "ollama"]

        for py_path in package_dir.glob("*.py"):
            tree = ast.parse(py_path.read_text(encoding="utf-8"), filename=str(py_path))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        top = alias.name.split(".")[0].lower()
                        self.assertNotIn(
                            top,
                            forbidden,
                            f"Forbidden import '{top}' detected in {py_path.name}",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        top = node.module.split(".")[0].lower()
                        self.assertNotIn(
                            top,
                            forbidden,
                            f"Forbidden from-import '{top}' detected in {py_path.name}",
                        )

    def test_config_validation(self):
        """Settings validate semantic cache dependencies and vector dimensions."""
        # By default, semantic cache is disabled
        cfg = Settings(groq_api_key="gsk_test")
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertEqual(cfg.vector_dimensions, 768)

        # Enabling semantic cache without postgres backend raises ValueError
        with self.assertRaises(ValueError) as ctx:
            Settings(groq_api_key="gsk_test", semantic_cache_enabled=True, cache_backend="memory")
        self.assertIn("SEMANTIC_CACHE_ENABLED requires CACHE_BACKEND='postgres'", str(ctx.exception))

        # Non-positive vector dimensions raise ValueError
        with self.assertRaises(ValueError) as ctx:
            Settings(groq_api_key="gsk_test", vector_dimensions=0)
        self.assertIn("VECTOR_DIMENSIONS must be positive", str(ctx.exception))


class TestPostgresVectorStoreIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Integration tests against a PostgreSQL instance with pgvector installed.
    Tests storage, retrieval, nearest-neighbour ordering, isolation, and cascading deletes.
    """

    async def asyncSetUp(self):
        # Look for explicit TEST_DATABASE_URL or test port 5433 (pgvector cluster)
        explicit_url = os.getenv("TEST_DATABASE_URL")
        self.is_explicit_url = explicit_url is not None
        self.db_url = explicit_url or "postgresql://gnx@127.0.0.1:5433/intellicache_test"
        safe_url = _redact_url(self.db_url)

        # Safety check: test database name must contain 'test'
        parsed = urlsplit(self.db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            raise ValueError(
                f"Safety check failed: Refusing to run tests against database '{db_name}'. "
                "Database name must contain 'test' to avoid altering non-test environments."
            )

        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
            # Ensure base exact cache tables exist
            await run_migrations(self.pool)
            # Ensure vector tables exist (dimensions=768)
            await run_vector_migrations(self.pool, dimensions=768)

            self.store = PostgresVectorStore(
                pool=self.pool,
                default_namespace="test_vec_ns",
                default_model="nomic-embed-text",
                dimensions=768,
            )
            # Clean namespace before test
            await self.store.clear("test_vec_ns")
            await self.store.clear("other_ns")
        except Exception as e:
            redacted_err = str(e)
            if parsed.password and parsed.password in redacted_err:
                redacted_err = redacted_err.replace(parsed.password, "***")
            if parsed.username and parsed.username in redacted_err:
                redacted_err = redacted_err.replace(parsed.username, "***")

            if self.is_explicit_url:
                self.fail(
                    f"PostgreSQL integration test setup failed for explicit TEST_DATABASE_URL ({safe_url}): {redacted_err}"
                )
            else:
                self.skipTest(
                    f"pgvector test database unavailable at {safe_url}: {redacted_err}"
                )

    async def asyncTearDown(self):
        if hasattr(self, "store"):
            try:
                await self.store.clear("test_vec_ns")
                await self.store.clear("other_ns")
            except Exception:
                pass
        if hasattr(self, "pool"):
            await self.pool.close()

    async def _create_exact_entry(self, fingerprint: str, namespace: str = "test_vec_ns", response: str = "Test response"):
        """Creates an exact cache entry required by the foreign key constraint."""
        query = """
            INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider)
            VALUES ($1, $2, $3, 'llama-3', 'groq')
            ON CONFLICT (cache_namespace, fingerprint) DO NOTHING;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query, namespace, fingerprint, response)

    def _make_vector(self, head: list[float], fill: float = 0.0, total_dim: int = 768) -> list[float]:
        """Helper to create a 768-dimension vector with specific leading coordinates."""
        vec = list(head)
        if len(vec) < total_dim:
            vec.extend([fill] * (total_dim - len(vec)))
        return vec[:total_dim]

    @staticmethod
    def _generate_normalized_vector(rng: random.Random, dim: int = 768) -> list[float]:
        """Generates a random unit-normalized vector on the 768-dimensional unit sphere."""
        raw = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    @staticmethod
    def _perturb_vector(vec: list[float], rng: random.Random, epsilon: float = 0.08) -> list[float]:
        """Perturbs a unit vector by adding a small directional noise vector and re-normalizing."""
        dim = len(vec)
        noise = [rng.gauss(0.0, 1.0) for _ in range(dim)]
        noise_norm = math.sqrt(sum(x * x for x in noise))
        raw = [vec[i] + epsilon * (noise[i] / noise_norm) for i in range(dim)]
        norm = math.sqrt(sum(x * x for x in raw))
        return [x / norm for x in raw]

    @staticmethod
    def _format_pgvector(vec: list[float]) -> str:
        """Formats a float vector into a PostgreSQL pgvector literal string [x1,x2,...]."""
        return "[" + ",".join(f"{x:.6f}" for x in vec) + "]"

    @staticmethod
    def _compute_percentiles(values: list[float]) -> tuple[float, float, float]:
        """Calculates (mean, p50, p95) in milliseconds for a list of latency samples."""
        if not values:
            return 0.0, 0.0, 0.0
        s = sorted(values)
        n = len(s)
        mean_val = sum(s) / n
        p50_val = s[int(n * 0.50)]
        p95_val = s[min(int(n * 0.95), n - 1)]
        return round(mean_val, 3), round(p50_val, 3), round(p95_val, 3)

    async def test_insert_and_retrieve_vector_record(self):
        """Successfully inserts and retrieves a vector record."""
        fp = "fp_test_001"
        await self._create_exact_entry(fp)

        vec = self._make_vector([0.5, -0.2, 0.8])
        record = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="How does semantic caching work?",
            vector=vec,
            model="nomic-embed-text",
            metadata={"source": "integration_test"},
        )

        await self.store.upsert(record)
        fetched = await self.store.get(fp, namespace="test_vec_ns")
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.fingerprint, fp)
        self.assertEqual(fetched.query_text, "How does semantic caching work?")
        self.assertEqual(fetched.model, "nomic-embed-text")
        self.assertEqual(fetched.dimensions, 768)
        self.assertAlmostEqual(fetched.vector[0], 0.5, places=5)
        self.assertAlmostEqual(fetched.vector[1], -0.2, places=5)
        self.assertAlmostEqual(fetched.vector[2], 0.8, places=5)

    async def test_update_existing_vector_record(self):
        """Upserting with same namespace, fingerprint, and model updates the record."""
        fp = "fp_test_update"
        await self._create_exact_entry(fp)

        vec1 = self._make_vector([0.1, 0.2])
        rec1 = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Initial text",
            vector=vec1,
            model="nomic-embed-text",
        )
        await self.store.upsert(rec1)

        vec2 = self._make_vector([0.9, 0.8])
        rec2 = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Updated text",
            vector=vec2,
            model="nomic-embed-text",
        )
        await self.store.upsert(rec2)

        fetched = await self.store.get(fp)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.query_text, "Updated text")
        self.assertAlmostEqual(fetched.vector[0], 0.9, places=5)

    async def test_delete_record(self):
        """Deletes a vector record by fingerprint and namespace."""
        fp = "fp_test_delete"
        await self._create_exact_entry(fp)
        rec = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Delete me",
            vector=self._make_vector([0.1]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec)

        deleted = await self.store.delete(fp, namespace="test_vec_ns")
        self.assertTrue(deleted)

        fetched = await self.store.get(fp, namespace="test_vec_ns")
        self.assertIsNone(fetched)

        # Deleting nonexistent returns False
        deleted_again = await self.store.delete("nonexistent_fp")
        self.assertFalse(deleted_again)

    async def test_namespace_scoped_clearing(self):
        """Clearing one namespace does not delete records in another namespace."""
        fp_a = "fp_ns_a"
        fp_b = "fp_ns_b"
        await self._create_exact_entry(fp_a, namespace="test_vec_ns")
        await self._create_exact_entry(fp_b, namespace="other_ns")

        rec_a = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp_a,
            query_text="A",
            vector=self._make_vector([0.1]),
            model="nomic-embed-text",
        )
        rec_b = VectorRecord(
            cache_namespace="other_ns",
            fingerprint=fp_b,
            query_text="B",
            vector=self._make_vector([0.2]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec_a)
        await self.store.upsert(rec_b)

        cleared_count = await self.store.clear("test_vec_ns")
        self.assertGreaterEqual(cleared_count, 1)

        self.assertIsNone(await self.store.get(fp_a, namespace="test_vec_ns"))
        self.assertIsNotNone(await self.store.get(fp_b, namespace="other_ns"))

    async def test_nearest_neighbour_ordering_and_cosine_distance(self):
        """
        Nearest neighbour retrieval correctly orders by ascending cosine distance,
        computes similarity score, and returns associated exact-cache response.
        """
        # Target query vector is pointing along coordinate 0: [1.0, 0.0, ...]
        query_vec = self._make_vector([1.0, 0.0, 0.0])

        # Candidate 1: Identical direction [1.0, 0.0, 0.0] -> distance = 0.0, similarity = 1.0
        fp1 = "fp_identical"
        await self._create_exact_entry(fp1, response="Exact response for identical query")
        rec1 = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp1,
            query_text="Identical query",
            vector=self._make_vector([1.0, 0.0, 0.0]),
            model="nomic-embed-text",
        )

        # Candidate 2: 45-degree angle [1.0, 1.0, 0.0] -> distance = 1 - 1/sqrt(2) ≈ 0.2929
        fp2 = "fp_close"
        await self._create_exact_entry(fp2, response="Response for close query")
        rec2 = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp2,
            query_text="Close query",
            vector=self._make_vector([1.0, 1.0, 0.0]),
            model="nomic-embed-text",
        )

        # Candidate 3: Orthogonal [0.0, 1.0, 0.0] -> distance = 1.0, similarity = 0.0
        fp3 = "fp_orthogonal"
        await self._create_exact_entry(fp3, response="Response for orthogonal query")
        rec3 = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp3,
            query_text="Orthogonal query",
            vector=self._make_vector([0.0, 1.0, 0.0]),
            model="nomic-embed-text",
        )

        await self.store.upsert(rec1)
        await self.store.upsert(rec2)
        await self.store.upsert(rec3)

        results = await self.store.search(query_vec, top_k=3, namespace="test_vec_ns")
        self.assertEqual(len(results), 3)

        # Result 1: Identical
        self.assertEqual(results[0].fingerprint, fp1)
        self.assertAlmostEqual(results[0].distance, 0.0, places=4)
        self.assertAlmostEqual(results[0].similarity, 1.0, places=4)
        self.assertEqual(results[0].cached_response, "Exact response for identical query")

        # Result 2: Close
        self.assertEqual(results[1].fingerprint, fp2)
        self.assertAlmostEqual(results[1].distance, 0.2929, places=3)
        self.assertAlmostEqual(results[1].similarity, 0.7071, places=3)
        self.assertEqual(results[1].cached_response, "Response for close query")

        # Result 3: Orthogonal
        self.assertEqual(results[2].fingerprint, fp3)
        self.assertAlmostEqual(results[2].distance, 1.0, places=4)
        self.assertAlmostEqual(results[2].similarity, 0.0, places=4)
        self.assertEqual(results[2].cached_response, "Response for orthogonal query")

        # Verify ascending order of distance
        self.assertLess(results[0].distance, results[1].distance)
        self.assertLess(results[1].distance, results[2].distance)

    async def test_search_excludes_expired_cache_entries(self):
        """Vector search excludes records whose associated exact-cache entry has expired."""
        fp_active = "fp_active_search"
        fp_expired = "fp_expired_search"

        # Active entry (expires in future)
        query_active = """
            INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider, expires_at)
            VALUES ('test_vec_ns', $1, 'Active response', 'llama-3', 'groq', NOW() + INTERVAL '1 hour')
            ON CONFLICT (cache_namespace, fingerprint) DO NOTHING;
        """
        # Expired entry (expired 10 minutes ago)
        query_expired = """
            INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider, expires_at)
            VALUES ('test_vec_ns', $1, 'Expired response', 'llama-3', 'groq', NOW() - INTERVAL '10 minutes')
            ON CONFLICT (cache_namespace, fingerprint) DO NOTHING;
        """
        async with self.pool.acquire() as conn:
            await conn.execute(query_active, fp_active)
            await conn.execute(query_expired, fp_expired)

        rec_active = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp_active,
            query_text="Active query",
            vector=self._make_vector([1.0, 0.0]),
            model="nomic-embed-text",
        )
        rec_expired = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp_expired,
            query_text="Expired query",
            vector=self._make_vector([1.0, 0.0]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec_active)
        await self.store.upsert(rec_expired)

        # Search should return ONLY the active entry, not the expired entry
        results = await self.store.search(self._make_vector([1.0, 0.0]), top_k=5, namespace="test_vec_ns")
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fingerprint, fp_active)
        self.assertEqual(results[0].cached_response, "Active response")

    async def test_namespace_isolation(self):
        """Vectors in namespace A are never returned when searching namespace B."""
        fp = "fp_ns_iso"
        await self._create_exact_entry(fp, namespace="test_vec_ns")
        rec = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Namespace iso query",
            vector=self._make_vector([1.0]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec)

        results = await self.store.search(self._make_vector([1.0]), namespace="other_ns")
        self.assertEqual(len(results), 0)

    async def test_embedding_model_isolation(self):
        """Vectors indexed with model A are never returned when searching for model B."""
        fp = "fp_model_iso"
        await self._create_exact_entry(fp)
        rec = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Model iso query",
            vector=self._make_vector([1.0]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec)

        results = await self.store.search(
            self._make_vector([1.0]),
            namespace="test_vec_ns",
            model="different-embedding-model",
        )
        self.assertEqual(len(results), 0)

    async def test_dimension_mismatch_rejection(self):
        """Upserting or searching with mismatched dimensions raises VectorDimensionMismatchError."""
        fp = "fp_dim_mismatch"
        await self._create_exact_entry(fp)

        # Store expects 768 dimensions; provide 512
        bad_vec = [0.1] * 512
        with self.assertRaises(VectorDimensionMismatchError) as ctx:
            # Note: VectorRecord itself accepts 512, but PostgresVectorStore enforces 768
            rec = VectorRecord(
                cache_namespace="test_vec_ns",
                fingerprint=fp,
                query_text="Bad dim",
                vector=bad_vec,
                model="nomic-embed-text",
            )
            await self.store.upsert(rec)
        self.assertIn("does not match store dimension (768)", str(ctx.exception))

        # Query vector with wrong dimensions
        with self.assertRaises(VectorDimensionMismatchError):
            await self.store.search([0.1] * 384, namespace="test_vec_ns")

    async def test_invalid_top_k_rejected(self):
        """Invalid top_k values (<= 0, boolean, or non-int) are rejected."""
        valid_vec = self._make_vector([0.1])
        for bad_k in [0, -1, -10, True, False]:
            with self.assertRaises(VectorInvalidError):
                await self.store.search(valid_vec, top_k=bad_k)

    async def test_cascading_deletion_from_cache_entries(self):
        """Deleting or evicting an entry in cache_entries cascades and deletes vector_records."""
        fp = "fp_cascade_test"
        await self._create_exact_entry(fp)
        rec = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Cascade query",
            vector=self._make_vector([0.1]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec)

        # Confirm vector record exists
        fetched_before = await self.store.get(fp)
        self.assertIsNotNone(fetched_before)

        # Delete the exact entry from cache_entries
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cache_entries WHERE cache_namespace = $1 AND fingerprint = $2",
                "test_vec_ns",
                fp,
            )

        # Confirm vector record was automatically deleted via ON DELETE CASCADE
        fetched_after = await self.store.get(fp)
        self.assertIsNone(fetched_after)

    async def test_concurrent_upserts_same_record(self):
        """Concurrent upserts of the same vector record do not violate constraints."""
        fp = "fp_concurrent"
        await self._create_exact_entry(fp)

        records = [
            VectorRecord(
                cache_namespace="test_vec_ns",
                fingerprint=fp,
                query_text=f"Concurrent query {i}",
                vector=self._make_vector([0.1 * (i + 1)]),
                model="nomic-embed-text",
            )
            for i in range(8)
        ]

        # Execute concurrent upserts
        await asyncio.gather(*[self.store.upsert(r) for r in records])

        # Exactly 1 record should remain in database
        async with self.pool.acquire() as conn:
            count = await conn.fetchval(
                "SELECT COUNT(*) FROM vector_records WHERE cache_namespace = $1 AND fingerprint = $2",
                "test_vec_ns",
                fp,
            )
        self.assertEqual(count, 1)

    async def test_hnsw_index_info_and_availability(self):
        """get_index_info reports the HNSW vector index presence on vector_records."""
        info = await self.store.get_index_info()
        self.assertEqual(info["vector_index_type"], "hnsw")
        self.assertTrue(info["vector_index_available"])
        self.assertIn("idx_vector_records_embedding_hnsw", info["indexes"])

    async def test_hnsw_migration_is_idempotent(self):
        """Applying vector migrations multiple times succeeds without error."""
        await run_vector_migrations(self.pool, dimensions=768)
        info = await self.store.get_index_info()
        self.assertTrue(info["vector_index_available"])

    async def test_explain_search_small_table_and_plan_sanitization(self):
        """
        On small tables, PostgreSQL chooses exact sequential/B-tree scan;
        explain_search reports vector_index_used=False and sanitizes vector coordinates.
        """
        fp = "fp_small_table_test"
        await self._create_exact_entry(fp)
        rec = VectorRecord(
            cache_namespace="test_vec_ns",
            fingerprint=fp,
            query_text="Small table query",
            vector=self._make_vector([0.1, 0.2]),
            model="nomic-embed-text",
        )
        await self.store.upsert(rec)

        query_vec = self._make_vector([0.3712, 0.8491])
        plan = await self.store.explain_search(query_vec, top_k=5, namespace="test_vec_ns")
        self.assertFalse(plan["vector_index_used"])
        self.assertIn("Exact filtered vector search", plan["retrieval_method"])
        self.assertGreater(len(plan["execution_plan_summary"]), 0)

        # Confirm coordinates are sanitized from execution plan summary
        plan_str = " ".join(plan["execution_plan_summary"])
        self.assertNotIn("0.3712", plan_str)
        self.assertNotIn("0.8491", plan_str)

    async def test_explain_search_hnsw_selection_on_seeded_table(self):
        """
        On seeded tables where row count and selectivity favor graph search,
        the PostgreSQL query planner cost model selects the HNSW index scan.
        Note: The crossover point is cost-dependent on row count, selectivity,
        and PostgreSQL cost parameters (e.g. random_page_cost), not a universal constant.
        """
        ns = "test_hnsw_seed_ns"
        rng = random.Random(123)
        async with self.pool.acquire() as conn:
            cache_entries = [(ns, f"fp_seed_{i}", f"resp_{i}", "llama-3", "groq") for i in range(3000)]
            vec_records = [
                (ns, f"fp_seed_{i}", f"query {i}", "nomic-embed-text", self._format_pgvector(self._generate_normalized_vector(rng, 768)))
                for i in range(3000)
            ]
            chunk_size = 500
            for i in range(0, len(cache_entries), chunk_size):
                await conn.executemany(
                    "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    cache_entries[i : i + chunk_size],
                )
                await conn.executemany(
                    "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT DO NOTHING",
                    vec_records[i : i + chunk_size],
                )
            await conn.execute("ANALYZE vector_records;")
            await conn.execute("ANALYZE cache_entries;")

        try:
            query_vec = self._generate_normalized_vector(rng, 768)
            plan = await self.store.explain_search(query_vec, top_k=5, namespace=ns, analyze=True)
            self.assertTrue(plan["vector_index_used"])
            self.assertEqual(plan["retrieval_method"], "HNSW approximate nearest-neighbour search")
            self.assertTrue(any("idx_vector_records_embedding_hnsw" in line for line in plan["execution_plan_summary"]))
        finally:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", ns)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", ns)

    async def test_controlled_ann_vs_exact_retrieval_recall(self):
        """
        Controlled comparison between exact nearest-neighbour search and HNSW retrieval
        over 5,000+ diverse, normalized 768-dimensional vectors.
        Validates Recall@5 and Recall@10 across 10 distinct queries, asserts cosine distance
        spread (non-degenerate), verifies planner selected HNSW, and validates against
        a controlled exact baseline with index/bitmap scans explicitly disabled.
        """
        ns = "test_ann_recall_ns"
        rng = random.Random(42)
        total_vectors = 5000

        # 1. Generate diverse, normalized 768-dimensional float vectors
        vectors = [self._generate_normalized_vector(rng, 768) for _ in range(total_vectors)]

        # 2. Assert cosine distance variation across a sample (non-degenerate, diverse vectors)
        sample = vectors[:50]
        distances = []
        for i in range(len(sample)):
            for j in range(i + 1, len(sample)):
                dot = sum(sample[i][k] * sample[j][k] for k in range(768))
                distances.append(1.0 - dot)
        min_d, max_d, avg_d = min(distances), max(distances), sum(distances) / len(distances)
        self.assertGreater(min_d, 0.60, "Vectors must not be collinear or duplicate (min distance too small)")
        self.assertLess(max_d, 1.40, "Vectors must be valid normalized vectors")
        self.assertGreater(max_d - min_d, 0.15, "Cosine distance distribution must show non-trivial spread")
        self.assertAlmostEqual(avg_d, 1.0, delta=0.08, msg="Unit random sphere vectors should average near 1.0 cosine distance")

        # 3. Create 10 distinct query vectors and place perturbed candidates nearby in the dataset
        num_queries = 10
        query_vectors = [self._generate_normalized_vector(rng, 768) for _ in range(num_queries)]
        for q_idx, q_vec in enumerate(query_vectors):
            for k in range(10):
                eps = 0.04 + k * 0.015
                vectors[q_idx * 10 + k] = self._perturb_vector(q_vec, rng, epsilon=eps)

        # 4. Insert 5,000 records into database
        async with self.pool.acquire() as conn:
            cache_entries = [(ns, f"fp_ann_{i}", f"resp_{i}", "llama-3", "groq") for i in range(total_vectors)]
            vec_records = [
                (ns, f"fp_ann_{i}", f"query {i}", "nomic-embed-text", self._format_pgvector(vectors[i]))
                for i in range(total_vectors)
            ]
            chunk_size = 500
            for i in range(0, total_vectors, chunk_size):
                await conn.executemany(
                    "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    cache_entries[i : i + chunk_size],
                )
                await conn.executemany(
                    "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT DO NOTHING",
                    vec_records[i : i + chunk_size],
                )
            await conn.execute("ANALYZE vector_records;")
            await conn.execute("ANALYZE cache_entries;")

        try:
            recalls_at_5 = []
            recalls_at_10 = []

            for q_idx, q_vec in enumerate(query_vectors):
                # 5. Verify query planner selects HNSW index scan for this query
                plan = await self.store.explain_search(q_vec, top_k=10, namespace=ns, analyze=True)
                if not plan["vector_index_used"]:
                    self.fail(
                        f"Query {q_idx} did not use HNSW index in execution plan: "
                        f"{plan['execution_plan_summary']}. Recall cannot be reported without confirmed HNSW index usage."
                    )
                self.assertIn("idx_vector_records_embedding_hnsw", " ".join(plan["execution_plan_summary"]))

                # 6. Retrieve approximate nearest neighbours via vector store
                ann_results = await self.store.search(q_vec, top_k=10, namespace=ns)
                ann_fps = [r.fingerprint for r in ann_results]
                self.assertEqual(len(ann_fps), 10)

                # 7. Establish exact baseline via a controlled transaction with indexscan & bitmapscan disabled
                q_pg_str = self._format_pgvector(q_vec)
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL enable_indexscan = off;")
                        await conn.execute("SET LOCAL enable_bitmapscan = off;")

                        # Verify exact plan does NOT use HNSW
                        exact_plan = await conn.fetch(
                            f"""
                            EXPLAIN SELECT v.fingerprint
                            FROM vector_records v
                            JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                            WHERE v.cache_namespace = '{ns}' AND v.model = 'nomic-embed-text'
                              AND (c.expires_at IS NULL OR c.expires_at > NOW())
                            ORDER BY (v.vector <=> '{q_pg_str}'::vector) ASC
                            LIMIT 10;
                            """
                        )
                        exact_plan_str = " ".join(r[0] for r in exact_plan)
                        self.assertNotIn("idx_vector_records_embedding_hnsw", exact_plan_str)

                        # Fetch exact top-10 rows
                        exact_rows = await conn.fetch(
                            f"""
                            SELECT v.fingerprint
                            FROM vector_records v
                            JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                            WHERE v.cache_namespace = '{ns}' AND v.model = 'nomic-embed-text'
                              AND (c.expires_at IS NULL OR c.expires_at > NOW())
                            ORDER BY (v.vector <=> '{q_pg_str}'::vector) ASC
                            LIMIT 10;
                            """
                        )
                exact_fps = [r["fingerprint"] for r in exact_rows]
                self.assertEqual(len(exact_fps), 10)

                # 8. Calculate Recall@5 and Recall@10
                r5 = len(set(ann_fps[:5]).intersection(set(exact_fps[:5]))) / 5.0
                r10 = len(set(ann_fps[:10]).intersection(set(exact_fps[:10]))) / 10.0
                recalls_at_5.append(r5)
                recalls_at_10.append(r10)

            # 9. Verify aggregate Recall metrics across all 10 queries
            avg_recall_5 = sum(recalls_at_5) / len(recalls_at_5)
            min_recall_5 = min(recalls_at_5)
            avg_recall_10 = sum(recalls_at_10) / len(recalls_at_10)
            min_recall_10 = min(recalls_at_10)

            # Assert high retrieval recall
            self.assertGreaterEqual(avg_recall_5, 0.95, f"Average Recall@5 was {avg_recall_5:.4f}")
            self.assertGreaterEqual(min_recall_5, 0.80, f"Minimum Recall@5 was {min_recall_5:.4f}")
            self.assertGreaterEqual(avg_recall_10, 0.95, f"Average Recall@10 was {avg_recall_10:.4f}")
            self.assertGreaterEqual(min_recall_10, 0.80, f"Minimum Recall@10 was {min_recall_10:.4f}")
            self.assertEqual(len(recalls_at_5), 10)
        finally:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", ns)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", ns)

    async def test_hnsw_multi_namespace_filtering_and_iterative_scan(self):
        """
        Tests HNSW index performance under multi-namespace and multi-model filtering
        with varying selectivity proportions:
        - Large proportion namespace (~77%, 4,000 vectors with nomic-embed-text)
        - Second embedding model in large namespace (~12.5%, 650 vectors with text-embedding-3-small)
        - Moderate proportion namespace (~9.6%, 500 vectors with nomic-embed-text)
        - Highly selective namespace (~1.0%, 50 vectors with nomic-embed-text)
        Evaluates top-K returned, Recall@5/10 against exact baseline, planner selection,
        latency, and verifies transaction-scoped hnsw.iterative_scan = strict_order.
        """
        ns_large = "test_multi_ns_large"
        ns_mod = "test_multi_ns_mod"
        ns_sel = "test_multi_ns_sel"
        all_namespaces = [ns_large, ns_mod, ns_sel]

        rng = random.Random(99)
        total_vectors = 5200
        vectors = [self._generate_normalized_vector(rng, 768) for _ in range(total_vectors)]
        q_vec = self._generate_normalized_vector(rng, 768)
        q_str = self._format_pgvector(q_vec)

        # Place 10 nearby candidates for q_vec in each partition
        for k in range(10):
            vectors[k] = self._perturb_vector(q_vec, rng, epsilon=0.04 + k * 0.015)
        for k in range(10):
            vectors[4000 + k] = self._perturb_vector(q_vec, rng, epsilon=0.04 + k * 0.015)
        for k in range(10):
            vectors[4650 + k] = self._perturb_vector(q_vec, rng, epsilon=0.04 + k * 0.015)
        for k in range(10):
            vectors[5150 + k] = self._perturb_vector(q_vec, rng, epsilon=0.04 + k * 0.015)

        cache_records = []
        vec_records = []
        idx = 0

        # 4,000 large nomic (~77%)
        for i in range(4000):
            fp = f"fp_large_{i}"
            cache_records.append((ns_large, fp, f"resp_large_{i}", "llama-3", "groq"))
            vec_records.append((ns_large, fp, f"query large {i}", "nomic-embed-text", self._format_pgvector(vectors[idx])))
            idx += 1

        # 650 large second_model (~12.5%)
        for i in range(650):
            fp = f"fp_large_m2_{i}"
            cache_records.append((ns_large, fp, f"resp_large_m2_{i}", "llama-3", "groq"))
            vec_records.append((ns_large, fp, f"query large m2 {i}", "text-embedding-3-small", self._format_pgvector(vectors[idx])))
            idx += 1

        # 500 mod nomic (~9.6%)
        for i in range(500):
            fp = f"fp_mod_{i}"
            cache_records.append((ns_mod, fp, f"resp_mod_{i}", "llama-3", "groq"))
            vec_records.append((ns_mod, fp, f"query mod {i}", "nomic-embed-text", self._format_pgvector(vectors[idx])))
            idx += 1

        # 50 sel nomic (~1.0%)
        for i in range(50):
            fp = f"fp_sel_{i}"
            cache_records.append((ns_sel, fp, f"resp_sel_{i}", "llama-3", "groq"))
            vec_records.append((ns_sel, fp, f"query sel {i}", "nomic-embed-text", self._format_pgvector(vectors[idx])))
            idx += 1

        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'vector';")
            chunk_size = 500
            for i in range(0, len(cache_records), chunk_size):
                await conn.executemany(
                    "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    cache_records[i : i + chunk_size],
                )
                await conn.executemany(
                    "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT DO NOTHING",
                    vec_records[i : i + chunk_size],
                )
            await conn.execute("ANALYZE vector_records;")
            await conn.execute("ANALYZE cache_entries;")

        try:
            partitions = [
                ("large_nomic", ns_large, "nomic-embed-text", 4000),
                ("large_model2", ns_large, "text-embedding-3-small", 650),
                ("moderate_nomic", ns_mod, "nomic-embed-text", 500),
                ("selective_nomic", ns_sel, "nomic-embed-text", 50),
            ]

            for label, ns, model, expected_total in partitions:
                # 1. Test with default settings
                plan_info = await self.store.explain_search(q_vec, top_k=10, namespace=ns, model=model, analyze=True)
                t0 = time.perf_counter()
                results = await self.store.search(q_vec, top_k=10, namespace=ns, model=model)
                lat_default_ms = (time.perf_counter() - t0) * 1000.0

                self.assertEqual(len(results), min(10, expected_total))

                # Exact baseline
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL enable_indexscan = off;")
                        await conn.execute("SET LOCAL enable_bitmapscan = off;")
                        exact_rows = await conn.fetch(
                            f"""
                            SELECT v.fingerprint
                            FROM vector_records v
                            JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                            WHERE v.cache_namespace = '{ns}' AND v.model = '{model}'
                              AND (c.expires_at IS NULL OR c.expires_at > NOW())
                            ORDER BY (v.vector <=> '{q_str}'::vector) ASC
                            LIMIT 10;
                            """
                        )
                exact_fps = [r["fingerprint"] for r in exact_rows]
                ann_fps = [r.fingerprint for r in results]

                k5 = min(5, len(results), len(exact_rows))
                k10 = min(10, len(results), len(exact_rows))
                r5 = len(set(ann_fps[:k5]).intersection(set(exact_fps[:k5]))) / float(k5)
                r10 = len(set(ann_fps[:k10]).intersection(set(exact_fps[:k10]))) / float(k10)

                self.assertGreaterEqual(r5, 0.90, f"{label} Recall@5 below 0.90: {r5}")
                self.assertGreaterEqual(r10, 0.90, f"{label} Recall@10 below 0.90: {r10}")

                # Planner selection observation:
                # Large namespace with nomic-embed-text selects HNSW;
                # Highly selective namespace selects B-tree index scan
                if label == "large_nomic":
                    self.assertTrue(plan_info["vector_index_used"], "Large namespace must select HNSW")
                    self.assertEqual(plan_info["retrieval_method"], "HNSW approximate nearest-neighbour search")
                    iterative_hnsw_status = "Applicable (HNSW index selected)"
                elif label == "selective_nomic":
                    self.assertFalse(plan_info["vector_index_used"], "Selective namespace should not use HNSW")
                    self.assertIn("Exact filtered vector search", plan_info["retrieval_method"])
                    self.assertTrue(
                        any("idx_vector_records_" in l for l in plan_info["execution_plan_summary"]),
                        "Selective namespace should use a B-tree index on vector_records",
                    )
                    iterative_hnsw_status = "Not applicable (B-tree index selected)"
                else:
                    iterative_hnsw_status = (
                        "Applicable (HNSW index selected)"
                        if plan_info["vector_index_used"]
                        else "Not applicable (B-tree index selected)"
                    )

                if not plan_info["vector_index_used"]:
                    self.assertEqual(iterative_hnsw_status, "Not applicable (B-tree index selected)")

                # 2. Test with SET LOCAL hnsw.iterative_scan = 'strict_order'
                async with self.pool.acquire() as conn:
                    async with conn.transaction():
                        await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order';")
                        in_tx_setting = await conn.fetchval("SHOW hnsw.iterative_scan;")
                        self.assertEqual(in_tx_setting, "strict_order")

                        it_rows = await conn.fetch(
                            f"""
                            SELECT v.fingerprint
                            FROM vector_records v
                            JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                            WHERE v.cache_namespace = '{ns}' AND v.model = '{model}'
                              AND (c.expires_at IS NULL OR c.expires_at > NOW())
                            ORDER BY (v.vector <=> '{q_str}'::vector) ASC
                            LIMIT 10;
                            """
                        )
                        self.assertEqual(len(it_rows), min(10, expected_total))

                    # Verify that SET LOCAL did not leak outside transaction
                    out_tx_setting = await conn.fetchval("SHOW hnsw.iterative_scan;")
                    self.assertEqual(out_tx_setting, "off", "SET LOCAL hnsw.iterative_scan leaked outside transaction")

        finally:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace = ANY($1)", all_namespaces)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = ANY($1)", all_namespaces)

    async def test_controlled_iterative_hnsw_scan_comparison(self):
        """
        Controlled verification of HNSW iterative scan (hnsw.iterative_scan = strict_order vs off)
        under selective filtering and varying hnsw.ef_search (10, 20, 40):
        1. Seeds controlled workload of 5,000 vectors (3,800 target, 1,200 distractor/bg).
        2. Confirms HNSW index scan (idx_vector_records_embedding_hnsw) is selected naturally via EXPLAIN (ANALYZE, BUFFERS).
        3. Establishes exact baseline with indexscan/bitmapscan disabled (asserting HNSW is not used).
        4. Compares candidate count, exact NN overlap, Recall@5, and Recall@10 across Mode A (off) and Mode B (strict_order).
        5. Reports whether iterative scan provided a measurable recall improvement for this workload.
        6. Runs >= 30 interleaved measured iterations (with warmup) to report sample count, mean, p50, and p95 latency.
        7. Produces a structured result dictionary containing all required configuration and performance fields.
        """
        ns_target = "test_iter_target_ns"
        ns_distractor = "test_iter_distractor_ns"
        model = "nomic-embed-text"
        total_target = 3800
        total_distractor = 1200
        total_vectors = total_target + total_distractor
        selectivity = total_target / total_vectors  # 0.76

        rng = random.Random(42)
        dim = 768

        target_vectors = [self._generate_normalized_vector(rng, dim) for _ in range(total_target)]
        distractor_vectors = [self._generate_normalized_vector(rng, dim) for _ in range(total_distractor)]

        q_vec = self._generate_normalized_vector(rng, dim)
        q_str = self._format_pgvector(q_vec)

        # Distractor: 50 vectors very close to q_vec (epsilon = 0.02..0.05) to exercise post-filtering
        for k in range(50):
            distractor_vectors[k] = self._perturb_vector(q_vec, rng, epsilon=0.02 + k * 0.0006)

        # Target: 10 vectors at moderate distance (epsilon = 0.15..0.25)
        for k in range(10):
            target_vectors[k] = self._perturb_vector(q_vec, rng, epsilon=0.15 + k * 0.01)

        async with self.pool.acquire() as conn:
            await conn.execute("LOAD 'vector';")
            pg_version = await conn.fetchval("SHOW server_version;")
            pgvector_version = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector';")

            await conn.execute("DELETE FROM vector_records WHERE cache_namespace IN ($1, $2)", ns_target, ns_distractor)
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)", ns_target, ns_distractor)

            cache_records = []
            vec_records = []
            for i in range(total_target):
                fp = f"fp_iter_t_{i}"
                cache_records.append((ns_target, fp, f"resp_iter_t_{i}", "llama-3", "groq"))
                vec_records.append((ns_target, fp, f"target q {i}", model, self._format_pgvector(target_vectors[i])))

            for i in range(total_distractor):
                fp = f"fp_iter_d_{i}"
                cache_records.append((ns_distractor, fp, f"resp_iter_d_{i}", "llama-3", "groq"))
                vec_records.append((ns_distractor, fp, f"distractor q {i}", model, self._format_pgvector(distractor_vectors[i])))

            chunk_size = 500
            for i in range(0, len(cache_records), chunk_size):
                await conn.executemany(
                    "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                    cache_records[i : i + chunk_size],
                )
                await conn.executemany(
                    "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT DO NOTHING",
                    vec_records[i : i + chunk_size],
                )

            await conn.execute("ANALYZE vector_records;")
            await conn.execute("ANALYZE cache_entries;")

        try:
            query_sql = f"""
                SELECT v.fingerprint, (v.vector <=> '{q_str}'::vector) as dist
                FROM vector_records v
                JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                WHERE v.cache_namespace = '{ns_target}' AND v.model = '{model}'
                  AND (c.expires_at IS NULL OR c.expires_at > NOW())
                ORDER BY (v.vector <=> '{q_str}'::vector) ASC
                LIMIT 10;
            """

            # 1. Verify HNSW index naturally selected by query planner with EXPLAIN (ANALYZE, BUFFERS)
            async with self.pool.acquire() as conn:
                plan_rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS) {query_sql}")
            plan_summary = [r[0] for r in plan_rows]
            plan_str = " ".join(plan_summary)
            hnsw_selected = "idx_vector_records_embedding_hnsw" in plan_str
            self.assertTrue(hnsw_selected, f"HNSW index was not selected by planner: {plan_summary[:10]}")

            # 2. Exact baseline
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    await conn.execute("SET LOCAL enable_indexscan = off;")
                    await conn.execute("SET LOCAL enable_bitmapscan = off;")
                    exact_plan_rows = await conn.fetch(f"EXPLAIN {query_sql}")
                    exact_plan_str = " ".join(r[0] for r in exact_plan_rows)
                    self.assertNotIn("idx_vector_records_embedding_hnsw", exact_plan_str, "Exact baseline should not use HNSW")
                    exact_rows = await conn.fetch(query_sql)
            exact_fps = [r["fingerprint"] for r in exact_rows]
            self.assertEqual(len(exact_fps), 10, f"Expected 10 exact matches, got {len(exact_fps)}")

            # 3. Compare Default (off) vs Iterative (strict_order) across ef_search in [10, 20, 40]
            structured_results: list[dict] = []

            for ef in [10, 20, 40]:
                quality_by_mode = {}

                for mode in ["off", "strict_order"]:
                    # Verification of HNSW usage in transaction and result quality
                    async with self.pool.acquire() as conn:
                        async with conn.transaction():
                            await conn.execute(f"SET LOCAL hnsw.ef_search = {ef};")
                            await conn.execute(f"SET LOCAL hnsw.iterative_scan = '{mode}';")
                            mode_plan = await conn.fetch(f"EXPLAIN {query_sql}")
                            mode_plan_str = " ".join(r[0] for r in mode_plan)
                            hnsw_in_mode = "idx_vector_records_embedding_hnsw" in mode_plan_str
                            self.assertTrue(hnsw_in_mode, f"Mode {mode} (ef={ef}) did not use HNSW index")

                            res_rows = await conn.fetch(query_sql)
                            ret_fps = [r["fingerprint"] for r in res_rows]
                            overlap = len(set(ret_fps).intersection(set(exact_fps)))
                            r5 = len(set(ret_fps[:5]).intersection(set(exact_fps[:5]))) / 5.0
                            r10 = len(set(ret_fps[:10]).intersection(set(exact_fps[:10]))) / 10.0
                            quality_by_mode[mode] = {
                                "hnsw_used": hnsw_in_mode,
                                "returned_count": len(ret_fps),
                                "overlap": overlap,
                                "recall_at_5": r5,
                                "recall_at_10": r10,
                            }

                # Evaluate whether iterative scan provided a recall improvement
                default_q = quality_by_mode["off"]
                strict_q = quality_by_mode["strict_order"]
                if default_q["recall_at_10"] >= 1.0 and default_q["returned_count"] == 10:
                    recall_improvement_note = (
                        f"Iterative scanning did not provide a measurable recall improvement for ef_search={ef} "
                        f"because default retrieval already achieved full recall (1.0) and returned all 10 candidates."
                    )
                else:
                    recall_improvement_note = (
                        f"Recall change at ef_search={ef}: default R@10={default_q['recall_at_10']:.2f}, "
                        f"iterative R@10={strict_q['recall_at_10']:.2f}."
                    )

                # 4. Latency Measurements: Warmup runs followed by >= 30 interleaved iterations
                async with self.pool.acquire() as conn:
                    # Warmup: 5 runs for each mode
                    for _ in range(5):
                        async with conn.transaction():
                            await conn.execute(f"SET LOCAL hnsw.ef_search = {ef};")
                            await conn.execute("SET LOCAL hnsw.iterative_scan = 'off';")
                            await conn.fetch(query_sql)
                        async with conn.transaction():
                            await conn.execute(f"SET LOCAL hnsw.ef_search = {ef};")
                            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order';")
                            await conn.fetch(query_sql)

                    # 30 measured interleaved iterations (alternating Mode A and Mode B)
                    latencies_default_ms: list[float] = []
                    latencies_strict_ms: list[float] = []

                    for _ in range(30):
                        # Mode A: off
                        async with conn.transaction():
                            await conn.execute(f"SET LOCAL hnsw.ef_search = {ef};")
                            await conn.execute("SET LOCAL hnsw.iterative_scan = 'off';")
                            t0 = time.perf_counter()
                            await conn.fetch(query_sql)
                            latencies_default_ms.append((time.perf_counter() - t0) * 1000.0)

                        # Mode B: strict_order
                        async with conn.transaction():
                            await conn.execute(f"SET LOCAL hnsw.ef_search = {ef};")
                            await conn.execute("SET LOCAL hnsw.iterative_scan = 'strict_order';")
                            t0 = time.perf_counter()
                            await conn.fetch(query_sql)
                            latencies_strict_ms.append((time.perf_counter() - t0) * 1000.0)

                mean_def, p50_def, p95_def = self._compute_percentiles(latencies_default_ms)
                mean_str, p50_str, p95_str = self._compute_percentiles(latencies_strict_ms)

                # 5. Export Reproducible Results dictionary for each mode
                record_default = {
                    "postgres_version": pg_version,
                    "pgvector_version": pgvector_version,
                    "dataset_size": total_vectors,
                    "namespace_selectivity": round(selectivity, 4),
                    "embedding_model": model,
                    "hnsw.ef_search": ef,
                    "hnsw.iterative_scan": "off",
                    "execution_plan_confirmed": default_q["hnsw_used"],
                    "returned_candidate_count": default_q["returned_count"],
                    "recall_at_5": default_q["recall_at_5"],
                    "recall_at_10": default_q["recall_at_10"],
                    "mean_latency_ms": mean_def,
                    "p50_latency_ms": p50_def,
                    "p95_latency_ms": p95_def,
                    "sample_count": len(latencies_default_ms),
                    "recall_improvement_note": recall_improvement_note,
                }
                record_strict = {
                    "postgres_version": pg_version,
                    "pgvector_version": pgvector_version,
                    "dataset_size": total_vectors,
                    "namespace_selectivity": round(selectivity, 4),
                    "embedding_model": model,
                    "hnsw.ef_search": ef,
                    "hnsw.iterative_scan": "strict_order",
                    "execution_plan_confirmed": strict_q["hnsw_used"],
                    "returned_candidate_count": strict_q["returned_count"],
                    "recall_at_5": strict_q["recall_at_5"],
                    "recall_at_10": strict_q["recall_at_10"],
                    "mean_latency_ms": mean_str,
                    "p50_latency_ms": p50_str,
                    "p95_latency_ms": p95_str,
                    "sample_count": len(latencies_strict_ms),
                    "recall_improvement_note": recall_improvement_note,
                }

                structured_results.extend([record_default, record_strict])

            # Assert structured results properties
            self.assertEqual(len(structured_results), 6)  # 3 ef_search * 2 modes
            for rec in structured_results:
                self.assertTrue(rec["execution_plan_confirmed"])
                self.assertEqual(rec["sample_count"], 30)
                self.assertEqual(rec["returned_candidate_count"], 10)
                self.assertGreaterEqual(rec["recall_at_5"], 0.90)
                self.assertGreaterEqual(rec["recall_at_10"], 0.90)

            # Confirm transaction isolation: hnsw.iterative_scan reverted outside transactions
            async with self.pool.acquire() as conn:
                outer_guc = await conn.fetchval("SHOW hnsw.iterative_scan;")
                self.assertEqual(outer_guc, "off", "SET LOCAL leaked outside transaction boundary")

        finally:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace IN ($1, $2)", ns_target, ns_distractor)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)", ns_target, ns_distractor)


class TestPgvectorAvailabilityAndDisabledBehavior(unittest.IsolatedAsyncioTestCase):
    """
    Tests behavior when pgvector is unavailable and when semantic caching is disabled.
    """

    async def test_clear_failure_when_pgvector_unavailable(self):
        """
        When semantic storage is explicitly enabled against a PostgreSQL instance
        without the vector extension, it fails with a clear VectorStoreConfigurationError.
        """
        # Port 5432 is a standard PostgreSQL cluster without pgvector installed
        non_pgvector_url = "postgresql://postgres@localhost:5432/intellicache_test"
        try:
            conn = await asyncpg.connect(non_pgvector_url)
        except Exception as e:
            self.skipTest(f"Non-pgvector PostgreSQL instance unavailable: {e}")

        try:
            with self.assertRaises(VectorStoreConfigurationError) as ctx:
                await run_vector_migrations(conn, dimensions=768)
            self.assertIn("pgvector extension 'vector' is not available", str(ctx.exception))
        finally:
            await conn.close()

    async def test_exact_only_deployment_starts_without_pgvector(self):
        """
        When SEMANTIC_CACHE_ENABLED=false, exact-cache service starts and works
        normally without requiring pgvector or running vector migrations.
        """
        # Test against non-pgvector cluster on 5432
        non_pgvector_url = "postgresql://postgres@localhost:5432/intellicache_test"
        try:
            pool = await asyncpg.create_pool(non_pgvector_url, min_size=1, max_size=2)
        except Exception as e:
            self.skipTest(f"Non-pgvector PostgreSQL instance unavailable: {e}")

        try:
            # Exact cache migration runs fine without pgvector
            await run_migrations(pool)

            # Confirm vector_records table was NOT created
            async with pool.acquire() as conn:
                table_exists = await conn.fetchval(
                    "SELECT to_regclass('vector_records') IS NOT NULL"
                )
            self.assertFalse(table_exists)
        finally:
            await pool.close()
