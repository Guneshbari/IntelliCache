"""
Comprehensive regression tests for SingleFlight cancellation-safe lifecycle.
Verifies that leaders release in-flight futures on every exit path (success, failure, cancellation),
followers receive controlled outcomes without hanging, and metrics accurately track cancellations
separately from provider failures across tenant namespaces.
"""

import asyncio
import gc
import random
import unittest
import warnings
from unittest.mock import AsyncMock

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.errors import ProviderError, SingleFlightLeaderCancelledError
from app.core.service import IntelliCacheCore, generate_fingerprint
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.metrics.collector import RuntimeMetricsCollector
from app.providers.interfaces import LLMProvider
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorSearchResult
from app.vector_store.interfaces import BaseVectorStore


class AsyncFakeProvider(LLMProvider):
    """Test LLM provider with configurable delays, event hooks, and exceptions."""

    def __init__(self, response_text: str = "Fake generated response"):
        self.response_text = response_text
        self.call_count = 0
        self.entered_event: asyncio.Event | None = None
        self.proceed_event: asyncio.Event | None = None
        self.fail_with: BaseException | None = None

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.call_count += 1
        if self.entered_event is not None:
            self.entered_event.set()
        if self.proceed_event is not None:
            await self.proceed_event.wait()
        if self.fail_with is not None:
            raise self.fail_with
        return GenerationResult(
            text=self.response_text,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=15.0,
            cached=False,
            fingerprint="",
        )

    async def stream(self, request: GenerationRequest):
        raise NotImplementedError()

    async def close(self) -> None:
        pass


class AsyncFakeEmbedder(EmbeddingProvider):
    """Test embedding provider with synchronization events."""

    def __init__(self):
        self.entered_event: asyncio.Event | None = None
        self.proceed_event: asyncio.Event | None = None
        self.fail_with: BaseException | None = None

    async def embed(self, text: str, task: EmbeddingTask | str = EmbeddingTask.QUERY) -> EmbeddingResult:
        if self.entered_event is not None:
            self.entered_event.set()
        if self.proceed_event is not None:
            await self.proceed_event.wait()
        if self.fail_with is not None:
            raise self.fail_with
        return EmbeddingResult(vector=[0.1] * 768, model="nomic-embed-text", dimensions=768)

    async def close(self) -> None:
        pass


class AsyncFakeVectorStore(BaseVectorStore):
    """Test vector store with synchronization events."""

    def __init__(self):
        self.search_entered_event: asyncio.Event | None = None
        self.search_proceed_event: asyncio.Event | None = None
        self.search_fail_with: BaseException | None = None

    async def insert(self, record):
        pass

    async def upsert(self, record):
        pass

    async def delete(self, fingerprint, namespace="default"):
        pass

    async def search(self, query_vector, top_k=5, namespace="default", model=None):
        if self.search_entered_event is not None:
            self.search_entered_event.set()
        if self.search_proceed_event is not None:
            await self.search_proceed_event.wait()
        if self.search_fail_with is not None:
            raise self.search_fail_with
        return []

    async def count(self, namespace=None):
        return 0

    async def clear(self, namespace=None):
        pass

    async def close(self):
        pass


