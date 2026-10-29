"""
Unit and architecture boundary tests for IntelliCache Core.
Tests orchestration, exact caching, single-flight coordination, and metric consistency (PRE-01, PRE-02)
using a pure FakeProvider without network or Groq dependencies.
"""

import ast
import asyncio
from pathlib import Path
import sys
import unittest
from typing import AsyncGenerator, List, Optional
import warnings

from app.cache.exact_cache import ExactCache
from app.core.contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from app.core.errors import (
    IntelliCacheError,
    ProviderAuthenticationError,
    ProviderError,
    ProviderRateLimitError,
)
from app.core.fingerprinting import generate_fingerprint, normalize_text
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.providers.interfaces import LLMProvider


class FakeProvider(LLMProvider):
    """Deterministic in-memory fake LLM provider for unit testing core orchestration."""

    def __init__(self, response_text: str = "Fake generated response"):
        self.response_text = response_text
        self.call_count = 0
        self.recorded_requests: List[GenerationRequest] = []
        self.closed = False
        self.fail_with: Optional[Exception] = None
        self.entered_event: Optional[asyncio.Event] = None
        self.proceed_event: Optional[asyncio.Event] = None

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.call_count += 1
        self.recorded_requests.append(request)
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
            latency_ms=12.5,
        )

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        self.call_count += 1
        self.recorded_requests.append(request)
        if self.fail_with is not None:
            raise self.fail_with
        yield StreamChunk(delta="Fake ", done=False, model=request.model)
        yield StreamChunk(delta="stream", done=False, model=request.model)
        yield StreamChunk(
            delta="",
            done=True,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=15.0,
        )

    async def close(self) -> None:
        self.closed = True


class TestArchitectureBoundaries(unittest.TestCase):
    """Verifies that core modules remain strictly independent of FastAPI, Starlette, and Groq SDK."""

    def test_core_ast_imports_have_no_fastapi_or_groq(self):
        core_dir = Path(__file__).resolve().parent.parent / "app" / "core"
        forbidden_roots = {"fastapi", "starlette", "groq"}

        for py_file in core_dir.glob("*.py"):
            with open(py_file, "r", encoding="utf-8") as f:
                tree = ast.parse(f.read(), filename=str(py_file))

            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        root_mod = alias.name.split(".")[0]
                        self.assertNotIn(
                            root_mod,
                            forbidden_roots,
                            f"Module {py_file.name} directly imports forbidden dependency '{root_mod}'",
                        )
                elif isinstance(node, ast.ImportFrom):
                    if node.module:
                        root_mod = node.module.split(".")[0]
                        self.assertNotIn(
                            root_mod,
                            forbidden_roots,
                            f"Module {py_file.name} imports from forbidden dependency '{root_mod}'",
                        )


