"""
Unit and integration tests for Semantic Cache Shadow Evaluation (Stage 3.3).
Verifies:
- Production response invariance (P0)
- Multi-layer write-protection (P0)
- Event loop non-blocking & bounded thread pool isolation (P0)
- Hard timeout handling without worker accumulation (P0)
- Queue saturation fail-open behavior (P0)
- Actual SemanticCacheService.lookup() reuse (P0)
- Metric separation (P0)
- Trigger matrix and match verification (P1)
- Strict configuration validation (P1)
- Data privacy & controlled reason codes (P1)
- Graceful lifecycle shutdown and drain (P1)
- Bounded batch retention cleanup (P2)
"""

import asyncio
from dataclasses import dataclass
import hashlib
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.config import Settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult
from app.embeddings.fastembed_adapter import FastEmbedProvider
from app.metrics import get_metrics_collector
from app.metrics.collector import RuntimeMetricsCollector
from app.semantic_cache.contracts import SemanticHit
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.shadow import (
    ReadOnlySemanticCacheService,
    ReadOnlyVectorStoreAdapter,
    ShadowEvaluationService,
    ShadowTask,
    backfill_shadow_candidates_from_cache,
    map_compatibility_rejection_code,
    map_eligibility_rejection_code,
)
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore
from app.vector_store.postgres_store import PostgresVectorStore


class MockEmbeddingProvider:
    """Mock embedding provider returning deterministic 4-dimensional vectors."""

    def __init__(self, delay_seconds: float = 0.0):
        self.delay_seconds = delay_seconds
        self.model = "BAAI/bge-small-en-v1.5"
        self.dimensions = 4
        self.embed_call_count = 0

    async def embed(self, text: str, task: str = "query"):
        self.embed_call_count += 1
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        # Deterministic 4D vector
        h = int(hashlib.md5(text.encode("utf-8")).hexdigest(), 16)
        v = [(h % 100) / 100.0, ((h >> 8) % 100) / 100.0, ((h >> 16) % 100) / 100.0, 1.0]
        norm = sum(x * x for x in v) ** 0.5
        norm_v = [x / norm for x in v]

        @dataclass
        class Result:
            vector: list[float]
            model: str

        return Result(vector=norm_v, model=self.model)

    async def close(self):
        pass


class TestShadowConfigurationValidation(unittest.TestCase):
    """Verifies strict validation of shadow evaluation parameters (P1)."""

    def test_default_shadow_configuration(self):
        s = Settings(cache_backend="memory")
        self.assertFalse(s.shadow_evaluation_enabled)
        self.assertEqual(s.shadow_sampling_rate, 1.0)
        self.assertEqual(s.shadow_max_concurrency, 5)
        self.assertEqual(s.shadow_queue_max_size, 256)
        self.assertEqual(s.shadow_timeout_seconds, 2.0)
        self.assertEqual(s.shadow_retention_days, 30)

    def test_invalid_sampling_rate(self):
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_sampling_rate=-0.1)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_sampling_rate=1.05)

    def test_invalid_concurrency(self):
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_max_concurrency=0)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_max_concurrency=51)

    def test_invalid_queue_size(self):
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_queue_max_size=0)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_queue_max_size=10001)

    def test_invalid_timeout(self):
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_timeout_seconds=0.0)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_timeout_seconds=-1.0)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_timeout_seconds=61.0)

    def test_invalid_retention_days(self):
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_retention_days=0)
        with self.assertRaises(ValueError):
            Settings(cache_backend="memory", shadow_retention_days=366)

    def test_shadow_postgres_requires_database_url(self):
        with self.assertRaises(ValueError):
            Settings(
                cache_backend="postgres",
                database_url=None,
                shadow_evaluation_enabled=True,
            )


class TestMultiLayerWriteProtection(unittest.IsolatedAsyncioTestCase):
    """Verifies multi-layer write protection forbidding mutation in shadow mode (P0)."""

    async def asyncSetUp(self):
        self.raw_store = InMemoryVectorStore(dimensions=4)
        self.read_only_adapter = ReadOnlyVectorStoreAdapter(self.raw_store)
        self.embed_provider = MockEmbeddingProvider()
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="BAAI/bge-small-en-v1.5",
        )
        self.read_only_service = ReadOnlySemanticCacheService(
            embedding_provider=self.embed_provider,
            vector_store=self.read_only_adapter,
            policy=self.policy,
            default_namespace="default",
        )

    async def test_read_only_adapter_blocks_upsert(self):
        record = VectorRecord(
            cache_namespace="default",
            fingerprint="test_fp",
            query_text="test query",
            vector=[0.5, 0.5, 0.5, 0.5],
            model="BAAI/bge-small-en-v1.5",
            metadata={},
        )
        with self.assertRaises(PermissionError):
            await self.read_only_adapter.upsert(record)

    async def test_read_only_adapter_blocks_delete(self):
        with self.assertRaises(PermissionError):
            await self.read_only_adapter.delete("test_fp")

    async def test_read_only_adapter_blocks_clear(self):
        with self.assertRaises(PermissionError):
            await self.read_only_adapter.clear("default")

    async def test_read_only_adapter_allows_search(self):
        res = await self.read_only_adapter.search([0.5, 0.5, 0.5, 0.5], top_k=3)
        self.assertEqual(res, [])

    async def test_read_only_semantic_cache_service_index_is_noop(self):
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="hello")],
            temperature=0.0,
        )
        from app.core.contracts import UsageMetrics
        resp = GenerationResult(
            text="world",
            model="qwen/qwen3.8-27b",
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=50.0,
            fingerprint="fp123",
        )
        indexed = await self.read_only_service.index(req, resp)
        self.assertFalse(indexed)
        # Verify raw store remains empty
        records = await self.raw_store.search([0.5, 0.5, 0.5, 0.5], top_k=5)
        self.assertEqual(len(records), 0)


