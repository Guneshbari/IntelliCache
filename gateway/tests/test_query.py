"""
Hardened unit and integration tests for query execution, exact-match caching,
concurrency coordination, streaming failure isolation, and API security.
"""

import asyncio
import json
import unittest
from unittest.mock import AsyncMock
import warnings

from fastapi.testclient import TestClient
import groq
import httpx

from app.main import app
from app.routes.query import (
    CacheEntry,
    QueryCache,
    QueryRequest,
    UsageMetrics,
    get_groq_client,
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
        self.cache = get_query_cache()
        self.cache.clear()
        self.single_flight = get_single_flight_coordinator()

        # Set up mock Groq client
        self.mock_groq = AsyncMock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Mocked LLM Response")
        app.dependency_overrides[get_groq_client] = lambda: self.mock_groq

    def tearDown(self):
        app.dependency_overrides.clear()
        self.cache.clear()

    # -----------------------------------------------------------------------
    # 1. Exact-Cache Correctness & Parameter Isolation Tests
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

    def test_parameter_isolation_temperature(self):
        """Verify requests with different temperatures do NOT share cache entries."""
        payload_deterministic = {"prompt": "Write a sorting function", "temperature": 0.0}
        payload_creative = {"prompt": "Write a sorting function", "temperature": 1.2}

        resp1 = self.client.post("/query", json=payload_deterministic)
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])

        resp2 = self.client.post("/query", json=payload_creative)
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(resp2.json()["cached"])
        self.assertNotEqual(resp1.json()["fingerprint"], resp2.json()["fingerprint"])

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
        # Role difference
        payload_user = {"messages": [{"role": "user", "content": "Help me"}]}
        payload_system = {"messages": [{"role": "system", "content": "Help me"}]}

        r_user = self.client.post("/query", json=payload_user)
        r_sys = self.client.post("/query", json=payload_system)
        self.assertNotEqual(r_user.json()["fingerprint"], r_sys.json()["fingerprint"])

        # Sequence ordering difference
        seq_1 = {"messages": [{"role": "user", "content": "A"}, {"role": "assistant", "content": "B"}]}
        seq_2 = {"messages": [{"role": "assistant", "content": "B"}, {"role": "user", "content": "A"}]}
        r_seq1 = self.client.post("/query", json=seq_1)
        r_seq2 = self.client.post("/query", json=seq_2)
        self.assertNotEqual(r_seq1.json()["fingerprint"], r_seq2.json()["fingerprint"])

    # -----------------------------------------------------------------------
    # 2. Strict Bypass Cache Semantics
    # -----------------------------------------------------------------------

    def test_strict_bypass_cache_semantics(self):
        """
        Verify bypass_cache=True:
        1. Does not return an existing cached entry.
        2. Does not write to cache on completion.
        3. Leaves existing cache entry unchanged.
        """
        # Step 1: Pre-populate cache with an original response
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Original Cached Answer")
        initial_resp = self.client.post("/query", json={"prompt": "Explain immutability."})
        self.assertEqual(initial_resp.json()["response"], "Original Cached Answer")
        self.assertFalse(initial_resp.json()["cached"])

        # Step 2: Query with bypass_cache=True and a new upstream answer
        self.mock_groq.chat.completions.create.reset_mock()
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Fresh Bypassed Answer")
        bypass_resp = self.client.post("/query", json={"prompt": "Explain immutability.", "bypass_cache": True})
        self.assertEqual(bypass_resp.status_code, 200)
        self.assertFalse(bypass_resp.json()["cached"])
        self.assertEqual(bypass_resp.json()["response"], "Fresh Bypassed Answer")
        self.mock_groq.chat.completions.create.assert_called_once()

        # Step 3: Regular query must still retrieve the UNTOUCHED original cached answer!
        self.mock_groq.chat.completions.create.reset_mock()
        subsequent_resp = self.client.post("/query", json={"prompt": "Explain immutability."})
        self.assertTrue(subsequent_resp.json()["cached"])
        self.assertEqual(subsequent_resp.json()["response"], "Original Cached Answer")
        self.mock_groq.chat.completions.create.assert_not_called()

    def test_bypass_cache_on_uncached_does_not_write(self):
        """Verify an uncached request with bypass_cache=True does not write to cache."""
        self.client.post("/query", json={"prompt": "Completely new prompt", "bypass_cache": True})
        stats = self.client.get("/cache/stats").json()
        self.assertEqual(stats["total_entries"], 0)

    # -----------------------------------------------------------------------
    # 3. Streaming and Failure Isolation Tests
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

        # Subsequent streaming query should hit cache and return matching SSE schema
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
        self.assertIn("Simulated mid-stream failure", resp.text)

        # Cache must NOT contain this entry
        stats = self.client.get("/cache/stats").json()
        self.assertEqual(stats["total_entries"], 0)

    # -----------------------------------------------------------------------
    # 4. Single-Flight Concurrency Tests
    # -----------------------------------------------------------------------

    def test_single_flight_concurrent_success(self):
        """Verify simultaneous identical requests trigger exactly 1 Groq API call."""
        async def run_concurrent():
            # Setup mock with slight async delay to allow followers to join
            async def delayed_completion(*args, **kwargs):
                await asyncio.sleep(0.05)
                return MockCompletion("Concurrent Shared Answer")

            self.mock_groq.chat.completions.create.side_effect = delayed_completion

            # Use httpx.AsyncClient with ASGI app
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Concurrent thundering herd test"}
                # Launch 4 requests concurrently
                tasks = [client.post("/query", json=payload) for _ in range(4)]
                responses = await asyncio.gather(*tasks)

                # All 4 responses must be 200 OK
                for r in responses:
                    self.assertEqual(r.status_code, 200)
                    self.assertEqual(r.json()["response"], "Concurrent Shared Answer")

                # Count cached responses: 1 leader (cached=False), 3 followers (cached=True)
                cached_flags = [r.json()["cached"] for r in responses]
                self.assertEqual(cached_flags.count(False), 1)
                self.assertEqual(cached_flags.count(True), 3)

                # Exactly 1 Groq call occurred!
                self.assertEqual(self.mock_groq.chat.completions.create.call_count, 1)

        asyncio.run(run_concurrent())

    def test_single_flight_leader_failure_zero_followers(self):
        """
        Verify leader failure with 0 followers raises error cleanly and does NOT
        emit 'Future exception was never retrieved' warning.
        """
        async def run_test():
            self.mock_groq.chat.completions.create.side_effect = RuntimeError("Zero follower upstream error")

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                with warnings.catch_warnings(record=True) as recorded_warnings:
                    warnings.simplefilter("always")
                    resp = await client.post("/query", json={"prompt": "Zero followers failure test"})
                    self.assertEqual(resp.status_code, 500)

                    # Ensure in-flight map is completely empty
                    self.assertEqual(len(self.single_flight._in_flight), 0)

                    # Check recorded warnings for unretrieved future exceptions
                    for w in recorded_warnings:
                        self.assertNotIn("Future exception was never retrieved", str(w.message))

        asyncio.run(run_test())

    def test_single_flight_leader_failure_multiple_followers(self):
        """Verify leader failure propagates the error to all waiting followers and clears state."""
        async def run_test():
            async def delayed_failure(*args, **kwargs):
                await asyncio.sleep(0.04)
                raise RuntimeError("Shared upstream failure")

            self.mock_groq.chat.completions.create.side_effect = delayed_failure

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Multi-follower failure test"}
                tasks = [client.post("/query", json=payload) for _ in range(3)]
                responses = await asyncio.gather(*tasks)

                for r in responses:
                    self.assertEqual(r.status_code, 500)
                    self.assertIn("Shared upstream failure", r.text)

                # In-flight map must be empty
                self.assertEqual(len(self.single_flight._in_flight), 0)

        asyncio.run(run_test())

    def test_single_flight_follower_cancellation_does_not_abort_leader(self):
        """
        Verify that cancelling a waiting follower coroutine does NOT cancel the leader,
        and the leader finishes and commits to cache.
        """
        async def run_test():
            leader_finished = False

            async def slow_generation(*args, **kwargs):
                nonlocal leader_finished
                await asyncio.sleep(0.08)
                leader_finished = True
                return MockCompletion("Leader finished successfully")

            self.mock_groq.chat.completions.create.side_effect = slow_generation

            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                payload = {"prompt": "Follower cancellation test"}

                # Start leader task
                t_leader = asyncio.create_task(client.post("/query", json=payload))
                await asyncio.sleep(0.01)

                # Start follower task
                t_follower = asyncio.create_task(client.post("/query", json=payload))
                await asyncio.sleep(0.01)

                # Cancel follower
                t_follower.cancel()
                try:
                    await t_follower
                except asyncio.CancelledError:
                    pass

                # Await leader
                leader_resp = await t_leader
                self.assertEqual(leader_resp.status_code, 200)
                self.assertTrue(leader_finished)

                # Verify subsequent request hits the cache!
                subsequent = await client.post("/query", json=payload)
                self.assertEqual(subsequent.status_code, 200)
                self.assertTrue(subsequent.json()["cached"])

        asyncio.run(run_test())

    def test_single_flight_retry_after_failure(self):
        """Verify that after a leader failure, subsequent requests can retry normally."""
        # First call fails
        self.mock_groq.chat.completions.create.side_effect = RuntimeError("Temporary failure")
        resp1 = self.client.post("/query", json={"prompt": "Retry prompt"})
        self.assertEqual(resp1.status_code, 500)

        # Second call succeeds
        self.mock_groq.chat.completions.create.side_effect = None
        self.mock_groq.chat.completions.create.return_value = MockCompletion("Recovered on retry")
        resp2 = self.client.post("/query", json={"prompt": "Retry prompt"})
        self.assertEqual(resp2.status_code, 200)
        self.assertEqual(resp2.json()["response"], "Recovered on retry")

    # -----------------------------------------------------------------------
    # 5. Security & Input Bounds Validation Tests
    # -----------------------------------------------------------------------

    def test_payload_size_limits(self):
        """Verify requests exceeding character payload bounds are rejected with 422."""
        # 1. Prompt exceeds max_prompt_chars (131072)
        huge_prompt = "a" * 131073
        r1 = self.client.post("/query", json={"prompt": huge_prompt})
        self.assertEqual(r1.status_code, 422)
        self.assertIn("payload size limit", r1.text)

        # 2. Messages count exceeds max_messages_count (100)
        many_messages = [{"role": "user", "content": f"msg {i}"} for i in range(101)]
        r2 = self.client.post("/query", json={"messages": many_messages})
        self.assertEqual(r2.status_code, 422)
        self.assertIn("exceeds maximum allowed", r2.text)

        # 3. Individual message content exceeds limit
        huge_message = [{"role": "user", "content": "x" * 131073}]
        r3 = self.client.post("/query", json={"messages": huge_message})
        self.assertEqual(r3.status_code, 422)

    def test_secret_sanitization(self):
        """Verify API keys (gsk_...) are redacted in error messages."""
        mock_req = httpx.Request("POST", "https://api.groq.com")
        mock_resp = httpx.Response(status_code=401, request=mock_req)
        self.mock_groq.chat.completions.create.side_effect = groq.AuthenticationError(
            message="Invalid token gsk_secretApiKey1234567890abcdef provided",
            response=mock_resp,
            body=None,
        )

        resp = self.client.post("/query", json={"prompt": "Trigger auth error with secret"})
        self.assertEqual(resp.status_code, 401)
        # Must NOT contain the raw key
        self.assertNotIn("gsk_secretApiKey1234567890abcdef", resp.text)
        # Must contain redacted pattern
        self.assertIn("gsk_***", resp.text)

    def test_error_handling_rate_limit(self):
        """Verify 429 status code returned when Groq rate limits."""
        mock_req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
        mock_resp = httpx.Response(status_code=429, request=mock_req)
        self.mock_groq.chat.completions.create.side_effect = groq.RateLimitError(
            message="Rate limit reached", response=mock_resp, body=None
        )

        resp = self.client.post("/query", json={"prompt": "Trigger rate limit"})
        self.assertEqual(resp.status_code, 429)
        self.assertIn("Rate Limit Exceeded", resp.json()["detail"])

    # -----------------------------------------------------------------------
    # 6. TTL Expiration & LRU Eviction Tests
    # -----------------------------------------------------------------------

    def test_ttl_cache_expiration(self):
        """Verify entries expire after TTL."""
        entry = CacheEntry("Old response", "qwen/qwen3.8-27b", UsageMetrics(), 10.0)
        # Artificially set created_at in the past
        entry.created_at = entry.created_at - 4000
        self.assertTrue(entry.is_expired(ttl_seconds=3600))

    def test_lru_cache_eviction(self):
        """Verify oldest entries are evicted when max capacity is reached."""
        test_cache = QueryCache(max_entries=2, ttl_seconds=3600)
        e1 = CacheEntry("r1", "m", UsageMetrics(), 1.0)
        e2 = CacheEntry("r2", "m", UsageMetrics(), 1.0)
        e3 = CacheEntry("r3", "m", UsageMetrics(), 1.0)

        test_cache.set("k1", e1)
        test_cache.set("k2", e2)
        self.assertEqual(len(test_cache._entries), 2)

        # Adding k3 should evict k1
        test_cache.set("k3", e3)
        self.assertEqual(len(test_cache._entries), 2)
        self.assertIsNone(test_cache.get("k1"))
        self.assertIsNotNone(test_cache.get("k2"))
        self.assertIsNotNone(test_cache.get("k3"))


if __name__ == "__main__":
    unittest.main()
