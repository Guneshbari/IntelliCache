"""
End-to-End Gateway API Acceptance and Semantic Cache Validation Test Suite (Stage 2.2.0).
Validates:
1. Exact-match cache lifecycle (initial query, identical query hit, token savings, latency reduction).
2. Explicit cache bypass via request body ("bypass_cache": true).
3. TTL eviction and entry refresh.
4. Client-token multi-tenant namespace isolation (via X-Client-Token header).
5. Semantic cache retrieval for paraphrased technical queries ("cached": true, "cache_type": "semantic").
6. Empirical hard negative rejection (language differences, constraint modifications).
7. Fail-open resilience (embedding provider failure, vector store failure -> HTTP 200 via upstream LLM).
8. Context safety & multi-turn guardrails (conversation_id or multi-message bypass).
9. Streaming request isolation & strict SSE framing (data: {...}\\n\\n followed strictly by data: [DONE]\\n\\n).
"""

import asyncio
from datetime import datetime, timezone
import json
import math
import os
import time
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.config import Settings, get_settings, reload_settings
from app.core.contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from app.core.errors import ProviderError
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingTimeoutError
from app.embeddings.interfaces import EmbeddingProvider
from app.main import app
from app.metrics import get_metrics_collector
from app.providers.interfaces import LLMProvider
from app.routes.query import (
    get_core_service,
    get_llm_provider,
    get_query_cache,
    get_semantic_cache,
    get_single_flight_coordinator,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from app.semantic_cache.contracts import SemanticHit
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.errors import VectorStoreError
from app.vector_store.in_memory_store import InMemoryVectorStore


class MockAcceptanceLLMProvider(LLMProvider):
    """Deterministic LLM Provider for Acceptance Testing."""

    def __init__(self, default_response: str = "Acceptance LLM answer"):
        self.default_response = default_response
        self.call_count = 0
        self.generate_calls: list[GenerationRequest] = []
        self.stream_calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.call_count += 1
        self.generate_calls.append(request)
        prompt_text = request.messages[0].content if request.messages else ""
        text = f"{self.default_response} for: {prompt_text}"
        return GenerationResult(
            text=text,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=20, completion_tokens=30, total_tokens=50),
            latency_ms=85.0,
            cached=False,
            fingerprint="",
        )

    async def stream(self, request: GenerationRequest):
        self.call_count += 1
        self.stream_calls.append(request)
        tokens = ["Acceptance", " ", "stream", " ", "token", " ", "stream."]
        for tok in tokens:
            yield StreamChunk(delta=tok, done=False, cached=False)
        yield StreamChunk(
            delta="",
            done=True,
            cached=False,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=15, completion_tokens=25, total_tokens=40),
        )

    async def close(self) -> None:
        pass