class TestSingleFlightCancellation(unittest.IsolatedAsyncioTestCase):
    """Regression tests for SingleFlight leader cancellation lifecycle and follower protection."""

    async def asyncSetUp(self):
        self.cache = ExactCache(max_entries=100)
        self.coordinator = SingleFlightCoordinator()
        self.collector = RuntimeMetricsCollector()
        self.core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            metrics_collector=self.collector,
        )
        self.req = GenerationRequest(
            model="llama-3-8b",
            messages=[ChatMessage(role="user", content="How do I sort a list in Python?")],
            namespace="tenant_alpha",
        )

    def _flight_key(self, request: GenerationRequest) -> tuple[str, str]:
        fp = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )
        return (request.namespace or "default", fp)

    async def test_leader_cancelled_during_provider_generation(self):
        """Leader is cancelled during provider inference: entry cleaned up, followers notified, no hang."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        # Follower joins leader in flight deterministically
        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        self.assertEqual(self.coordinator.in_flight_count(), 1)

        # Cancel leader
        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        # Follower finishes promptly with SingleFlightLeaderCancelledError
        with self.assertRaises(SingleFlightLeaderCancelledError):
            await asyncio.wait_for(t_follower, timeout=1.0)

        # Coordinator in-flight map is completely clean
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Metrics verification
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.request_and_cache.coalesced_joins, 1)
        self.assertEqual(snap.request_and_cache.coalesced_failures, 1)

    async def test_leader_cancelled_during_embedding(self):
        """Leader is cancelled during semantic query embedding: future released promptly."""
        embedder = AsyncFakeEmbedder()
        embedder.entered_event = asyncio.Event()
        embedder.proceed_event = asyncio.Event()

        vector_store = AsyncFakeVectorStore()
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_cache = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            semantic_cache=sem_cache,
            metrics_collector=self.collector,
        )

        provider = AsyncFakeProvider()

        t_leader = asyncio.create_task(core.execute_query(self.req, provider))
        await embedder.entered_event.wait()

        t_follower = asyncio.create_task(core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        self.assertEqual(self.coordinator.in_flight_count(), 1)

        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        with self.assertRaises(SingleFlightLeaderCancelledError):
            await asyncio.wait_for(t_follower, timeout=1.0)

        self.assertEqual(self.coordinator.in_flight_count(), 0)

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.semantic.embedding_failures, 0)
        self.assertEqual(snap.semantic.semantic_misses, 0)

    async def test_leader_cancelled_during_vector_search(self):
        """Leader is cancelled during vector store nearest neighbor search: future released promptly."""
        embedder = AsyncFakeEmbedder()
        vector_store = AsyncFakeVectorStore()
        vector_store.search_entered_event = asyncio.Event()
        vector_store.search_proceed_event = asyncio.Event()

        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_cache = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            semantic_cache=sem_cache,
            metrics_collector=self.collector,
        )

        provider = AsyncFakeProvider()

        t_leader = asyncio.create_task(core.execute_query(self.req, provider))
        await vector_store.search_entered_event.wait()

        t_follower = asyncio.create_task(core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        self.assertEqual(self.coordinator.in_flight_count(), 1)

        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        with self.assertRaises(SingleFlightLeaderCancelledError):
            await asyncio.wait_for(t_follower, timeout=1.0)

        self.assertEqual(self.coordinator.in_flight_count(), 0)

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.semantic.vector_store_failures, 0)

    async def test_leader_cancelled_during_cache_write(self):
        """Leader provider call succeeds, but is cancelled during cache.set(): followers succeed, provider success retained."""
        write_started = asyncio.Event()
        allow_write = asyncio.Event()

        orig_set = self.cache.set

        async def controlled_set(*args, **kwargs):
            write_started.set()
            await allow_write.wait()
            return await orig_set(*args, **kwargs)

        self.cache.set = controlled_set

        provider = AsyncFakeProvider(response_text="Generated Before Cancel")

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await write_started.wait()

        # Follower joins or awaits leader's completed future
        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))

        # Because leader completed SingleFlight immediately upon provider success,
        # the follower finishes successfully!
        follower_res = await asyncio.wait_for(t_follower, timeout=1.0)
        self.assertEqual(follower_res.text, "Generated Before Cancel")

        # Now cancel leader during cache write
        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Provider success must be preserved, provider failure must remain 0
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.provider_and_savings.provider_calls_succeeded, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.request_and_cache.coalesced_successes, 1)

    async def test_unexpected_exception_during_semantic_lookup(self):
        """Unhandled exception during semantic lookup completes leader future with error and allows future retries."""
        embedder = AsyncFakeEmbedder()
        embedder.fail_with = RuntimeError("Hardware failure during embedding")

        vector_store = AsyncFakeVectorStore()
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_cache = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            semantic_cache=sem_cache,
            metrics_collector=self.collector,
        )

        provider = AsyncFakeProvider(response_text="Recovered after fail open")

        # Because SemanticCacheService catches Exception and fails open,
        # it falls through to normal provider generation!
        res = await core.execute_query(self.req, provider)
        self.assertEqual(res.text, "Recovered after fail open")
        self.assertEqual(self.coordinator.in_flight_count(), 0)

    async def test_new_leader_after_previous_leader_cancellation(self):
        """A subsequent request with identical namespace and fingerprint can become leader after previous cancellation."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader1 = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()
        t_leader1.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader1

        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Second request registers as leader cleanly
        provider.entered_event = None
        provider.proceed_event = None
        provider.response_text = "Second leader victory"

        res2 = await self.core.execute_query(self.req, provider)
        self.assertEqual(res2.text, "Second leader victory")
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Third request hits exact cache
        res3 = await self.core.execute_query(self.req, provider)
        self.assertEqual(res3.text, "Second leader victory")
        self.assertTrue(res3.cached)

    async def test_follower_cancellation_does_not_cancel_leader(self):
        """Cancelling a waiting follower does not cancel the leader or abort provider generation."""
        provider = AsyncFakeProvider(response_text="Leader completes unhindered")
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        # Cancel the follower
        t_follower.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_follower

        # Leader is still active in flight
        self.assertEqual(self.coordinator.in_flight_count(), 1)

        # Unblock leader
        provider.proceed_event.set()
        leader_res = await t_leader
        self.assertEqual(leader_res.text, "Leader completes unhindered")

        # Now in flight is empty
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Metrics check: follower cancellation recorded
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_succeeded, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)

    async def test_same_namespace_requests_still_coalesce(self):
        """Simultaneous identical requests in the same namespace coalesce into a single provider call."""
        provider = AsyncFakeProvider(response_text="Shared Coalesced Output")
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t1 = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        t2 = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        provider.proceed_event.set()
        res1, res2 = await asyncio.gather(t1, t2)

        self.assertEqual(res1.text, "Shared Coalesced Output")
        self.assertEqual(res2.text, "Shared Coalesced Output")
        self.assertEqual(provider.call_count, 1)

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.coalesced_joins, 1)
        self.assertEqual(snap.request_and_cache.coalesced_successes, 1)

    async def test_cross_namespace_requests_remain_isolated(self):
        """Simultaneous identical prompts in different namespaces execute independently without coalescing."""
        provider = AsyncFakeProvider()
        req_a = GenerationRequest(
            model="llama-3-8b",
            messages=[ChatMessage(role="user", content="Tenant isolation test")],
            namespace="team_alpha",
        )
        req_b = GenerationRequest(
            model="llama-3-8b",
            messages=[ChatMessage(role="user", content="Tenant isolation test")],
            namespace="team_beta",
        )

        res_a, res_b = await asyncio.gather(
            self.core.execute_query(req_a, provider),
            self.core.execute_query(req_b, provider),
        )

        self.assertEqual(provider.call_count, 2)
        snap_a = self.collector.get_namespace_snapshot("team_alpha")
        snap_b = self.collector.get_namespace_snapshot("team_beta")

        self.assertEqual(snap_a.request_and_cache.coalesced_joins, 0)
        self.assertEqual(snap_b.request_and_cache.coalesced_joins, 0)
        self.assertEqual(self.coordinator.in_flight_count(), 0)

    async def test_cancellation_metrics_are_separate(self):
        """Cancelled requests increment request_cancellations and never provider_calls_failed."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()
        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.provider_and_savings.provider_calls_attempted, 1)

    async def test_no_abandoned_singleflight_entries_on_provider_error(self):
        """When provider fails with an ordinary ProviderError, in-flight entry is removed and follower notified."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()
        provider.fail_with = ProviderError("Upstream timeout 504")

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        provider.proceed_event.set()

        with self.assertRaises(ProviderError):
            await t_leader

        with self.assertRaises(ProviderError):
            await t_follower

        self.assertEqual(self.coordinator.in_flight_count(), 0)
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 1)
        self.assertEqual(snap.request_and_cache.request_cancellations, 0)

    async def test_repeated_cancellation_during_cleanup(self):
        """Repeated cancellation of leader task does not interrupt SingleFlight coordinator cleanup."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        # Follower joins in flight
        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=1)

        # Trigger repeated cancellations on leader task
        t_leader.cancel()
        t_leader.cancel()
        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        with self.assertRaises(SingleFlightLeaderCancelledError):
            await asyncio.wait_for(t_follower, timeout=1.0)

        self.assertEqual(self.coordinator.in_flight_count(), 0)

    async def test_all_followers_cancelled_leader_completes(self):
        """
        Multiple followers cancel while leader is generating.
        Leader completes normally, caches the result, request_cancellations equals the number of followers,
        and subsequent request gets a cache hit.
        """
        provider = AsyncFakeProvider(response_text="Leader Victory")
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        num_followers = 3
        t_followers = [
            asyncio.create_task(self.core.execute_query(self.req, provider))
            for _ in range(num_followers)
        ]
        await self.coordinator.wait_for_waiters(self._flight_key(self.req), count=num_followers)

        # Cancel all followers
        for tf in t_followers:
            tf.cancel()

        for tf in t_followers:
            with self.assertRaises(asyncio.CancelledError):
                await tf

        # Leader should still be running and in flight
        self.assertEqual(self.coordinator.in_flight_count(), 1)

        # Allow leader to finish
        provider.proceed_event.set()
        leader_res = await t_leader
        self.assertEqual(leader_res.text, "Leader Victory")
        self.assertFalse(leader_res.cached)

        # Coordinator is clean
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Verify metrics
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, num_followers)
        self.assertEqual(snap.provider_and_savings.provider_calls_succeeded, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.request_and_cache.coalesced_joins, num_followers)

        # Subsequent identical request gets exact cache hit
        subsequent_res = await self.core.execute_query(self.req, provider)
        self.assertEqual(subsequent_res.text, "Leader Victory")
        self.assertTrue(subsequent_res.cached)

    async def test_unretrieved_future_exception_no_warning(self):
        """
        When a leader fails with 0 waiting followers, no 'Future exception was never retrieved'
        warning or unhandled exception is emitted.
        """
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()
        provider.fail_with = RuntimeError("Simulated unhandled provider failure")

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")

            t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
            await provider.entered_event.wait()
            provider.proceed_event.set()

            with self.assertRaises(RuntimeError):
                await t_leader

            # Force garbage collection to trigger any __del__ warnings on abandoned futures
            gc.collect()

            self.assertEqual(self.coordinator.in_flight_count(), 0)
            for w in recorded_warnings:
                self.assertNotIn("Future exception was never retrieved", str(w.message))

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 1)
        self.assertEqual(snap.request_and_cache.request_cancellations, 0)

    async def test_genuine_concurrent_cancellation_stress_test(self):
        """
        Randomized concurrency stress test across multiple keys and tenant namespaces
        with concurrent cancellations. Ensures zero lingering in-flight entries and consistent metrics.
        """
        namespaces = ["stress_tenant_1", "stress_tenant_2", "stress_tenant_3"]
        prompts = [
            "What is asyncio?",
            "Explain single-flight pattern",
            "How does vector search work?",
        ]

        class StressProvider(LLMProvider):
            def __init__(self):
                self.call_count = 0

            async def generate(self, request: GenerationRequest) -> GenerationResult:
                self.call_count += 1
                await asyncio.sleep(0.01)
                return GenerationResult(
                    text=f"Answer to: {request.messages[0].content}",
                    model=request.model,
                    usage=UsageMetrics(prompt_tokens=5, completion_tokens=10, total_tokens=15),
                    latency_ms=10.0,
                    cached=False,
                    fingerprint="",
                )

            async def stream(self, request: GenerationRequest):
                raise NotImplementedError()

            async def close(self):
                pass

        stress_provider = StressProvider()
        num_tasks = 40
        tasks = []
        task_info = []

        for i in range(num_tasks):
            ns = namespaces[i % len(namespaces)]
            prompt = prompts[i % len(prompts)]
            req = GenerationRequest(
                model="llama-3-8b",
                messages=[ChatMessage(role="user", content=prompt)],
                namespace=ns,
            )
            t = asyncio.create_task(self.core.execute_query(req, stress_provider))
            tasks.append(t)
            task_info.append((t, ns))

        # Cancel a subset of tasks at micro-delays
        await asyncio.sleep(0.002)
        to_cancel_indices = random.sample(range(num_tasks), k=15)
        for idx in to_cancel_indices:
            task_info[idx][0].cancel()

        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Verify all in-flight entries are completely cleared
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Count cancellations and successes
        cancelled_count = 0
        success_count = 0
        for r in results:
            if isinstance(r, asyncio.CancelledError):
                cancelled_count += 1
            elif isinstance(r, GenerationResult):
                success_count += 1

        # Check total metric cancellations across the three namespaces
        total_cancellations_recorded = sum(
            self.collector.get_namespace_snapshot(ns).request_and_cache.request_cancellations
            for ns in namespaces
        )
        self.assertEqual(total_cancellations_recorded, cancelled_count)

    async def test_cancellation_during_exact_cache_lookup(self):
        """Cancellation occurring during exact cache read records request_cancellations exactly once."""
        lookup_started = asyncio.Event()
        allow_lookup = asyncio.Event()

        orig_get = self.cache.get

        async def slow_get(*args, **kwargs):
            lookup_started.set()
            await allow_lookup.wait()
            return await orig_get(*args, **kwargs)

        self.cache.get = slow_get
        provider = AsyncFakeProvider()

        t = asyncio.create_task(self.core.execute_query(self.req, provider))
        await lookup_started.wait()

        # Cancel while awaiting cache.get
        t.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_attempted, 0)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(self.coordinator.in_flight_count(), 0)

    async def test_cancellation_during_bypass_miss_recording(self):
        """Cancellation occurring during cache.record_miss in bypass mode records cancellation exactly once."""
        miss_started = asyncio.Event()
        allow_miss = asyncio.Event()

        orig_miss = self.cache.record_miss

        async def slow_miss(*args, **kwargs):
            miss_started.set()
            await allow_miss.wait()
            return await orig_miss(*args, **kwargs)

        self.cache.record_miss = slow_miss
        provider = AsyncFakeProvider()

        bypass_req = GenerationRequest(
            model="llama-3-8b",
            messages=[ChatMessage(role="user", content="Bypass cancellation test")],
            namespace="tenant_alpha",
            bypass_cache=True,
        )

        t = asyncio.create_task(self.core.execute_query(bypass_req, provider))
        await miss_started.wait()

        # Cancel while awaiting cache.record_miss
        t.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(self.coordinator.in_flight_count(), 0)

    async def test_cancellation_during_streaming_cache_lookup(self):
        """Cancellation occurring during streaming cache read records request_cancellations exactly once."""
        lookup_started = asyncio.Event()
        allow_lookup = asyncio.Event()

        orig_get = self.cache.get

        async def slow_get(*args, **kwargs):
            lookup_started.set()
            await allow_lookup.wait()
            return await orig_get(*args, **kwargs)

        self.cache.get = slow_get
        provider = AsyncFakeProvider()

        async def consume_stream():
            async for _ in self.core.execute_stream(self.req, provider):
                pass

        t = asyncio.create_task(consume_stream())
        await lookup_started.wait()

        t.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t

        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.request_and_cache.request_cancellations, 1)
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)

    async def test_flight_released_before_semantic_indexing(self):
        """
        Issue 3: Leader releases SingleFlight key immediately after exact cache persistence,
        BEFORE optional/slow semantic indexing, so subsequent requests hit exact cache without waiting.
        """
        indexing_started = asyncio.Event()
        allow_indexing = asyncio.Event()

        class SlowSemanticCache(SemanticCacheService):
            async def index(self, request, result):
                indexing_started.set()
                await allow_indexing.wait()

        embedder = AsyncFakeEmbedder()
        vector_store = AsyncFakeVectorStore()
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_cache = SlowSemanticCache(
            embedding_provider=embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            semantic_cache=sem_cache,
            metrics_collector=self.collector,
        )

        provider = AsyncFakeProvider(response_text="Released before indexing")

        t_leader = asyncio.create_task(core.execute_query(self.req, provider))
        await indexing_started.wait()

        # At this point, exact cache write is done and leader is blocked in semantic_cache.index
        # Verify single-flight key is ALREADY released!
        flight_key = self._flight_key(self.req)
        self.assertFalse(self.coordinator.is_in_flight(flight_key))
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # A new identical request arrives NOW while leader is still indexing:
        # It must NOT coalesce and must hit the exact cache immediately!
        subsequent = await core.execute_query(self.req, provider)
        self.assertEqual(subsequent.text, "Released before indexing")
        self.assertTrue(subsequent.cached)
        self.assertEqual(subsequent.cache_type, "exact")

        # Unblock indexing and let leader finish
        allow_indexing.set()
        leader_res = await t_leader
        self.assertEqual(leader_res.text, "Released before indexing")

    async def test_cache_write_failure_retains_flight_until_semantic_indexing_finishes(self):
        """
        Step 11.2.2: When exact-cache persistence fails, SingleFlight key MUST be retained
        while semantic indexing is in flight. A concurrent identical request joins the existing flight
        and receives the generated answer without triggering duplicate upstream provider generation.
        """
        indexing_started = asyncio.Event()
        allow_indexing = asyncio.Event()

        class SlowSemanticCache(SemanticCacheService):
            async def index(self, request, result):
                indexing_started.set()
                await allow_indexing.wait()

        # Rig cache.set to fail with an exception
        async def failing_set(*args, **kwargs):
            raise RuntimeError("Simulated exact cache write failure")

        self.cache.set = failing_set

        embedder = AsyncFakeEmbedder()
        vector_store = AsyncFakeVectorStore()
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_cache = SlowSemanticCache(
            embedding_provider=embedder,
            vector_store=vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.coordinator,
            cache_enabled=True,
            semantic_cache=sem_cache,
            metrics_collector=self.collector,
        )

        provider = AsyncFakeProvider(response_text="Single Generation For Both")

        # Request A starts as leader
        t_req_a = asyncio.create_task(core.execute_query(self.req, provider))
        await indexing_started.wait()

        # At this point:
        # 1. Provider generation completed for Request A.
        # 2. Exact-cache write failed.
        # 3. Request A is blocked in semantic_cache.index.
        # 4. SingleFlight key MUST STILL BE REGISTERED in flight!
        flight_key = self._flight_key(self.req)
        self.assertTrue(self.coordinator.is_in_flight(flight_key))
        self.assertEqual(self.coordinator.in_flight_count(), 1)

        # Request B sends an identical query NOW while Request A is indexing:
        # Exact cache lookup misses because persistence failed.
        # But SingleFlight key is retained, so Request B joins the existing flight!
        t_req_b = asyncio.create_task(core.execute_query(self.req, provider))

        # Request B immediately resolves via the completed shared future without waiting for semantic indexing!
        res_b = await asyncio.wait_for(t_req_b, timeout=1.0)
        self.assertEqual(res_b.text, "Single Generation For Both")
        self.assertTrue(res_b.cached)

        # Verify NO duplicate provider call was made!
        self.assertEqual(provider.call_count, 1)

        # Unblock Request A's semantic indexing
        allow_indexing.set()
        res_a = await t_req_a
        self.assertEqual(res_a.text, "Single Generation For Both")

        # Now SingleFlight key is completely released
        self.assertFalse(self.coordinator.is_in_flight(flight_key))
        self.assertEqual(self.coordinator.in_flight_count(), 0)

        # Verify metrics
        snap = self.collector.get_namespace_snapshot("tenant_alpha")
        self.assertEqual(snap.provider_and_savings.provider_calls_succeeded, 1)
        self.assertEqual(snap.request_and_cache.coalesced_joins, 1)
        self.assertEqual(snap.request_and_cache.coalesced_successes, 1)
        self.assertEqual(snap.request_and_cache.cache_write_failures, 1)

    async def test_waiter_count_semantics_active_vs_registered(self):
        """
        Verifies that waiter_count() accurately reflects currently active/waiting followers,
        decrementing when followers cancel, succeed, or fail, while registered_waiter_count()
        tracks the cumulative registrations for the flight.
        """
        provider = AsyncFakeProvider(response_text="Waiter Semantics Verification")
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        flight_key = self._flight_key(self.req)
        self.assertEqual(self.coordinator.waiter_count(flight_key), 0)
        self.assertEqual(self.coordinator.registered_waiter_count(flight_key), 0)

        # Follower 1 joins
        t_f1 = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(flight_key, count=1)
        self.assertEqual(self.coordinator.waiter_count(flight_key), 1)
        self.assertEqual(self.coordinator.registered_waiter_count(flight_key), 1)

        # Follower 2 joins
        t_f2 = asyncio.create_task(self.core.execute_query(self.req, provider))
        await self.coordinator.wait_for_waiters(flight_key, count=2)
        self.assertEqual(self.coordinator.waiter_count(flight_key), 2)
        self.assertEqual(self.coordinator.registered_waiter_count(flight_key), 2)

        # Follower 1 cancels
        t_f1.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t_f1

        # Active waiter count must decrement to 1, while registered remains 2
        self.assertEqual(self.coordinator.waiter_count(flight_key), 1)
        self.assertEqual(self.coordinator.registered_waiter_count(flight_key), 2)

        # Allow leader to finish
        provider.proceed_event.set()
        await t_leader
        await t_f2

        # Flight released -> all cleared
        self.assertEqual(self.coordinator.waiter_count(flight_key), 0)
        self.assertEqual(self.coordinator.registered_waiter_count(flight_key), 0)

    async def test_waiter_count_race_across_flight_generations(self):
        """
        Step 11.2.2 follow-up: When an old flight is released and a new flight with the same key
        registers a follower, an old follower resuming its wait_flight() finally block must NOT
        decrement the new flight's active waiter count.
        """
        key = self._flight_key(self.req)

        # 1. Flight A registers as leader
        is_leader_a, fut_a = await self.coordinator.register_or_wait(key)
        self.assertTrue(is_leader_a)

        # Follower A registers
        is_leader_fa, fut_fa = await self.coordinator.register_or_wait(key)
        self.assertFalse(is_leader_fa)
        self.assertEqual(self.coordinator.waiter_count(key), 1)

        delayed_follower_res = None
        follower_started_event = asyncio.Event()
        follower_finished_event = asyncio.Event()

        async def run_follower_a():
            nonlocal delayed_follower_res
            follower_started_event.set()
            delayed_follower_res = await self.coordinator.wait_flight(fut_fa, key=key)
            follower_finished_event.set()

        t_fa = asyncio.create_task(run_follower_a())
        await follower_started_event.wait()
        # Scheduler yield to guarantee Follower A enters wait_flight before Flight A completes
        await asyncio.sleep(0)

        # 2. Flight A completes and releases its key
        await self.coordinator.complete_success(key, "result_a", expected_future=fut_a, release=True)
        self.assertEqual(self.coordinator.waiter_count(key), 0)

        # 3. New Flight B immediately registers with the SAME key!
        is_leader_b, fut_b = await self.coordinator.register_or_wait(key)
        self.assertTrue(is_leader_b)
        self.assertIsNot(fut_b, fut_a)

        # Follower B joins Flight B
        is_leader_fb, fut_fb = await self.coordinator.register_or_wait(key)
        self.assertFalse(is_leader_fb)
        self.assertEqual(self.coordinator.waiter_count(key), 1)

        # 4. Follower A resumes and completes its wait_flight() finally block
        await follower_finished_event.wait()
        await t_fa
        self.assertEqual(delayed_follower_res, "result_a")

        # Flight B active waiter count must remain 1 (not decremented by Flight A's follower!)
        self.assertEqual(self.coordinator.waiter_count(key), 1)

        # Clean up Flight B
        await self.coordinator.complete_success(key, "result_b", expected_future=fut_b, release=True)
        self.assertEqual(self.coordinator.waiter_count(key), 0)


if __name__ == "__main__":
    unittest.main()
