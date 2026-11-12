"""
Unit tests for InMemoryVectorStore and FastEmbedProvider.
Validates pure in-memory vector storage, nearest-neighbour retrieval,
exact-cache integration, and semantic caching execution.
"""

import asyncio
from datetime import datetime, timezone
import math
import unittest
from unittest.mock import MagicMock, patch

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingResponseError, EmbeddingTimeoutError
from app.embeddings.fastembed_adapter import FastEmbedProvider
from app.embeddings.interfaces import EmbeddingProvider
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.errors import VectorDimensionMismatchError, VectorInvalidError
from app.vector_store.in_memory_store import InMemoryVectorStore


class MockEmbeddingProvider(EmbeddingProvider):
    """Deterministic mock embedding provider for tests."""

    def __init__(self, mapping=None, default_vector=None, model="test-embed-model", dimensions=4):
        self.mapping = mapping or {}
        self.default_vector = default_vector or [1.0, 0.0, 0.0, 0.0]
        self.model = model
        self.dimensions = dimensions

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        if not text or not text.strip():
            raise EmbeddingResponseError("Input text cannot be empty.")
        vec = self.mapping.get(text.strip(), self.default_vector)
        return EmbeddingResult(vector=vec, model=self.model, dimensions=self.dimensions)

    async def close(self) -> None:
        pass