class DeterministicAcceptanceEmbeddingProvider(EmbeddingProvider):
    """
    Deterministic embedding provider that maps specific known technical queries
    to unit-length vectors with controlled cosine similarities.
    """

    def __init__(self, dimensions: int = 384, model_name: str = "BAAI/bge-small-en-v1.5"):
        self.dimensions = dimensions
        self.model_name = model_name
        self.embed_calls: list[tuple[str, str]] = []
        self.fail_mode: bool = False

        # Predefined unit vectors using orthogonal axes to prevent cross-negative collisions:
        # Base Rust ownership vector: e0
        # Paraphrase vector (cos sim ~ 0.96): 0.96*e0 + sqrt(1 - 0.96^2)*e1
        # Hard negative - C++ language (cos sim ~ 0.80): 0.80*e0 + sqrt(1 - 0.80^2)*e2
        # Hard negative - Unsafe pointers constraint (cos sim ~ 0.85): 0.85*e0 + sqrt(1 - 0.85^2)*e3

        p_val = math.sqrt(1.0 - 0.96**2)
        c_val = math.sqrt(1.0 - 0.80**2)
        u_val = math.sqrt(1.0 - 0.85**2)

        v_base = [0.0] * self.dimensions
        v_base[0] = 1.0

        v_para = [0.0] * self.dimensions
        v_para[0] = 0.96
        v_para[1] = p_val

        v_cpp = [0.0] * self.dimensions
        v_cpp[0] = 0.80
        v_cpp[2] = c_val

        v_unsafe = [0.0] * self.dimensions
        v_unsafe[0] = 0.85
        v_unsafe[3] = u_val

        self.vector_map: dict[str, list[float]] = {
            "Explain memory ownership in Rust": v_base,
            "How does Rust manage memory ownership?": v_para,
            "Explain memory ownership in C++": v_cpp,
            "Explain Rust ownership with unsafe pointers": v_unsafe,
        }

    async def embed(self, text: str, task: EmbeddingTask | str = EmbeddingTask.QUERY) -> EmbeddingResult:
        task_str = task.value if isinstance(task, EmbeddingTask) else str(task)
        self.embed_calls.append((text, task_str))

        if self.fail_mode:
            raise EmbeddingTimeoutError("Deterministic embedding timeout simulated")

        if text in self.vector_map:
            vec = list(self.vector_map[text])
        else:
            # Deterministic pseudo-random vector based on string hash
            h = abs(hash(text))
            vec = [0.0] * self.dimensions
            vec[0] = (h % 100) / 100.0
            vec[1] = ((h // 100) % 100) / 100.0
            # Normalize
            norm = math.sqrt(sum(x * x for x in vec)) or 1.0
            vec = [x / norm for x in vec]

        return EmbeddingResult(
            vector=vec,
            model=self.model_name,
            dimensions=self.dimensions,
        )

    async def close(self) -> None:
        pass


class FailingVectorStore(InMemoryVectorStore):
    """Vector store that can be configured to fail during search or upsert."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fail_search = False
        self.search_call_count = 0

    async def search(self, *args, **kwargs):
        self.search_call_count += 1
        if self.fail_search:
            raise VectorStoreError("Simulated internal vector store search corruption")
        return await super().search(*args, **kwargs)


class TestApiAcceptance(unittest.TestCase):
    """
    Stage 2.2.0 Gateway API Acceptance Test Suite.
    Verifies full lifecycle over HTTP: Exact Cache, Semantic Cache, Context Safety,
    Multi-tenant isolation, Fail-open semantics, and SSE Streaming framing.
    """

    def setUp(self):
        get_metrics_collector().reset()
        self._saved_overrides = dict(app.dependency_overrides)
        self._saved_cache = get_query_cache()
        self._saved_vector_store = get_vector_store()
        self._saved_semantic_cache = get_semantic_cache()

        # Multi-client token configuration: tenant_a, tenant_b
        self.tokens_config = json.dumps({
            "token-alpha": "tenant_a",
            "token-beta": "tenant_b",
        })

        self.env_patcher = patch.dict(os.environ, {
            "CLIENT_API_TOKENS": self.tokens_config,
            "CACHE_BACKEND": "memory",
            "CACHE_ENABLED": "true",
            "CACHE_TTL_SECONDS": "3600",
            "SEMANTIC_CACHE_ENABLED": "true",
            "VECTOR_STORE_BACKEND": "memory",
            "VECTOR_DIMENSIONS": "384",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "SEMANTIC_CACHE_MIN_SIMILARITY": "0.92",
            "SEMANTIC_CACHE_MIN_MARGIN": "0.03",
        }, clear=False)
        self.env_patcher.start()
        reload_settings()

        self.client = TestClient(app)

        # Setup in-memory caches
        self.exact_cache = ExactCache(max_entries=1000, ttl_seconds=3600)
        set_query_cache(self.exact_cache)

        self.vector_store = FailingVectorStore(
            dimensions=384,
            default_namespace="tenant_a",
            default_model="BAAI/bge-small-en-v1.5",
            exact_cache=self.exact_cache,
            max_records=1000,
        )
        set_vector_store(self.vector_store)

        self.embedding_provider = DeterministicAcceptanceEmbeddingProvider(
            dimensions=384,
            model_name="BAAI/bge-small-en-v1.5",
        )

        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="BAAI/bge-small-en-v1.5",
        )

        self.semantic_cache = SemanticCacheService(
            embedding_provider=self.embedding_provider,
            vector_store=self.vector_store,
            policy=self.policy,
            default_namespace="tenant_a",
        )
        set_semantic_cache(self.semantic_cache)

        self.mock_llm = MockAcceptanceLLMProvider()
        app.dependency_overrides[get_llm_provider] = lambda: self.mock_llm

        # Reset core singleton to use our newly configured services
        from app.api.routes import _core_singleton
        import app.api.routes as routes_mod
        routes_mod._core_singleton = None

    def tearDown(self):
        self.env_patcher.stop()
        reload_settings()
        app.dependency_overrides.clear()
        app.dependency_overrides.update(self._saved_overrides)
        set_query_cache(self._saved_cache)
        set_vector_store(self._saved_vector_store)
        set_semantic_cache(self._saved_semantic_cache)
        from app.api.routes import _core_singleton
        import app.api.routes as routes_mod
        routes_mod._core_singleton = None
        get_metrics_collector().reset()

    # -----------------------------------------------------------------------
    # Area 3: End-to-End Gateway API Lifecycle Acceptance
    # -----------------------------------------------------------------------

    def test_01_exact_cache_lifecycle_and_latency_reduction(self):
        """
        Scenario 1 & 2:
        - Initial query results in cache miss (cached: false, cache_type: 'none').
        - Identical second query results in exact hit (cached: true, cache_type: 'exact').
        - Verifies latency reduction and token savings.
        """
        headers = {"X-Client-Token": "token-alpha"}
        payload = {"prompt": "What is memory ownership in programming?"}

        # First request: Miss
        t0 = time.perf_counter()
        resp1 = self.client.post("/query", json=payload, headers=headers)
        lat1 = (time.perf_counter() - t0) * 1000.0

        self.assertEqual(resp1.status_code, 200)
        data1 = resp1.json()
        self.assertFalse(data1["cached"])
        self.assertEqual(data1["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 1)

        # Second request: Exact Hit
        t1 = time.perf_counter()
        resp2 = self.client.post("/query", json=payload, headers=headers)
        lat2 = (time.perf_counter() - t1) * 1000.0

        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertTrue(data2["cached"])
        self.assertEqual(data2["cache_type"], "exact")
        self.assertEqual(data2["response"], data1["response"])
        self.assertEqual(self.mock_llm.call_count, 1, "Upstream LLM must not be called on exact hit")

        # Metric check: tokens saved
        self.assertGreater(data2["usage"]["total_tokens"], 0)

    def test_02_explicit_cache_bypass_semantics(self):
        """
        Scenario 3:
        Query with 'bypass_cache: true' in request body bypasses both exact and semantic cache,
        forcing an upstream provider call even if an entry is cached.
        """
        headers = {"X-Client-Token": "token-alpha"}
        payload = {"prompt": "Bypass test prompt"}

        # Populate cache
        resp1 = self.client.post("/query", json=payload, headers=headers)
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 1)

        # Call with bypass_cache: true
        bypass_payload = {"prompt": "Bypass test prompt", "bypass_cache": True}
        resp2 = self.client.post("/query", json=bypass_payload, headers=headers)
        self.assertEqual(resp2.status_code, 200)
        data2 = resp2.json()
        self.assertFalse(data2["cached"])
        self.assertEqual(data2["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 2, "Bypass must invoke upstream LLM")

    def test_03_ttl_expiration_and_entry_eviction(self):
        """
        Scenario 4:
        Entries past their TTL duration are evicted and transparently regenerated.
        """
        # Configure short TTL (1 second)
        short_cache = ExactCache(max_entries=100, ttl_seconds=1)
        set_query_cache(short_cache)
        self.vector_store._exact_cache = short_cache

        headers = {"X-Client-Token": "token-alpha"}
        payload = {"prompt": "TTL test prompt"}

        resp1 = self.client.post("/query", json=payload, headers=headers)
        self.assertEqual(resp1.status_code, 200)
        self.assertFalse(resp1.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 1)

        # Immediate repeat -> hit
        resp_immediate = self.client.post("/query", json=payload, headers=headers)
        self.assertTrue(resp_immediate.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 1)

        # Sleep past TTL
        time.sleep(1.1)

        # Post-expiry repeat -> miss and regenerate
        resp_expired = self.client.post("/query", json=payload, headers=headers)
        self.assertFalse(resp_expired.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 2, "Expired entry must trigger upstream generation")

    def test_04_client_token_multi_tenant_namespace_isolation(self):
        """
        Scenario 5:
        Client tokens map strictly to server-managed tenant namespaces.
        token-alpha (tenant_a) cannot contaminate token-beta (tenant_b).
        """
        payload = {"prompt": "Tenant isolation verification prompt"}

        # Client A queries and caches
        resp_a = self.client.post("/query", json=payload, headers={"X-Client-Token": "token-alpha"})
        self.assertEqual(resp_a.status_code, 200)
        self.assertFalse(resp_a.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 1)

        # Client A identical repeat -> Hit
        resp_a2 = self.client.post("/query", json=payload, headers={"X-Client-Token": "token-alpha"})
        self.assertTrue(resp_a2.json()["cached"])
        self.assertEqual(self.mock_llm.call_count, 1)

        # Client B sends the exact same prompt with token-beta -> Must Miss (tenant isolation)
        resp_b = self.client.post("/query", json=payload, headers={"X-Client-Token": "token-beta"})
        self.assertEqual(resp_b.status_code, 200)
        self.assertFalse(resp_b.json()["cached"], "Client B must miss cache populated by Client A")
        self.assertEqual(self.mock_llm.call_count, 2)

    # -----------------------------------------------------------------------
    # Area 4: Semantic Cache Retrieval, Rejection & Fail-Open
    # -----------------------------------------------------------------------

    def test_05_semantic_cache_hit_on_paraphrased_query(self):
        """
        Scenario 6:
        - Seed query: 'Explain memory ownership in Rust'
        - Paraphrased query: 'How does Rust manage memory ownership?'
        Cosine similarity between seed and paraphrase is ~0.96 (>= threshold 0.92).
        Exact cache misses; semantic cache retrieves and validates candidate, returning:
        cached: true, cache_type: 'semantic'.
        """
        headers = {"X-Client-Token": "token-alpha"}

        # 1. Seed query
        seed_prompt = "Explain memory ownership in Rust"
        resp_seed = self.client.post("/query", json={"prompt": seed_prompt}, headers=headers)
        self.assertEqual(resp_seed.status_code, 200)
        self.assertFalse(resp_seed.json()["cached"])
        self.assertEqual(resp_seed.json()["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 1)
        original_answer = resp_seed.json()["response"]

        # 2. Paraphrased query
        para_prompt = "How does Rust manage memory ownership?"
        resp_para = self.client.post("/query", json={"prompt": para_prompt}, headers=headers)
        self.assertEqual(resp_para.status_code, 200)
        data_para = resp_para.json()

        self.assertTrue(data_para["cached"])
        self.assertEqual(data_para["cache_type"], "semantic")
        self.assertEqual(data_para["response"], original_answer)
        self.assertEqual(self.mock_llm.call_count, 1, "Semantic hit must not invoke upstream LLM")

    def test_06_deterministic_threshold_and_margin_rejection(self):
        """
        Scenario 7 (Deterministic Policy Unit Validation):
        Seed query: 'Explain memory ownership in Rust'
        Hard negative 1 (Language change synthetic vector): similarity 0.80 (< 0.92 threshold)
        Hard negative 2 (Constraint change synthetic vector): similarity 0.85 (< 0.92 threshold)
        Verifies that when similarity scores fall below the policy threshold,
        the deterministic candidate evaluator safely rejects the cache candidate.
        Note: Real dense embedding models without compatibility filters produce higher similarities
        (e.g. 0.8923 - 0.9661) on cross-language and constraint inversions, which requires
        future domain-level AST/metadata compatibility filters.
        """
        headers = {"X-Client-Token": "token-alpha"}

        # 1. Seed query
        self.client.post("/query", json={"prompt": "Explain memory ownership in Rust"}, headers=headers)
        self.assertEqual(self.mock_llm.call_count, 1)

        # 2. Language difference hard negative
        resp_cpp = self.client.post("/query", json={"prompt": "Explain memory ownership in C++"}, headers=headers)
        self.assertEqual(resp_cpp.status_code, 200)
        self.assertFalse(resp_cpp.json()["cached"], "Language difference must not trigger false semantic hit")
        self.assertEqual(resp_cpp.json()["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 2)

        # 3. Constraint difference hard negative
        resp_unsafe = self.client.post(
            "/query", json={"prompt": "Explain Rust ownership with unsafe pointers"}, headers=headers
        )
        self.assertEqual(resp_unsafe.status_code, 200)
        self.assertFalse(resp_unsafe.json()["cached"], "Constraint modification must not trigger false semantic hit")
        self.assertEqual(resp_unsafe.json()["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 3)

    def test_07_fail_open_on_embedding_provider_error(self):
        """
        Scenario 8a:
        When embedding provider raises an error (e.g. timeout), the gateway fails open:
        logs warning and serves fresh response from upstream LLM with HTTP 200 and cached: false.
        Uses an eligible technical query and asserts that embedding failure counter increments.
        """
        headers = {"X-Client-Token": "token-alpha"}
        self.embedding_provider.fail_mode = True
        initial_embed_count = len(self.embedding_provider.embed_calls)
        initial_failures = self.semantic_cache.stats.embedding_failures

        # Eligible technical query that passes semantic cache policy check_eligibility
        prompt = "How do I debug an embedding timeout in Python?"
        resp = self.client.post("/query", json={"prompt": prompt}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["cached"])
        self.assertEqual(data["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 1)

        # Verify failure path was actually exercised
        self.assertGreater(len(self.embedding_provider.embed_calls), initial_embed_count, "Embedding lookup must be attempted")
        self.assertEqual(self.semantic_cache.stats.embedding_failures, initial_failures + 1, "Embedding failures counter must increment")

    def test_08_fail_open_on_vector_store_error(self):
        """
        Scenario 8b:
        When vector store raises a VectorStoreError during candidate retrieval, the gateway fails open:
        logs warning and serves fresh response from upstream LLM with HTTP 200 and cached: false.
        Uses an eligible technical query and asserts that vector store failure counter increments.
        """
        headers = {"X-Client-Token": "token-alpha"}
        self.vector_store.fail_search = True
        initial_search_count = self.vector_store.search_call_count
        initial_failures = self.semantic_cache.stats.vector_store_failures

        # Eligible technical query that passes semantic cache policy check_eligibility
        prompt = "How do I debug a vector store search error in Python?"
        resp = self.client.post("/query", json={"prompt": prompt}, headers=headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertFalse(data["cached"])
        self.assertEqual(data["cache_type"], "none")
        self.assertEqual(self.mock_llm.call_count, 1)

        # Verify failure path was actually exercised
        self.assertGreater(self.vector_store.search_call_count, initial_search_count, "Vector search must be attempted")
        self.assertEqual(self.semantic_cache.stats.vector_store_failures, initial_failures + 1, "Vector store failures counter must increment")

    # -----------------------------------------------------------------------
    # Area 5: Context Safety & Multi-Turn Guardrails
    # -----------------------------------------------------------------------

    def test_09_multi_turn_and_context_semantic_cache_bypass(self):
        """
        Scenario 9:
        Multi-turn requests (multiple messages), requests with conversation_id, or system prompts
        must be deemed ineligible for semantic cache and bypass semantic lookup completely.
        """
        headers = {"X-Client-Token": "token-alpha"}

        # 1. Explicit conversation_id payload (client-managed conversation turn)
        initial_embeds = len(self.embedding_provider.embed_calls)
        initial_searches = self.vector_store.search_call_count
        conv_payload = {
            "messages": [{"role": "user", "content": "How do I write a binary search in Python?"}],
            "conversation_id": "conv-test-bypass-id-12345",
        }
        resp_conv = self.client.post("/query", json=conv_payload, headers=headers)
        self.assertEqual(resp_conv.status_code, 200)
        self.assertFalse(resp_conv.json()["cached"])
        self.assertEqual(resp_conv.json()["cache_type"], "none")
        self.assertEqual(len(self.embedding_provider.embed_calls), initial_embeds, "conversation_id must not invoke embedding generation")
        self.assertEqual(self.vector_store.search_call_count, initial_searches, "conversation_id must not invoke vector search")

        # 2. Multi-turn messages payload
        initial_embeds = len(self.embedding_provider.embed_calls)
        initial_searches = self.vector_store.search_call_count
        multi_msg_payload = {
            "messages": [
                {"role": "user", "content": "Explain memory ownership in Rust"},
                {"role": "assistant", "content": "In Rust, ownership is enforced at compile time."},
                {"role": "user", "content": "How does Rust manage memory ownership?"},
            ]
        }
        resp = self.client.post("/query", json=multi_msg_payload, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.json()["cached"])
        self.assertEqual(resp.json()["cache_type"], "none")
        self.assertEqual(len(self.embedding_provider.embed_calls), initial_embeds, "Multi-message request must not invoke embedding generation")
        self.assertEqual(self.vector_store.search_call_count, initial_searches, "Multi-message request must not invoke vector search")

        # 3. System prompt payload
        initial_embeds = len(self.embedding_provider.embed_calls)
        initial_searches = self.vector_store.search_call_count
        system_payload = {
            "prompt": "How does Rust manage memory ownership?",
            "system_prompt": "You are a compiler tutor.",
        }
        resp_sys = self.client.post("/query", json=system_payload, headers=headers)
        self.assertEqual(resp_sys.status_code, 200)
        self.assertFalse(resp_sys.json()["cached"])
        self.assertEqual(len(self.embedding_provider.embed_calls), initial_embeds, "System prompt request must not invoke embedding generation")
        self.assertEqual(self.vector_store.search_call_count, initial_searches, "System prompt request must not invoke vector search")

    # -----------------------------------------------------------------------
    # Area 6: Streaming Request Isolation & SSE Framing
    # -----------------------------------------------------------------------

    def test_10_streaming_semantic_cache_bypass_and_sse_framing(self):
        """
        Scenario 10 & 11:
        1. stream: true bypasses semantic cache lookup completely.
        2. SSE events strictly adhere to data: {...}\\n\\n framing.
        3. Terminal data event has done: true, followed strictly by data: [DONE]\\n\\n.
        4. No trailing data or duplicate events after [DONE].
        """
        headers = {"X-Client-Token": "token-alpha"}

        # Seed exact cache first with a query
        seed_prompt = "Streaming test prompt"
        self.client.post("/query", json={"prompt": seed_prompt}, headers=headers)
        self.assertEqual(self.mock_llm.call_count, 1)

        # Send streaming request with paraphrased prompt -> should NOT hit semantic cache
        initial_embeds = len(self.embedding_provider.embed_calls)
        initial_searches = self.vector_store.search_call_count
        stream_payload = {
            "prompt": "How does Rust manage memory ownership?",
            "stream": True,
        }
        resp = self.client.post("/query", json=stream_payload, headers=headers)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/event-stream", resp.headers["content-type"])
        self.assertEqual(len(self.embedding_provider.embed_calls), initial_embeds, "Streaming request must not invoke semantic embedding generation")
        self.assertEqual(self.vector_store.search_call_count, initial_searches, "Streaming request must not invoke vector store search")

        raw_text = resp.text
        lines = raw_text.split("\n")

        # Parse SSE events
        events: list[dict] = []
        done_marker_seen = False
        trailing_lines_after_done: list[str] = []

        for line in lines:
            line_str = line.strip()
            if not line_str:
                continue

            if done_marker_seen:
                trailing_lines_after_done.append(line_str)
                continue

            if line_str == "data: [DONE]":
                done_marker_seen = True
                continue

            self.assertTrue(line_str.startswith("data: "), f"Malformed SSE line: '{line_str}'")
            json_payload = line_str[len("data: "):]
            event_obj = json.loads(json_payload)
            events.append(event_obj)

        self.assertTrue(done_marker_seen, "Stream must terminate with 'data: [DONE]'")
        self.assertEqual(
            trailing_lines_after_done,
            [],
            f"No trailing data permitted after 'data: [DONE]', received: {trailing_lines_after_done}",
        )
        self.assertGreater(len(events), 0, "Expected at least one stream chunk event")

        # Verify terminal chunk has done=True
        terminal_event = events[-1]
        self.assertTrue(terminal_event.get("done"), "Final data payload before [DONE] must have done: true")
        self.assertFalse(terminal_event.get("cached"), "Streaming requests bypass semantic cache")

    @unittest.skipUnless(os.environ.get("RUN_FASTEMBED_SMOKE") == "1", "FastEmbed real acceptance requires RUN_FASTEMBED_SMOKE=1")
    def test_11_real_fastembed_e2e_semantic_cache_lifecycle(self):
        """
        Opt-in end-to-end acceptance test using real local FastEmbed CPU inference.
        Verifies actual model weights download, ONNX execution, model identity/dimensions matching,
        and live semantic cache hit over HTTP using the configured runtime threshold 0.92 and margin 0.03.
        """
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        real_provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5")
        try:
            # Verify provider model identity and dimensions
            self.assertEqual(real_provider.model_name, "BAAI/bge-small-en-v1.5")
            self.assertEqual(real_provider.dimensions, 384)

            real_semantic_cache = SemanticCacheService(
                embedding_provider=real_provider,
                vector_store=self.vector_store,
                policy=SemanticCachePolicy(
                    min_similarity=0.92,  # Configured runtime default
                    min_margin=0.03,
                    top_k=5,
                    configured_model="BAAI/bge-small-en-v1.5",
                ),
                default_namespace="tenant_a",
            )
            set_semantic_cache(real_semantic_cache)
            from app.api.routes import _core_singleton
            import app.api.routes as routes_mod
            routes_mod._core_singleton = None

            headers = {"X-Client-Token": "token-alpha"}

            # 1. Seed query
            seed_prompt = "Explain memory ownership in Rust"
            resp1 = self.client.post("/query", json={"prompt": seed_prompt}, headers=headers)
            self.assertEqual(resp1.status_code, 200)
            self.assertFalse(resp1.json()["cached"])

            # 2. Paraphrased query with real FastEmbed embeddings (real cosine sim ~ 0.9602 >= 0.92)
            para_prompt = "How does Rust manage memory ownership?"
            resp2 = self.client.post("/query", json={"prompt": para_prompt}, headers=headers)
            self.assertEqual(resp2.status_code, 200)
            data2 = resp2.json()
            self.assertTrue(data2["cached"], "Real FastEmbed must match natural language paraphrase at runtime threshold 0.92")
            self.assertEqual(data2["cache_type"], "semantic")
        finally:
            asyncio.run(real_provider.close())


if __name__ == "__main__":
    unittest.main()