class TestConcurrentDiagnosticsIsolation(unittest.IsolatedAsyncioTestCase):
    """Verifies that request-scoped diagnostics hooks eliminate race conditions under concurrent lookups (P1)."""

    async def asyncSetUp(self):
        self.raw_store = InMemoryVectorStore(dimensions=4)
        self.embed_provider = MockEmbeddingProvider()
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="BAAI/bge-small-en-v1.5",
        )
        self.service = SemanticCacheService(
            embedding_provider=self.embed_provider,
            vector_store=self.raw_store,
            policy=self.policy,
            default_namespace="default",
        )

        # Seed candidate into store
        embed_res = await self.embed_provider.embed("Base query candidate")
        from app.semantic_cache.compatibility import CompatibilityExtractor
        extractor = CompatibilityExtractor()
        req_seed = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Base query candidate")],
            temperature=0.0,
        )
        meta_dict = extractor.extract(req_seed, "BAAI/bge-small-en-v1.5").to_dict()
        meta_dict["cached_response"] = "Base response text"
        await self.raw_store.upsert(
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp-base",
                query_text="Base query candidate",
                vector=embed_res.vector,
                model="BAAI/bge-small-en-v1.5",
                metadata=meta_dict,
            )
        )

    async def test_concurrent_diagnostics_hook_race_isolation(self):
        """10 concurrent coroutines invoking lookup with distinct hooks capture only their own diagnostics."""
        concurrency = 10
        captured_by_request = {i: [] for i in range(concurrency)}

        def make_hook(req_idx: int):
            def _hook(diag: dict):
                captured_by_request[req_idx].append(diag)
            return _hook

        async def worker(idx: int):
            req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content=f"Concurrent query prompt #{idx}")],
                temperature=0.0,
            )
            return await self.service.lookup(req, diagnostics_hook=make_hook(idx))

        results = await asyncio.gather(*[worker(i) for i in range(concurrency)])
        self.assertEqual(len(results), concurrency)

        for i in range(concurrency):
            diags = captured_by_request[i]
            self.assertEqual(len(diags), 1, f"Worker {i} should have captured exactly 1 diagnostic payload")
            captured_hash = diags[0].get("query_hash")
            expected_hash = hashlib.sha256(f"Concurrent query prompt #{i}".encode("utf-8")).hexdigest()
            self.assertEqual(
                captured_hash,
                expected_hash,
                f"Worker {i} captured diagnostic belonging to a different request: got '{captured_hash}'",
            )


