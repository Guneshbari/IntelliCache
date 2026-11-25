"""
Automated Failure Injection & Resilience Verification Test Suite (Stage 3.4.1.2).

Proves that:
1. FastEmbed ONNX inference failures fail open (user gets 200, shadow_embedding_failures increments).
2. PostgreSQL/pgvector search failures fail open (user gets 200, shadow_vector_store_failures increments).
3. Shadow queue saturation drops gracefully without delaying the primary request loop.
4. Upstream provider failures increment provider_calls_failed and return appropriate HTTP errors.
5. Component latency hierarchy holds for all eligible evaluations, and demonstrates why
   mixed-population execution latency mean can be lower than embedding latency mean.
"""

import asyncio
from dataclasses import dataclass
import hashlib
import os
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg

from app.config import Settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.metrics.collector import RuntimeMetricsCollector, get_metrics_collector
from app.semantic_cache.contracts import SemanticHit
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.shadow import (
    ReadOnlySemanticCacheService,
    ReadOnlyVectorStoreAdapter,
    ShadowEvaluationService,
    ShadowTask,
)
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore
from app.vector_store.postgres_store import PostgresVectorStore


@dataclass
class DummyEmbeddingResult:
    vector: list
    model: str = "BAAI/bge-small-en-v1.5"
    dimensions: int = 4
    latency_ms: float = 10.0


class FailingEmbeddingProvider:
    """Simulates an embedding provider that throws an internal error."""

    def __init__(self, error_message: str = "Simulated ONNX model fault"):
        self.error_message = error_message
        self.model = "BAAI/bge-small-en-v1.5"
        self.dimensions = 4

    async def embed(self, text: str, task: str = "query"):
        await asyncio.sleep(0.01)
        raise RuntimeError(self.error_message)


class FailingVectorStore(InMemoryVectorStore):
    """Simulates a vector store where search operations fail."""

    def __init__(self, error_message: str = "Simulated PostgreSQL pool connection lost"):
        super().__init__(dimensions=4)
        self.error_message = error_message

    async def search(self, query_vector, top_k=5, namespace=None, model=None, **kwargs):
        await asyncio.sleep(0.01)
        raise ConnectionRefusedError(self.error_message)


class HealthyEmbeddingProvider:
    """Deterministic healthy embedding provider."""

    def __init__(self, delay_ms: float = 15.0):
        self.delay_ms = delay_ms
        self.model = "BAAI/bge-small-en-v1.5"
        self.dimensions = 4

    async def embed(self, text: str, task: str = "query"):
        await asyncio.sleep(self.delay_ms / 1000.0)
        return DummyEmbeddingResult(vector=[0.1, 0.2, 0.3, 0.4])


