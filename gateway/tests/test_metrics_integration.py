"""
Integration tests for IntelliCache Runtime Metrics and Observability layer.
Verifies namespace-scoped metrics, multi-client isolation, exact and semantic hit accounting,
SingleFlight coalesced accounting, provider failure accounting, security controls, and admin snapshots.
"""

import asyncio
from datetime import datetime, timezone
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from fastapi.testclient import TestClient
import httpx

from app.api.routes import (
    get_groq_client_factory,
    get_query_cache,
    get_semantic_cache,
    set_query_cache,
    set_semantic_cache,
)
from app.cache.exact_cache import ExactCache
from app.config import reload_settings
from app.core.contracts import GenerationResult, UsageMetrics
from app.core.errors import ProviderUpstreamError
from app.main import app
from app.metrics import get_metrics_collector
from app.semantic_cache.contracts import SemanticHit


class MockChatCompletion:
    """Mock structure mimicking Groq / OpenAI chat completion response."""
    def __init__(self, content: str = "Mock answer", prompt_tokens: int = 15, completion_tokens: int = 25):
        self.choices = [type("Choice", (), {"message": type("Msg", (), {"content": content})()})()]
        self.usage = type(
            "Usage",
            (),
            {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
        )()


class TestMetricsIntegration(unittest.TestCase):
    """Integration test suite for metrics collection, isolation, and security endpoints."""

    def setUp(self):
        self._saved_dependency_overrides = dict(app.dependency_overrides)
        self._saved_query_cache = get_query_cache()
        self._saved_semantic_cache = get_semantic_cache()

        self.collector = get_metrics_collector()
        self.collector.reset()

        self.cache = ExactCache()
        set_query_cache(self.cache)
        set_semantic_cache(None)

        # Mock Groq client
        self.mock_groq = AsyncMock()
        self.provider_call_count = 0

        async def fake_create(*args, **kwargs):
            self.provider_call_count += 1
            return MockChatCompletion("Generated response", prompt_tokens=20, completion_tokens=30)

        self.mock_groq.chat.completions.create = AsyncMock(side_effect=fake_create)
        self.mock_groq.close = AsyncMock()
        app.dependency_overrides[get_groq_client_factory] = lambda: lambda: self.mock_groq

        self.token_alpha_1 = "tok_team_alpha_primary"
        self.token_alpha_2 = "tok_team_alpha_secondary"
        self.token_beta = "tok_team_beta"
        self.admin_token = "admin_super_secret"

        self.env_patcher = patch.dict(os.environ, {
            "CLIENT_API_TOKENS": json.dumps({
                self.token_alpha_1: "team_alpha",
                self.token_alpha_2: "team_alpha",
                self.token_beta: "team_beta",
            }),
            "ADMIN_API_TOKEN": self.admin_token,
            "CACHE_ENABLED": "true",
            "ALLOW_UNAUTHENTICATED_LOOPBACK": "false",
        }, clear=False)
        self.env_patcher.start()
        reload_settings()

        self.client = TestClient(app)

    def tearDown(self):
        self.env_patcher.stop()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._saved_dependency_overrides)
        set_query_cache(self._saved_query_cache)
        set_semantic_cache(self._saved_semantic_cache)
        self.collector.reset()
        reload_settings()

    # -----------------------------------------------------------------------
    # Multi-Client Metrics Lifecycle & Isolation
    # -----------------------------------------------------------------------

    def test_multi_client_metrics_lifecycle_and_isolation(self):
        """
        Validates:
        1. Client A generates a response -> 1 request, 1 provider call, 0 hits.
        2. Client A repeats query -> 1 exact hit, 0 provider calls, tokens & latency saved.
        3. Client B (same namespace) queries same prompt -> exact hit reused, shared namespace metrics.
        4. Client C (different namespace) queries same prompt -> isolated metrics, 0 prior hits, new provider call.
        """
        headers_alpha_1 = {"X-Client-Token": self.token_alpha_1}
        headers_alpha_2 = {"X-Client-Token": self.token_alpha_2}
        headers_beta = {"X-Client-Token": self.token_beta}

        # Step 1: Client A issues first query (Cache Miss)
        resp1 = self.client.post("/query", json={"prompt": "Explain Quantum Computing"}, headers=headers_alpha_1)
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])
        self.assertEqual(self.provider_call_count, 1)

        # Inspect Client A's metrics
        m_a1 = self.client.get("/metrics/me", headers=headers_alpha_1)
        self.assertEqual(m_a1.status_code, 200)
        snap_a1 = m_a1.json()
        self.assertEqual(snap_a1["namespace"], "team_alpha")
        self.assertEqual(snap_a1["request_and_cache"]["total_authenticated_requests"], 1)
        self.assertEqual(snap_a1["request_and_cache"]["exact_cache_lookups"], 1)
        self.assertEqual(snap_a1["request_and_cache"]["exact_cache_hits"], 0)
        self.assertEqual(snap_a1["request_and_cache"]["exact_cache_misses"], 1)
        self.assertEqual(snap_a1["provider_and_savings"]["provider_calls_attempted"], 1)
        self.assertEqual(snap_a1["provider_and_savings"]["provider_calls_succeeded"], 1)
        self.assertEqual(snap_a1["provider_and_savings"]["provider_prompt_tokens_used"], 20)
        self.assertEqual(snap_a1["provider_and_savings"]["provider_completion_tokens_used"], 30)
        self.assertEqual(snap_a1["provider_and_savings"]["provider_calls_avoided"], 0)

        # Step 2: Client A repeats identical query (Exact Cache Hit)
        resp2 = self.client.post("/query", json={"prompt": "Explain Quantum Computing"}, headers=headers_alpha_1)
        self.assertEqual(resp2.status_code, 200)
        self.assertTrue(resp2.json()["cached"])
        self.assertEqual(resp2.json()["cache_type"], "exact")
        # Provider MUST NOT be called again
        self.assertEqual(self.provider_call_count, 1)

        m_a2 = self.client.get("/metrics/me", headers=headers_alpha_1)
        snap_a2 = m_a2.json()
        self.assertEqual(snap_a2["request_and_cache"]["total_authenticated_requests"], 2)
        self.assertEqual(snap_a2["request_and_cache"]["exact_cache_hits"], 1)
        self.assertEqual(snap_a2["provider_and_savings"]["provider_calls_avoided"], 1)
        self.assertEqual(snap_a2["provider_and_savings"]["prompt_tokens_saved"], 20)
        self.assertEqual(snap_a2["provider_and_savings"]["completion_tokens_saved"], 30)
        self.assertEqual(snap_a2["provider_and_savings"]["total_tokens_saved"], 50)
        self.assertEqual(snap_a2["derived_rates"]["exact_hit_rate"], 0.5)

        # Step 3: Client B (different token, same team_alpha namespace) reuses cached response
        resp3 = self.client.post("/query", json={"prompt": "Explain Quantum Computing"}, headers=headers_alpha_2)
        self.assertEqual(resp3.status_code, 200)
        self.assertTrue(resp3.json()["cached"])
        self.assertEqual(self.provider_call_count, 1)

        m_b = self.client.get("/metrics/me", headers=headers_alpha_2)
        snap_b = m_b.json()
        # Shared namespace metrics
        self.assertEqual(snap_b["namespace"], "team_alpha")
        self.assertEqual(snap_b["request_and_cache"]["total_authenticated_requests"], 3)
        self.assertEqual(snap_b["request_and_cache"]["exact_cache_hits"], 2)
        self.assertEqual(snap_b["provider_and_savings"]["provider_calls_avoided"], 2)
        self.assertEqual(snap_b["provider_and_savings"]["total_tokens_saved"], 100)

        # Step 4: Client C (team_beta namespace) queries the exact same prompt
        # Before query, team_beta metrics MUST be pristine (zero)
        m_c_pre = self.client.get("/metrics/me", headers=headers_beta)
        self.assertEqual(m_c_pre.status_code, 200)
        snap_c_pre = m_c_pre.json()
        self.assertEqual(snap_c_pre["namespace"], "team_beta")
        self.assertEqual(snap_c_pre["request_and_cache"]["total_authenticated_requests"], 0)
        self.assertEqual(snap_c_pre["provider_and_savings"]["total_tokens_saved"], 0)

        # Team beta query -> cannot access team_alpha cache, triggers new provider generation
        resp_beta = self.client.post("/query", json={"prompt": "Explain Quantum Computing"}, headers=headers_beta)
        self.assertEqual(resp_beta.status_code, 200)
        self.assertFalse(resp_beta.json()["cached"])
        self.assertEqual(self.provider_call_count, 2)  # incremented for team_beta

        m_c_post = self.client.get("/metrics/me", headers=headers_beta)
        snap_c_post = m_c_post.json()
        self.assertEqual(snap_c_post["namespace"], "team_beta")
        self.assertEqual(snap_c_post["request_and_cache"]["total_authenticated_requests"], 1)
        self.assertEqual(snap_c_post["request_and_cache"]["exact_cache_misses"], 1)
        self.assertEqual(snap_c_post["request_and_cache"]["exact_cache_hits"], 0)
        self.assertEqual(snap_c_post["provider_and_savings"]["total_tokens_saved"], 0)

        # Client C cannot spoof namespace via query params
        m_c_spoof = self.client.get("/metrics/me?namespace=team_alpha", headers=headers_beta)
        self.assertEqual(m_c_spoof.status_code, 200)
        # Still returns team_beta!
        self.assertEqual(m_c_spoof.json()["namespace"], "team_beta")
        self.assertEqual(m_c_spoof.json()["request_and_cache"]["exact_cache_hits"], 0)

    # -----------------------------------------------------------------------
    # Endpoint Authentication & Authorization Controls
    # -----------------------------------------------------------------------

    def test_metrics_me_unauthenticated_and_invalid_token_rejected(self):
        """Unauthenticated requests or invalid tokens are strictly rejected with 401."""
        # Missing token
        resp_none = self.client.get("/metrics/me")
        self.assertEqual(resp_none.status_code, 401)

        # Invalid token
        resp_bad = self.client.get("/metrics/me", headers={"X-Client-Token": "invalid_fake_token"})
        self.assertEqual(resp_bad.status_code, 401)

    def test_admin_metrics_security_and_aggregate_snapshot(self):
        """
        Validates:
        1. /admin/metrics requires ADMIN_API_TOKEN (403 if invalid or missing).
        2. Client token cannot access /admin/metrics.
        3. Valid admin token returns system aggregate and per-namespace breakdown.
        4. No secret tokens, user prompts, responses, or vectors are exposed.
        """
        # Missing admin token
        r_none = self.client.get("/admin/metrics")
        self.assertEqual(r_none.status_code, 403)

        # Invalid admin token
        r_bad = self.client.get("/admin/metrics", headers={"X-Admin-Token": "wrong_admin_token"})
        self.assertEqual(r_bad.status_code, 403)

        # Client token attempting to call admin metrics
        r_client = self.client.get("/admin/metrics", headers={"X-Client-Token": self.token_alpha_1})
        self.assertEqual(r_client.status_code, 403)

        # Generate some activity first
        self.client.post("/query", json={"prompt": "Alpha 1"}, headers={"X-Client-Token": self.token_alpha_1})
        self.client.post("/query", json={"prompt": "Beta 1"}, headers={"X-Client-Token": self.token_beta})

        # Valid admin request via header
        r_admin = self.client.get("/admin/metrics", headers={"X-Admin-Token": self.admin_token})
        self.assertEqual(r_admin.status_code, 200)
        data = r_admin.json()

        self.assertEqual(data["scope"], "admin")
        self.assertIn("aggregate", data)
        self.assertIn("namespaces", data)
        self.assertEqual(data["aggregate"]["request_and_cache"]["total_authenticated_requests"], 2)
        self.assertIn("team_alpha", data["namespaces"])
        self.assertIn("team_beta", data["namespaces"])

        # Also works via Authorization: Bearer <admin_token>
        r_bearer = self.client.get("/admin/metrics", headers={"Authorization": f"Bearer {self.admin_token}"})
        self.assertEqual(r_bearer.status_code, 200)

        # Verify no sensitive data leakage
        raw_text = r_admin.text
        self.assertNotIn(self.token_alpha_1, raw_text)
        self.assertNotIn(self.token_beta, raw_text)
        self.assertNotIn("Alpha 1", raw_text)
        self.assertNotIn("Generated response", raw_text)

    def test_cache_stats_and_semantic_stats_protected_and_scoped(self):
        """
        GET /cache/stats and /cache/semantic/stats enforce authentication:
        - Unauthenticated access returns 401.
        - Client token returns namespace-isolated stats.
        - Admin token returns global stats.
        """
        # Unauthenticated rejected
        resp_unauth = self.client.get("/cache/stats")
        self.assertEqual(resp_unauth.status_code, 401)

        resp_sem_unauth = self.client.get("/cache/semantic/stats")
        self.assertEqual(resp_sem_unauth.status_code, 401)

        # Populate entry in team_alpha
        self.client.post("/query", json={"prompt": "Q1"}, headers={"X-Client-Token": self.token_alpha_1})
        self.client.post("/query", json={"prompt": "Q1"}, headers={"X-Client-Token": self.token_alpha_1})

        # Client token receives team_alpha stats
        resp_client = self.client.get("/cache/stats", headers={"X-Client-Token": self.token_alpha_1})
        self.assertEqual(resp_client.status_code, 200)
        stats_a = resp_client.json()
        self.assertEqual(stats_a["hits"], 1)
        self.assertEqual(stats_a["total_entries"], 1)

        # Client token for team_beta sees 0 hits
        resp_client_b = self.client.get("/cache/stats", headers={"X-Client-Token": self.token_beta})
        self.assertEqual(resp_client_b.status_code, 200)
        stats_b = resp_client_b.json()
        self.assertEqual(stats_b["hits"], 0)
        self.assertEqual(stats_b["total_entries"], 0)

        # Admin token sees aggregate
        resp_admin = self.client.get("/cache/stats", headers={"X-Admin-Token": self.admin_token})
        self.assertEqual(resp_admin.status_code, 200)
        stats_admin = resp_admin.json()
        self.assertEqual(stats_admin["hits"], 1)
        self.assertEqual(stats_admin["total_entries"], 1)

    # -----------------------------------------------------------------------
    # Provider Failure Accounting
    # -----------------------------------------------------------------------

    def test_provider_failure_increments_failure_counter_not_success(self):
        """When the LLM provider fails, failure counters increment without incrementing successes."""
        async def failing_generate(*args, **kwargs):
            raise ProviderUpstreamError(status_code=502, message="Bad gateway upstream")

        self.mock_groq.chat.completions.create = AsyncMock(side_effect=failing_generate)

        resp = self.client.post("/query", json={"prompt": "Will fail"}, headers={"X-Client-Token": self.token_alpha_1})
        self.assertEqual(resp.status_code, 502)

        m = self.client.get("/metrics/me", headers={"X-Client-Token": self.token_alpha_1})
        self.assertEqual(m.status_code, 200)
        snap = m.json()

        self.assertEqual(snap["request_and_cache"]["total_authenticated_requests"], 1)
        self.assertEqual(snap["request_and_cache"]["provider_generation_attempts"], 1)
        self.assertEqual(snap["request_and_cache"]["provider_generation_failures"], 1)
        self.assertEqual(snap["request_and_cache"]["provider_generation_successes"], 0)
        self.assertEqual(snap["provider_and_savings"]["provider_calls_attempted"], 1)
        self.assertEqual(snap["provider_and_savings"]["provider_calls_failed"], 1)
        self.assertEqual(snap["provider_and_savings"]["provider_calls_succeeded"], 0)

    # -----------------------------------------------------------------------
    # Bypass and Streaming Accounting
    # -----------------------------------------------------------------------

    def test_bypass_and_streaming_requests_accounting(self):
        """Bypass and streaming requests are distinctly accounted."""
        headers = {"X-Client-Token": self.token_alpha_1}

        # Bypass query
        resp_bypass = self.client.post(
            "/query",
            json={"prompt": "Do not cache me", "bypass_cache": True},
            headers=headers,
        )
        self.assertEqual(resp_bypass.status_code, 200)

        # Streaming query (miss)
        resp_stream = self.client.post(
            "/query",
            json={"prompt": "Stream this answer", "stream": True},
            headers=headers,
        )
        self.assertEqual(resp_stream.status_code, 200)

        snap = self.client.get("/metrics/me", headers=headers).json()
        self.assertEqual(snap["request_and_cache"]["total_authenticated_requests"], 2)
        self.assertEqual(snap["request_and_cache"]["cache_bypass_requests"], 1)
        self.assertEqual(snap["request_and_cache"]["streaming_requests"], 1)

    # -----------------------------------------------------------------------
    # Semantic Hit Accounting
    # -----------------------------------------------------------------------

    def test_semantic_hit_metrics_accounting(self):
        """Semantic hit increments semantic counters and token savings exactly once."""
        mock_semantic = AsyncMock()
        mock_hit = SemanticHit(
            cached_response="Semantically similar answer",
            fingerprint="fp_semantic_match_123",
            distance=0.05,
            similarity=0.95,
            model="llama-3.3-70b-versatile",
            query_text="What is quantum computing?",
            prompt_tokens=40,
            completion_tokens=60,
            total_tokens=100,
            original_latency_ms=250.0,
        )
        mock_semantic.lookup = AsyncMock(return_value=mock_hit)
        set_semantic_cache(mock_semantic)

        headers = {"X-Client-Token": self.token_alpha_1}
        resp = self.client.post("/query", json={"prompt": "Tell me about quantum computing"}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(resp.json()["cached"])
        self.assertEqual(resp.json()["cache_type"], "semantic")

        snap = self.client.get("/metrics/me", headers=headers).json()
        self.assertEqual(snap["request_and_cache"]["exact_cache_hits"], 0)
        self.assertEqual(snap["request_and_cache"]["semantic_hits"], 1)
        self.assertEqual(snap["provider_and_savings"]["provider_calls_avoided"], 1)
        self.assertEqual(snap["provider_and_savings"]["total_tokens_saved"], 100)
        self.assertEqual(snap["provider_and_savings"]["prompt_tokens_saved"], 40)
        self.assertEqual(snap["provider_and_savings"]["completion_tokens_saved"], 60)
        self.assertEqual(snap["latencies"]["estimated_latency_saved_ms"]["sample_count"], 1)
        self.assertGreater(snap["latencies"]["estimated_latency_saved_ms"]["p50"], 200.0)

    # -----------------------------------------------------------------------
    # Coalesced Followers SingleFlight Accounting
    # -----------------------------------------------------------------------

    def test_coalesced_follower_accounting_via_direct_core(self):
        """Simulates concurrent SingleFlight leader and follower requests to verify distinct coalesced counters."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore
        from app.core.singleflight import SingleFlightCoordinator

        coord = SingleFlightCoordinator()
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=coord,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        async def slow_generate(req):
            await asyncio.sleep(0.05)
            return GenerationResult(
                text="Slow leader response",
                model="test-model",
                usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
                latency_ms=50.0,
            )

        provider.generate = AsyncMock(side_effect=slow_generate)

        msgs = [ChatMessage(role="user", content="Concurrency test")]
        req1 = GenerationRequest(model="test-model", messages=msgs, namespace="team_alpha")
        req2 = GenerationRequest(model="test-model", messages=msgs, namespace="team_alpha")

        async def run_pair():
            # Leader starts, follower joins
            t1 = asyncio.create_task(core.execute_query(req1, provider))
            await asyncio.sleep(0.01)
            t2 = asyncio.create_task(core.execute_query(req2, provider))
            res1, res2 = await asyncio.gather(t1, t2)
            return res1, res2

        res1, res2 = asyncio.run(run_pair())
        self.assertFalse(res1.cached)
        self.assertTrue(res2.cached)

        snap = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap.request_and_cache.exact_cache_hits, 0)
        self.assertEqual(snap.request_and_cache.coalesced_joins, 1)
        self.assertEqual(snap.request_and_cache.coalesced_successes, 1)
        # Avoided = 1 (follower avoided provider call)
        self.assertEqual(snap.provider_and_savings.provider_calls_avoided, 1)
        # Follower saved 30 tokens
        self.assertEqual(snap.provider_and_savings.total_tokens_saved, 30)
        self.assertEqual(snap.derived_rates.coalesced_reuse_rate, 1.0)

    def test_single_flight_cross_namespace_isolation(self):
        """Step 11.1: Concurrent identical prompts across different namespaces must NOT coalesce."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore
        from app.core.singleflight import SingleFlightCoordinator

        coord = SingleFlightCoordinator()
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=coord,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        call_log = []
        provider = AsyncMock()

        async def logging_generate(req):
            call_log.append(req.namespace)
            await asyncio.sleep(0.04)
            return GenerationResult(
                text=f"Response for {req.namespace}",
                model="test-model",
                usage=UsageMetrics(prompt_tokens=10, completion_tokens=15, total_tokens=25),
                latency_ms=40.0,
            )

        provider.generate = AsyncMock(side_effect=logging_generate)

        msgs = [ChatMessage(role="user", content="Cross-namespace concurrency test")]
        req_alpha = GenerationRequest(model="test-model", messages=msgs, namespace="team_alpha")
        req_beta = GenerationRequest(model="test-model", messages=msgs, namespace="team_beta")

        async def run_isolated_concurrent():
            t1 = asyncio.create_task(core.execute_query(req_alpha, provider))
            t2 = asyncio.create_task(core.execute_query(req_beta, provider))
            return await asyncio.gather(t1, t2)

        res_alpha, res_beta = asyncio.run(run_isolated_concurrent())

        # Must NOT coalesce across namespaces: both invoked provider independently
        self.assertEqual(len(call_log), 2)
        self.assertIn("team_alpha", call_log)
        self.assertIn("team_beta", call_log)
        self.assertEqual(res_alpha.text, "Response for team_alpha")
        self.assertEqual(res_beta.text, "Response for team_beta")

        # In-flight SingleFlight keys were distinct composite tuples
        snap_alpha = self.collector.get_namespace_snapshot("team_alpha")
        snap_beta = self.collector.get_namespace_snapshot("team_beta")
        self.assertEqual(snap_alpha.request_and_cache.coalesced_joins, 0)
        self.assertEqual(snap_beta.request_and_cache.coalesced_joins, 0)

    def test_exact_cache_lookups_reconcile_with_hits_and_misses(self):
        """Step 11.1: Invariant: exact_cache_lookups == exact_cache_hits + exact_cache_misses."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore
        from app.core.singleflight import SingleFlightCoordinator

        coord = SingleFlightCoordinator()
        core = IntelliCacheCore(
            cache=self.cache,
            single_flight=coord,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        async def slow_generate(req):
            await asyncio.sleep(0.03)
            return GenerationResult(
                text="Leader text",
                model="test-model",
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=30.0,
            )
        provider.generate = AsyncMock(side_effect=slow_generate)

        msgs = [ChatMessage(role="user", content="Accounting invariant test")]
        req1 = GenerationRequest(model="test-model", messages=msgs, namespace="team_alpha")
        req2 = GenerationRequest(model="test-model", messages=msgs, namespace="team_alpha")

        async def run_pair():
            t1 = asyncio.create_task(core.execute_query(req1, provider))
            await asyncio.sleep(0.005)
            t2 = asyncio.create_task(core.execute_query(req2, provider))
            return await asyncio.gather(t1, t2)

        asyncio.run(run_pair())

        snap = self.collector.get_namespace_snapshot("team_alpha")
        r = snap.request_and_cache
        # Both leader and follower attempted exact lookup and missed before single-flight
        self.assertEqual(r.exact_cache_lookups, 2)
        self.assertEqual(r.exact_cache_misses, 2)
        self.assertEqual(r.exact_cache_hits, 0)
        self.assertEqual(r.exact_cache_lookups, r.exact_cache_hits + r.exact_cache_misses)

    def test_semantic_indexing_and_eligibility_not_double_counted(self):
        """Step 11.1: 1 eligible query that misses semantic cache must increment eligibility check once and index once."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore
        from app.embeddings.interfaces import EmbeddingProvider
        from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
        from app.semantic_cache.policy import SemanticCachePolicy
        from app.semantic_cache.service import SemanticCacheService
        from app.vector_store.interfaces import BaseVectorStore

        mock_embedder = MagicMock(spec=EmbeddingProvider)
        mock_embedder.embed = AsyncMock(return_value=EmbeddingResult(vector=[0.1] * 768, dimensions=768, model="nomic-embed-text"))

        mock_vector_store = MagicMock(spec=BaseVectorStore)
        mock_vector_store.search = AsyncMock(return_value=[])  # miss
        mock_vector_store.upsert = AsyncMock(return_value=None)  # index success

        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        core = IntelliCacheCore(
            cache=self.cache,
            semantic_cache=sem_service,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        provider.generate = AsyncMock(return_value=GenerationResult(
            text="Indexed answer",
            model="llama-3",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=15, total_tokens=25),
            latency_ms=45.0,
        ))

        req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I sort a list in Python?")],
            temperature=0.7,
            namespace="team_alpha",
            provider="groq",
        )

        asyncio.run(core.execute_query(req, provider))

        snap = self.collector.get_namespace_snapshot("team_alpha")
        s = snap.semantic
        # Exactly 1 request-level eligibility check (not duplicated by index())
        ns_data = self.collector.get_or_create_namespace("team_alpha")
        self.assertEqual(ns_data.semantic_eligibility_checks, 1)
        self.assertEqual(s.semantic_eligibility_rejections, 0)
        # Exactly 1 indexing success and 1 vector indexing latency sample
        self.assertEqual(s.indexing_successes, 1)
        self.assertEqual(s.indexing_failures, 0)
        self.assertEqual(snap.latencies.vector_indexing_latency_ms.sample_count, 1)

    def test_semantic_miss_only_on_completed_evaluation(self):
        """Step 11.1: Ineligible requests, embedding failures, and vector failures are NOT semantic misses."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.embeddings.interfaces import EmbeddingProvider
        from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
        from app.embeddings.errors import EmbeddingTimeoutError
        from app.semantic_cache.policy import SemanticCachePolicy
        from app.semantic_cache.service import SemanticCacheService
        from app.vector_store.interfaces import BaseVectorStore
        from app.vector_store.errors import VectorStoreError

        # 1. Ineligible request
        mock_embedder = MagicMock(spec=EmbeddingProvider)
        mock_vector_store = MagicMock(spec=BaseVectorStore)
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        sem_service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=self.collector,
        )

        ineligible_req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="Hi")],  # too short (< 3 tokens)
            namespace="team_alpha",
        )
        res = asyncio.run(sem_service.lookup(ineligible_req))
        self.assertIsNone(res)
        snap = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap.semantic.semantic_eligibility_rejections, 1)
        self.assertEqual(snap.request_and_cache.semantic_lookups, 0)
        self.assertEqual(snap.semantic.semantic_misses, 0)

        # 2. Embedding failure
        mock_embedder.embed = AsyncMock(side_effect=EmbeddingTimeoutError("Timeout"))
        eligible_req = GenerationRequest(
            model="llama-3",
            messages=[ChatMessage(role="user", content="How do I reverse a binary tree in Python?")],
            temperature=0.7,
            namespace="team_alpha",
        )
        res = asyncio.run(sem_service.lookup(eligible_req))
        self.assertIsNone(res)
        snap = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap.semantic.embedding_failures, 1)
        self.assertEqual(snap.semantic.semantic_misses, 0)  # NOT counted as miss!

        # 3. Vector store failure
        mock_embedder.embed = AsyncMock(return_value=EmbeddingResult(vector=[0.1] * 768, dimensions=768, model="nomic-embed-text"))
        mock_vector_store.search = AsyncMock(side_effect=VectorStoreError("DB fail"))
        res = asyncio.run(sem_service.lookup(eligible_req))
        self.assertIsNone(res)
        snap = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap.semantic.vector_store_failures, 1)
        self.assertEqual(snap.semantic.semantic_misses, 0)  # NOT counted as miss!

        # 4. Completed candidate search with no matching candidate
        mock_vector_store.search = AsyncMock(return_value=[])
        res = asyncio.run(sem_service.lookup(eligible_req))
        self.assertIsNone(res)
        snap = self.collector.get_namespace_snapshot("team_alpha")
        # Now and only now is a semantic miss recorded!
        self.assertEqual(snap.semantic.semantic_misses, 1)

    def test_provider_failure_not_incremented_on_cache_write_error(self):
        """Step 11.1: Exact cache write failure must NOT increment provider_calls_failed."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore

        failing_cache = MagicMock()
        failing_cache.get = AsyncMock(return_value=None)
        failing_cache.set = AsyncMock(side_effect=RuntimeError("Disk write failed"))
        failing_cache.record_miss = AsyncMock()

        core = IntelliCacheCore(
            cache=failing_cache,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        provider.generate = AsyncMock(return_value=GenerationResult(
            text="Valid LLM answer",
            model="test-model",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            latency_ms=35.0,
        ))

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Cache write failure test")],
            namespace="team_alpha",
        )

        # Must succeed and return the generated answer
        res = asyncio.run(core.execute_query(req, provider))
        self.assertEqual(res.text, "Valid LLM answer")

        snap = self.collector.get_namespace_snapshot("team_alpha")
        p = snap.provider_and_savings
        self.assertEqual(p.provider_calls_succeeded, 1)
        self.assertEqual(p.provider_calls_failed, 0)  # Provider succeeded!
        self.assertEqual(snap.request_and_cache.cache_write_failures, 1)  # Cache write failed!

    def test_cancellation_does_not_increment_provider_failure(self):
        """Step 11.1: asyncio.CancelledError must NOT be classified as upstream provider failure."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore

        core = IntelliCacheCore(
            cache=self.cache,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        provider.generate = AsyncMock(side_effect=asyncio.CancelledError("Client cancelled"))

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Cancellation test")],
            namespace="team_alpha",
        )

        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(core.execute_query(req, provider))

        snap = self.collector.get_namespace_snapshot("team_alpha")
        # Cancellation must NOT increment provider failure
        self.assertEqual(snap.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap.provider_and_savings.provider_calls_attempted, 1)

    def test_cache_clear_does_not_reset_runtime_metrics(self):
        """Step 11.1: Clearing cache entries must NOT wipe process lifetime metrics in collector."""
        from app.core.contracts import ChatMessage, GenerationRequest
        from app.core.service import IntelliCacheCore

        core = IntelliCacheCore(
            cache=self.cache,
            cache_enabled=True,
            metrics_collector=self.collector,
        )

        provider = AsyncMock()
        provider.generate = AsyncMock(return_value=GenerationResult(
            text="Clear cache test answer",
            model="test-model",
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=20.0,
        ))

        req = GenerationRequest(
            model="test-model",
            messages=[ChatMessage(role="user", content="Persistence test prompt")],
            namespace="team_alpha",
        )

        asyncio.run(core.execute_query(req, provider))

        # Metrics recorded
        snap_before = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap_before.request_and_cache.exact_cache_lookups, 1)

        # Clear cache entries
        asyncio.run(self.cache.clear())
        self.assertEqual(len(self.cache._entries), 0)

        # Metrics are preserved!
        snap_after = self.collector.get_namespace_snapshot("team_alpha")
        self.assertEqual(snap_after.request_and_cache.exact_cache_lookups, 1)
        self.assertEqual(snap_after.provider_and_savings.provider_calls_succeeded, 1)

    def test_unauthenticated_health_does_not_expose_usage_counters(self):
        """Step 11.1: Public GET /health must not leak hits, misses, hit_rate, or tenant metrics."""
        client = TestClient(app)
        resp = client.get("/health")
        self.assertEqual(resp.status_code, 200)
        data = resp.json()

        # Operational counters must be absent
        self.assertNotIn("hits", data)
        self.assertNotIn("misses", data)
        self.assertNotIn("hit_rate", data)
        self.assertNotIn("entries", data)
        self.assertNotIn("cache_namespace", data)

        cache_dict = data.get("cache", {})
        self.assertNotIn("hits", cache_dict)
        self.assertNotIn("misses", cache_dict)
        self.assertNotIn("hit_rate", cache_dict)
        self.assertNotIn("entries", cache_dict)

        # System readiness remains intact
        self.assertIn("status", data)
        self.assertIn("groq_configured", data)
        self.assertIn("cache_backend", data)
        self.assertIn("database", data)

    def test_admin_percentiles_unbiased_across_namespaces(self):
        """Step 11.1: Admin aggregate percentiles combine retained samples from all namespaces without insertion order bias."""
        # Namespace A: 5 samples at 10.0 ms
        for _ in range(5):
            self.collector.record_latency("ns_alpha", "total_request_latency_ms", 10.0)

        # Namespace B: 5 samples at 100.0 ms
        for _ in range(5):
            self.collector.record_latency("ns_beta", "total_request_latency_ms", 100.0)

        admin_snap = self.collector.get_admin_snapshot()
        summary = admin_snap.aggregate.latencies.total_request_latency_ms

        self.assertEqual(summary.sample_count, 10)
        self.assertEqual(summary.min, 10.0)
        self.assertEqual(summary.mean, 55.0)  # (5 * 10 + 5 * 100) / 10 = 55.0
        self.assertEqual(summary.p50, 55.0)


if __name__ == "__main__":
    unittest.main()