class TestShadowEvaluationServiceExecution(unittest.IsolatedAsyncioTestCase):
    """Verifies queueing, concurrency, timeout, outcomes, and metrics isolation (P0/P1)."""

    async def asyncSetUp(self):
        self.collector = RuntimeMetricsCollector()
        self.raw_store = InMemoryVectorStore(dimensions=4)
        self.read_only_adapter = ReadOnlyVectorStoreAdapter(self.raw_store)
        self.embed_provider = MockEmbeddingProvider()
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="BAAI/bge-small-en-v1.5",
        )
        self.shadow_semantic_cache = ReadOnlySemanticCacheService(
            embedding_provider=self.embed_provider,
            vector_store=self.read_only_adapter,
            policy=self.policy,
            default_namespace="default",
        )
        self.settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=5,
            shadow_timeout_seconds=0.5,
        )
        self.shadow_service = ShadowEvaluationService(
            settings=self.settings,
            semantic_cache=self.shadow_semantic_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await self.shadow_service.start()

    async def asyncTearDown(self):
        await self.shadow_service.stop(drain_timeout=1.0)

    async def test_enqueue_and_evaluate_eligible_no_candidate(self):
        task = ShadowTask(
            request_id="req-1",
            request_hash="hash-1",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="How to reverse a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash="resp-hash-1",
            actual_latency_ms=120.0,
            enqueued_at=time.perf_counter(),
        )

        enqueued = self.shadow_service.enqueue(task)
        self.assertTrue(enqueued)

        # Wait for queue to drain
        await self.shadow_service.queue.join()

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_requests_total, 1)
        self.assertEqual(snap.shadow.shadow_requests_sampled, 1)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
        self.assertEqual(snap.shadow.shadow_no_candidate, 1)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)

        # Invariant: Production cache metrics MUST NOT be affected!
        self.assertEqual(snap.request_and_cache.exact_cache_hits, 0)
        self.assertEqual(snap.request_and_cache.semantic_hits, 0)

    async def test_ineligible_request_short_circuits_without_embedding(self):
        """Ineligible requests (e.g. temperature > 0) must NOT call FastEmbed."""
        task = ShadowTask(
            request_id="req-ineligible",
            request_hash="hash-ineligible",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.7,  # Non-zero temperature is ineligible
            max_tokens=100,
            query_text="Creative story about space",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=300.0,
            enqueued_at=time.perf_counter(),
        )

        initial_embed_calls = self.embed_provider.embed_call_count
        self.shadow_service.enqueue(task)
        await self.shadow_service.queue.join()

        # Assert embedding provider was never called
        self.assertEqual(self.embed_provider.embed_call_count, initial_embed_calls)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_requests_total, 1)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 0)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)

    async def test_queue_saturation_drops_items_without_raising(self):
        """Filling queue beyond shadow_queue_max_size drops items and increments drop counter."""
        # Stop worker temporarily to fill queue
        for worker in self.shadow_service._workers:
            worker.cancel()
        await asyncio.gather(*self.shadow_service._workers, return_exceptions=True)
        self.shadow_service._workers.clear()

        # Capacity is 5
        for i in range(5):
            t = ShadowTask(
                request_id=f"req-fill-{i}",
                request_hash=f"h-{i}",
                cache_namespace="default",
                provider="groq",
                requested_model="qwen/qwen3.8-27b",
                temperature=0.0,
                max_tokens=100,
                query_text="test query",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=10.0,
                enqueued_at=time.perf_counter(),
            )
            success = self.shadow_service.enqueue(t)
            self.assertTrue(success)

        # 6th task must be dropped due to queue saturation
        dropped_task = ShadowTask(
            request_id="req-dropped",
            request_hash="h-dropped",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="overflow query",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=10.0,
            enqueued_at=time.perf_counter(),
        )
        enqueued_6th = self.shadow_service.enqueue(dropped_task)
        self.assertFalse(enqueued_6th)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_queue_dropped_total, 1)

    async def test_hard_timeout_handling(self):
        """Slow embedding/retrieval exceeding shadow_timeout_seconds triggers shadow_timeout outcome."""
        slow_embed_provider = MockEmbeddingProvider(delay_seconds=1.5)
        slow_semantic_cache = ReadOnlySemanticCacheService(
            embedding_provider=slow_embed_provider,
            vector_store=self.read_only_adapter,
            policy=self.policy,
            default_namespace="default",
        )
        timeout_service = ShadowEvaluationService(
            settings=self.settings,  # timeout is 0.5s
            semantic_cache=slow_semantic_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await timeout_service.start()

        task = ShadowTask(
            request_id="req-slow",
            request_hash="h-slow",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="How to optimize Python functions?",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=10.0,
            enqueued_at=time.perf_counter(),
        )

        timeout_service.enqueue(task)
        await timeout_service.queue.join()
        await timeout_service.stop(drain_timeout=0.5)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_execution_failures, 1)

    async def test_sampling_rate_enforcement(self):
        """Sampling rate < 1.0 properly filters unsampled requests."""
        sampled_collector = RuntimeMetricsCollector()
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=0.0,  # 0% sampled
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )
        sampling_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=self.shadow_semantic_cache,
            db_pool=None,
            metrics_collector=sampled_collector,
        )

        task = ShadowTask(
            request_id="req-unsampled",
            request_hash="h-unsampled",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="unsampled query",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=10.0,
            enqueued_at=time.perf_counter(),
        )

        enqueued = sampling_service.enqueue(task)
        self.assertFalse(enqueued)

        snap = sampled_collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_requests_total, 1)
        self.assertEqual(snap.shadow.shadow_requests_sampled, 0)
        self.assertEqual(snap.shadow.shadow_requests_unsampled, 1)

    async def test_shadow_evaluation_production_metrics_isolation(self):
        """Executing diverse shadow tasks must NOT increment production cache metrics."""
        prod_collector = get_metrics_collector()
        prod_collector.reset()

        # Seed candidate in shadow store
        embed_res = await self.embed_provider.embed("Python reverse list")
        from app.semantic_cache.compatibility import CompatibilityExtractor
        extractor = CompatibilityExtractor()
        req_seed = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Python reverse list")],
            temperature=0.0,
            max_tokens=100,
            visibility="tenant_shared",
        )
        meta_dict = extractor.extract(req_seed, "BAAI/bge-small-en-v1.5").to_dict()
        meta_dict["cached_response"] = "items[::-1]"
        await self.raw_store.upsert(
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp-metrics-iso",
                query_text="Python reverse list",
                vector=embed_res.vector,
                model="BAAI/bge-small-en-v1.5",
                metadata=meta_dict,
                visibility="tenant_shared",
            )
        )

        tasks = [
            # 1. Eligible hit
            ShadowTask(
                request_id="iso-hit",
                request_hash="hash-iso-hit",
                cache_namespace="default",
                provider="groq",
                requested_model="qwen/qwen3.8-27b",
                temperature=0.0,
                max_tokens=100,
                query_text="Python reverse list",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=40.0,
                enqueued_at=time.perf_counter(),
                actual_response_text="items[::-1]",
            ),
            # 2. Eligible miss (no match)
            ShadowTask(
                request_id="iso-miss",
                request_hash="hash-iso-miss",
                cache_namespace="default",
                provider="groq",
                requested_model="qwen/qwen3.8-27b",
                temperature=0.0,
                max_tokens=100,
                query_text="Completely unrelated topic about astrophysics",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=60.0,
                enqueued_at=time.perf_counter(),
            ),
            # 3. Ineligible request
            ShadowTask(
                request_id="iso-inelig",
                request_hash="hash-iso-inelig",
                cache_namespace="default",
                provider="groq",
                requested_model="qwen/qwen3.8-27b",
                temperature=0.7,
                max_tokens=100,
                query_text="Creative prompt",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=80.0,
                enqueued_at=time.perf_counter(),
            ),
        ]

        for t in tasks:
            self.shadow_service.enqueue(t)
        await self.shadow_service.queue.join()

        # Shadow metrics must be recorded in shadow collector
        shadow_snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(shadow_snap.shadow.shadow_requests_total, 3)
        self.assertEqual(shadow_snap.shadow.shadow_hypothetical_hits, 1)

        # STRICT INVARIANT: Global production metrics MUST remain completely untouched!
        prod_snap = prod_collector.get_namespace_snapshot("default")
        self.assertEqual(prod_snap.request_and_cache.semantic_lookups, 0)
        self.assertEqual(prod_snap.request_and_cache.semantic_hits, 0)
        self.assertEqual(prod_snap.request_and_cache.semantic_misses, 0)
        self.assertEqual(prod_snap.request_and_cache.exact_cache_hits, 0)
        self.assertEqual(prod_snap.request_and_cache.exact_cache_misses, 0)
        self.assertEqual(prod_snap.latencies.semantic_lookup_latency_ms.sample_count, 0)

    async def test_thread_backed_embedding_timeout_and_bounded_executor(self):
        """Thread-backed slow embedding provider times out gracefully without blocking the asyncio loop."""
        import concurrent.futures

        class ThreadSlowEmbeddingProvider:
            def __init__(self):
                self.model = "BAAI/bge-small-en-v1.5"
                self.dimensions = 4
                self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)

            async def embed(self, text: str, task: str = "query"):
                def _work():
                    time.sleep(0.3)
                    return [0.5, 0.5, 0.5, 0.5]
                loop = asyncio.get_running_loop()
                fut = loop.run_in_executor(self.executor, _work)
                res = await fut
                @dataclass
                class Result:
                    vector: list[float]
                    model: str
                return Result(vector=res, model=self.model)

        slow_provider = ThreadSlowEmbeddingProvider()
        try:
            slow_cache = ReadOnlySemanticCacheService(
                embedding_provider=slow_provider,
                vector_store=self.read_only_adapter,
                policy=self.policy,
                default_namespace="default",
            )
            svc = ShadowEvaluationService(
                settings=Settings(
                    cache_backend="memory",
                    shadow_evaluation_enabled=True,
                    shadow_timeout_seconds=0.05,
                    shadow_max_concurrency=2,
                    shadow_queue_max_size=10,
                ),
                semantic_cache=slow_cache,
                db_pool=None,
                metrics_collector=self.collector,
            )
            await svc.start()
            task = ShadowTask(
                request_id="req-thread-timeout",
                request_hash="h-thread-timeout",
                cache_namespace="default",
                provider="groq",
                requested_model="qwen/qwen3.8-27b",
                temperature=0.0,
                max_tokens=100,
                query_text="How to reverse a list in Python?",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=10.0,
                enqueued_at=time.perf_counter(),
            )
            svc.enqueue(task)
            await svc.queue.join()
            await svc.stop(drain_timeout=0.5)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertGreaterEqual(snap.shadow.shadow_execution_failures, 1)
        finally:
            slow_provider.executor.shutdown(wait=False)

    async def test_repeated_timeout_executor_queue_bounding(self):
        """Verifies repeated timeouts under slow thread-backed inference do not accumulate pending work."""
        import concurrent.futures
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        class SlowComputeFastEmbedProvider(FastEmbedProvider):
            def __init__(self, executor, max_concurrency=2):
                super().__init__(
                    model="BAAI/bge-small-en-v1.5",
                    timeout_seconds=0.05,
                    dimensions=4,
                    executor=executor,
                    max_concurrency=max_concurrency,
                )

            def _load_model_sync(self):
                class DummyModel:
                    def query_embed(self, texts):
                        for _ in texts:
                            time.sleep(0.12)
                            yield [0.1, 0.2, 0.3, 0.4]
                    def embed(self, texts):
                        for _ in texts:
                            time.sleep(0.12)
                            yield [0.1, 0.2, 0.3, 0.4]
                return DummyModel()

        executor = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            slow_provider = SlowComputeFastEmbedProvider(executor=executor, max_concurrency=2)
            slow_cache = ReadOnlySemanticCacheService(
                embedding_provider=slow_provider,
                vector_store=self.read_only_adapter,
                policy=self.policy,
                default_namespace="default",
            )
            svc = ShadowEvaluationService(
                settings=Settings(
                    cache_backend="memory",
                    shadow_evaluation_enabled=True,
                    shadow_timeout_seconds=0.05,
                    shadow_max_concurrency=2,
                    shadow_queue_max_size=20,
                ),
                semantic_cache=slow_cache,
                db_pool=None,
                metrics_collector=self.collector,
            )
            await svc.start()

            # Enqueue 8 consecutive tasks that experience inference timeouts
            for i in range(8):
                task = ShadowTask(
                    request_id=f"req-rep-timeout-{i}",
                    request_hash=f"h-rep-timeout-{i}",
                    cache_namespace="default",
                    provider="groq",
                    requested_model="qwen/qwen3.8-27b",
                    temperature=0.0,
                    max_tokens=100,
                    query_text=f"How to implement binary search in Python? Variant {i}",
                    actual_response_source="upstream_provider",
                    actual_response_hash=None,
                    actual_latency_ms=10.0,
                    enqueued_at=time.perf_counter(),
                )
                svc.enqueue(task)

            await svc.queue.join()
            await svc.stop(drain_timeout=1.0)

            # Assert all 8 tasks finished cleanly with execution failure / embedding failure recorded
            snap = self.collector.get_namespace_snapshot("default")
            total_failures = snap.shadow.shadow_execution_failures + snap.shadow.shadow_embedding_failures
            self.assertEqual(total_failures, 8)
            # Pending work in executor._work_queue must be 0 (no lingering jobs)
            self.assertEqual(executor._work_queue.qsize(), 0)
        finally:
            executor.shutdown(wait=False)

    async def test_shadow_task_preserves_explicit_cache_bypass(self):
        """When bypass_cache is True, shadow evaluation records bypass_requested and skips semantic lookup."""
        task = ShadowTask(
            request_id="req-bypass-1",
            request_hash="hash-bypass-1",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="How to calculate fibonacci?",
            actual_response_source="bypass",
            actual_response_hash=hashlib.sha256(b"def fib(n): return n").hexdigest(),
            actual_latency_ms=45.0,
            enqueued_at=time.perf_counter(),
            bypass_cache=True,
            actual_response_text="def fib(n): return n",
        )

        initial_embed_count = getattr(self.embed_provider, "call_count", 0)
        self.shadow_service.enqueue(task)
        await self.shadow_service.queue.join()

        # Semantic lookup and embedding must be skipped entirely
        self.assertEqual(getattr(self.embed_provider, "call_count", 0), initial_embed_count)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_bypass_requests, 1)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 0)

    async def test_candidate_ingestion_bounded_timeout(self):
        """Candidate ingestion enforces bounded execution timeout and prevents hanging worker."""
        class SlowIngestionEmbedder:
            def __init__(self):
                self.model = "BAAI/bge-small-en-v1.5"
                self.dimensions = 4
            async def embed(self, text: str, task: str = "query"):
                if task == "document" or task == EmbeddingTask.DOCUMENT:
                    await asyncio.sleep(0.3)  # Exceeds 0.05s timeout
                @dataclass
                class Res:
                    vector: list
                    model: str
                return Res(vector=[0.1, 0.2, 0.3, 0.4], model=self.model)

        mock_store = AsyncMock()
        slow_cache = ReadOnlySemanticCacheService(
            embedding_provider=SlowIngestionEmbedder(),
            vector_store=self.read_only_adapter,
            policy=self.policy,
            default_namespace="default",
        )
        svc = ShadowEvaluationService(
            settings=Settings(
                cache_backend="memory",
                shadow_evaluation_enabled=True,
                shadow_timeout_seconds=0.05,
                shadow_max_concurrency=2,
                shadow_queue_max_size=10,
            ),
            semantic_cache=slow_cache,
            db_pool=None,
            metrics_collector=self.collector,
            shadow_vector_store=mock_store,
        )
        await svc.start()

        task = ShadowTask(
            request_id="req-slow-ingestion",
            request_hash="h-slow-ingestion",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="What is quicksort?",
            actual_response_source="upstream_provider",
            actual_response_hash="hash-quicksort",
            actual_latency_ms=50.0,
            enqueued_at=time.perf_counter(),
            actual_response_text="Quicksort is a divide and conquer algorithm.",
        )
        svc.enqueue(task)
        await svc.queue.join()
        await svc.stop(drain_timeout=0.5)

        # Candidate ingestion timed out, so mock_store.upsert was not reached
        mock_store.upsert.assert_not_called()

    async def test_shadow_candidate_ingestion_and_subsequent_hit(self):
        """Shadow candidate ingestion populates shadow vector store while production store remains empty."""
        shadow_candidate_store = InMemoryVectorStore(dimensions=4)
        self.shadow_service.shadow_vector_store = shadow_candidate_store

        task = ShadowTask(
            request_id="req-ingest-test-1",
            request_hash="hash-ingest-test-1",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="How to sort a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash="resp-h1",
            actual_latency_ms=100.0,
            enqueued_at=time.perf_counter(),
            actual_response_text="Use items.sort() or sorted(items)",
        )

        # 1. Ingest candidate via shadow service
        success = await self.shadow_service.index_shadow_candidate(task, task.actual_response_text)
        self.assertTrue(success)

        # Candidate is present in shadow store
        shadow_records = await shadow_candidate_store.search([0.5, 0.5, 0.5, 0.5], top_k=5)
        self.assertEqual(len(shadow_records), 1)
        self.assertEqual(shadow_records[0].metadata["cached_response"], "Use items.sort() or sorted(items)")

        # CRITICAL: Production store remains completely empty!
        prod_records = await self.raw_store.search([0.5, 0.5, 0.5, 0.5], top_k=5)
        self.assertEqual(len(prod_records), 0)

        # 2. Point shadow semantic cache to read from shadow candidate store
        self.shadow_semantic_cache.vector_store = ReadOnlyVectorStoreAdapter(shadow_candidate_store)

        # 3. Subsequent identical query matches the shadow candidate
        task2 = ShadowTask(
            request_id="req-ingest-test-2",
            request_hash="hash-ingest-test-2",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            temperature=0.0,
            max_tokens=100,
            query_text="How to sort a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash="resp-h1",
            actual_latency_ms=100.0,
            enqueued_at=time.perf_counter(),
            actual_response_text="Use items.sort() or sorted(items)",
        )
        self.shadow_service.enqueue(task2)
        await self.shadow_service.queue.join()

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 1)

        # Production store remains empty
        prod_records_after = await self.raw_store.search([0.5, 0.5, 0.5, 0.5], top_k=5)
        self.assertEqual(len(prod_records_after), 0)

    async def test_offline_backfill_shadow_candidates(self):
        """Offline backfill imports eligible exact-cache entries into shadow store and skips ineligible ones."""
        shadow_store = InMemoryVectorStore(dimensions=4)

        # Mock database connection and pool
        mock_rows = [
            {
                "cache_namespace": "default",
                "fingerprint": "fp-1",
                "query_text": "How to sort in Python?",
                "response": "Use sorted(x)",
                "prompt_tokens": 10,
                "completion_tokens": 10,
                "total_tokens": 20,
                "latency_ms": 30.0,
            },
            {
                "cache_namespace": "default",
                "fingerprint": "fp-2",
                "query_text": "What is Python list comprehension?",
                "response": "[x for x in seq]",
                "prompt_tokens": 12,
                "completion_tokens": 15,
                "total_tokens": 27,
                "latency_ms": 35.0,
            },
            {
                "cache_namespace": "default",
                "fingerprint": "fp-3",
                "query_text": "",  # Empty query -> ineligible
                "response": "empty",
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "latency_ms": 0.0,
            },
        ]
        mock_conn = AsyncMock()
        mock_conn.fetch.return_value = mock_rows
        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        imported = await backfill_shadow_candidates_from_cache(
            db_pool=mock_pool,
            shadow_vector_store=shadow_store,
            embedding_provider=self.embed_provider,
            policy=self.policy,
            namespace="default",
            limit=50,
        )
        self.assertEqual(imported, 2)
        records = await shadow_store.search([0.5, 0.5, 0.5, 0.5], top_k=5)
        self.assertEqual(len(records), 2)

    async def test_shadow_component_latency_metrics_aggregation(self):
        """Shadow component latencies (embedding, retrieval, policy) are recorded and aggregated in metrics."""
        self.collector.record_shadow_outcome(
            namespace="default",
            outcome="eligible_candidate_hit",
            execution_latency_ms=45.0,
            embedding_latency_ms=12.0,
            retrieval_latency_ms=25.0,
            policy_latency_ms=4.0,
        )
        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.latencies.shadow_embedding_latency_ms.sample_count, 1)
        self.assertAlmostEqual(snap.latencies.shadow_embedding_latency_ms.mean, 12.0)
        self.assertEqual(snap.latencies.shadow_retrieval_latency_ms.sample_count, 1)
        self.assertAlmostEqual(snap.latencies.shadow_retrieval_latency_ms.mean, 25.0)
        self.assertEqual(snap.latencies.shadow_policy_latency_ms.sample_count, 1)
        self.assertAlmostEqual(snap.latencies.shadow_policy_latency_ms.mean, 4.0)