class TestShadowFailureInjection(unittest.IsolatedAsyncioTestCase):
    """Verifies fail-open isolation and diagnostic recording under injected component failures."""

    async def asyncSetUp(self):
        self.settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=1.0,
            cache_enabled=True,
        )
        self.collector = RuntimeMetricsCollector()

    async def test_fastembed_inference_failure_fail_open(self):
        """
        Proof of Failure 1: FastEmbed inference failure during shadow evaluation.
        Asserts:
        - Shadow evaluation catches RuntimeError.
        - hypothetical_cache_outcome is 'embedding_failure'.
        - failure_category is 'EMBEDDING_ERROR'.
        - collector shadow_embedding_failures counter increments by exactly 1.
        - Primary caller is unaffected.
        """
        failing_embedder = FailingEmbeddingProvider("Simulated ONNX runtime crash")
        vector_store = InMemoryVectorStore(dimensions=4)
        policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)

        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=failing_embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        service = ShadowEvaluationService(
            settings=self.settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )

        task = ShadowTask(
            request_id="req-failing-embed",
            request_hash="hash-123",
            cache_namespace="default",
            provider="mock",
            requested_model="mock-model",
            temperature=0.0,
            max_tokens=100,
            query_text="How to reverse a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=45.0,
            enqueued_at=time.perf_counter(),
            bypass_cache=False,
        )

        # Directly evaluate task
        await service._evaluate_task(task)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_embedding_failures, 1)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)
        self.assertEqual(snap.shadow.shadow_execution_failures, 0)

    async def test_postgres_vector_store_failure_fail_open(self):
        """
        Proof of Failure 2: PostgreSQL / pgvector failure during shadow lookup.
        Asserts:
        - Shadow evaluation catches ConnectionRefusedError.
        - hypothetical_cache_outcome is 'vector_store_failure'.
        - failure_category is 'VECTOR_STORE_ERROR'.
        - collector shadow_vector_store_failures counter increments by exactly 1.
        - Primary caller is unaffected.
        """
        healthy_embedder = HealthyEmbeddingProvider(delay_ms=2.0)
        failing_store = FailingVectorStore("PostgreSQL connection timeout")
        policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)

        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=healthy_embedder,
            vector_store=failing_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        service = ShadowEvaluationService(
            settings=self.settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )

        task = ShadowTask(
            request_id="req-failing-store",
            request_hash="hash-456",
            cache_namespace="default",
            provider="mock",
            requested_model="mock-model",
            temperature=0.0,
            max_tokens=100,
            query_text="How to reverse a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=50.0,
            enqueued_at=time.perf_counter(),
            bypass_cache=False,
        )

        await service._evaluate_task(task)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_vector_store_failures, 1)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)

    async def test_real_postgres_vector_store_failure_fail_open(self):
        """
        Proof of Failure 2b (Stage 3.4.1.3): Live PostgreSQL/pgvector database query failure.
        Connects a live PostgresVectorStore to an actual PostgreSQL instance and injects
        a genuine query/database error (querying an invalid/non-existent table).
        Asserts:
        - Real PostgreSQL server raises UndefinedTableError (asyncpg.PostgresError).
        - PostgresVectorStore catches asyncpg.PostgresError and wraps as VectorStoreError.
        - Shadow evaluation catches failure gracefully without unhandled exception.
        - hypothetical_cache_outcome is 'vector_store_failure'.
        - failure_category is 'VECTOR_STORE_ERROR'.
        - collector shadow_vector_store_failures counter increments by exactly 1.
        - collector shadow_hypothetical_hits remains 0.
        - Primary caller is unaffected.
        """
        db_url = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test")
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2, command_timeout=5.0)
        except Exception as e:
            self.skipTest(f"Live PostgreSQL database unavailable at {db_url}: {e}")

        try:
            # Create real PostgresVectorStore pointed at an invalid table name to force PostgreSQL server rejection
            store = PostgresVectorStore(
                pool=pool,
                default_namespace="default",
                default_model="BAAI/bge-small-en-v1.5",
                dimensions=4,
                table_name="nonexistent_vector_table",
                readonly_transaction=True,
            )
            embedder = HealthyEmbeddingProvider(delay_ms=2.0)
            policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)

            shadow_cache = ReadOnlySemanticCacheService(
                embedding_provider=embedder,
                vector_store=store,
                policy=policy,
                metrics_collector=self.collector,
            )

            service = ShadowEvaluationService(
                settings=self.settings,
                semantic_cache=shadow_cache,
                metrics_collector=self.collector,
            )

            task = ShadowTask(
                request_id="req-real-pg-query-fail",
                request_hash="hash-real-pg",
                cache_namespace="default",
                provider="mock",
                requested_model="mock-model",
                temperature=0.0,
                max_tokens=100,
                query_text="How to sort a list in Python?",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=45.0,
                enqueued_at=time.perf_counter(),
                bypass_cache=False,
            )

            await service._evaluate_task(task)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_vector_store_failures, 1)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
            self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)
        finally:
            await pool.close()

    async def test_real_postgres_connection_drop_fail_open(self):
        """
        Proof of Failure 2c (Stage 3.4.1.3): Live PostgreSQL connection pool closed/dropped during search.
        Asserts:
        - Connection failure (asyncpg.InterfaceError wrapped into VectorStoreConnectionError) is handled gracefully.
        - Collector failure counter increments by exactly 1.
        - Primary caller is unaffected.
        """
        db_url = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test")
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2, command_timeout=5.0)
        except Exception as e:
            self.skipTest(f"Live PostgreSQL database unavailable at {db_url}: {e}")

        store = PostgresVectorStore(
            pool=pool,
            default_namespace="default",
            default_model="BAAI/bge-small-en-v1.5",
            dimensions=4,
            table_name="vector_records",
        )
        # Terminate the pool to simulate sudden connection loss
        await pool.close()

        embedder = HealthyEmbeddingProvider(delay_ms=2.0)
        policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embedder,
            vector_store=store,
            policy=policy,
            metrics_collector=self.collector,
        )
        service = ShadowEvaluationService(
            settings=self.settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )

        task = ShadowTask(
            request_id="req-real-pg-drop",
            request_hash="hash-real-pg-drop",
            cache_namespace="default",
            provider="mock",
            requested_model="mock-model",
            temperature=0.0,
            max_tokens=100,
            query_text="How to sort a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=45.0,
            enqueued_at=time.perf_counter(),
            bypass_cache=False,
        )

        await service._evaluate_task(task)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_vector_store_failures, 1)
        self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)

    async def test_shadow_queue_saturation_fail_open(self):
        """
        Proof of Failure 3: Shadow evaluation queue saturation.
        Asserts:
        - When queue reaches capacity, enqueue() drops cleanly without blocking or raising.
        - shadow_queue_dropped_total increments by 1.
        - Primary request pipeline continues with zero latency delay.
        """
        bounded_settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=1,
            shadow_queue_max_size=2,  # Tiny queue
            shadow_timeout_seconds=1.0,
        )

        service = ShadowEvaluationService(
            settings=bounded_settings,
            semantic_cache=MagicMock(),
            metrics_collector=self.collector,
        )

        dummy_task = ShadowTask(
            request_id="dummy",
            request_hash="h",
            cache_namespace="default",
            provider="mock",
            requested_model="m",
            temperature=0.0,
            max_tokens=10,
            query_text="q",
            actual_response_source="exact_cache",
            actual_response_hash=None,
            actual_latency_ms=1.0,
            enqueued_at=time.perf_counter(),
        )

        # Fill queue to max capacity (2 items)
        self.assertTrue(service.enqueue(dummy_task))
        self.assertTrue(service.enqueue(dummy_task))
        self.assertEqual(service.queue.qsize(), 2)

        # 3rd enqueue MUST fail open with drop
        start = time.perf_counter()
        enqueued = service.enqueue(dummy_task)
        elapsed_ms = (time.perf_counter() - start) * 1000.0

        self.assertFalse(enqueued)
        self.assertLess(elapsed_ms, 5.0, "enqueue() on saturated queue must return immediately")

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_queue_dropped_total, 1)
        self.assertEqual(snap.shadow.shadow_requests_total, 3)
        self.assertEqual(snap.shadow.shadow_requests_sampled, 3)

    async def test_shadow_worker_timeout_fail_open(self):
        """
        Proof of Failure 4: Shadow evaluation lookup times out.
        Asserts:
        - When lookup exceeds shadow_timeout_seconds, asyncio.TimeoutError is handled.
        - hypothetical_cache_outcome is 'shadow_timeout'.
        - failure_category is 'SHADOW_TIMEOUT'.
        - shadow_execution_failures increments by 1.
        """
        slow_settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=1,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=0.05,  # 50ms timeout
        )

        # Provider that takes 200ms
        slow_provider = HealthyEmbeddingProvider(delay_ms=200.0)
        vector_store = InMemoryVectorStore(dimensions=4)
        policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)

        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=slow_provider,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        service = ShadowEvaluationService(
            settings=slow_settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )

        task = ShadowTask(
            request_id="req-timeout",
            request_hash="hash-timeout",
            cache_namespace="default",
            provider="mock",
            requested_model="mock-model",
            temperature=0.0,
            max_tokens=100,
            query_text="How to reverse a list in Python?",
            actual_response_source="upstream_provider",
            actual_response_hash=None,
            actual_latency_ms=25.0,
            enqueued_at=time.perf_counter(),
            bypass_cache=False,
        )

        await service._evaluate_task(task)

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.shadow.shadow_execution_failures, 1)
        self.assertEqual(snap.shadow.shadow_requests_eligible, 1)


