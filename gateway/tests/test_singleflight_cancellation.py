"""
Comprehensive regression tests for SingleFlight cancellation-safe lifecycle.
Verifies that leaders release in-flight futures on every exit path (success, failure, cancellation),
followers receive controlled outcomes without hanging, and metrics accurately track cancellations
separately from provider failures across tenant namespaces.
"""

import asyncio
import unittest
from unittest.mock import AsyncMock

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.errors import ProviderError, SingleFlightLeaderCancelledError
from app.core.service import IntelliCacheCore
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

    async def test_leader_cancelled_during_provider_generation(self):
        """Leader is cancelled during provider inference: entry cleaned up, followers notified, no hang."""
        provider = AsyncFakeProvider()
        provider.entered_event = asyncio.Event()
        provider.proceed_event = asyncio.Event()

        t_leader = asyncio.create_task(self.core.execute_query(self.req, provider))
        await provider.entered_event.wait()

        # Follower joins leader in flight
        t_follower = asyncio.create_task(self.core.execute_query(self.req, provider))
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

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
        await asyncio.sleep(0.01)

        # Trigger repeated cancellations on leader task
        t_leader.cancel()
        t_leader.cancel()
        t_leader.cancel()

        with self.assertRaises(asyncio.CancelledError):
            await t_leader

        with self.assertRaises(SingleFlightLeaderCancelledError):
            await asyncio.wait_for(t_follower, timeout=1.0)

        self.assertEqual(self.coordinator.in_flight_count(), 0)


if __name__ == "__main__":
    unittest.main()