class TestInMemoryVectorStore(unittest.IsolatedAsyncioTestCase):
    """Tests for InMemoryVectorStore contract compliance."""

    async def asyncSetUp(self):
        self.dimensions = 4
        self.store = InMemoryVectorStore(
            dimensions=self.dimensions,
            default_namespace="default",
            default_model="test-model",
        )

    async def asyncTearDown(self):
        await self.store.close()

    async def test_upsert_and_search_cosine_ordering(self):
        """Records are retrieved and sorted strictly by cosine distance ascending."""
        rec1 = VectorRecord(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="How to sort a list in Python?",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
            metadata={"cached_response": "Use list.sort()"},
        )
        rec2 = VectorRecord(
            cache_namespace="default",
            fingerprint="fp2",
            query_text="How to reverse a list in Python?",
            vector=[0.0, 1.0, 0.0, 0.0],
            model="test-model",
            metadata={"cached_response": "Use list.reverse()"},
        )
        await self.store.upsert(rec1)
        await self.store.upsert(rec2)

        # Query vector close to rec1: [0.9, 0.1, 0.0, 0.0]
        results = await self.store.search(
            query_vector=[0.9, 0.1, 0.0, 0.0],
            top_k=5,
            namespace="default",
            model="test-model",
        )
        self.assertEqual(len(results), 2)
        # rec1 is closer than rec2
        self.assertEqual(results[0].fingerprint, "fp1")
        self.assertEqual(results[0].cached_response, "Use list.sort()")
        self.assertGreater(results[0].similarity, 0.90)
        self.assertEqual(results[1].fingerprint, "fp2")

    async def test_namespace_and_model_isolation(self):
        """Searches enforce strict namespace and model boundaries."""
        rec = VectorRecord(
            cache_namespace="team_alpha",
            fingerprint="fp_alpha",
            query_text="Query in Alpha",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
            metadata={"cached_response": "Alpha answer"},
        )
        await self.store.upsert(rec)

        # Query in team_beta returns nothing
        res_beta = await self.store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="team_beta",
            model="test-model",
        )
        self.assertEqual(res_beta, [])

        # Query with different model returns nothing
        res_model = await self.store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="team_alpha",
            model="different-model",
        )
        self.assertEqual(res_model, [])

        # Query in team_alpha returns match
        res_alpha = await self.store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="team_alpha",
            model="test-model",
        )
        self.assertEqual(len(res_alpha), 1)
        self.assertEqual(res_alpha[0].fingerprint, "fp_alpha")

    async def test_exact_cache_integration_and_eviction(self):
        """When linked with ExactCache, missing/expired entries are filtered out."""
        exact_cache = ExactCache(max_entries=10, ttl_seconds=3600)
        store = InMemoryVectorStore(
            dimensions=4,
            default_namespace="default",
            default_model="test-model",
            exact_cache=exact_cache,
        )

        entry = CacheEntry(
            response="Active cached response",
            model="groq-model",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=120.0,
            provider="groq",
        )
        await exact_cache.set("fp_active", entry, namespace="default")

        rec_active = VectorRecord(
            cache_namespace="default",
            fingerprint="fp_active",
            query_text="Active query",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        )
        rec_orphaned = VectorRecord(
            cache_namespace="default",
            fingerprint="fp_orphaned",
            query_text="Orphaned query without exact cache entry",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        )
        await store.upsert(rec_active)
        await store.upsert(rec_orphaned)

        results = await store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="default",
            model="test-model",
        )
        # Orphaned record must be excluded because exact cache has no entry
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fingerprint, "fp_active")
        self.assertEqual(results[0].cached_response, "Active cached response")
        self.assertEqual(results[0].total_tokens, 30)
        self.assertEqual(results[0].original_latency_ms, 120.0)

        await store.close()
        await exact_cache.close()

    async def test_dimension_and_value_validation(self):
        """Invalid vector dimensions or numeric values raise appropriate errors."""
        with self.assertRaises(VectorDimensionMismatchError):
            rec = VectorRecord(
                cache_namespace="default",
                fingerprint="fp_wrong_dim",
                query_text="Test",
                vector=[1.0, 2.0],  # expected 4
                model="test-model",
            )
            await self.store.upsert(rec)

        with self.assertRaises(VectorDimensionMismatchError):
            await self.store.search(query_vector=[1.0, 2.0])

        with self.assertRaises(VectorInvalidError):
            await self.store.search(query_vector=[])

    async def test_delete_and_clear(self):
        """Records can be deleted individually by fingerprint or cleared per namespace."""
        rec1 = VectorRecord(
            cache_namespace="ns1",
            fingerprint="fp1",
            query_text="Q1",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        )
        rec2 = VectorRecord(
            cache_namespace="ns1",
            fingerprint="fp2",
            query_text="Q2",
            vector=[0.0, 1.0, 0.0, 0.0],
            model="test-model",
        )
        await self.store.upsert(rec1)
        await self.store.upsert(rec2)

        deleted = await self.store.delete("fp1", namespace="ns1")
        self.assertTrue(deleted)
        deleted_again = await self.store.delete("fp1", namespace="ns1")
        self.assertFalse(deleted_again)

        cleared_count = await self.store.clear(namespace="ns1")
        self.assertEqual(cleared_count, 1)

    async def test_search_peek_does_not_mutate_exact_cache(self):
        """Searching candidates uses peek() and must not alter hit metrics or LRU access times."""
        exact_cache = ExactCache(max_entries=10, ttl_seconds=3600)
        store = InMemoryVectorStore(
            dimensions=4,
            default_namespace="default",
            default_model="test-model",
            exact_cache=exact_cache,
        )

        entry = CacheEntry(
            response="Candidate response",
            model="test-model",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=100.0,
            provider="groq",
        )
        await exact_cache.set("fp_candidate", entry, namespace="default")
        stored_entry = await exact_cache.peek("fp_candidate", namespace="default")
        self.assertIsNotNone(stored_entry)
        initial_hit_count = stored_entry.hit_count
        initial_last_accessed = stored_entry.last_accessed

        rec = VectorRecord(
            cache_namespace="default",
            fingerprint="fp_candidate",
            query_text="Candidate question",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        )
        await store.upsert(rec)

        # Execute search in vector store
        results = await store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="default",
            model="test-model",
        )
        self.assertEqual(len(results), 1)

        # Inspect entry in exact cache again via peek
        inspected_entry = await exact_cache.peek("fp_candidate", namespace="default")
        self.assertIsNotNone(inspected_entry)
        self.assertEqual(inspected_entry.hit_count, initial_hit_count)
        self.assertEqual(inspected_entry.last_accessed, initial_last_accessed)

        # Exact cache stats must show 0 hits
        stats = await exact_cache.stats()
        self.assertEqual(stats.hits, 0)

        await store.close()
        await exact_cache.close()

    async def test_bounded_capacity_eviction(self):
        """When max_records is reached, older records are evicted deterministically."""
        store = InMemoryVectorStore(
            dimensions=4,
            default_namespace="default",
            default_model="test-model",
            max_records=3,
        )

        for i in range(5):
            rec = VectorRecord(
                cache_namespace="default",
                fingerprint=f"fp_{i}",
                query_text=f"Query {i}",
                vector=[float(i == 0), float(i == 1), float(i == 2), float(i >= 3)],
                model="test-model",
            )
            await store.upsert(rec)

        count = await store.get_record_count("default")
        self.assertEqual(count, 3)

        # Earliest records (fp_0, fp_1) should have been evicted
        results = await store.search(
            query_vector=[1.0, 0.0, 0.0, 0.0],
            namespace="default",
            model="test-model",
            top_k=10,
        )
        returned_fps = {r.fingerprint for r in results}
        self.assertNotIn("fp_0", returned_fps)
        self.assertNotIn("fp_1", returned_fps)
        self.assertIn("fp_2", returned_fps)
        self.assertIn("fp_3", returned_fps)
        self.assertIn("fp_4", returned_fps)

        await store.close()

    async def test_orphaned_pruning_on_search(self):
        """Expired or deleted exact cache records are actively pruned during search."""
        exact_cache = ExactCache(max_entries=10, ttl_seconds=3600)
        store = InMemoryVectorStore(
            dimensions=4,
            default_namespace="default",
            default_model="test-model",
            exact_cache=exact_cache,
        )

        entry1 = CacheEntry(response="R1", model="test-model", usage=UsageMetrics(), latency_ms=10.0)
        entry2 = CacheEntry(response="R2", model="test-model", usage=UsageMetrics(), latency_ms=10.0)
        await exact_cache.set("fp_keep", entry1, namespace="default")
        await exact_cache.set("fp_expire", entry2, namespace="default")

        await store.upsert(VectorRecord(
            cache_namespace="default",
            fingerprint="fp_keep",
            query_text="Keep",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        ))
        await store.upsert(VectorRecord(
            cache_namespace="default",
            fingerprint="fp_expire",
            query_text="Expire",
            vector=[0.0, 1.0, 0.0, 0.0],
            model="test-model",
        ))
        self.assertEqual(await store.get_record_count("default"), 2)

        # Explicitly remove fp_expire from exact cache
        exact_cache._entries.pop(("default", "fp_expire"), None)

        # Run search
        results = await store.search(
            query_vector=[0.0, 1.0, 0.0, 0.0],
            namespace="default",
            model="test-model",
        )
        # fp_expire was pruned, so only fp_keep remains
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].fingerprint, "fp_keep")

        # Verify that fp_expire was pruned from store
        self.assertEqual(await store.get_record_count("default"), 1)

        await store.close()
        await exact_cache.close()

    async def test_concurrent_upsert_during_stale_pruning_preserved(self):
        """A concurrent upsert for the same fingerprint during search pruning is preserved."""
        exact_cache = ExactCache(max_entries=10, ttl_seconds=3600)
        store = InMemoryVectorStore(
            dimensions=4,
            default_namespace="default",
            default_model="test-model",
            exact_cache=exact_cache,
        )

        # 1. Insert initial record (stale candidate)
        rec_old = VectorRecord(
            cache_namespace="default",
            fingerprint="fp_concurrent",
            query_text="Initial stale query",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="test-model",
        )
        await store.upsert(rec_old)
        self.assertEqual(await store.get_record_count("default"), 1)

        # Ensure exact_cache does not have this entry so peek() returns None
        self.assertIsNone(await exact_cache.peek("fp_concurrent", namespace="default"))

        # Synchronization events to coordinate deterministic concurrency
        search_peeking = asyncio.Event()
        upsert_completed = asyncio.Event()

        original_peek = exact_cache.peek

        async def hooked_peek(fingerprint, namespace=None):
            result = await original_peek(fingerprint, namespace=namespace)
            if fingerprint == "fp_concurrent":
                # Signal that search has identified the stale entry and is about to prune
                search_peeking.set()
                # Await the concurrent upsert to complete before search acquires the lock to prune
                await upsert_completed.wait()
            return result

        exact_cache.peek = hooked_peek

        # Define concurrent upsert task
        rec_new = VectorRecord(
            cache_namespace="default",
            fingerprint="fp_concurrent",
            query_text="Replacement query during concurrent flight",
            vector=[0.0, 1.0, 0.0, 0.0],
            model="test-model",
        )

        async def concurrent_upsert_worker():
            await search_peeking.wait()
            await store.upsert(rec_new)
            upsert_completed.set()

        # Run search and concurrent upsert concurrently
        search_task = asyncio.create_task(
            store.search(
                query_vector=[1.0, 0.0, 0.0, 0.0],
                namespace="default",
                model="test-model",
            )
        )
        upsert_task = asyncio.create_task(concurrent_upsert_worker())

        await asyncio.gather(search_task, upsert_task)

        # Verification:
        # 1. The store must still hold the record
        self.assertEqual(await store.get_record_count("default"), 1)
        stored_rec = store._records.get(("default", "fp_concurrent", "test-model"))
        # 2. It must be rec_new, NOT rec_old
        self.assertIs(stored_rec, rec_new)
        self.assertEqual(stored_rec.query_text, "Replacement query during concurrent flight")

        await store.close()
        await exact_cache.close()

    async def test_record_versioning_lifecycle(self):
        """Record version counter increments on upsert and is cleaned up on delete/clear."""
        store = InMemoryVectorStore(dimensions=4)
        rec1 = VectorRecord(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="Q1",
            vector=[1.0, 0.0, 0.0, 0.0],
            model="m1",
        )
        await store.upsert(rec1)
        key1 = ("default", "fp1", "m1")
        self.assertIn(key1, store._record_versions)
        v1 = store._record_versions[key1]

        # Upserting again increments version
        rec1_updated = VectorRecord(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="Q1 updated",
            vector=[0.0, 1.0, 0.0, 0.0],
            model="m1",
        )
        await store.upsert(rec1_updated)
        v2 = store._record_versions[key1]
        self.assertGreater(v2, v1)

        # Deleting cleans up version mapping
        await store.delete("fp1", namespace="default")
        self.assertNotIn(key1, store._record_versions)

        # Clear cleans up versions too
        await store.upsert(rec1)
        self.assertIn(key1, store._record_versions)
        await store.clear(namespace="default")
        self.assertNotIn(key1, store._record_versions)

        await store.close()