class TestIntelliCacheCoreOrchestration(unittest.IsolatedAsyncioTestCase):
    """Tests core exact caching, single-flight coordination, and metric consistency."""

    def setUp(self):
        self.cache = ExactCache(max_entries=100, ttl_seconds=3600)
        self.single_flight = SingleFlightCoordinator()
        self.core = IntelliCacheCore(
            cache=self.cache,
            single_flight=self.single_flight,
            cache_enabled=True,
        )

    async def test_cache_miss_then_hit(self):
        provider = FakeProvider(response_text="Hello World")
        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="What is 2+2?")],
            temperature=0.7,
        )

        # First execution -> Cache miss
        res1 = await self.core.execute_query(req, provider)
        self.assertEqual(res1.text, "Hello World")
        self.assertFalse(res1.cached)
        self.assertEqual(provider.call_count, 1)

        # Second execution -> Exact cache hit (0 provider calls)
        res2 = await self.core.execute_query(req, provider)
        self.assertEqual(res2.text, "Hello World")
        self.assertTrue(res2.cached)
        self.assertEqual(res2.cache_hit_count, 1)
        self.assertEqual(provider.call_count, 1)

        stats = await self.cache.stats()
        self.assertEqual(stats.hits, 1)
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.total_requests, 2)

    async def test_provider_metadata_populated_on_cache_entry(self):
        """Verify that CacheEntry created by core stores the request provider."""
        provider = FakeProvider(response_text="Provider specific answer")
        req = GenerationRequest(
            model="custom-engine",
            messages=[ChatMessage(role="user", content="Test provider metadata")],
            provider="anthropic",
            namespace="provider_ns",
        )

        res = await self.core.execute_query(req, provider)
        self.assertEqual(res.text, "Provider specific answer")

        entry = await self.cache.get(res.fingerprint, namespace="provider_ns")
        self.assertIsNotNone(entry)
        self.assertEqual(entry.provider, "anthropic")
        self.assertEqual(entry.model, "custom-engine")

    async def test_strict_bypass_semantics(self):
        provider = FakeProvider(response_text="Live Answer")
        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Bypass test")],
            bypass_cache=True,
        )

        res = await self.core.execute_query(req, provider)
        self.assertEqual(res.text, "Live Answer")
        self.assertFalse(res.cached)
        self.assertEqual(provider.call_count, 1)

        # Bypass query must NOT populate the cache
        stats = await self.cache.stats()
        self.assertEqual(stats.total_entries, 0)
        self.assertEqual(stats.misses, 1)

    async def test_pre01_coalesced_metrics_on_leader_success(self):
        """PRE-01: In-flight followers increment coalesced_requests; total_requests counts every request."""
        leader_entered = asyncio.Event()
        leader_proceed = asyncio.Event()

        provider = FakeProvider(response_text="Coalesced Output")
        provider.entered_event = leader_entered
        provider.proceed_event = leader_proceed

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Concurrent Question")],
        )

        leader_task = asyncio.create_task(self.core.execute_query(req, provider))
        await leader_entered.wait()

        # Launch 3 follower requests while leader is in-flight
        f1 = asyncio.create_task(self.core.execute_query(req, provider))
        f2 = asyncio.create_task(self.core.execute_query(req, provider))
        f3 = asyncio.create_task(self.core.execute_query(req, provider))

        # Yield control so followers register with single-flight
        await asyncio.sleep(0.01)
        leader_proceed.set()

        results = await asyncio.gather(leader_task, f1, f2, f3)
        self.assertEqual(len(results), 4)
        for r in results:
            self.assertEqual(r.text, "Coalesced Output")

        # 1 provider call for all 4 requests
        self.assertEqual(provider.call_count, 1)

        stats = await self.cache.stats()
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.coalesced_requests, 3)
        self.assertEqual(stats.hits, 0)
        self.assertEqual(stats.total_requests, 4)
        self.assertEqual(stats.hit_rate, 0.75)  # 3 reused out of 4 total

    async def test_pre01_coalesced_metrics_on_leader_failure(self):
        """PRE-01: When leader fails, followers still count in coalesced_requests; total_requests is consistent."""
        leader_entered = asyncio.Event()
        leader_proceed = asyncio.Event()

        provider = FakeProvider()
        provider.fail_with = ProviderError("Upstream API broke")
        provider.entered_event = leader_entered
        provider.proceed_event = leader_proceed

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Will Fail")],
        )

        leader_task = asyncio.create_task(self.core.execute_query(req, provider))
        await leader_entered.wait()

        f1 = asyncio.create_task(self.core.execute_query(req, provider))
        f2 = asyncio.create_task(self.core.execute_query(req, provider))

        await asyncio.sleep(0.01)
        leader_proceed.set()

        results = await asyncio.gather(leader_task, f1, f2, return_exceptions=True)
        self.assertEqual(len(results), 3)
        for r in results:
            self.assertIsInstance(r, ProviderError)

        stats = await self.cache.stats()
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.coalesced_requests, 2)
        # 1 leader miss + 2 followers joined = 3 total requests accounted for!
        self.assertEqual(stats.total_requests, 3)
        self.assertEqual(stats.total_entries, 0)  # Failure was not cached

    async def test_pre01_follower_cancellation_does_not_abort_leader(self):
        """PRE-01: Follower cancellation does not abort leader and preserves metric consistency."""
        leader_entered = asyncio.Event()
        leader_proceed = asyncio.Event()

        provider = FakeProvider(response_text="Leader Victory")
        provider.entered_event = leader_entered
        provider.proceed_event = leader_proceed

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Cancel follower test")],
        )

        leader_task = asyncio.create_task(self.core.execute_query(req, provider))
        await leader_entered.wait()

        follower_task = asyncio.create_task(self.core.execute_query(req, provider))
        await asyncio.sleep(0.01)

        # Cancel follower
        follower_task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await follower_task

        # Leader completes normally
        leader_proceed.set()
        leader_res = await leader_task
        self.assertEqual(leader_res.text, "Leader Victory")

        # Entry was cached by leader
        stats = await self.cache.stats()
        self.assertEqual(stats.total_entries, 1)
        self.assertEqual(stats.misses, 1)
        self.assertEqual(stats.coalesced_requests, 1)
        self.assertEqual(stats.total_requests, 2)

    async def test_pre02_model_identity_consistency(self):
        """PRE-02: Fingerprint generation and provider request use the exact same model identity string."""
        provider = FakeProvider(response_text="Model Casing Test")
        mixed_casing_model = "Qwen/Qwen3.8-27B-Instruct"

        req = GenerationRequest(
            model=mixed_casing_model,
            messages=[ChatMessage(role="user", content="echo")],
            temperature=0.7,
        )

        res = await self.core.execute_query(req, provider)
        self.assertEqual(provider.call_count, 1)

        # Check recorded request on provider
        recorded_model = provider.recorded_requests[0].model
        self.assertEqual(recorded_model, mixed_casing_model)

        # Expected fingerprint with the exact same model string
        expected_fp = generate_fingerprint(
            messages=req.messages,
            model=mixed_casing_model,
            temperature=0.7,
            provider=req.provider,
        )
        self.assertEqual(res.fingerprint, expected_fp)

        # Different casing must produce distinct fingerprint
        lower_fp = generate_fingerprint(
            messages=req.messages,
            model=mixed_casing_model.lower(),
            temperature=0.7,
            provider=req.provider,
        )
        self.assertNotEqual(res.fingerprint, lower_fp)

    async def test_single_flight_leader_failure_zero_followers(self):
        """Leader failure with 0 followers must not emit unretrieved future warning."""
        provider = FakeProvider()
        provider.fail_with = ProviderError("Zero follower crash")

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="alone")],
        )

        with warnings.catch_warnings(record=True) as recorded_warnings:
            warnings.simplefilter("always")
            with self.assertRaises(ProviderError):
                await self.core.execute_query(req, provider)

            await asyncio.sleep(0.01)
            future_warnings = [
                w for w in recorded_warnings
                if "never retrieved" in str(w.message).lower()
            ]
            self.assertEqual(len(future_warnings), 0)

    async def test_retry_after_leader_failure(self):
        """After leader failure, in-flight state is cleaned up and subsequent requests retry."""
        provider = FakeProvider()
        provider.fail_with = ProviderError("Temporary outage")

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="retry test")],
        )

        with self.assertRaises(ProviderError):
            await self.core.execute_query(req, provider)

        # Retry with recovered provider
        provider.fail_with = None
        provider.response_text = "Recovered response"

        retry_res = await self.core.execute_query(req, provider)
        self.assertEqual(retry_res.text, "Recovered response")
        self.assertFalse(retry_res.cached)
        self.assertEqual(provider.call_count, 2)
