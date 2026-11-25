"""
Stage 3.5.4 Comprehensive Test Suite:
- Server-Side Runtime Serving Kill Switch (two-point enforcement, fail-closed states, atomic concurrency, HTTP admin control)
- Per-Principal Privacy Isolation ('private' vs 'tenant_shared' vs 'unknown'/legacy across InMemory & PostgreSQL/pgvector)
- Evidence Scope Binding & Tamper/Stripping Rejection
- HTTP Spoofing Attack Rejection & Scenarios A through F
- Streaming & SingleFlight Privacy Isolation
- Performance Microbenchmark & PostgreSQL Scoped Vector Search Timing
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import threading
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import asyncpg
from fastapi.testclient import TestClient

from app.api.routes import (
    get_llm_provider,
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import ClientTokenPolicy, Settings, reload_settings
from app.conversations.identity import derive_principal_id
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.main import create_app
from app.metrics import get_metrics_collector
from app.privacy import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    CACHE_VISIBILITY_UNKNOWN,
    derive_scoped_exact_key,
    matches_cache_scope,
    normalize_cache_visibility,
)
from app.providers.interfaces import LLMProvider
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.contracts import CompatibilityReasonCode, SemanticCompatibilityMetadata
from app.semantic_cache.control import (
    SemanticServingController,
    reset_semantic_serving_controller,
    sync_semantic_serving_controller_from_settings,
)
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.verification import ResponseArtifactVerifier
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore

_STRONG_EVIDENCE_SECRET = "s354-evidence-hmac-key-0123456789abcdefABCDEF!@"
_STRONG_IDENTITY_SECRET = "s354-principal-hmac-key-9876543210fedcbaFEDCBA!"


def _topic_vec(dim: int, angle_deg: float) -> list[float]:
    vec = [0.0] * dim
    rad = math.radians(angle_deg)
    vec[0] = math.cos(rad)
    vec[1] = math.sin(rad)
    return vec


class TopicEmbedder(EmbeddingProvider):
    """Deterministic unit-vector embedder grouping prompts by keyword angle."""

    def __init__(
        self,
        keyword_angles: list[tuple[str, float]] | None = None,
        *,
        model: str = "nomic-embed-text",
        dim: int = 768,
    ) -> None:
        self._angles = list(keyword_angles or [("reverse", 0.0), ("sort", 35.0), ("json", 70.0)])
        self._model = model
        self._dim = dim
        self.calls: list[tuple[str, EmbeddingTask]] = []

    @property
    def provider_name(self) -> str:
        return "topic-mock"

    @property
    def model_name(self) -> str:
        return self._model

    @property
    def dimensions(self) -> int:
        return self._dim

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        self.calls.append((text, task))
        lowered = text.lower()
        angle = 140.0
        for kw, deg in self._angles:
            if kw in lowered:
                angle = deg
                break
        return EmbeddingResult(
            vector=_topic_vec(self._dim, angle),
            model=self._model,
            dimensions=self._dim,
        )

    async def embed_batch(self, texts: list[str], task: EmbeddingTask = EmbeddingTask.QUERY) -> list[EmbeddingResult]:
        return [await self.embed(t, task=task) for t in texts]

    async def close(self) -> None:
        return None


class CountingMockProvider(LLMProvider):
    """Async mock LLM provider counting non-streaming & streaming upstream invocations."""

    def __init__(self, prefix: str = "LIVE", delay_s: float = 0.0) -> None:
        self.prefix = prefix
        self.delay_s = delay_s
        self.calls: list[GenerationRequest] = []
        self.stream_calls: list[GenerationRequest] = []
        self.seq = 0

    @property
    def name(self) -> str:
        return "groq"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        if self.delay_s > 0:
            await asyncio.sleep(self.delay_s)
        self.seq += 1
        self.calls.append(request)
        return GenerationResult(
            text=f"{self.prefix}-{self.seq}: def rev(x): return list(reversed(x))",
            model=request.model,
            usage=UsageMetrics(prompt_tokens=21, completion_tokens=34, total_tokens=55),
            latency_ms=420.0,
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        from app.core.contracts import StreamChunk

        self.seq += 1
        self.stream_calls.append(request)
        yield StreamChunk(
            delta=f"{self.prefix}-STREAM-{self.seq}",
            done=False,
            model=request.model,
        )
        yield StreamChunk(
            delta="",
            done=True,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=15, total_tokens=25),
        )

    async def close(self) -> None:
        return None


def _make_req(
    prompt: str,
    *,
    namespace: str = "team_alpha",
    visibility: str = CACHE_VISIBILITY_PRIVATE,
    principal_id: str | None = None,
    allow_shared_reads: bool = False,
    model: str = "llama-3.3-70b-versatile",
    provider: str = "groq",
    temperature: float = 0.7,
    max_tokens: int | None = 1000,
    bypass_cache: bool = False,
    stream: bool = False,
) -> GenerationRequest:
    return GenerationRequest(
        model=model,
        messages=[ChatMessage(role="user", content=prompt)],
        temperature=temperature,
        max_tokens=max_tokens,
        provider=provider,
        namespace=namespace,
        visibility=visibility,
        principal_id=principal_id,
        allow_shared_reads=allow_shared_reads,
        bypass_cache=bypass_cache,
        stream=stream,
    )


class TestStage354ConfigAndControl(unittest.IsolatedAsyncioTestCase):
    """Config defaults, token policies, runtime control matrix, concurrency & Checkpoint 2."""

    def setUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)

    def tearDown(self) -> None:
        reset_semantic_serving_controller(False)
        reload_settings()

    def test_defaults_and_config_validation(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            s = Settings()
            self.assertFalse(s.semantic_cache_enabled)
            self.assertFalse(s.semantic_cache_serving_enabled)
            self.assertEqual(s.semantic_cache_default_visibility, "private")
            self.assertIsNone(s.cache_default_visibility)
            self.assertFalse(s.shadow_evaluation_enabled)
            self.assertAlmostEqual(s.semantic_cache_min_similarity, 0.92)
            self.assertAlmostEqual(s.semantic_cache_min_margin, 0.03)
            self.assertEqual(s.semantic_cache_top_k, 5)
            self.assertEqual(s.semantic_cache_max_retrieval_candidates, 25)

        with self.assertRaises(ValueError):
            with patch.dict(os.environ, {"SEMANTIC_CACHE_DEFAULT_VISIBILITY": "public"}, clear=True):
                Settings()

        with self.assertRaises(ValueError):
            with patch.dict(os.environ, {"CACHE_DEFAULT_VISIBILITY": "unknown"}, clear=True):
                Settings()

        with self.assertRaises(ValueError):
            with patch.dict(os.environ, {"PRINCIPAL_IDENTITY_SECRET": "short"}, clear=True):
                Settings()

        with self.assertRaises(ValueError):
            with patch.dict(
                os.environ,
                {"PRINCIPAL_IDENTITY_SECRET": "intellicache-default-identity-secret-change-in-production"},
                clear=True,
            ):
                Settings()

        raw_tokens = json.dumps({
            "tok_shorthand": "team_alpha",
            "tok_priv": {"namespace": "team_alpha", "visibility": "private", "allow_shared_reads": False},
            "tok_shared": {"namespace": "team_alpha", "visibility": "tenant_shared"},
            "tok_hybrid": {"namespace": "team_beta", "visibility": "private", "allow_shared_reads": True},
        })
        with patch.dict(os.environ, {"CLIENT_API_TOKENS": raw_tokens}, clear=True):
            s_pol = Settings()
            policies = s_pol.get_client_token_policies()
            self.assertEqual(policies["tok_shorthand"], ClientTokenPolicy(namespace="team_alpha"))
            self.assertEqual(
                policies["tok_priv"],
                ClientTokenPolicy(namespace="team_alpha", visibility="private", allow_shared_reads=False),
            )
            self.assertEqual(
                policies["tok_shared"],
                ClientTokenPolicy(namespace="team_alpha", visibility="tenant_shared", allow_shared_reads=False),
            )
            self.assertEqual(
                policies["tok_hybrid"],
                ClientTokenPolicy(namespace="team_beta", visibility="private", allow_shared_reads=True),
            )

        for bad_tok in (
            {"tok": {"namespace": "a", "visibility": "global"}},
            {"tok": {"namespace": "a", "allow_shared_reads": "true"}},
            {"tok": {"namespace": "a", "extra": 1}},
        ):
            with self.assertRaises(ValueError):
                with patch.dict(os.environ, {"CLIENT_API_TOKENS": json.dumps(bad_tok)}, clear=True):
                    Settings()

    def test_controller_fail_closed_and_concurrent_toggles(self) -> None:
        collector = get_metrics_collector()
        ctrl = SemanticServingController(initial_enabled=False, metrics_collector=collector)
        self.assertFalse(ctrl.is_serving_enabled("team_alpha"))

        # Corrupt / non-bool state provider fails closed and increments runtime_control_failures
        for bad_val in ("true", 1, None, {"enabled": True}):
            ctrl.set_state_provider(lambda _ns, v=bad_val: v)
            self.assertFalse(ctrl.is_serving_enabled("team_alpha"))
        ctrl.set_state_provider(lambda _ns: (_ for _ in ()).throw(RuntimeError("control store down")))
        self.assertFalse(ctrl.is_serving_enabled("team_alpha"))
        snap = collector.get_namespace_snapshot("team_alpha")
        self.assertGreaterEqual(snap.semantic.runtime_control_failures, 5)

        # Settings sync with non-bool attribute fails closed
        fake_settings = type("BadSettings", (), {"semantic_cache_serving_enabled": "yes"})()
        global_ctrl = sync_semantic_serving_controller_from_settings(fake_settings)
        self.assertFalse(global_ctrl.is_serving_enabled("default"))

        # Namespace override & clearing
        ctrl.set_state_provider(None)
        ctrl.set_serving_enabled(True, updated_by="ops", reason="canary")
        self.assertTrue(ctrl.is_serving_enabled("team_alpha"))
        self.assertTrue(ctrl.is_serving_enabled("team_beta"))
        ctrl.set_serving_enabled(False, namespace="team_alpha", reason="pause tenant")
        self.assertFalse(ctrl.is_serving_enabled("team_alpha"))
        self.assertTrue(ctrl.is_serving_enabled("team_beta"))
        ctrl.set_serving_enabled(True, namespace="team_alpha", clear_namespace_override=True, reason="resume")
        self.assertTrue(ctrl.is_serving_enabled("team_alpha"))

        # Concurrent multi-threaded readers and writers must remain strictly boolean and consistent
        errors: list[BaseException] = []

        def writer() -> None:
            try:
                for i in range(150):
                    ctrl.set_serving_enabled((i % 2) == 0, reason=f"toggle-{i}")
            except BaseException as exc:
                errors.append(exc)

        def reader() -> None:
            try:
                for _ in range(300):
                    val = ctrl.is_serving_enabled("team_alpha")
                    self.assertIsInstance(val, bool)
                    st = ctrl.get_state()
                    self.assertIsInstance(st.runtime_enabled, bool)
            except BaseException as exc:
                errors.append(exc)

        threads = [threading.Thread(target=writer), threading.Thread(target=reader), threading.Thread(target=reader)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])

    async def test_startup_runtime_permutation_matrix_and_checkpoint_2_cancellation(self) -> None:
        collector = get_metrics_collector()
        auth = EvidenceAuthority(secret_key=_STRONG_EVIDENCE_SECRET)
        embedder = TopicEmbedder([("reverse", 0.0)], dim=768)

        # (startup_enabled, runtime_enabled, expect_semantic_hit)
        matrix = [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ]
        for startup_en, runtime_en, expect_hit in matrix:
            collector.reset()
            cache = ExactCache(max_entries=100, ttl_seconds=3600)
            vstore = InMemoryVectorStore(dimensions=768, default_model="nomic-embed-text", exact_cache=cache)
            policy = SemanticCachePolicy(
                min_similarity=0.92,
                min_margin=0.03,
                top_k=5,
                configured_model="nomic-embed-text",
                evidence_authority=auth,
            )
            svc = SemanticCacheService(
                embedding_provider=embedder,
                vector_store=vstore,
                policy=policy,
                evidence_authority=auth,
            )
            ctrl = SemanticServingController(initial_enabled=runtime_en, metrics_collector=collector)
            core = IntelliCacheCore(
                cache=cache,
                semantic_cache=svc,
                cache_enabled=True,
                semantic_serving_controller=ctrl,
                semantic_cache_startup_enabled=startup_en,
            )
            # Pre-seed an identical verified entry directly via service
            seed_req = _make_req("How do I reverse a list in Python?", namespace="ns_m", visibility="tenant_shared")
            seed_res = GenerationResult(
                text="def rev(x): return list(reversed(x))",
                model=seed_req.model,
                usage=UsageMetrics(prompt_tokens=20, completion_tokens=30, total_tokens=50),
                latency_ms=300.0,
                fingerprint="seed_fp_matrix",
                finish_reason="stop",
            )
            await cache.set(
                "seed_fp_matrix",
                CacheEntry(
                    response=seed_res.text,
                    model=seed_req.model,
                    usage=seed_res.usage,
                    latency_ms=300.0,
                    provider="groq",
                    visibility="tenant_shared",
                ),
                namespace="ns_m",
            )
            self.assertTrue(await svc.index(seed_req, seed_res))

            prov = CountingMockProvider()
            probe_req = _make_req("How to write Python code to reverse a list?", namespace="ns_m", visibility="tenant_shared")
            out = await core.execute_query(probe_req, prov)
            snap = collector.get_namespace_snapshot("ns_m")
            if expect_hit:
                self.assertTrue(out.cached)
                self.assertEqual(out.cache_type, "semantic")
                self.assertEqual(len(prov.calls), 0)
                self.assertEqual(snap.semantic.semantic_hits, 1)
            else:
                self.assertFalse(out.cached)
                self.assertEqual(out.cache_type, "none")
                self.assertEqual(len(prov.calls), 1)
                self.assertEqual(snap.semantic.semantic_hits, 0)
                if startup_en and not runtime_en:
                    self.assertGreaterEqual(snap.semantic.semantic_runtime_disabled_skips, 1)

        # Checkpoint 2 test: runtime switch is flipped OFF during in-flight vector_store.search()
        collector.reset()
        cache2 = ExactCache(max_entries=100, ttl_seconds=3600)
        vstore2 = InMemoryVectorStore(dimensions=768, default_model="nomic-embed-text", exact_cache=cache2)
        policy2 = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            evidence_authority=auth,
        )
        svc2 = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=vstore2,
            policy=policy2,
            evidence_authority=auth,
        )
        ctrl2 = SemanticServingController(initial_enabled=True, metrics_collector=collector)

        req_seed = _make_req("How do I reverse a list in Python?", namespace="ns_cp2", visibility="tenant_shared")
        res_seed = GenerationResult(
            text="def rev(x): return list(reversed(x))",
            model=req_seed.model,
            usage=UsageMetrics(prompt_tokens=20, completion_tokens=30, total_tokens=50),
            latency_ms=300.0,
            fingerprint="seed_fp_cp2",
            finish_reason="stop",
        )
        await cache2.set(
            "seed_fp_cp2",
            CacheEntry(
                response=res_seed.text,
                model=req_seed.model,
                usage=res_seed.usage,
                latency_ms=300.0,
                provider="groq",
                visibility="tenant_shared",
            ),
            namespace="ns_cp2",
        )
        self.assertTrue(await svc2.index(req_seed, res_seed))

        orig_search = vstore2.search

        async def search_and_flip_off(*args, **kwargs):
            found = await orig_search(*args, **kwargs)
            ctrl2.set_serving_enabled(False, updated_by="incident_kill", reason="checkpoint2 test")
            return found

        vstore2.search = search_and_flip_off  # type: ignore[method-assign]
        core2 = IntelliCacheCore(
            cache=cache2,
            semantic_cache=svc2,
            cache_enabled=True,
            semantic_serving_controller=ctrl2,
            semantic_cache_startup_enabled=True,
        )
        prov2 = CountingMockProvider("FALLBACK")
        res2 = await core2.execute_query(
            _make_req("Way to reverse Python list?", namespace="ns_cp2", visibility="tenant_shared"),
            prov2,
        )
        self.assertFalse(res2.cached)
        self.assertEqual(res2.cache_type, "none")
        self.assertTrue(res2.text.startswith("FALLBACK-1:"))
        self.assertEqual(len(prov2.calls), 1)
        snap2 = collector.get_namespace_snapshot("ns_cp2")
        self.assertEqual(snap2.semantic.semantic_hits, 0)
        self.assertEqual(snap2.semantic.semantic_runtime_disabled_skips, 1)
        self.assertGreaterEqual(snap2.semantic.upstream_fallbacks_after_semantic_miss, 1)


class TestStage354PrivacyIsolationInMemory(unittest.IsolatedAsyncioTestCase):
    """Per-principal isolation, shared opt-in, unknown fail-closed, SingleFlight & streaming."""

    async def asyncSetUp(self) -> None:
        get_metrics_collector().reset()
        self.auth = EvidenceAuthority(secret_key=_STRONG_EVIDENCE_SECRET)
        self.cache = ExactCache(max_entries=200, ttl_seconds=3600)
        self.vstore = InMemoryVectorStore(dimensions=768, default_model="nomic-embed-text", exact_cache=self.cache)
        self.embedder = TopicEmbedder([("reverse", 0.0), ("sort", 35.0)], dim=768)
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            evidence_authority=self.auth,
        )
        self.svc = SemanticCacheService(
            embedding_provider=self.embedder,
            vector_store=self.vstore,
            policy=self.policy,
            evidence_authority=self.auth,
        )
        self.ctrl = SemanticServingController(initial_enabled=True)
        self.core = IntelliCacheCore(
            cache=self.cache,
            semantic_cache=self.svc,
            single_flight=SingleFlightCoordinator(),
            cache_enabled=True,
            semantic_serving_controller=self.ctrl,
            semantic_cache_startup_enabled=True,
        )
        self.p_alice = derive_principal_id("tok_alice", _STRONG_IDENTITY_SECRET)
        self.p_bob = derive_principal_id("tok_bob", _STRONG_IDENTITY_SECRET)

    async def test_same_tenant_two_principals_exact_semantic_and_stream_isolation(self) -> None:
        prov = CountingMockProvider("PRIV")

        # 1. Alice executes private technical query -> LIVE-1
        r_a1 = await self.core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace="team_alpha", visibility="private", principal_id=self.p_alice),
            prov,
        )
        self.assertFalse(r_a1.cached)
        self.assertTrue(r_a1.text.startswith("PRIV-1:"))

        # 2. Bob in SAME tenant queries exact same prompt -> MUST NOT reuse Alice's private exact or semantic entry!
        r_b1 = await self.core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace="team_alpha", visibility="private", principal_id=self.p_bob),
            prov,
        )
        self.assertFalse(r_b1.cached)
        self.assertTrue(r_b1.text.startswith("PRIV-2:"))

        # 3. Bob asks a paraphrased query -> MUST NOT retrieve Alice's vector entry! (only sees his own LIVE-2)
        r_b_para = await self.core.execute_query(
            _make_req("Python reverse list snippet please", namespace="team_alpha", visibility="private", principal_id=self.p_bob),
            prov,
        )
        self.assertTrue(r_b_para.cached)
        self.assertEqual(r_b_para.cache_type, "semantic")
        self.assertTrue(r_b_para.text.startswith("PRIV-2:"))

        # 4. Alice asks a paraphrased query -> reuses Alice's PRIV-1, never Bob's PRIV-2
        r_a_para = await self.core.execute_query(
            _make_req("How to write Python code to reverse a list?", namespace="team_alpha", visibility="private", principal_id=self.p_alice),
            prov,
        )
        self.assertTrue(r_a_para.cached)
        self.assertEqual(r_a_para.cache_type, "semantic")
        self.assertTrue(r_a_para.text.startswith("PRIV-1:"))

        # 5. Streaming exact cache also strictly isolates Alice and Bob
        chunks_a = [
            c
            async for c in self.core.execute_stream(
                _make_req("How do I sort a tuple in Python?", namespace="team_alpha", visibility="private", principal_id=self.p_alice, stream=True),
                prov,
            )
        ]
        self.assertFalse(chunks_a[-1].cached)
        self.assertIn("PRIV-STREAM-3", chunks_a[0].delta)

        chunks_b = [
            c
            async for c in self.core.execute_stream(
                _make_req("How do I sort a tuple in Python?", namespace="team_alpha", visibility="private", principal_id=self.p_bob, stream=True),
                prov,
            )
        ]
        self.assertFalse(chunks_b[-1].cached)
        self.assertIn("PRIV-STREAM-4", chunks_b[0].delta)

        chunks_a_hit = [
            c
            async for c in self.core.execute_stream(
                _make_req("How do I sort a tuple in Python?", namespace="team_alpha", visibility="private", principal_id=self.p_alice, stream=True),
                prov,
            )
        ]
        self.assertTrue(chunks_a_hit[-1].cached)
        self.assertEqual(chunks_a_hit[-1].cache_type, "exact")
        self.assertEqual(chunks_a_hit[0].delta, "PRIV-STREAM-3")

    async def test_singleflight_isolation_between_principals_in_same_tenant(self) -> None:
        slow_prov = CountingMockProvider("COALESCE", delay_s=0.04)
        req_a1 = _make_req("Explain list comprehension in Python", namespace="team_alpha", visibility="private", principal_id=self.p_alice)
        req_a2 = _make_req("Explain list comprehension in Python", namespace="team_alpha", visibility="private", principal_id=self.p_alice)
        req_b1 = _make_req("Explain list comprehension in Python", namespace="team_alpha", visibility="private", principal_id=self.p_bob)

        res_a1, res_a2, res_b1 = await asyncio.gather(
            self.core.execute_query(req_a1, slow_prov),
            self.core.execute_query(req_a2, slow_prov),
            self.core.execute_query(req_b1, slow_prov),
        )
        # Same principal coalesced (2 total upstream calls: 1 for Alice pair, 1 separate flight for Bob)
        self.assertEqual(len(slow_prov.calls), 2)
        self.assertEqual(res_a1.text, res_a2.text)
        self.assertNotEqual(res_a1.text, res_b1.text)

    async def test_shared_vs_private_and_allow_shared_reads_and_clear_isolation(self) -> None:
        prov = CountingMockProvider("SCOPE")

        # Shared writer seeds tenant_shared entry in team_alpha
        r_shared = await self.core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace="team_alpha", visibility="tenant_shared"),
            prov,
        )
        self.assertFalse(r_shared.cached)
        self.assertTrue(r_shared.text.startswith("SCOPE-1:"))

        # Private reader WITHOUT allow_shared_reads MUST NOT consume tenant_shared entry
        r_priv_strict = await self.core.execute_query(
            _make_req(
                "How to write Python code to reverse a list?",
                namespace="team_alpha",
                visibility="private",
                principal_id=self.p_alice,
                allow_shared_reads=False,
            ),
            prov,
        )
        self.assertFalse(r_priv_strict.cached)
        self.assertTrue(r_priv_strict.text.startswith("SCOPE-2:"))

        # Bob WITH allow_shared_reads=True CAN read tenant_shared entry when Bob has no private entry
        r_bob_opt_in = await self.core.execute_query(
            _make_req(
                "How to reverse Python lists?",
                namespace="team_alpha",
                visibility="private",
                principal_id=self.p_bob,
                allow_shared_reads=True,
            ),
            prov,
        )
        self.assertTrue(r_bob_opt_in.cached)
        self.assertEqual(r_bob_opt_in.cache_type, "semantic")
        self.assertTrue(r_bob_opt_in.text.startswith("SCOPE-1:"))

        # Writing as private user with allow_shared_reads=True STILL stores write privately (never leaks to tenant_shared)
        r_bob_write = await self.core.execute_query(
            _make_req(
                "How do I sort a list in Python?",
                namespace="team_alpha",
                visibility="private",
                principal_id=self.p_bob,
                allow_shared_reads=True,
            ),
            prov,
        )
        self.assertFalse(r_bob_write.cached)
        self.assertTrue(r_bob_write.text.startswith("SCOPE-3:"))

        # Shared caller querying the exact/semantic topic MUST NOT read Bob's private entry
        r_shared_after = await self.core.execute_query(
            _make_req("How to sort lists in Python?", namespace="team_alpha", visibility="tenant_shared"),
            prov,
        )
        self.assertFalse(r_shared_after.cached)
        self.assertTrue(r_shared_after.text.startswith("SCOPE-4:"))

        # Cross-tenant MUST NOT read team_alpha's tenant_shared entries
        r_other_tenant = await self.core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace="team_beta", visibility="tenant_shared"),
            prov,
        )
        self.assertFalse(r_other_tenant.cached)
        self.assertTrue(r_other_tenant.text.startswith("SCOPE-5:"))

        # Principal-scoped clear removes ONLY Alice's entries, leaving Bob's and tenant_shared intact
        await self.cache.clear(namespace="team_alpha", visibility="private", principal_id=self.p_alice)
        await self.vstore.clear(namespace="team_alpha", visibility="private", principal_id=self.p_alice)

        # Alice misses now (strict private)
        r_alice_after_clear = await self.core.execute_query(
            _make_req(
                "How to write Python code to reverse a list?",
                namespace="team_alpha",
                visibility="private",
                principal_id=self.p_alice,
                allow_shared_reads=False,
            ),
            prov,
        )
        self.assertFalse(r_alice_after_clear.cached)

        # Bob's private entry & shared entry are intact
        r_bob_intact = await self.core.execute_query(
            _make_req(
                "How do I sort a list in Python?",
                namespace="team_alpha",
                visibility="private",
                principal_id=self.p_bob,
                allow_shared_reads=True,
            ),
            prov,
        )
        self.assertTrue(r_bob_intact.cached)
        self.assertEqual(r_bob_intact.cache_type, "exact")
        self.assertTrue(r_bob_intact.text.startswith("SCOPE-3:"))

    async def test_unknown_visibility_and_missing_principal_fail_closed(self) -> None:
        prov = CountingMockProvider("FAILCLOSED")

        # Seed a legacy row with visibility='unknown' in exact & vector store
        fp_leg = "fp_legacy_unknown_1"
        await self.cache.set(
            fp_leg,
            CacheEntry(
                response="LEGACY_UNKNOWN_RESP",
                model="llama-3.3-70b-versatile",
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=5.0,
                provider="groq",
                visibility=CACHE_VISIBILITY_UNKNOWN,
            ),
            namespace="team_alpha",
        )
        await self.vstore.upsert(
            VectorRecord(
                cache_namespace="team_alpha",
                fingerprint=fp_leg,
                query_text="How do I reverse a list in Python?",
                vector=_topic_vec(768, 0.0),
                model="nomic-embed-text",
                visibility=CACHE_VISIBILITY_UNKNOWN,
                metadata=CompatibilityExtractor().extract_from_text("How do I reverse a list in Python?", configured_model="nomic-embed-text").to_dict(),
                response_text="LEGACY_UNKNOWN_RESP",
            )
        )

        # Neither tenant_shared nor private reads MUST ever return an 'unknown' row
        self.assertEqual(
            await self.vstore.search(
                _topic_vec(768, 0.0),
                top_k=5,
                namespace="team_alpha",
                model="nomic-embed-text",
                visibility="tenant_shared",
            ),
            [],
        )
        self.assertEqual(
            await self.vstore.search(
                _topic_vec(768, 0.0),
                top_k=5,
                namespace="team_alpha",
                model="nomic-embed-text",
                visibility="private",
                principal_id=self.p_alice,
            ),
            [],
        )
        self.assertIsNone(
            await self.cache.get(fp_leg, namespace="team_alpha", visibility="tenant_shared")
        )

        # Request with visibility='private' and principal_id=None MUST fail closed without fabricating identity
        req_no_principal = _make_req(
            "How do I reverse a list in Python?",
            namespace="team_alpha",
            visibility="private",
            principal_id=None,
        )
        res_np = await self.core.execute_query(req_no_principal, prov)
        self.assertFalse(res_np.cached)
        self.assertEqual(len(prov.calls), 1)
        snap = get_metrics_collector().get_namespace_snapshot("team_alpha")
        self.assertGreaterEqual(snap.semantic.principal_isolation_rejections, 1)

        # Ensure index() also refuses missing principal on private scope
        ok_idx = await self.svc.index(req_no_principal, res_np)
        self.assertFalse(ok_idx)

        # Unknown request visibility rejected directly by policy.validate_candidate
        bad_cand = VectorSearchResult(
            cache_namespace="team_alpha",
            fingerprint="fp_x",
            query_text="How do I reverse a list in Python?",
            model="nomic-embed-text",
            distance=0.0,
            similarity=1.0,
            cached_response="x",
            visibility="unknown",
            metadata={},
        )
        dec = self.policy.validate_candidate(
            bad_cand,
            _make_req("How do I reverse a list in Python?", namespace="team_alpha", visibility="tenant_shared"),
        )
        self.assertEqual(dec.reason_code, CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN)


class TestStage354PostgresPrivacyAndMigration(unittest.IsolatedAsyncioTestCase):
    """PostgreSQL + pgvector scope enforcement, pre-006/005 upgrade behavior, and evidence tamper rejection."""

    async def asyncSetUp(self) -> None:
        db_url = os.getenv("TEST_DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test"
        parsed = urlsplit(db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.skipTest(f"Database '{db_name}' is not a test DB.")
        try:
            self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=4)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=768)
        except Exception as exc:
            self.skipTest(f"PostgreSQL unavailable: {exc}")

        get_metrics_collector().reset()
        self.ns = "test_s354_pg"
        self.authority = EvidenceAuthority(secret_key=_STRONG_EVIDENCE_SECRET, allow_ephemeral=False)
        self.cache = PostgresCache(pool=self.pool, max_entries=1000, ttl_seconds=3600, default_namespace=self.ns)
        self.store = PostgresVectorStore(pool=self.pool, default_namespace=self.ns, default_model="nomic-embed-text", dimensions=768)
        self.embedder = TopicEmbedder([("reverse", 0.0), ("sort", 35.0)], dim=768)
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            evidence_authority=self.authority,
        )
        self.svc = SemanticCacheService(
            embedding_provider=self.embedder,
            vector_store=self.store,
            policy=self.policy,
            default_namespace=self.ns,
            evidence_authority=self.authority,
        )
        self.p_alice = derive_principal_id("pg_alice", _STRONG_IDENTITY_SECRET)
        self.p_bob = derive_principal_id("pg_bob", _STRONG_IDENTITY_SECRET)
        await self._cleanup()

    async def _cleanup(self) -> None:
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)

    async def asyncTearDown(self) -> None:
        if hasattr(self, "pool"):
            try:
                await self._cleanup()
            finally:
                await self.pool.close()

    async def test_postgres_pre_migration_legacy_rows_fail_closed_and_privacy_isolation_persists(self) -> None:
        # 1. Verify schema idempotency by running migrations a second time
        await run_migrations(self.pool)
        await run_vector_migrations(self.pool, dimensions=768)

        # 2. Simulate pre-3.5.4 rows inserted WITHOUT visibility/owner columns -> DB default MUST be 'unknown'
        v_str = "[" + ",".join(f"{x:.6f}" for x in _topic_vec(768, 0.0)) + "]"
        async with self.pool.acquire() as conn:
            await conn.execute(
                "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, 'fp_pre006', 'OLD_SECRET_OR_SHARED', 'llama-3.3-70b-versatile', 'groq')",
                self.ns,
            )
            await conn.execute(
                "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, 'fp_pre006', 'How do I reverse a list in Python?', 'nomic-embed-text', $2::vector)",
                self.ns,
                v_str,
            )
            await conn.execute(
                "INSERT INTO semantic_variant_records (cache_namespace, fingerprint, variant_key, query_text, model, vector, cached_response) VALUES ($1, 'fp_pre006', 'v3:pre005', 'How do I reverse a list in Python?', 'nomic-embed-text', $2::vector, 'OLD_VARIANT')",
                self.ns,
                v_str,
            )
            vis_ce = await conn.fetchval("SELECT visibility FROM cache_entries WHERE cache_namespace = $1 AND fingerprint = 'fp_pre006'", self.ns)
            vis_vr = await conn.fetchval("SELECT visibility FROM vector_records WHERE cache_namespace = $1 AND fingerprint = 'fp_pre006'", self.ns)
            vis_sv = await conn.fetchval("SELECT visibility FROM semantic_variant_records WHERE cache_namespace = $1 AND fingerprint = 'fp_pre006'", self.ns)
            self.assertEqual((vis_ce, vis_vr, vis_sv), ("unknown", "unknown", "unknown"))

        # Legacy 'unknown' rows MUST fail closed on BOTH tenant_shared and private queries
        self.assertIsNone(await self.cache.get("fp_pre006", namespace=self.ns, visibility="tenant_shared"))
        self.assertEqual(
            await self.store.search(_topic_vec(768, 0.0), top_k=10, namespace=self.ns, model="nomic-embed-text", visibility="tenant_shared"),
            [],
        )
        self.assertEqual(
            await self.store.search(_topic_vec(768, 0.0), top_k=10, namespace=self.ns, model="nomic-embed-text", visibility="private", principal_id=self.p_alice),
            [],
        )

        # 3. Full private + shared admission via IntelliCacheCore on PostgreSQL
        ctrl = SemanticServingController(initial_enabled=True)
        core = IntelliCacheCore(
            cache=self.cache,
            semantic_cache=self.svc,
            cache_enabled=True,
            semantic_serving_controller=ctrl,
            semantic_cache_startup_enabled=True,
        )
        prov = CountingMockProvider("PG")
        await core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace=self.ns, visibility="private", principal_id=self.p_alice),
            prov,
        )
        # Re-instantiate fresh instances simulating worker/gateway restart
        cache_restarted = PostgresCache(pool=self.pool, max_entries=1000, ttl_seconds=3600, default_namespace=self.ns)
        store_restarted = PostgresVectorStore(pool=self.pool, default_namespace=self.ns, default_model="nomic-embed-text", dimensions=768)
        auth_restarted = EvidenceAuthority(secret_key=_STRONG_EVIDENCE_SECRET, allow_ephemeral=False)
        policy_restarted = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            evidence_authority=auth_restarted,
        )
        svc_restarted = SemanticCacheService(
            embedding_provider=self.embedder,
            vector_store=store_restarted,
            policy=policy_restarted,
            default_namespace=self.ns,
            evidence_authority=auth_restarted,
        )
        core_restarted = IntelliCacheCore(
            cache=cache_restarted,
            semantic_cache=svc_restarted,
            cache_enabled=True,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
            semantic_cache_startup_enabled=True,
        )

        # Bob MUST NOT hit Alice's private persisted semantic entry
        bob_out = await core_restarted.execute_query(
            _make_req("Python reverse list example?", namespace=self.ns, visibility="private", principal_id=self.p_bob),
            prov,
        )
        self.assertFalse(bob_out.cached)
        self.assertTrue(bob_out.text.startswith("PG-2:"))

        # Alice DOES hit her own persisted semantic entry after restart
        alice_out = await core_restarted.execute_query(
            _make_req("How to write Python code to reverse a list?", namespace=self.ns, visibility="private", principal_id=self.p_alice),
            prov,
        )
        self.assertTrue(alice_out.cached)
        self.assertEqual(alice_out.cache_type, "semantic")
        self.assertTrue(alice_out.text.startswith("PG-1:"))

    async def test_postgres_evidence_scope_tampering_rejected(self) -> None:
        ctrl = SemanticServingController(initial_enabled=True)
        core = IntelliCacheCore(
            cache=self.cache,
            semantic_cache=self.svc,
            cache_enabled=True,
            semantic_serving_controller=ctrl,
            semantic_cache_startup_enabled=True,
        )
        prov = CountingMockProvider("TAMPER")
        res = await core.execute_query(
            _make_req("How do I reverse a list in Python?", namespace=self.ns, visibility="private", principal_id=self.p_alice),
            prov,
        )
        scoped_fp = derive_scoped_exact_key(res.fingerprint, "private", self.p_alice)

        # Attack: downgrade private DB row to tenant_shared across DB tables without valid shared HMAC signature
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE cache_entries SET visibility = 'tenant_shared', owner_principal_id = NULL WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                scoped_fp,
            )
            await conn.execute(
                "UPDATE semantic_variant_records SET visibility = 'tenant_shared', owner_principal_id = NULL WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                scoped_fp,
            )
            await conn.execute(
                "UPDATE vector_records SET visibility = 'tenant_shared', owner_principal_id = NULL WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                scoped_fp,
            )
        diag: list[dict] = []
        miss = await self.svc.lookup(
            _make_req("Python reverse list helper", namespace=self.ns, visibility="tenant_shared"),
            diagnostics_hook=diag.append,
        )
        self.assertIsNone(miss)
        codes1 = [item["decision"]["reason_code"] for item in diag[-1]["candidate_decisions"]]
        self.assertIn(CompatibilityReasonCode.EVIDENCE_SCOPE_MISMATCH.value, codes1)

        # Attack 2: strip evidence visibility/owner inside metadata JSON on a private row
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE cache_entries SET visibility = 'private', owner_principal_id = $3 WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                scoped_fp,
                self.p_alice,
            )
            await conn.execute(
                "UPDATE semantic_variant_records SET visibility = 'private', owner_principal_id = $3, metadata = metadata #- '{compatibility_evidence,visibility}' #- '{compatibility_evidence,owner_principal_id}' WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                scoped_fp,
                self.p_alice,
            )
        diag.clear()
        miss2 = await self.svc.lookup(
            _make_req("Python reverse list helper", namespace=self.ns, visibility="private", principal_id=self.p_alice),
            diagnostics_hook=diag.append,
        )
        self.assertIsNone(miss2)
        codes2 = [item["decision"]["reason_code"] for item in diag[-1]["candidate_decisions"]]
        self.assertIn(CompatibilityReasonCode.EVIDENCE_SCOPE_MISMATCH.value, codes2)
        self.assertGreaterEqual(get_metrics_collector().get_namespace_snapshot(self.ns).semantic.evidence_validation_rejections, 2)


class TestStage354HttpAndScenarios(unittest.TestCase):
    """HTTP admin endpoints, spoofing rejections, Scenarios A..F over real FastAPI app."""

    def setUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)
        self._saved_cache = get_query_cache()
        self._saved_sem = get_semantic_cache()
        self._saved_vs = get_vector_store()

    def tearDown(self) -> None:
        set_query_cache(self._saved_cache)
        set_semantic_cache(self._saved_sem)
        set_vector_store(self._saved_vs)
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()
        reload_settings()

    def _run_app_test(self, env_vars: dict[str, str], body_fn):
        prov = CountingMockProvider("HTTP")
        embedder = TopicEmbedder([("reverse", 0.0), ("sort", 35.0)], dim=384, model="BAAI/bge-small-en-v1.5")
        with patch.dict(os.environ, env_vars, clear=False):
            reload_settings()
            with patch("app.main.EmbeddingProviderFactory.create", return_value=embedder):
                test_app = create_app()
                test_app.dependency_overrides[get_llm_provider] = lambda: prov
                with TestClient(test_app) as client:
                    return body_fn(client, prov, embedder)

    def test_admin_serving_control_and_spoof_rejection(self) -> None:
        env = {
            "CLIENT_API_TOKENS": json.dumps({
                "tok_alice": {"namespace": "team_alpha", "visibility": "private"},
                "tok_bob": {"namespace": "team_alpha", "visibility": "private"},
                "tok_beta": {"namespace": "team_beta", "visibility": "private"},
            }),
            "ADMIN_API_TOKEN": "admin-secret-stage-354",
            "PRINCIPAL_IDENTITY_SECRET": _STRONG_IDENTITY_SECRET,
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "false",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "VECTOR_DIMENSIONS": "384",
        }

        def _check(client: TestClient, prov: CountingMockProvider, _emb) -> None:
            # 1. Admin auth enforcement
            self.assertEqual(client.get("/admin/semantic-cache/serving").status_code, 403)
            self.assertEqual(client.post("/admin/semantic-cache/serving", json={"enabled": True}).status_code, 403)
            self.assertEqual(
                client.get("/admin/semantic-cache/serving", headers={"X-Admin-Token": "tok_alice"}).status_code,
                403,
            )

            admin_hdr = {"X-Admin-Token": "admin-secret-stage-354"}
            # Validation rejects coerced string/int/extra keys
            for bad_payload in ({"enabled": "false"}, {"enabled": 1}, {"enabled": True, "principal_id": "x"}):
                self.assertEqual(client.post("/admin/semantic-cache/serving", json=bad_payload, headers=admin_hdr).status_code, 422)

            st = client.get("/admin/semantic-cache/serving", headers=admin_hdr)
            self.assertEqual(st.status_code, 200)
            body = st.json()
            self.assertTrue(body["startup_enabled"])
            self.assertFalse(body["runtime_enabled"])
            self.assertFalse(body["effective_enabled"])
            self.assertNotIn("tok_alice", json.dumps(body))
            self.assertNotIn(_STRONG_IDENTITY_SECRET, json.dumps(body))

            # Valid admin toggle + per-namespace override & clear
            post_en = client.post(
                "/admin/semantic-cache/serving",
                json={"enabled": True, "reason": "admin test enable"},
                headers=admin_hdr,
            )
            self.assertEqual(post_en.status_code, 200)
            self.assertTrue(post_en.json()["effective_enabled"])
            post_ns = client.post(
                "/admin/semantic-cache/serving",
                json={"enabled": False, "namespace": "team_alpha", "reason": "canary off"},
                headers=admin_hdr,
            )
            self.assertEqual(post_ns.status_code, 200)
            self.assertFalse(post_ns.json()["namespace_runtime_enabled"])
            self.assertFalse(post_ns.json()["effective_enabled"])
            post_dis = client.post(
                "/admin/semantic-cache/serving",
                json={"enabled": False, "clear_namespace_override": True, "reason": "restore off"},
                headers=admin_hdr,
            )
            self.assertEqual(post_dis.status_code, 200)
            self.assertFalse(post_dis.json()["effective_enabled"])

            # 2. Spoofing attacks on /query
            alice_hdr = {"X-Client-Token": "tok_alice"}
            for spoof_body in (
                {"prompt": "Reverse list in Python", "principal_id": "spoof"},
                {"prompt": "Reverse list in Python", "owner_principal_id": "spoof"},
                {"prompt": "Reverse list in Python", "visibility": "tenant_shared"},
                {"prompt": "Reverse list in Python", "scope": "tenant_shared"},
                {"prompt": "Reverse list in Python", "allow_shared_reads": True},
                {"prompt": "Reverse list in Python", "tenant_id": "team_beta"},
                {"prompt": "Reverse list in Python", "metadata": {"namespace": "team_beta"}},
                {"prompt": "Reverse list in Python", "metadata": {"visibility": "tenant_shared"}},
            ):
                r_bad = client.post("/query", json=spoof_body, headers=alice_hdr)
                self.assertEqual(r_bad.status_code, 422, f"Expected 422 for {spoof_body}")

            # Ignored top-level namespace / headers / query params never cross tenants or override server scope
            r_hdr_spoof = client.post(
                "/query?visibility=tenant_shared&principal_id=bob&namespace=team_beta",
                json={"prompt": "How do I reverse a list in Python?", "namespace": "team_beta"},
                headers={
                    "X-Client-Token": "tok_alice",
                    "X-Cache-Namespace": "team_beta",
                    "X-Principal-ID": "bob",
                    "X-Cache-Visibility": "tenant_shared",
                },
            )
            self.assertEqual(r_hdr_spoof.status_code, 200)
            resp_json = r_hdr_spoof.json()
            # Ensure no internal secrets/principal/signature leakage
            raw_out = json.dumps(resp_json)
            for forbidden in ("principal_id", "owner_principal_id", "evidence_signature", "binding_digest", _STRONG_IDENTITY_SECRET):
                self.assertNotIn(forbidden, raw_out)

            # Bob must NOT see Alice's cached answer despite query/header spoof attempts
            bob_exact = client.post("/query", json={"prompt": "How do I reverse a list in Python?"}, headers={"X-Client-Token": "tok_bob"})
            self.assertEqual(bob_exact.status_code, 200)
            self.assertFalse(bob_exact.json()["cached"])
            self.assertEqual(len(prov.calls), 2)

            stats = client.get("/cache/semantic/stats", headers=admin_hdr).json()
            self.assertGreaterEqual(stats["privacy_violation_attempts"], 2)

        self._run_app_test(env, _check)

    def test_scenarios_a_b_c_d_f_over_http(self) -> None:
        # Scenario A: Defaults (both off)
        env_a = {
            "CLIENT_API_TOKENS": json.dumps({"tok_a": {"namespace": "team_alpha", "visibility": "tenant_shared"}}),
            "SEMANTIC_CACHE_ENABLED": "false",
            "SEMANTIC_CACHE_SERVING_ENABLED": "false",
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
        }

        def _scen_a(client: TestClient, prov: CountingMockProvider, _emb) -> None:
            r1 = client.post("/query", json={"prompt": "How do I reverse a list in Python?"}, headers={"X-Client-Token": "tok_a"})
            r2 = client.post("/query", json={"prompt": "Way to reverse Python list?"}, headers={"X-Client-Token": "tok_a"})
            self.assertFalse(r1.json()["cached"])
            self.assertFalse(r2.json()["cached"])
            self.assertEqual(client.get("/cache/semantic/stats", headers={"X-Client-Token": "tok_a"}).json()["hits"], 0)
            self.assertEqual(len(prov.calls), 2)

        self._run_app_test(env_a, _scen_a)

        # Scenario B: Startup OFF, runtime toggled ON via admin -> still never serves semantically
        env_b = {
            **env_a,
            "ADMIN_API_TOKEN": "admin-secret-b",
        }

        def _scen_b(client: TestClient, prov: CountingMockProvider, _emb) -> None:
            tog = client.post(
                "/admin/semantic-cache/serving",
                json={"enabled": True, "reason": "test startup off"},
                headers={"X-Admin-Token": "admin-secret-b"},
            )
            self.assertEqual(tog.status_code, 200)
            self.assertTrue(tog.json()["runtime_enabled"])
            self.assertFalse(tog.json()["startup_enabled"])
            self.assertFalse(tog.json()["effective_enabled"])
            client.post("/query", json={"prompt": "How do I reverse a list in Python?"}, headers={"X-Client-Token": "tok_a"})
            r2 = client.post("/query", json={"prompt": "Way to reverse Python list?"}, headers={"X-Client-Token": "tok_a"})
            self.assertFalse(r2.json()["cached"])
            self.assertEqual(client.get("/cache/semantic/stats", headers={"X-Client-Token": "tok_a"}).json()["hits"], 0)
            self.assertEqual(len(prov.calls), 2)

        self._run_app_test(env_b, _scen_b)

        # Scenario C & D: Startup ON, runtime starts OFF -> admit on miss -> enable -> semantic hit (with original usage) -> disable -> fallback
        env_cd = {
            "CLIENT_API_TOKENS": json.dumps({"tok_alice": {"namespace": "team_alpha", "visibility": "private"}}),
            "ADMIN_API_TOKEN": "admin-secret-cd",
            "PRINCIPAL_IDENTITY_SECRET": _STRONG_IDENTITY_SECRET,
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "false",
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "VECTOR_DIMENSIONS": "384",
        }

        def _scen_cd(client: TestClient, prov: CountingMockProvider, _emb) -> None:
            alice = {"X-Client-Token": "tok_alice"}
            adm = {"X-Admin-Token": "admin-secret-cd"}

            # Runtime OFF initially: miss + upstream + verified admission; second distinct query also skips semantic lookup
            m1 = client.post("/query", json={"prompt": "How do I reverse a list in Python?"}, headers=alice)
            self.assertFalse(m1.json()["cached"])
            m2 = client.post("/query", json={"prompt": "How do I sort a list in Python?"}, headers=alice)
            self.assertFalse(m2.json()["cached"])
            st_off = client.get("/cache/semantic/stats", headers=alice).json()
            self.assertEqual(st_off["hits"], 0)
            self.assertGreaterEqual(st_off["runtime_disabled_skips"], 2)

            # Scenario C: toggle runtime ON -> paraphrase HITS semantically with original usage
            en = client.post("/admin/semantic-cache/serving", json={"enabled": True, "reason": "enable"}, headers=adm)
            self.assertTrue(en.json()["effective_enabled"])
            hit = client.post("/query", json={"prompt": "Python reverse list technique?"}, headers=alice)
            self.assertEqual(hit.status_code, 200)
            hj = hit.json()
            self.assertTrue(hj["cached"])
            self.assertEqual(hj["cache_type"], "semantic")
            self.assertEqual(hj["usage"]["prompt_tokens"], 21)
            self.assertEqual(hj["usage"]["completion_tokens"], 34)
            self.assertEqual(hj["usage"]["total_tokens"], 55)
            st_on = client.get("/cache/semantic/stats", headers=alice).json()
            self.assertEqual(st_on["hits"], 1)

            # Scenario D: toggle runtime OFF -> immediate fallback to upstream, entries remain intact
            dis = client.post("/admin/semantic-cache/serving", json={"enabled": False, "reason": "rollback"}, headers=adm)
            self.assertFalse(dis.json()["effective_enabled"])
            fb = client.post("/query", json={"prompt": "Python sort list syntax?"}, headers=alice)
            self.assertFalse(fb.json()["cached"])
            self.assertEqual(fb.json()["cache_type"], "none")
            st_killed = client.get("/cache/semantic/stats", headers=alice).json()
            self.assertEqual(st_killed["hits"], 1)
            self.assertGreaterEqual(st_killed["runtime_disabled_skips"], 3)

            # Re-enable proves entries remained intact in vector store
            client.post("/admin/semantic-cache/serving", json={"enabled": True, "reason": "recover"}, headers=adm)
            hit_again = client.post("/query", json={"prompt": "How can I reverse lists in Python?"}, headers=alice)
            self.assertTrue(hit_again.json()["cached"])
            self.assertEqual(hit_again.json()["cache_type"], "semantic")

        self._run_app_test(env_cd, _scen_cd)

        # Scenario F: Unauthenticated loopback under private default fails closed (no fabricated principal), forwarded header 401
        env_f = {
            "ALLOW_UNAUTHENTICATED_LOOPBACK": "true",
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "true",
            "SEMANTIC_CACHE_DEFAULT_VISIBILITY": "private",
            "CACHE_BACKEND": "memory",
            "VECTOR_STORE_BACKEND": "memory",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "VECTOR_DIMENSIONS": "384",
        }
        # Ensure CLIENT_API_TOKENS isn't leaking from env
        with patch.dict(os.environ, env_f, clear=True):
            reload_settings()
            prov_f = CountingMockProvider("LOOP")
            emb_f = TopicEmbedder([("reverse", 0.0)], dim=384, model="BAAI/bge-small-en-v1.5")
            with patch("app.main.EmbeddingProviderFactory.create", return_value=emb_f):
                app_f = create_app()
                app_f.dependency_overrides[get_llm_provider] = lambda: prov_f
                with TestClient(app_f) as client_f:
                    r1 = client_f.post("/query", json={"prompt": "How do I reverse a list in Python?"})
                    r2 = client_f.post("/query", json={"prompt": "How do I reverse a list in Python?"})
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r2.status_code, 200)
                    # Private default + loopback principal_id=None -> NEVER fabricates principal or shares across requests
                    self.assertFalse(r1.json()["cached"])
                    self.assertFalse(r2.json()["cached"])
                    self.assertEqual(len(prov_f.calls), 2)
                    # Forwarded header triggers 401
                    r_fwd = client_f.post(
                        "/query",
                        json={"prompt": "Hello"},
                        headers={"X-Forwarded-For": "10.0.0.1"},
                    )
                    self.assertEqual(r_fwd.status_code, 401)

    def test_scenario_e_live_postgres_multi_principal_http_and_restart(self) -> None:
        db_url = os.getenv("TEST_DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test"
        parsed = urlsplit(db_url)
        if "test" not in (parsed.path or "").lower():
            self.skipTest("Database must contain 'test'.")

        async def _probe_and_clean():
            pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
            try:
                await run_migrations(pool)
                await run_vector_migrations(pool, dimensions=768)
                async with pool.acquire() as conn:
                    await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = 'scen_e_alpha'")
                    await conn.execute("DELETE FROM vector_records WHERE cache_namespace = 'scen_e_alpha'")
                    await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = 'scen_e_alpha'")
            finally:
                await pool.close()

        try:
            asyncio.run(_probe_and_clean())
        except Exception as exc:
            self.skipTest(f"PostgreSQL unavailable: {exc}")

        env_e = {
            "CLIENT_API_TOKENS": json.dumps({
                "tok_e_alice": {"namespace": "scen_e_alpha", "visibility": "private"},
                "tok_e_bob": {"namespace": "scen_e_alpha", "visibility": "private"},
                "tok_e_shared_w": {"namespace": "scen_e_alpha", "visibility": "tenant_shared"},
                "tok_e_bob_optin": {"namespace": "scen_e_alpha", "visibility": "private", "allow_shared_reads": True},
            }),
            "ADMIN_API_TOKEN": "admin-secret-e",
            "PRINCIPAL_IDENTITY_SECRET": _STRONG_IDENTITY_SECRET,
            "SEMANTIC_CACHE_EVIDENCE_SECRET": _STRONG_EVIDENCE_SECRET,
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": db_url,
            "CACHE_NAMESPACE": "scen_e_alpha",
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "true",
            "EMBEDDING_PROVIDER": "ollama",
            "EMBEDDING_MODEL": "nomic-embed-text",
            "VECTOR_DIMENSIONS": "768",
        }
        prov1 = CountingMockProvider("E1")
        emb1 = TopicEmbedder([("reverse", 0.0), ("sort", 35.0)], dim=768, model="nomic-embed-text")
        try:
            with patch.dict(os.environ, env_e, clear=False):
                reload_settings()
                with patch("app.main.EmbeddingProviderFactory.create", return_value=emb1):
                    app1 = create_app()
                    app1.dependency_overrides[get_llm_provider] = lambda: prov1
                    with TestClient(app1) as c1:
                        alice = {"X-Client-Token": "tok_e_alice"}
                        bob = {"X-Client-Token": "tok_e_bob"}
                        shared_w = {"X-Client-Token": "tok_e_shared_w"}

                        r_a = c1.post("/query", json={"prompt": "How do I reverse a list in Python?"}, headers=alice)
                        self.assertFalse(r_a.json()["cached"])
                        # Shared write on sort
                        r_s = c1.post("/query", json={"prompt": "How do I sort a list in Python?"}, headers=shared_w)
                        self.assertFalse(r_s.json()["cached"])
                        # Bob private cannot read Alice's reverse
                        r_b = c1.post("/query", json={"prompt": "Way to reverse Python list?"}, headers=bob)
                        self.assertFalse(r_b.json()["cached"])
                        self.assertEqual(len(prov1.calls), 3)

            # Simulate process restart with new create_app()
            prov2 = CountingMockProvider("E2")
            emb2 = TopicEmbedder([("reverse", 0.0), ("sort", 35.0)], dim=768, model="nomic-embed-text")
            with patch.dict(os.environ, env_e, clear=False):
                reload_settings()
                with patch("app.main.EmbeddingProviderFactory.create", return_value=emb2):
                    app2 = create_app()
                    app2.dependency_overrides[get_llm_provider] = lambda: prov2
                    with TestClient(app2) as c2:
                        # Alice still hits her persisted private semantic entry
                        r_a_hit = c2.post("/query", json={"prompt": "Python reverse list syntax?"}, headers={"X-Client-Token": "tok_e_alice"})
                        self.assertTrue(r_a_hit.json()["cached"])
                        self.assertEqual(r_a_hit.json()["cache_type"], "semantic")
                        self.assertTrue(r_a_hit.json()["response"].startswith("E1-1:"))

                        # Bob opt-in reads tenant_shared sort entry
                        r_bob_shared = c2.post("/query", json={"prompt": "How to sort Python lists?"}, headers={"X-Client-Token": "tok_e_bob_optin"})
                        self.assertTrue(r_bob_shared.json()["cached"])
                        self.assertEqual(r_bob_shared.json()["cache_type"], "semantic")
                        self.assertTrue(r_bob_shared.json()["response"].startswith("E1-2:"))

                        # Scoped clear removes ONLY Alice's private entries; shared entry stays
                        p_alice = derive_principal_id("tok_e_alice", _STRONG_IDENTITY_SECRET)
                        clr = c2.post(
                            f"/cache/clear?namespace=scen_e_alpha&visibility=private&principal_id={p_alice}",
                            headers={"X-Admin-Token": "admin-secret-e"},
                        )
                        self.assertEqual(clr.status_code, 200)
                        r_a_miss = c2.post("/query", json={"prompt": "Python reverse list syntax?"}, headers={"X-Client-Token": "tok_e_alice"})
                        self.assertFalse(r_a_miss.json()["cached"])
                        r_s_still = c2.post("/query", json={"prompt": "How to sort Python lists?"}, headers={"X-Client-Token": "tok_e_shared_w"})
                        self.assertTrue(r_s_still.json()["cached"])
        finally:
            asyncio.run(_probe_and_clean())


class TestStage354PerformanceOverhead(unittest.TestCase):
    """Microbenchmark measuring runtime control + privacy scope + scoped key derivation overhead."""

    def test_runtime_control_and_privacy_overhead_microbenchmark(self) -> None:
        ctrl = SemanticServingController(initial_enabled=True)
        iterations = 2000

        base_us = []
        scoped_us = []
        for i in range(iterations):
            t0 = time.perf_counter_ns()
            normalize_cache_visibility("private")
            t1 = time.perf_counter_ns()
            base_us.append((t1 - t0) / 1000.0)

            t2 = time.perf_counter_ns()
            enabled = ctrl.is_serving_enabled("team_alpha")
            key = derive_scoped_exact_key(f"fp_{i}", "private", "p_alice_1234567890")
            matched = matches_cache_scope("private", "p_alice_1234567890", "private", "p_alice_1234567890")
            t3 = time.perf_counter_ns()
            self.assertTrue(enabled and len(key) == 64 and matched)
            scoped_us.append((t3 - t2) / 1000.0)

        med_base = statistics.median(base_us)
        med_scoped = statistics.median(scoped_us)
        sorted_scoped = sorted(scoped_us)
        p95_scoped = sorted_scoped[int(0.95 * len(sorted_scoped))]
        print(
            f"\n[STAGE_3_5_4_OVERHEAD] iterations={iterations} "
            f"baseline_median_us={med_base:.3f} scoped_median_us={med_scoped:.3f} "
            f"scoped_p95_us={p95_scoped:.3f}"
        )
        # Overhead must remain comfortably under 100 microseconds per operation on CPython
        self.assertLess(med_scoped, 100.0)


if __name__ == "__main__":
    unittest.main()