class TestFastEmbedProvider(unittest.IsolatedAsyncioTestCase):
    """Unit tests for FastEmbedProvider adapter."""

    async def test_empty_input_rejected(self):
        """Empty or whitespace text raises EmbeddingResponseError."""
        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5")
        with self.assertRaises(EmbeddingResponseError):
            await provider.embed("")
        with self.assertRaises(EmbeddingResponseError):
            await provider.embed("   ")
        await provider.close()

    async def test_fastembed_timeout(self):
        """Operations exceeding timeout raise EmbeddingTimeoutError."""
        provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", timeout_seconds=0.0001)

        # Mock compute to simulate slow inference
        with patch.object(provider, "_get_model") as mock_get:
            mock_model = MagicMock()
            def slow_embed(*args, **kwargs):
                import time
                time.sleep(0.05)
                return [[0.1] * 384]
            mock_model.query_embed.side_effect = slow_embed
            mock_model.passage_embed.side_effect = slow_embed
            mock_model.embed.side_effect = slow_embed
            mock_get.return_value = mock_model

            with self.assertRaises(EmbeddingTimeoutError):
                await provider.embed("Test timeout")

        await provider.close()


class TestSemanticCacheInMemoryIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end integration of SemanticCacheService with InMemoryVectorStore."""

    async def test_semantic_lookup_and_index_lifecycle(self):
        """Index a query result, then lookup with a semantically similar query."""
        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        dimensions = 4
        vector_store = InMemoryVectorStore(
            dimensions=dimensions,
            default_namespace="default",
            default_model="test-model",
            exact_cache=exact_cache,
        )

        # Mock embedding provider: Q1 and Q2 have cosine similarity ~0.99
        v_q1 = [1.0, 0.0, 0.0, 0.0]
        v_q2 = [0.999, 0.044, 0.0, 0.0]  # dot product ~0.999
        provider = MockEmbeddingProvider(
            mapping={
                "Explain memory management in Rust": v_q1,
                "How does Rust manage memory?": v_q2,
            },
            model="test-model",
            dimensions=dimensions,
        )

        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="test-model",
        )
        service = SemanticCacheService(
            embedding_provider=provider,
            vector_store=vector_store,
            policy=policy,
            default_namespace="default",
        )

        req1 = GenerationRequest(
            messages=[ChatMessage(role="user", content="Explain memory management in Rust")],
            model="qwen/qwen3.8-27b",
            provider="groq",
            temperature=0.7,
            namespace="default",
        )
        res1 = GenerationResult(
            text="Rust manages memory through its ownership and borrowing system.",
            model="qwen/qwen3.8-27b",
            usage=UsageMetrics(prompt_tokens=15, completion_tokens=30, total_tokens=45),
            latency_ms=150.0,
            cached=False,
            fingerprint="fp_rust_ownership",
        )

        # 1. Exact cache write
        entry = CacheEntry(
            response=res1.text,
            model=req1.model,
            usage=res1.usage,
            latency_ms=res1.latency_ms,
            provider=req1.provider,
        )
        await exact_cache.set(res1.fingerprint, entry, namespace="default")

        # 2. Vector index
        indexed = await service.index(req1, res1)
        self.assertTrue(indexed)

        # 3. Lookup with different phrasing
        req2 = GenerationRequest(
            messages=[ChatMessage(role="user", content="How does Rust manage memory?")],
            model="qwen/qwen3.8-27b",
            provider="groq",
            temperature=0.7,
            namespace="default",
        )
        hit = await service.lookup(req2)
        self.assertIsNotNone(hit)
        self.assertEqual(hit.cached_response, "Rust manages memory through its ownership and borrowing system.")
        self.assertGreater(hit.similarity, 0.95)
        self.assertEqual(hit.fingerprint, "fp_rust_ownership")
        self.assertEqual(hit.total_tokens, 45)

        await service.close()
        await exact_cache.close()


if __name__ == "__main__":
    unittest.main()