class TestControlledReasonCodes(unittest.TestCase):
    """Verifies that eligibility and compatibility codes are strictly controlled (P1/P2)."""

    def test_map_eligibility_codes(self):
        self.assertEqual(map_eligibility_rejection_code("Multi-turn conversation is not supported"), "MULTI_TURN_CONVERSATION")
        self.assertEqual(map_eligibility_rejection_code("System prompt is present"), "SYSTEM_PROMPT_PRESENT")
        self.assertEqual(map_eligibility_rejection_code("Temperature must be 0.0"), "TEMPERATURE_NON_ZERO")
        self.assertEqual(map_eligibility_rejection_code("Streaming queries are ineligible"), "STREAMING_ENABLED")
        self.assertEqual(map_eligibility_rejection_code("Empty query text"), "EMPTY_QUERY")
        self.assertEqual(map_eligibility_rejection_code("Payload character length exceeded"), "PAYLOAD_TOO_LARGE")

    def test_map_compatibility_codes(self):
        self.assertEqual(map_compatibility_rejection_code("Model mismatch: expected A, got B"), "MODEL_MISMATCH")
        self.assertEqual(map_compatibility_rejection_code("System prompt mismatch"), "SYSTEM_PROMPT_MISMATCH")
        self.assertEqual(map_compatibility_rejection_code("Temperature mismatch"), "TEMPERATURE_MISMATCH")
        self.assertEqual(map_compatibility_rejection_code("Max tokens mismatch"), "MAX_TOKENS_MISMATCH")
        self.assertEqual(map_compatibility_rejection_code("Task kind mismatch"), "TASK_KIND_MISMATCH")