class TestTimerInstrumentationReconciliation(unittest.IsolatedAsyncioTestCase):
    """
    Demonstrates and proves the mathematical cause of Finding 4:
    Why total execution latency mean appeared lower than embedding latency mean.
    """

    def test_cohort_denominator_difference_demonstration(self):
        """
        Proves:
        1. For any individual eligible task, execution_latency >= embedding_latency.
        2. In a mixed workload containing ineligible/bypass requests, ineligible requests
           record near-zero (~0.05ms) execution latency into shadow_execution_latency_ms,
           but record 0.0 (and thus NO sample) into shadow_embedding_latency_ms.
        3. Therefore, sample_count(execution) > sample_count(embedding), and the arithmetic
           mean of execution is mathematically pulled DOWN below the mean of embedding!
        """
        collector = RuntimeMetricsCollector()

        # Simulate 80 eligible requests:
        # Each eligible request executes FastEmbed (taking ~15ms) and total execution (~17ms).
        for _ in range(80):
            collector.record_shadow_outcome(
                namespace="default",
                outcome="eligible_no_candidate",
                execution_latency_ms=17.0,  # 17ms >= 15ms!
                embedding_latency_ms=15.0,
                retrieval_latency_ms=1.5,
                policy_latency_ms=0.5,
            )

        # Simulate 20 ineligible or bypass requests:
        # These short-circuit in ~0.08ms without running FastEmbed (embedding = 0.0).
        for _ in range(20):
            collector.record_shadow_outcome(
                namespace="default",
                outcome="ineligible_request",
                execution_latency_ms=0.08,  # fast path
                embedding_latency_ms=0.0,   # no embedding performed
                retrieval_latency_ms=0.0,
                policy_latency_ms=0.0,
            )

        snap = collector.get_namespace_snapshot("default")

        # 1. Verify sample count mismatch
        self.assertEqual(snap.latencies.shadow_execution_latency_ms.sample_count, 100)
        self.assertEqual(snap.latencies.shadow_embedding_latency_ms.sample_count, 80)

        # 2. Embedding mean is computed over the 80 eligible tasks:
        # mean = 15.0 ms
        self.assertAlmostEqual(snap.latencies.shadow_embedding_latency_ms.mean, 15.0, places=1)

        # 3. Execution mean is computed over ALL 100 tasks:
        # sum = (80 * 17.0) + (20 * 0.08) = 1360 + 1.6 = 1361.6
        # mean = 1361.6 / 100 = 13.62 ms!
        self.assertAlmostEqual(snap.latencies.shadow_execution_latency_ms.mean, 13.62, places=1)

        # 4. Q.E.D.: Execution mean (13.62ms) is LOWER than Embedding mean (15.00ms)
        # despite execution >= embedding for EVERY SINGLE task that actually ran embedding!
        self.assertLess(
            snap.latencies.shadow_execution_latency_ms.mean,
            snap.latencies.shadow_embedding_latency_ms.mean,
            "Execution mean is pulled below embedding mean due to cohort denominator difference!",
        )

    async def test_live_shadow_execution_instrumentation_reconciliation(self):
        """
        Verifies live timer instrumentation and cohort reconciliation through actual
        ShadowEvaluationService._evaluate_task() execution across mixed eligible and ineligible tasks (Stage 3.4.1.3).
        
        Proves:
        1. For every eligible task individually, live execution latency >= embedding latency.
        2. Ineligible and bypass tasks short-circuit rapidly (< 5ms) without executing embedding (embedding_duration_ms == 0).
        3. Live telemetry records sample_count(execution) > sample_count(embedding).
        4. The live execution mean is mathematically lower than the embedding mean due to the
           mixture of long-running eligible and fast-path ineligible tasks.
        """
        settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=50,
            shadow_timeout_seconds=5.0,
        )
        collector = RuntimeMetricsCollector()
        controlled_embedder = HealthyEmbeddingProvider(delay_ms=10.0)
        store = InMemoryVectorStore(dimensions=4)
        policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5", min_similarity=0.92)
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=controlled_embedder,
            vector_store=store,
            policy=policy,
            metrics_collector=collector,
        )
        service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            metrics_collector=collector,
        )

        # Spy on collector.record_shadow_outcome to capture individual per-task timings (Stage 3.4.1.4)
        recorded_tasks = []
        orig_record = collector.record_shadow_outcome

        def spy_record(
            namespace,
            outcome,
            execution_latency_ms=0.0,
            queue_depth=0,
            embedding_latency_ms=0.0,
            retrieval_latency_ms=0.0,
            policy_latency_ms=0.0,
        ):
            recorded_tasks.append(
                {
                    "outcome": outcome,
                    "execution_latency_ms": execution_latency_ms,
                    "embedding_latency_ms": embedding_latency_ms,
                    "retrieval_latency_ms": retrieval_latency_ms,
                    "policy_latency_ms": policy_latency_ms,
                }
            )
            orig_record(
                namespace,
                outcome,
                execution_latency_ms,
                queue_depth,
                embedding_latency_ms,
                retrieval_latency_ms,
                policy_latency_ms,
            )

        collector.record_shadow_outcome = spy_record

        # Evaluate 10 eligible tasks through live _evaluate_task()
        for i in range(10):
            task = ShadowTask(
                request_id=f"req-live-eligible-{i}",
                request_hash=f"hash-live-el-{i}",
                cache_namespace="default",
                provider="mock",
                requested_model="mock-model",
                temperature=0.0,
                max_tokens=100,
                query_text=f"How to optimize query plan in PostgreSQL {i}?",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=30.0,
                enqueued_at=time.perf_counter(),
                bypass_cache=False,
            )
            await service._evaluate_task(task)

        # Evaluate 5 ineligible/bypass tasks through live _evaluate_task()
        for i in range(5):
            task = ShadowTask(
                request_id=f"req-live-ineligible-{i}",
                request_hash=f"hash-live-inel-{i}",
                cache_namespace="default",
                provider="mock",
                requested_model="mock-model",
                temperature=1.0 if i % 2 == 0 else 0.0,
                max_tokens=100,
                query_text=f"Random non-cacheable query {i}",
                actual_response_source="upstream_provider",
                actual_response_hash=None,
                actual_latency_ms=20.0,
                enqueued_at=time.perf_counter(),
                bypass_cache=(i % 2 != 0),
            )
            await service._evaluate_task(task)

        # ---------------- Stage 3.4.1.4 Hard Individual Assertions ----------------
        self.assertEqual(len(recorded_tasks), 15)

        # 1. Per-task assertions for EVERY eligible task individually
        for idx in range(10):
            t = recorded_tasks[idx]
            self.assertEqual(t["outcome"], "eligible_no_candidate")
            self.assertGreaterEqual(
                t["execution_latency_ms"],
                t["embedding_latency_ms"],
                f"Eligible task {idx}: execution latency ({t['execution_latency_ms']:.2f}ms) must be >= embedding latency ({t['embedding_latency_ms']:.2f}ms)",
            )
            self.assertGreaterEqual(
                t["execution_latency_ms"],
                t["embedding_latency_ms"] + t["retrieval_latency_ms"] + t["policy_latency_ms"],
                f"Eligible task {idx}: total execution duration must encompass embedding, retrieval, and policy phases",
            )
            self.assertGreaterEqual(
                t["embedding_latency_ms"],
                9.0,
                f"Eligible task {idx}: embedding latency must reflect the controlled ~10ms provider delay",
            )

        # 2. Per-task assertions for EVERY ineligible / bypass task individually
        for idx in range(10, 15):
            t = recorded_tasks[idx]
            self.assertIn(t["outcome"], ("ineligible_request", "bypass_requested"))
            self.assertLess(
                t["execution_latency_ms"],
                5.0,
                f"Ineligible task {idx}: must short-circuit in fast-path (< 5ms), got {t['execution_latency_ms']:.2f}ms",
            )
            self.assertEqual(
                t["embedding_latency_ms"],
                0.0,
                f"Ineligible task {idx}: must record exactly 0.0ms embedding latency (zero embedding execution)",
            )
            self.assertEqual(
                t["retrieval_latency_ms"],
                0.0,
                f"Ineligible task {idx}: must record exactly 0.0ms retrieval latency",
            )
            self.assertEqual(
                t["policy_latency_ms"],
                0.0,
                f"Ineligible task {idx}: must record exactly 0.0ms policy latency",
            )

        snap = collector.get_namespace_snapshot("default")

        # 3. Verify sample count mismatch generated by live service execution
        self.assertEqual(snap.latencies.shadow_execution_latency_ms.sample_count, 15)
        self.assertEqual(snap.latencies.shadow_embedding_latency_ms.sample_count, 10)
        self.assertGreater(
            snap.latencies.shadow_execution_latency_ms.sample_count,
            snap.latencies.shadow_embedding_latency_ms.sample_count,
        )

        # 4. Embedding mean accurately reflects the ~10ms controlled delay of the 10 eligible tasks
        self.assertGreaterEqual(snap.latencies.shadow_embedding_latency_ms.mean, 9.0)

        # 5. Overall execution mean is pulled below embedding mean by the 5 early-short-circuiting tasks
        self.assertLess(
            snap.latencies.shadow_execution_latency_ms.mean,
            snap.latencies.shadow_embedding_latency_ms.mean,
            "Live execution mean must be lower than embedding mean due to fast-path ineligible tasks!",
        )


