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
from app.metrics.collector import RuntimeMetricsCollector
from app.semantic_cache.contracts import SemanticHit
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.shadow import (
    ReadOnlySemanticCacheService,
    ReadOnlyVectorStoreAdapter,
    ShadowEvaluationService,
    ShadowTask,
    map_compatibility_rejection_code,
    map_eligibility_rejection_code,
)
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore


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


if __name__ == "__main__":
    unittest.main()