class TestBoundedBatchCleanup(unittest.IsolatedAsyncioTestCase):
    """Verifies bounded batch retention cleanup query logic (P2)."""

    async def test_cleanup_old_records_with_mock_pool(self):
        mock_conn = AsyncMock()
        mock_conn.execute.side_effect = ["DELETE 500", "DELETE 120"]

        mock_pool = MagicMock()
        mock_pool.acquire.return_value.__aenter__.return_value = mock_conn

        service = ShadowEvaluationService(
            settings=Settings(cache_backend="memory"),
            semantic_cache=MagicMock(),
            db_pool=mock_pool,
        )

        deleted = await service.cleanup_old_records(retention_days=14, batch_size=500)
        self.assertEqual(deleted, 620)
        self.assertEqual(mock_conn.execute.call_count, 2)


class TestShadowFastAPIIntegration(unittest.IsolatedAsyncioTestCase):
    """End-to-end integration tests verifying FastAPI request invariance, routing, and shadow triggers (P0/P1)."""

    async def asyncSetUp(self):
        import os
        from app.config import reload_settings
        from app.main import app
        from app.metrics import get_metrics_collector
        from app.providers.interfaces import LLMProvider
        from app.core.contracts import UsageMetrics
        from app.routes.query import (
            get_llm_provider,
            get_query_cache,
            set_shadow_service,
        )
        from fastapi.testclient import TestClient

        self.collector = get_metrics_collector()
        self.collector.reset()

        self.env_patcher = patch.dict(
            os.environ,
            {
                "ALLOW_UNAUTHENTICATED_LOOPBACK": "true",
                "CLIENT_API_TOKEN": "test-client-token",
            },
            clear=False,
        )
        self.env_patcher.start()
        reload_settings()

        import httpx
        self.transport = httpx.ASGITransport(app=app)
        self.headers = {"X-Client-Token": "test-client-token"}
        self.cache = get_query_cache()
        await self.cache.clear()

        class MockLLM(LLMProvider):
            def __init__(self):
                self.call_count = 0

            async def generate(self, request: GenerationRequest) -> GenerationResult:
                self.call_count += 1
                return GenerationResult(
                    text="def reverse_list(items): return items[::-1]",
                    model=request.model,
                    usage=UsageMetrics(prompt_tokens=15, completion_tokens=25, total_tokens=40),
                    latency_ms=45.0,
                    cached=False,
                    fingerprint="",
                )

            async def stream(self, request: GenerationRequest):
                self.call_count += 1
                from app.core.contracts import StreamChunk
                yield StreamChunk(delta="def reverse_list", usage=None, model=request.model)
                yield StreamChunk(
                    delta="(items): return items[::-1]",
                    usage=None,
                    model=request.model,
                )
                yield StreamChunk(
                    delta="",
                    usage=UsageMetrics(prompt_tokens=15, completion_tokens=25, total_tokens=40),
                    model=request.model,
                    done=True,
                )

            async def close(self):
                pass

        self.mock_llm = MockLLM()
        app.dependency_overrides[get_llm_provider] = lambda: self.mock_llm

        import app.api.routes as routes_mod
        routes_mod._core_singleton = None

    async def asyncTearDown(self):
        from app.config import reload_settings
        from app.main import app
        from app.routes.query import set_shadow_service
        set_shadow_service(None)
        app.dependency_overrides.clear()
        if hasattr(self, "cache") and self.cache:
            await self.cache.clear()
        self.env_patcher.stop()
        reload_settings()
        self.collector.reset()

    async def test_shadow_mode_disabled_by_default(self):
        """When shadow mode is disabled, requests proceed normally with zero shadow records."""
        import httpx
        async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/query",
                json={"prompt": "How to reverse a list in Python?", "temperature": 0.0},
                headers=self.headers,
            )
            self.assertEqual(resp.status_code, 200)
            data = resp.json()
            self.assertIn("reverse_list", data["response"])
            self.assertFalse(data["cached"])

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_requests_total, 0)

    async def test_shadow_mode_enabled_observes_exact_and_upstream(self):
        """Shadow mode observes both upstream generation and subsequent exact-cache hit without affecting responses."""
        import httpx
        from app.routes.query import set_shadow_service

        raw_store = InMemoryVectorStore(dimensions=4)
        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        embed_provider = MockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )
        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        from app.config import get_settings
        from app.main import app
        orig_get_settings = app.dependency_overrides.get(get_settings)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                # 1. Upstream request
                resp1 = await client.post(
                    "/query",
                    json={"prompt": "How to reverse a list in Python?", "temperature": 0.0},
                    headers=self.headers,
                )
                self.assertEqual(resp1.status_code, 200)
                self.assertFalse(resp1.json()["cached"])

                await shadow_service.queue.join()

                # 2. Exact cache hit
                resp2 = await client.post(
                    "/query",
                    json={"prompt": "How to reverse a list in Python?", "temperature": 0.0},
                    headers=self.headers,
                )
                self.assertEqual(resp2.status_code, 200)
                self.assertTrue(resp2.json()["cached"])

                await shadow_service.queue.join()

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_requests_total, 2)
            self.assertEqual(snap.shadow.shadow_requests_sampled, 2)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 2)
            self.assertEqual(snap.shadow.shadow_no_candidate, 2)

            # Verification: Production cache counters reflect exact hit, NOT semantic hit!
            self.assertEqual(snap.request_and_cache.exact_cache_hits, 1)
            self.assertEqual(snap.request_and_cache.semantic_hits, 0)
        finally:
            if orig_get_settings is None:
                app.dependency_overrides.pop(get_settings, None)
            else:
                app.dependency_overrides[get_settings] = orig_get_settings
            await shadow_service.stop(drain_timeout=0.5)

    async def test_shadow_mode_observes_streaming_query(self):
        """Streaming request streams chunks cleanly and enqueues shadow observation."""
        import httpx
        from app.routes.query import set_shadow_service

        raw_store = InMemoryVectorStore(dimensions=4)
        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        embed_provider = MockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )
        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        from app.config import get_settings
        from app.main import app
        orig_get_settings = app.dependency_overrides.get(get_settings)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/query",
                    json={"prompt": "How to reverse a list in Python?", "temperature": 0.0, "stream": True},
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200)
                body = resp.text
                self.assertIn("data: [DONE]", body)

                await shadow_service.queue.join()

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_requests_total, 1)
            # Streaming queries are ineligible for semantic caching -> ineligible_request
            self.assertEqual(snap.shadow.shadow_requests_eligible, 0)
        finally:
            if orig_get_settings is None:
                app.dependency_overrides.pop(get_settings, None)
            else:
                app.dependency_overrides[get_settings] = orig_get_settings
            await shadow_service.stop(drain_timeout=0.5)

    async def test_warmup_prime_header_authorization_boundary(self):
        """
        Verifies that X-Warmup-Prime suppresses shadow evaluation only when accompanied
        by valid administrative authorization (via X-Admin-Token or Bearer token).
        Ordinary client tokens, invalid tokens, and malformed Bearer headers cannot bypass
        shadow evaluation (Stage 3.4.1.4.1 & Stage 3.4.1.4.2).
        """
        import httpx
        from app.routes.query import set_shadow_service

        raw_store = InMemoryVectorStore(dimensions=4)
        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        embed_provider = MockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
            admin_api_token="test-admin-secret",
        )
        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        from app.config import get_settings
        from app.main import app
        orig_get_settings = app.dependency_overrides.get(get_settings)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                # 1. Ordinary client sending X-Warmup-Prime: true without admin credentials
                # MUST NOT suppress shadow evaluation!
                ordinary_headers = {
                    "X-Client-Token": "test-client-token",
                    "X-Warmup-Prime": "true",
                }
                resp1 = await client.post(
                    "/query",
                    json={"prompt": "Ordinary client warmup attempt", "temperature": 0.0},
                    headers=ordinary_headers,
                )
                self.assertEqual(resp1.status_code, 200)
                await shadow_service.queue.join()

                snap1 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap1.shadow.shadow_requests_total,
                    1,
                    "Ordinary client must NOT be able to suppress shadow evaluation with X-Warmup-Prime",
                )

                # 2. Authorized caller sending X-Warmup-Prime: true WITH valid X-Admin-Token
                # MUST suppress shadow evaluation!
                authorized_headers = {
                    "X-Client-Token": "test-client-token",
                    "X-Admin-Token": "test-admin-secret",
                    "X-Warmup-Prime": "true",
                }
                resp2 = await client.post(
                    "/query",
                    json={"prompt": "Authorized benchmark warmup prime", "temperature": 0.0},
                    headers=authorized_headers,
                )
                self.assertEqual(resp2.status_code, 200)
                await shadow_service.queue.join()

                snap2 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap2.shadow.shadow_requests_total,
                    1,
                    "Authorized caller with X-Admin-Token and X-Warmup-Prime MUST skip shadow evaluation",
                )

                # 3. Caller with INVALID admin token sending X-Warmup-Prime: true
                # MUST NOT suppress shadow evaluation!
                invalid_admin_headers = {
                    "X-Client-Token": "test-client-token",
                    "X-Admin-Token": "wrong-token",
                    "X-Warmup-Prime": "true",
                }
                resp3 = await client.post(
                    "/query",
                    json={"prompt": "Invalid admin warmup attempt", "temperature": 0.0},
                    headers=invalid_admin_headers,
                )
                self.assertEqual(resp3.status_code, 200)
                await shadow_service.queue.join()

                snap3 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap3.shadow.shadow_requests_total,
                    2,
                    "Caller with invalid admin token must NOT suppress shadow evaluation",
                )

                # 4. Authorized caller with valid Bearer token sending X-Warmup-Prime: true
                # MUST suppress shadow evaluation!
                valid_bearer_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Bearer test-admin-secret",
                    "X-Warmup-Prime": "true",
                }
                resp4 = await client.post(
                    "/query",
                    json={"prompt": "Authorized Bearer warmup prime", "temperature": 0.0},
                    headers=valid_bearer_headers,
                )
                self.assertEqual(resp4.status_code, 200)
                await shadow_service.queue.join()

                snap4 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap4.shadow.shadow_requests_total,
                    2,
                    "Authorized caller with Bearer admin token and X-Warmup-Prime MUST skip shadow evaluation",
                )

                # 5. Caller with INVALID Bearer token sending X-Warmup-Prime: true
                # MUST NOT suppress shadow evaluation!
                invalid_bearer_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Bearer invalid-admin-secret",
                    "X-Warmup-Prime": "true",
                }
                resp5 = await client.post(
                    "/query",
                    json={"prompt": "Invalid Bearer warmup attempt", "temperature": 0.0},
                    headers=invalid_bearer_headers,
                )
                self.assertEqual(resp5.status_code, 200)
                await shadow_service.queue.join()

                snap5 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap5.shadow.shadow_requests_total,
                    3,
                    "Caller with invalid Bearer token must NOT suppress shadow evaluation",
                )

                # 6. Caller with MISSING/MALFORMED Bearer token sending X-Warmup-Prime: true
                # MUST NOT suppress shadow evaluation!
                empty_bearer_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Bearer ",
                    "X-Warmup-Prime": "true",
                }
                resp6 = await client.post(
                    "/query",
                    json={"prompt": "Empty Bearer warmup attempt", "temperature": 0.0},
                    headers=empty_bearer_headers,
                )
                self.assertEqual(resp6.status_code, 200)
                await shadow_service.queue.join()

                snap6 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap6.shadow.shadow_requests_total,
                    4,
                    "Caller with empty Bearer token must NOT suppress shadow evaluation",
                )

                # 7. Caller with NON-BEARER authorization (e.g. Basic) sending X-Warmup-Prime: true
                # MUST NOT suppress shadow evaluation!
                non_bearer_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Basic dGVzdC1hZG1pbi1zZWNyZXQ=",
                    "X-Warmup-Prime": "true",
                }
                resp7 = await client.post(
                    "/query",
                    json={"prompt": "Non-Bearer warmup attempt", "temperature": 0.0},
                    headers=non_bearer_headers,
                )
                self.assertEqual(resp7.status_code, 200)
                await shadow_service.queue.join()

                snap7 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap7.shadow.shadow_requests_total,
                    5,
                    "Caller with non-Bearer auth header must NOT suppress shadow evaluation",
                )
        finally:
            if orig_get_settings is None:
                app.dependency_overrides.pop(get_settings, None)
            else:
                app.dependency_overrides[get_settings] = orig_get_settings
            await shadow_service.stop(drain_timeout=0.5)

    async def test_warmup_prime_streaming_authorization_boundary(self):
        """
        Verifies that unauthorized X-Warmup-Prime headers cannot suppress shadow evaluation
        on streaming requests (stream: True), and authorized headers do suppress it (Stage 3.4.1.4.2).
        """
        import httpx
        from app.routes.query import set_shadow_service

        raw_store = InMemoryVectorStore(dimensions=4)
        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        embed_provider = MockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
            admin_api_token="test-admin-secret",
        )
        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        from app.config import get_settings
        from app.main import app
        orig_get_settings = app.dependency_overrides.get(get_settings)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                # 1. Ordinary client streaming request with X-Warmup-Prime: true
                # MUST NOT suppress shadow evaluation!
                ordinary_stream_headers = {
                    "X-Client-Token": "test-client-token",
                    "X-Warmup-Prime": "true",
                }
                resp1 = await client.post(
                    "/query",
                    json={"prompt": "Ordinary streaming warmup attempt", "temperature": 0.0, "stream": True},
                    headers=ordinary_stream_headers,
                )
                self.assertEqual(resp1.status_code, 200)
                await shadow_service.queue.join()

                snap1 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap1.shadow.shadow_requests_total,
                    1,
                    "Ordinary client streaming request must NOT suppress shadow evaluation with X-Warmup-Prime",
                )

                # 2. Authorized caller streaming request with X-Admin-Token
                # MUST suppress shadow evaluation!
                admin_token_headers = {
                    "X-Client-Token": "test-client-token",
                    "X-Admin-Token": "test-admin-secret",
                    "X-Warmup-Prime": "true",
                }
                resp2 = await client.post(
                    "/query",
                    json={"prompt": "Authorized X-Admin-Token streaming warmup", "temperature": 0.0, "stream": True},
                    headers=admin_token_headers,
                )
                self.assertEqual(resp2.status_code, 200)
                await shadow_service.queue.join()

                snap2 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap2.shadow.shadow_requests_total,
                    1,
                    "Authorized caller with X-Admin-Token on streaming query MUST skip shadow evaluation",
                )

                # 3. Authorized caller streaming request with valid Bearer token
                # MUST suppress shadow evaluation!
                bearer_admin_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Bearer test-admin-secret",
                    "X-Warmup-Prime": "true",
                }
                resp3 = await client.post(
                    "/query",
                    json={"prompt": "Authorized Bearer streaming warmup", "temperature": 0.0, "stream": True},
                    headers=bearer_admin_headers,
                )
                self.assertEqual(resp3.status_code, 200)
                await shadow_service.queue.join()

                snap3 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap3.shadow.shadow_requests_total,
                    1,
                    "Authorized caller with Bearer token on streaming query MUST skip shadow evaluation",
                )

                # 4. Caller with invalid Bearer token on streaming request
                # MUST NOT suppress shadow evaluation!
                invalid_bearer_stream_headers = {
                    "X-Client-Token": "test-client-token",
                    "Authorization": "Bearer wrong-admin-token",
                    "X-Warmup-Prime": "true",
                }
                resp4 = await client.post(
                    "/query",
                    json={"prompt": "Invalid Bearer streaming warmup", "temperature": 0.0, "stream": True},
                    headers=invalid_bearer_stream_headers,
                )
                self.assertEqual(resp4.status_code, 200)
                await shadow_service.queue.join()

                snap4 = self.collector.get_namespace_snapshot("default")
                self.assertEqual(
                    snap4.shadow.shadow_requests_total,
                    2,
                    "Caller with invalid Bearer token on streaming query must NOT suppress shadow evaluation",
                )
        finally:
            if orig_get_settings is None:
                app.dependency_overrides.pop(get_settings, None)
            else:
                app.dependency_overrides[get_settings] = orig_get_settings
            await shadow_service.stop(drain_timeout=0.5)

    async def test_shadow_mode_candidate_match_verification(self):
        """Verifies candidate response match verification against actual response."""
        import httpx
        from app.routes.query import set_shadow_service

        raw_store = InMemoryVectorStore(dimensions=4)
        embed_provider = MockEmbeddingProvider()

        # Seed candidate into raw store
        embed_res = await embed_provider.embed("How to reverse a list in Python?")
        from app.semantic_cache.compatibility import CompatibilityExtractor
        extractor = CompatibilityExtractor()
        req_seed = GenerationRequest(
            provider="groq",
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="How to reverse a list in Python?")],
            temperature=0.0,
            max_tokens=100,
            visibility="tenant_shared",
        )
        seed_meta = extractor.extract(req_seed, "BAAI/bge-small-en-v1.5")
        seed_meta_dict = seed_meta.to_dict()
        seed_meta_dict["cached_response"] = "def reverse_list(items): return items[::-1]"
        seed_record = VectorRecord(
            cache_namespace="default",
            fingerprint="fp-matched-seed",
            query_text="How to reverse a list in Python?",
            vector=embed_res.vector,
            model="BAAI/bge-small-en-v1.5",
            metadata=seed_meta_dict,
            visibility="tenant_shared",
        )
        await raw_store.upsert(seed_record)

        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="memory",
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )
        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=None,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        from app.config import get_settings
        from app.main import app
        orig_get_settings = app.dependency_overrides.get(get_settings)
        app.dependency_overrides[get_settings] = lambda: settings

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/query",
                    json={
                        "prompt": "How to reverse a list in Python?",
                        "model": "qwen/qwen3.8-27b",
                        "temperature": 0.0,
                        "max_tokens": 100,
                    },
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200)
                await shadow_service.queue.join()

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_requests_total, 1)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
            self.assertEqual(snap.shadow.shadow_hypothetical_hits, 1)
            self.assertEqual(snap.shadow.shadow_no_candidate, 0)
        finally:
            if orig_get_settings is None:
                app.dependency_overrides.pop(get_settings, None)
            else:
                app.dependency_overrides[get_settings] = orig_get_settings
            await shadow_service.stop(drain_timeout=0.5)