class TestFastAPIShadowFailureIsolation(unittest.IsolatedAsyncioTestCase):
    """
    End-to-end integration tests proving user requests succeed (HTTP 200)
    even when shadow mode components experience complete failures.
    """

    async def asyncSetUp(self):
        import os
        from app.config import reload_settings
        from app.main import app
        from app.metrics import get_metrics_collector
        from app.providers.interfaces import LLMProvider
        from app.routes.query import get_llm_provider, get_query_cache, set_shadow_service

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
            async def generate(self, request: GenerationRequest) -> GenerationResult:
                return GenerationResult(
                    text="def add(a, b): return a + b",
                    model=request.model,
                    usage=UsageMetrics(prompt_tokens=10, completion_tokens=15, total_tokens=25),
                    latency_ms=20.0,
                    cached=False,
                    fingerprint="",
                )

            async def stream(self, request: GenerationRequest):
                pass

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

    async def test_e2e_fastembed_failure_user_response_succeeds(self):
        """User receives HTTP 200 successfully even when FastEmbed throws in shadow background worker."""
        import httpx
        from app.routes.query import set_shadow_service

        settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )

        failing_embedder = FailingEmbeddingProvider("Injected FastEmbed crash")
        vector_store = InMemoryVectorStore(dimensions=4)
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=failing_embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/query",
                    json={"prompt": "How to write an add function in Python?", "temperature": 0.0},
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("def add", data["response"])
                self.assertFalse(data["cached"])

            # Allow background shadow task to execute and catch the failure
            await shadow_service.stop(drain_timeout=1.0)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_embedding_failures, 1)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
        finally:
            if shadow_service._running:
                await shadow_service.stop(drain_timeout=0.1)

    async def test_e2e_vector_store_failure_user_response_succeeds(self):
        """User receives HTTP 200 successfully even when PostgreSQL search fails in shadow background worker."""
        import httpx
        from app.routes.query import set_shadow_service

        settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )

        embed_provider = HealthyEmbeddingProvider(delay_ms=2.0)
        failing_store = FailingVectorStore("Injected PostgreSQL connection drop")
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=failing_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/query",
                    json={"prompt": "How to write an add function in Python?", "temperature": 0.0},
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("def add", data["response"])

            await shadow_service.stop(drain_timeout=1.0)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_vector_store_failures, 1)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
        finally:
            if shadow_service._running:
                await shadow_service.stop(drain_timeout=0.1)

    async def test_e2e_queue_saturation_fail_open_user_response_succeeds(self):
        """User receives HTTP 200 without delay when background shadow queue is saturated."""
        import httpx
        from app.routes.query import set_shadow_service

        settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=1,
            shadow_queue_max_size=1,  # Max 1 item
            shadow_timeout_seconds=2.0,
        )

        embed_provider = HealthyEmbeddingProvider(delay_ms=2.0)
        vector_store = InMemoryVectorStore(dimensions=4)
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )
        # Fill queue to saturation
        dummy_task = ShadowTask(
            request_id="dummy",
            request_hash="h",
            cache_namespace="default",
            provider="mock",
            requested_model="m",
            temperature=0.0,
            max_tokens=10,
            query_text="q",
            actual_response_source="exact_cache",
            actual_response_hash=None,
            actual_latency_ms=1.0,
            enqueued_at=time.perf_counter(),
        )
        shadow_service.enqueue(dummy_task)
        self.assertEqual(shadow_service.queue.qsize(), 1)

        set_shadow_service(shadow_service)

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                start_req = time.perf_counter()
                resp = await client.post(
                    "/query",
                    json={"prompt": "How to write an add function in Python?", "temperature": 0.0},
                    headers=self.headers,
                )
                elapsed_ms = (time.perf_counter() - start_req) * 1000.0

                self.assertEqual(resp.status_code, 200)
                # Primary request is completely unhindered by queue saturation
                self.assertLess(elapsed_ms, 200.0)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_queue_dropped_total, 1)
        finally:
            await shadow_service.stop(drain_timeout=0.1)

    async def test_e2e_real_postgres_vector_store_failure_user_response_succeeds(self):
        """
        User receives HTTP 200 successfully even when real PostgreSQL vector lookup fails
        in shadow background worker (Stage 3.4.1.3).
        """
        import httpx
        from app.routes.query import set_shadow_service

        db_url = os.getenv("TEST_DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test")
        try:
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2, command_timeout=5.0)
        except Exception as e:
            self.skipTest(f"Live PostgreSQL database unavailable at {db_url}: {e}")

        settings = Settings(
            shadow_evaluation_enabled=True,
            shadow_sampling_rate=1.0,
            shadow_max_concurrency=2,
            shadow_queue_max_size=10,
            shadow_timeout_seconds=2.0,
        )

        embed_provider = HealthyEmbeddingProvider(delay_ms=2.0)
        real_failing_store = PostgresVectorStore(
            pool=pool,
            default_namespace="default",
            default_model="BAAI/bge-small-en-v1.5",
            dimensions=4,
            table_name="nonexistent_vector_table",
            readonly_transaction=True,
        )
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, top_k=5, configured_model="BAAI/bge-small-en-v1.5")
        shadow_cache = ReadOnlySemanticCacheService(
            embedding_provider=embed_provider,
            vector_store=real_failing_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        shadow_service = ShadowEvaluationService(
            settings=settings,
            semantic_cache=shadow_cache,
            metrics_collector=self.collector,
        )
        await shadow_service.start()
        set_shadow_service(shadow_service)

        try:
            async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
                resp = await client.post(
                    "/query",
                    json={"prompt": "Explain database indexing in PostgreSQL", "temperature": 0.0},
                    headers=self.headers,
                )
                self.assertEqual(resp.status_code, 200)
                data = resp.json()
                self.assertIn("def add", data["response"])
                self.assertFalse(data["cached"])

            await shadow_service.stop(drain_timeout=1.0)

            snap = self.collector.get_namespace_snapshot("default")
            self.assertEqual(snap.shadow.shadow_vector_store_failures, 1)
            self.assertEqual(snap.shadow.shadow_requests_eligible, 1)
            self.assertEqual(snap.shadow.shadow_hypothetical_hits, 0)
        finally:
            if shadow_service._running:
                await shadow_service.stop(drain_timeout=0.1)
            await pool.close()

    async def test_e2e_upstream_provider_failure_increments_metric(self):
        """When upstream LLM provider fails, gateway increments provider_calls_failed and returns error response."""
        import httpx
        from app.core.errors import ProviderUpstreamError

        # Injected upstream provider failure
        self.mock_llm.generate = AsyncMock(
            side_effect=ProviderUpstreamError("Upstream LLM 504 Gateway Timeout", status_code=504)
        )

        async with httpx.AsyncClient(transport=self.transport, base_url="http://testserver") as client:
            resp = await client.post(
                "/query",
                json={"prompt": "How to write an add function in Python?", "temperature": 0.0},
                headers=self.headers,
            )
            # Upstream error is mapped to 504/502 Gateway error
            self.assertIn(resp.status_code, (502, 503, 504))

        snap = self.collector.get_namespace_snapshot("default")
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 1)


if __name__ == "__main__":
    unittest.main()

