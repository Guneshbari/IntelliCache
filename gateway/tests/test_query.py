"""
Hardened unit and integration tests for query execution, exact-match caching,
lazy provider client lifecycle, concurrency coordination, metrics, and security.
"""

import asyncio
import json
import logging
import os
import unittest
from unittest.mock import AsyncMock, patch
import warnings

from fastapi.testclient import TestClient
import groq
import httpx

from app.config import get_settings, reload_settings
from app.main import app
from app.routes.query import (
    CacheEntry,
    QueryCache,
    QueryRequest,
    UsageMetrics,
    get_groq_client_factory,
    get_query_cache,
    get_single_flight_coordinator,
)


class MockChoice:
    def __init__(self, content: str):
        self.message = type("Msg", (), {"content": content})


class MockUsage:
    def __init__(self, prompt_tokens: int = 10, completion_tokens: int = 20, total_tokens: int = 30):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = total_tokens


class MockCompletion:
    def __init__(self, content: str = "Test response from Groq"):
        self.choices = [MockChoice(content)]
        self.usage = MockUsage()


class MockChunk:
    def __init__(self, content: str):
        self.choices = [type("Choice", (), {"delta": type("Delta", (), {"content": content})})]


async def mock_chunk_stream():
    for piece in ["Hello", " ", "from", " ", "stream!"]:
        yield MockChunk(piece)


async def mock_failing_chunk_stream():
    yield MockChunk("Initial piece")
    raise RuntimeError("Simulated mid-stream failure")