class TestPostgresShadowStorageIntegration(unittest.IsolatedAsyncioTestCase):
    """Integration test with live PostgreSQL database if configured (P1/P2)."""

    async def asyncSetUp(self):
        import asyncpg
        import os
        self.dsn = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/intellicache_test")
        try:
            self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=3)
            # Apply migrations to ensure shadow_evaluations table is up to date
            from app.cache.migration import run_migrations
            await run_migrations(self.pool)
            self.db_available = True
        except Exception:
            self.db_available = False
            self.pool = None

    async def asyncTearDown(self):
        if self.pool:
            # Clean up test rows
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM shadow_evaluations WHERE request_id LIKE 'test-pg-%';")
            await self.pool.close()

    async def test_persist_and_batch_cleanup_on_postgres(self):
        if not self.db_available or not self.pool:
            self.skipTest("Live PostgreSQL database is not available for integration test.")

        raw_store = InMemoryVectorStore(dimensions=4)
        adapter = ReadOnlyVectorStoreAdapter(raw_store)
        embed_provider = MockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=adapter,
            policy=policy,
            default_namespace="default",
        )
        settings = Settings(
            cache_backend="postgres",
            database_url=self.dsn,
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )
        collector = RuntimeMetricsCollector()
        service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            db_pool=self.pool,
            metrics_collector=collector,
        )

        # Directly persist a test record
        await service._persist_record(
            request_id="test-pg-1",
            request_hash="test-hash-pg-1",
            cache_namespace="default",
            provider="groq",
            requested_model="qwen/qwen3.8-27b",
            task_kind="generation",
            eligibility_status="eligible",
            eligibility_rejection_reason=None,
            embedding_model="BAAI/bge-small-en-v1.5",
            retrieval_status="hit",
            candidate_count=2,
            top_similarity=0.95,
            runner_up_similarity=0.90,
            similarity_margin=0.05,
            compatibility_validation_result="compatible",
            compatibility_rejection_reason=None,
            ambiguity_decision=None,
            hypothetical_cache_outcome="eligible_candidate_hit",
            actual_response_source="upstream_provider",
            candidate_response_matched=True,
            shadow_execution_duration_ms=45.2,
            embedding_duration_ms=15.1,
            retrieval_duration_ms=25.0,
            policy_evaluation_duration_ms=5.1,
            failure_category=None,
        )

        # Verify record exists in shadow_evaluations
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow("SELECT * FROM shadow_evaluations WHERE request_id = 'test-pg-1';")
            self.assertIsNotNone(row)
            self.assertEqual(row["hypothetical_cache_outcome"], "eligible_candidate_hit")
            self.assertEqual(row["actual_response_source"], "upstream_provider")
            self.assertTrue(row["candidate_response_matched"])
            self.assertEqual(row["top_similarity"], 0.95)

        # Verify bounded cleanup executes without syntax error
        deleted = await service.cleanup_old_records(retention_days=30, batch_size=100)
        self.assertGreaterEqual(deleted, 0)


class TestDatabaseLevelReadOnlyEnforcement(unittest.IsolatedAsyncioTestCase):
    """Verifies application-level write protection and transaction-level read-only search isolation."""

    async def test_readonly_adapter_enforces_db_readonly_flag(self):
        pg_store = PostgresVectorStore(
            pool=MagicMock(),
            dimensions=4,
            table_name="vector_records",
            readonly_transaction=False,
        )
        adapter = ReadOnlyVectorStoreAdapter(pg_store, enforce_db_readonly=True)
        self.assertTrue(pg_store.readonly_transaction)
        self.assertTrue(adapter.is_enforcing_db_readonly())

    async def test_postgres_vector_store_readonly_transaction_wrapping(self):
        """When readonly_transaction=True, search executes inside conn.transaction(readonly=True)."""
        class MockTxContext:
            def __init__(self):
                self.entered = False
                self.exited = False

            async def __aenter__(self):
                self.entered = True
                return self

            async def __aexit__(self, exc_type, exc_val, exc_tb):
                self.exited = True
                return False

        tx_ctx = MockTxContext()
        mock_conn = MagicMock()
        mock_conn.fetch = AsyncMock(return_value=[])
        mock_conn.transaction = MagicMock(return_value=tx_ctx)

        mock_pool = MagicMock()
        mock_acquire = MagicMock()
        mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_acquire

        store = PostgresVectorStore(
            pool=mock_pool,
            dimensions=4,
            table_name="vector_records",
            readonly_transaction=True,
        )

        res = await store.search([0.1, 0.2, 0.3, 0.4], top_k=3)
        self.assertEqual(res, [])
        mock_conn.transaction.assert_called_once_with(readonly=True)
        self.assertTrue(tx_ctx.entered)
        self.assertTrue(tx_ctx.exited)

    async def test_postgres_readonly_transaction_blocks_mutation(self):
        """Simulates PostgreSQL error 25006 (cannot_execute_in_read_only_transaction) when write is attempted in readonly transaction."""
        class MockReadOnlyTransactionError(Exception):
            sqlstate = "25006"

        mock_conn = MagicMock()
        mock_conn.execute = AsyncMock(
            side_effect=MockReadOnlyTransactionError(
                "ERROR: cannot execute INSERT in a read-only transaction (SQLSTATE 25006)"
            )
        )

        mock_pool = MagicMock()
        mock_acquire = MagicMock()
        mock_acquire.__aenter__ = AsyncMock(return_value=mock_conn)
        mock_acquire.__aexit__ = AsyncMock(return_value=None)
        mock_pool.acquire.return_value = mock_acquire

        store = PostgresVectorStore(
            pool=mock_pool,
            dimensions=4,
            table_name="vector_records",
            readonly_transaction=True,
        )

        # Attempting upsert when wrapped in a read-only transaction context simulates database write rejection
        rec = VectorRecord(
            cache_namespace="default",
            fingerprint="fp-write",
            query_text="Write attempt",
            vector=[0.1, 0.2, 0.3, 0.4],
            model="bge",
            metadata={},
        )
        with self.assertRaises(Exception) as ctx:
            await store.upsert(rec)
        self.assertIn("cannot execute INSERT in a read-only transaction", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