class TestQueryRoute(unittest.TestCase):
    """Test suite for hardened query route logic, exact caching, and single-flight coordination."""

    def setUp(self):
        self.client = TestClient(app)
        reload_settings()
        self.cache = get_query_cache()
        asyncio.run(self.cache.clear())
        self.single_flight = get_single_flight_coordinator()

        # Set up mock Groq client with close tracker
        self.mock_groq = AsyncMock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Mocked LLM Response")
        self.mock_groq.close = AsyncMock()
        self.factory_call_count = 0

        def client_factory():
            self.factory_call_count += 1
            return self.mock_groq

        app.dependency_overrides[get_groq_client_factory] = lambda: client_factory

    def tearDown(self):
        app.dependency_overrides.clear()
        asyncio.run(self.cache.clear())

    # -----------------------------------------------------------------------
    # 1. Exact-Cache Correctness & Parameter Isolation Tests (TASK-01 / FIX-01)
    # -----------------------------------------------------------------------

    def test_query_validation_requires_prompt(self):
        """Verify request without prompt, query, or messages returns 422."""
        response = self.client.post("/query", json={})
        self.assertEqual(response.status_code, 422)

    def test_query_execution_cache_miss_then_hit(self):
        """Test initial query misses cache, calls Groq, and subsequent identical query hits cache."""
        payload = {"prompt": "What is exact caching?"}

        # First execution -> Cache Miss
        resp1 = self.client.post("/query", json=payload)
        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertFalse(data1["cached"])
        self.assertEqual(data1["response"], "Mocked LLM Response")
        self.assertEqual(data1["usage"]["total_tokens"], 30)
        self.mock_groq.chat.completions.create.assert_called_once()

        # Second execution -> Cache Hit
        self.mock_groq.chat.completions.create.reset_mock()
        resp2 = self.client.post("/query", json=payload)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["cached"])
        self.assertEqual(data2["response"], "Mocked LLM Response")
        self.assertEqual(data2["cache_hit_count"], 1)
        self.assertEqual(data2["fingerprint"], data1["fingerprint"])
        self.mock_groq.chat.completions.create.assert_not_called()

    def test_outer_whitespace_and_crlf_normalization(self):
        """
        Verify explicit equivalence rule: CRLF is normalized to LF and outer padding
        spaces are trimmed, while internal structure is preserved.
        """
        self.client.post("/query", json={"prompt": "Explain LLM quantization."})

        # Query with CRLF and outer padding
        padded_query = {"prompt": "\r\n  Explain LLM quantization.  \r\n"}
        resp = self.client.post("/query", json=padded_query)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["cached"])

    def test_code_indentation_differences_do_not_collide(self):
        """
        Verify conservative normalization preserves code indentation.
        Code with 4-space indent vs 2-space indent must NOT share cache entries.
        """
        code_4_spaces = "def add(a, b):\n    return a + b"
        code_2_spaces = "def add(a, b):\n  return a + b"

        resp1 = self.client.post("/query", json={"prompt": code_4_spaces})
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])

        resp2 = self.client.post("/query", json={"prompt": code_2_spaces})
        self.assertEqual(resp2.status_code, 200)
        # Must be a separate cache miss because indentation differs!
        self.assertFalse(resp2.json()["cached"])
        self.assertNotEqual(resp1.json()["fingerprint"], resp2.json()["fingerprint"])

    def test_temperature_canonicalization_and_isolation(self):
        """
        TASK-01 / FIX-01: Verify temperature is sent upstream without lossy rounding,
        and requests with different decimal values do NOT collide in fingerprint.
        """
        # Two fine-grained temperatures that would collide if rounded to 4 decimals
        t1 = 0.70001
        t2 = 0.70004
        resp1 = self.client.post("/query", json={"prompt": "Precision test", "temperature": t1})
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])

        resp2 = self.client.post("/query", json={"prompt": "Precision test", "temperature": t2})
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(resp2.json()["cached"])
        self.assertNotEqual(resp1.json()["fingerprint"], resp2.json()["fingerprint"])

        # Default temperature resolution consistency
        # Omitted temperature vs explicit default (0.7) must produce same fingerprint
        resp_omit = self.client.post("/query", json={"prompt": "Default temp test"})
        resp_explicit = self.client.post("/query", json={"prompt": "Default temp test", "temperature": 0.7})
        self.assertEqual(resp_omit.json()["fingerprint"], resp_explicit.json()["fingerprint"])
        self.assertTrue(resp_explicit.json()["cached"])

    def test_parameter_isolation_max_tokens(self):
        """Verify requests with different max_tokens do NOT share cache entries."""
        payload_short = {"prompt": "Summarize Paxos", "max_tokens": 50}
        payload_long = {"prompt": "Summarize Paxos", "max_tokens": 1000}

        resp1 = self.client.post("/query", json=payload_short)
        self.assertEqual(resp1.status_code, 200)

        resp2 = self.client.post("/query", json=payload_long)
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(resp2.json()["cached"])
        self.assertNotEqual(resp1.json()["fingerprint"], resp2.json()["fingerprint"])

    def test_parameter_isolation_model(self):
        """Verify requests with different models do NOT share cache entries."""
        payload_m1 = {"prompt": "What is Python?", "model": "qwen/qwen3.8-27b"}
        payload_m2 = {"prompt": "What is Python?", "model": "llama-3.3-70b-versatile"}

        resp1 = self.client.post("/query", json=payload_m1)
        resp2 = self.client.post("/query", json=payload_m2)

        self.assertFalse(resp1.json()["cached"])
        self.assertFalse(resp2.json()["cached"])
        self.assertNotEqual(resp1.json()["fingerprint"], resp2.json()["fingerprint"])

    def test_message_ordering_and_role_isolation(self):
        """Verify role differences and message sequence ordering produce distinct fingerprints."""
        payload_user = {"messages": [{"role": "user", "content": "Help me"}]}
        payload_system = {"messages": [{"role": "system", "content": "Help me"}]}

        r_user = self.client.post("/query", json=payload_user)
        r_sys = self.client.post("/query", json=payload_system)
        self.assertNotEqual(r_user.json()["fingerprint"], r_sys.json()["fingerprint"])

        seq_1 = {"messages": [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]}
        seq_2 = {"messages": [{"role": "assistant", "content": "B"}, {"role": "user", "content": "A"}]}
        r_seq1 = self.client.post("/query", json=seq_1)
        r_seq2 = self.client.post("/query", json=seq_2)
        self.assertNotEqual(r_seq1.json()["fingerprint"], r_seq2.json()["fingerprint"])

    # -----------------------------------------------------------------------
    # 2. Lazy Client Creation & Lifecycle Tests (TASK-02 / FIX-02)
    # -----------------------------------------------------------------------

    def test_cache_hit_without_provider_credentials(self):
        """
        TASK-02: An exact-cache hit must work even when Groq credentials are
        unconfigured/missing, because no client should be created.
        """
        # Step 1: Populate cache
        self.client.post("/query", json={"prompt": "Offline cache test"})

        # Step 2: Remove mock dependency override and simulate missing GROQ_API_KEY
        app.dependency_overrides.pop(get_groq_client_factory, None)
        with patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=True):
            reload_settings()
            resp = self.client.post("/query", json={"prompt": "Offline cache test"})
            self.assertEqual(resp.status_code, 200)
            self.assertTrue(resp.json()["cached"])
            self.assertEqual(resp.json()["response"], "Mocked LLM Response")

    def test_cache_hit_does_not_construct_client(self):
        """TASK-02: A cache hit must not construct or invoke the provider client factory."""
        self.assertEqual(self.factory_call_count, 0)

        # Initial miss -> constructs client
        self.client.post("/query", json={"prompt": "Lazy client test"})
        self.assertEqual(self.factory_call_count, 1)

        # Cache hit -> client factory must NOT be invoked again!
        self.client.post("/query", json={"prompt": "Lazy client test"})
        self.assertEqual(self.factory_call_count, 1)

    def test_client_closed_after_generation(self):
        """TASK-02: Provider client is properly closed after inference execution."""
        self.client.post("/query", json={"prompt": "Client cleanup test"})
        self.mock_groq.close.assert_awaited_once()

    # -----------------------------------------------------------------------
    # 3. Cache & Single-Flight Metrics Tests (TASK-03 / FIX-03)
    # -----------------------------------------------------------------------

    def test_single_flight_metrics_accuracy(self):
        """
        TASK-03 / FIX-03: Four concurrent identical requests that result in 1 generation
        must count as exactly 4 total_requests (1 miss, 3 coalesced, 0 duplicate hits).
        """
        async def run_metrics_test():
            leader_event = asyncio.Event()

            async def delayed_completion(*args, **kwargs):
                await asyncio.sleep(0.04)
                return MockCompletion("Concurrent Answer")

            self.mock_groq.chat.completions.create.side_effect = delayed_completion

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Metrics accuracy prompt"}
                tasks = [client.post("/query", json=payload) for _ in range(4)]
                responses = await asyncio.gather(*tasks)

                for r in responses:
                    self.assertEqual(r.status_code, 200)

                stats_resp = await client.get("/cache/stats")
                stats = stats_resp.json()

                # Exactly 4 total requests
                self.assertEqual(stats["total_requests"], 4)
                # Exactly 1 miss (leader)
                self.assertEqual(stats["misses"], 1)
                # Exactly 3 coalesced requests (followers)
                self.assertEqual(stats["coalesced_requests"], 3)
                # Exactly 0 sequential hits
                self.assertEqual(stats["hits"], 0)
                # Reused = (0 + 3) / 4 = 0.75 hit_rate
                self.assertEqual(stats["hit_rate"], 0.75)

        asyncio.run(run_metrics_test())

    def test_sequential_hit_distinguished_from_coalesced(self):
        """TASK-03: Sequential exact-cache hits are distinguishable from coalesced requests."""
        # 1. Miss
        self.client.post("/query", json={"prompt": "Distinct metrics test"})
        # 2. Sequential Hit
        self.client.post("/query", json={"prompt": "Distinct metrics test"})

        stats = self.client.get("/cache/stats").json()
        self.assertEqual(stats["total_requests"], 2)
        self.assertEqual(stats["misses"], 1)
        self.assertEqual(stats["hits"], 1)
        self.assertEqual(stats["coalesced_requests"], 0)

    # -----------------------------------------------------------------------
    # 4. Strict Bypass Cache Semantics
    # -----------------------------------------------------------------------

    def test_strict_bypass_cache_semantics(self):
        """
        Verify bypass_cache=True:
        1. Does not return an existing cached entry.
        2. Does not write to cache on completion.
        3. Leaves existing cache entry unchanged.
        """
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Original Cached Answer")
        initial_resp = self.client.post("/query", json={"prompt": "Explain immutability."})
        self.assertEqual(initial_resp.json()["response"], "Original Cached Answer")
        self.assertFalse(initial_resp.json()["cached"])

        self.mock_groq.chat.completions.create.reset_mock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Fresh Bypassed Answer")
        bypass_resp = self.client.post("/query", json={"prompt": "Explain immutability.", "bypass_cache": True})
        self.assertEqual(bypass_resp.status_code, 200)
        self.assertFalse(bypass_resp.json()["cached"])
        self.assertEqual(bypass_resp.json()["response"], "Fresh Bypassed Answer")
        self.mock_groq.chat.completions.create.assert_called_once()

        self.mock_groq.chat.completions.create.reset_mock()
        subsequent_resp = self.client.post("/query", json={"prompt": "Explain immutability."})
        self.assertTrue(subsequent_resp.json()["cached"])
        self.assertEqual(subsequent_resp.json()["response"], "Original Cached Answer")
        self.mock_groq.chat.completions.create.assert_not_called()

    # -----------------------------------------------------------------------
    # 5. Streaming and Failure Isolation Tests
    # -----------------------------------------------------------------------

    def test_streaming_query_success_and_cached_replay(self):
        """Verify SSE streaming contract and subsequent cached replay."""
        self.mock_groq.chat.completions.create.return_value = mock_chunk_stream()

        payload = {"prompt": "Stream this story", "stream": True}
        resp1 = self.client.post("/query", json=payload)
        self.assertEqual(resp1.status_code, 200)
        self.assertIn("text/event-stream", resp1.headers["content-type"])
        self.assertIn("Hello", resp1.text)
        self.assertIn("stream!", resp1.text)
        self.assertIn("[DONE]", resp1.text)

        resp2 = self.client.post("/query", json=payload)
        self.assertEqual(resp2.status_code, 200)
        self.assertIn("text/event-stream", resp2.headers["content-type"])
        self.assertIn("Hello from stream!", resp2.text)
        self.assertIn('"cached": true', resp2.text)

    def test_streaming_failure_is_not_cached(self):
        """Verify mid-stream failures yield an error event and are NEVER committed to cache."""
        self.mock_groq.chat.completions.create.return_value = mock_failing_chunk_stream()

        payload = {"prompt": "Failing stream prompt", "stream": True}
        resp = self.client.post("/query", json=payload)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("error", resp.text)
        self.assertIn("Internal Gateway Error", resp.text)

        stats = self.client.get("/cache/stats").json()
        self.assertEqual(stats["total_entries"], 0)

    # -----------------------------------------------------------------------
    # 6. Concurrency Coordination with Deterministic Events (TASK-06 / FIX-06)
    # -----------------------------------------------------------------------

    def test_single_flight_leader_cancellation_with_multiple_followers(self):
        """
        TASK-06: Dedicated leader cancellation test with multiple waiting followers
        using deterministic asyncio.Events. Followers must receive CancelledError,
        in-flight entry must be removed, and a retry must succeed.
        """
        async def run_test():
            leader_started = asyncio.Event()

            async def blocking_leader_call(*args, **kwargs):
                leader_started.set()
                # Hang indefinitely until cancelled
                await asyncio.Future()

            self.mock_groq.chat.completions.create.side_effect = blocking_leader_call

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Deterministic leader cancellation test"}

                # Start leader task
                t_leader = asyncio.create_task(client.post("/query", json=payload))
                await leader_started.wait()

                # Start two followers
                t_follower1 = asyncio.create_task(client.post("/query", json=payload))
                t_follower2 = asyncio.create_task(client.post("/query", json=payload))
                await asyncio.sleep(0.01)

                # Verify leader is in flight
                self.assertEqual(len(self.single_flight._in_flight), 1)

                # Cancel the leader!
                t_leader.cancel()

                # Await all tasks
                leader_exc = None
                try:
                    await t_leader
                except asyncio.CancelledError as e:
                    leader_exc = e

                # Waiting followers must receive CancelledError or controlled failure
                f1_res = await asyncio.gather(t_follower1, return_exceptions=True)
                f2_res = await asyncio.gather(t_follower2, return_exceptions=True)

                self.assertTrue(isinstance(f1_res[0], asyncio.CancelledError))
                self.assertTrue(isinstance(f2_res[0], asyncio.CancelledError))

                # In-flight map must be empty
                self.assertEqual(len(self.single_flight._in_flight), 0)

                # Subsequent request retries cleanly and succeeds
                self.mock_groq.chat.completions.create.side_effect = None
                self.mock_groq.chat.completions.create.return_value = MockCompletion("Retry Success")
                retry_resp = await client.post("/query", json=payload)
                self.assertEqual(retry_resp.status_code, 200)
                self.assertEqual(retry_resp.json()["response"], "Retry Success")

        asyncio.run(run_test())

    def test_single_flight_follower_cancellation_does_not_abort_leader(self):
        """
        TASK-06: Verify that cancelling a waiting follower coroutine does NOT cancel the leader,
        and the leader finishes and commits to cache.
        """
        async def run_test():
            leader_started = asyncio.Event()
            allow_leader_finish = asyncio.Event()

            async def synchronized_generation(*args, **kwargs):
                leader_started.set()
                await allow_leader_finish.wait()
                return MockCompletion("Leader finished successfully")

            self.mock_groq.chat.completions.create.side_effect = synchronized_generation

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Follower cancellation deterministic test"}

                t_leader = asyncio.create_task(client.post("/query", json=payload))
                await leader_started.wait()

                t_follower = asyncio.create_task(client.post("/query", json=payload))
                await asyncio.sleep(0.01)

                # Cancel follower
                t_follower.cancel()
                try:
                    await t_follower
                except asyncio.CancelledError:
                    pass

                # Allow leader to complete
                allow_leader_finish.set()
                leader_resp = await t_leader
                self.assertEqual(leader_resp.status_code, 200)

                # Subsequent request hits cache
                subsequent = await client.post("/query", json=payload)
                self.assertEqual(subsequent.status_code, 200)
                self.assertTrue(subsequent.json()["cached"])

        asyncio.run(run_test())

    def test_single_flight_leader_failure_zero_followers(self):
        """
        TASK-06: Leader failure with 0 followers raises error cleanly and does NOT
        emit 'Future exception was never retrieved' warning.
        """
        async def run_test():
            self.mock_groq.chat.completions.create.side_effect = RuntimeError("Zero follower error")

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                with warnings.catch_warnings(record=True) as recorded_warnings:
                    warnings.simplefilter("always")
                    resp = await client.post("/query", json={"prompt": "Zero followers failure test"})
                    self.assertEqual(resp.status_code, 500)
                    self.assertEqual(len(self.single_flight._in_flight), 0)

                    for w in recorded_warnings:
                        self.assertNotIn("Future exception was never retrieved", str(w.message))

        asyncio.run(run_test())

    def test_single_flight_leader_failure_multiple_followers(self):
        """TASK-06: Leader failure propagates error to all waiting followers without hanging."""
        async def run_test():
            leader_started = asyncio.Event()

            async def delayed_failure(*args, **kwargs):
                leader_started.set()
                await asyncio.sleep(0.03)
                raise RuntimeError("Shared upstream failure")

            self.mock_groq.chat.completions.create.side_effect = delayed_failure

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Multi-follower failure deterministic test"}
                t_leader = asyncio.create_task(client.post("/query", json=payload))
                await leader_started.wait()

                tasks = [client.post("/query", json=payload) for _ in range(2)]
                responses = await asyncio.gather(t_leader, *tasks)

                for r in responses:
                    self.assertEqual(r.status_code, 500)
                    self.assertIn("Internal Gateway Error", r.text)

                self.assertEqual(len(self.single_flight._in_flight), 0)

        asyncio.run(run_test())

    # -----------------------------------------------------------------------
    # 7. Security, Input Bounds, & Secret Redaction Tests (TASK-05 / FIX-05)
    # -----------------------------------------------------------------------

    def test_secret_sanitization_in_errors(self):
        """
        TASK-05 / FIX-05: Real keys, fake keys, bearer tokens, file paths, and IPs
        must not leak into client responses or captured logs.
        """
        mock_req = httpx.Request("POST", "https://api.groq.com/v1/chat")
        mock_resp = httpx.Response(status_code=401, request=mock_req)
        self.mock_groq.chat.completions.create.side_effect = groq.AuthenticationError(
            message="Auth failure with key gsk_fakeKey1234567890 and Bearer secretToken987 at /home/user/app/secret.py",
            response=mock_resp,
            body=None,
        )

        resp = self.client.post("/query", json={"prompt": "Trigger auth error with secret"})
        self.assertEqual(resp.status_code, 401)
        self.assertNotIn("gsk_fakeKey1234567890", resp.text)
        self.assertNotIn("secretToken987", resp.text)
        self.assertNotIn("/home/user/app", resp.text)

    def test_payload_size_limits(self):
        """Verify requests exceeding character payload bounds are rejected with 422."""
        # Prompt exceeds 131072 chars
        huge_prompt = "a" * 131073
        r1 = self.client.post("/query", json={"prompt": huge_prompt})
        self.assertEqual(r1.status_code, 422)

        # Messages count exceeds 100
        many_messages = [{"role": "user", "content": f"msg {i}"} for i in range(101)]
        r2 = self.client.post("/query", json={"messages": many_messages})
        self.assertEqual(r2.status_code, 422)

    def test_error_handling_rate_limit(self):
        """Verify 429 status code returned when Groq rate limits."""
        mock_req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        mock_resp = httpx.Response(status_code=429, request=mock_req)
        self.mock_groq.chat.completions.create.side_effect = groq.RateLimitError(
            message="Rate limit reached for gsk_fakeKey999", response=mock_resp, body=None
        )

        resp = self.client.post("/query", json={"prompt": "Trigger rate limit"})
        self.assertEqual(resp.status_code, 429)
        self.assertNotIn("gsk_fakeKey999", resp.text)
        self.assertIn("Rate Limit Exceeded", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 8. TTL Expiration & LRU Eviction Tests
    # -----------------------------------------------------------------------

    def test_ttl_cache_expiration(self):
        """Verify entries expire after TTL."""
        entry = CacheEntry("Old response", "qwen/qwen3.8-27b", UsageMetrics(), 10.0)
        entry.created_at = entry.created_at - 4000
        self.assertTrue(entry.is_expired(ttl_seconds=3600))

    def test_lru_cache_eviction(self):
        """Verify oldest entries are evicted when max capacity is reached."""
        test_cache = QueryCache(max_entries=2, ttl_seconds=3600)
        e1 = CacheEntry("r1", "m", UsageMetrics(), 1.0)
        e2 = CacheEntry("r2", "m", UsageMetrics(), 1.0)
        e3 = CacheEntry("r3", "m", UsageMetrics(), 1.0)

        async def _run_eviction():
            await test_cache.set("k1", e1)
            await test_cache.set("k2", e2)
            self.assertEqual(len(test_cache._entries), 2)

            await test_cache.set("k3", e3)
            self.assertEqual(len(test_cache._entries), 2)
            self.assertIsNone(await test_cache.get("k1"))
            self.assertIsNotNone(await test_cache.get("k2"))
            self.assertIsNotNone(await test_cache.get("k3"))

        asyncio.run(_run_eviction())


if __name__ == "__main__":
    unittest.main()
