"""
Stage 3.6 Context-Aware Centralized Semantic Cache Verification Suite.

Covers:
1. Context classification rules (CONTEXT_INDEPENDENT, CONTEXT_DEPENDENT, CONTEXT_UNCERTAIN).
2. Versioned ContextCompatibilityContract, variant key scoping, and EvidenceAuthority binding.
3. Response artifact fail-closed checks (CONTEXT_UNCERTAIN, sensitive material, missing-context refusals).
4. Defense-in-depth scope/budget telemetry via SemanticCacheService.lookup and Postgres SQL prefilters.
5. Complete HTTP end-to-end 24-scenario matrix (CTX-001 .. CTX-024) in-memory.
6. Unmocked FastEmbed (BAAI/bge-small-en-v1.5, 384-d) + PostgreSQL 16 + pgvector HTTP, index,
   EXPLAIN ANALYZE, starvation recovery, and server-managed /conversations verification.
"""

from __future__ import annotations

import json
import os
from typing import Any, AsyncGenerator, Dict, List
import unittest
from unittest.mock import patch

import asyncpg
import httpx

from app.cache.exact_cache import CacheEntry, ExactCache
from app.config import reload_settings
from app.conversations.identity import derive_principal_id
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, StreamChunk, UsageMetrics
from app.core.fingerprinting import derive_variant_exact_fingerprint, generate_fingerprint
from app.core.service import IntelliCacheCore
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.main import create_app
from app.metrics import get_metrics_collector
from app.privacy import derive_scoped_exact_key
from app.providers.interfaces import LLMProvider
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.context import (
    ContextClass,
    analyze_request_context,
    resolve_context_cache_scope,
)
from app.semantic_cache.contracts import (
    CompatibilityReasonCode,
    SemanticCompatibilityMetadata,
    compute_variant_key,
)
from app.semantic_cache.control import get_semantic_serving_controller, reset_semantic_serving_controller
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.verification import ResponseArtifactVerifier
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore
from app.vector_store.postgres_store import PostgresVectorStore
from app.api.routes import (
    get_llm_provider,
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from scripts.evaluate_stage3_5_5_operational import (
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    reset_pg_tables,
    resolve_fastembed_database_url,
)
from scripts.evaluate_stage3_6_context_cache import (
    MANDATORY_21_METRICS,
    ScriptedStage36Provider,
    run_stage3_6_evaluation,
)


class _FixedEmbedder(EmbeddingProvider):
    """Minimal unit-vector embedder for targeted policy/service unit tests."""

    def __init__(self, dim: int = FASTEMBED_DIM, model: str = FASTEMBED_MODEL) -> None:
        self._dim = dim
        self._model = model

    @property
    def provider_name(self) -> str:
        return "fixed_test_embedder"

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def expected_dimensions(self) -> int:
        return self._dim

    async def embed(
        self,
        text: str,
        task: EmbeddingTask = EmbeddingTask.QUERY,
        model: str | None = None,
    ) -> EmbeddingResult:
        vec = [0.0] * self._dim
        vec[0] = 1.0
        return EmbeddingResult(vector=vec, model=model or self._model, dimensions=self._dim)

    async def embed_batch(
        self,
        texts: List[str],
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        model: str | None = None,
    ) -> List[EmbeddingResult]:
        return [await self.embed(t, task=task, model=model) for t in texts]

    async def close(self) -> None:
        return None


class _MockLeakyVectorStore:
    """Simulates a legacy/unfiltered vector store to verify policy/service defense-in-depth."""

    def __init__(self, results: List[VectorSearchResult]) -> None:
        self._results = results

    async def search(self, *args: Any, **kwargs: Any) -> List[VectorSearchResult]:
        return list(self._results)

    async def upsert(self, record: VectorRecord) -> None:
        return None

    async def close(self) -> None:
        return None


class TestStage36ContextClassifierAndContracts(unittest.IsolatedAsyncioTestCase):
    """Fast unit verification for context classification, evidence binding, and verification gates."""

    def setUp(self) -> None:
        get_metrics_collector().reset()
        self.ext = CompatibilityExtractor()
        self.authority = EvidenceAuthority("stage36-unit-test-secret", allow_ephemeral=False)
        self.verifier = ResponseArtifactVerifier()

    def test_context_independent_and_chatter_exclusion(self) -> None:
        req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            conversation_id="conv_chatter_1",
            messages=[
                ChatMessage(role="user", content="Hello!"),
                ChatMessage(role="assistant", content="Hi, how can I help today?"),
                ChatMessage(role="user", content="How do I reverse a Python list?"),
            ],
            temperature=0.0,
            namespace="team_alpha",
            visibility="private",
            principal_id="p_alice",
        )
        assessment = analyze_request_context(req)
        self.assertEqual(assessment.context_class, ContextClass.CONTEXT_INDEPENDENT)
        self.assertTrue(assessment.contract.eligible_for_shared_reuse)
        self.assertEqual(assessment.effective_query, "How do I reverse a Python list?")
        self.assertIsNotNone(assessment.contract.context_fingerprint)

    def test_context_dependent_dimensions_produce_distinct_fingerprints(self) -> None:
        pairs = [
            (
                "schema",
                [ChatMessage(role="user", content="CREATE TABLE users (id INT PRIMARY KEY, email TEXT);"), ChatMessage(role="user", content="Write a SQL query to aggregate active records")],
                [ChatMessage(role="user", content="CREATE TABLE orders (order_id BIGINT PRIMARY KEY, total NUMERIC);"), ChatMessage(role="user", content="Write a SQL query to aggregate active records")],
            ),
            (
                "code",
                [
                    ChatMessage(role="user", content="def parse_records_v1(rows):\n    return [r.strip() for r in rows if r]"),
                    ChatMessage(role="assistant", content="Here is `parse_records_v1`."),
                    ChatMessage(role="user", content="Refactor the previous implementation to add structured error handling"),
                ],
                [
                    ChatMessage(role="user", content="async function fetchInvoices(clientId) {\n  return await db.query('SELECT * FROM invoices', [clientId]);\n}"),
                    ChatMessage(role="assistant", content="Here is `fetchInvoices`."),
                    ChatMessage(role="user", content="Refactor the previous implementation to add structured error handling"),
                ],
            ),
            (
                "config",
                [ChatMessage(role="user", content="Config:\nmax_retries: 2\ntimeout_seconds: 5"), ChatMessage(role="user", content="Recommend timeout and retry handling for the previous config")],
                [ChatMessage(role="user", content="Config:\nmax_retries: 5\ntimeout_seconds: 30"), ChatMessage(role="user", content="Recommend timeout and retry handling for the previous config")],
            ),
            (
                "framework",
                [ChatMessage(role="user", content="We are using FastAPI 0.110 with Python 3.12 for our backend router."), ChatMessage(role="user", content="Add a dependency injection health check route in the previous implementation")],
                [ChatMessage(role="user", content="We are using FastAPI 0.95 with Python 3.12 for our backend router."), ChatMessage(role="user", content="Add a dependency injection health check route in the previous implementation")],
            ),
            (
                "system_prompt",
                [
                    ChatMessage(role="system", content="Return strict TypeScript types and error codes."),
                    ChatMessage(role="user", content="How to validate JSON payloads in Python?"),
                ],
                [
                    ChatMessage(role="system", content="Return concise Rust serde examples only."),
                    ChatMessage(role="user", content="How to validate JSON payloads in Python?"),
                ],
            ),
        ]
        for label, msgs_a, msgs_b in pairs:
            with self.subTest(dimension=label):
                req_a = GenerationRequest(model="m", provider="mock", messages=msgs_a, temperature=0.0)
                req_b = GenerationRequest(model="m", provider="mock", messages=msgs_b, temperature=0.0)
                ass_a = analyze_request_context(req_a)
                ass_b = analyze_request_context(req_b)
                self.assertEqual(ass_a.context_class, ContextClass.CONTEXT_DEPENDENT)
                self.assertEqual(ass_b.context_class, ContextClass.CONTEXT_DEPENDENT)
                self.assertNotEqual(ass_a.context_fingerprint, ass_b.context_fingerprint)

    def test_context_uncertain_cases_fail_closed(self) -> None:
        uncertain_prompts = [
            "Why did that fail?",
            "Can you optimize that function?",
            "[truncated] continue refactoring the previous implementation",
            "Continue from earlier with the remaining error checks",
        ]
        for prompt in uncertain_prompts:
            with self.subTest(prompt=prompt):
                req = GenerationRequest(
                    model="mock-qwen-27b",
                    provider="mock",
                    messages=[ChatMessage(role="user", content=prompt)],
                    temperature=0.0,
                )
                ass = analyze_request_context(req)
                self.assertEqual(ass.context_class, ContextClass.CONTEXT_UNCERTAIN)
                self.assertFalse(ass.contract.eligible_for_shared_reuse)

    def test_verification_blocks_uncertain_secrets_and_refusals(self) -> None:
        # 1. CONTEXT_UNCERTAIN blocked at verification
        unc_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="Refactor that helper")],
            temperature=0.0,
        )
        unc_meta = self.ext.extract(unc_req, FASTEMBED_MODEL)
        v_unc = self.verifier.verify(response_text="Sure, here is an example.", request_metadata=unc_meta, finish_reason="stop")
        self.assertFalse(v_unc.is_verified)
        self.assertEqual(v_unc.reason_code, CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN)

        # 2. Secret material in generated response blocked before admission
        ok_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to connect to PostgreSQL in Python?")],
            temperature=0.0,
        )
        ok_meta = self.ext.extract(ok_req, FASTEMBED_MODEL)
        secret_resp = "Use postgres://admin_user:SuperSecretPass99@db.internal:5432/prod to connect."
        v_sec = self.verifier.verify(response_text=secret_resp, request_metadata=ok_meta, finish_reason="stop")
        self.assertFalse(v_sec.is_verified)
        self.assertEqual(v_sec.reason_code, CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_PRIVACY)

        # 3. Missing-context LLM refusal on a CONTEXT_DEPENDENT request blocked before admission
        dep_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[
                ChatMessage(role="user", content="CREATE TABLE invoices (id INT);"),
                ChatMessage(role="user", content="Write a SQL query for the previous table"),
            ],
            temperature=0.0,
        )
        dep_meta = self.ext.extract(dep_req, FASTEMBED_MODEL)
        refusal_resp = "Please provide more context or share the schema before continuing."
        v_ref = self.verifier.verify(response_text=refusal_resp, request_metadata=dep_meta, finish_reason="stop")
        self.assertFalse(v_ref.is_verified)
        self.assertEqual(v_ref.reason_code, CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN)

    def test_evidence_hmac_binds_context_contract_and_variant_key(self) -> None:
        req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[
                ChatMessage(role="user", content="CREATE TABLE invoices (id INT PRIMARY KEY);"),
                ChatMessage(role="user", content="Write a SQL query to select unpaid invoices"),
            ],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
        )
        meta = self.ext.extract(req, FASTEMBED_MODEL)
        resp = "```sql\nSELECT id FROM invoices;\n```"
        ver = self.verifier.verify(response_text=resp, request_metadata=meta, finish_reason="stop")
        ev = self.authority.mint_response_verified_evidence(
            cache_namespace="team_beta",
            fingerprint="fp_invoice",
            response_text=resp,
            metadata=meta,
            verification=ver,
            context_independent=False,
            visibility="tenant_shared",
        )
        full_meta = SemanticCompatibilityMetadata(
            schema_version=meta.schema_version,
            extractor_version=meta.extractor_version,
            task_kind=meta.task_kind,
            dimensions=meta.dimensions,
            generation_contract=meta.generation_contract,
            embedding_contract=meta.embedding_contract,
            compatibility_evidence=ev,
            context_contract=meta.context_contract,
        )
        vk_orig = compute_variant_key(full_meta, visibility="tenant_shared")
        tampered_dict = json.loads(json.dumps(full_meta.to_dict()))
        tampered_dict["context_contract"]["context_fingerprint"] = "ctx-v1:" + "f" * 64
        tampered_obj = SemanticCompatibilityMetadata.from_dict(tampered_dict)
        vk_tampered = compute_variant_key(tampered_obj, visibility="tenant_shared")
        self.assertNotEqual(vk_orig, vk_tampered)

        policy = SemanticCachePolicy(evidence_authority=self.authority, configured_model=FASTEMBED_MODEL)
        decision = policy.validate_candidate(
            VectorSearchResult(
                cache_namespace="team_beta",
                fingerprint="fp_invoice",
                query_text="Write a SQL query to select unpaid invoices",
                model=FASTEMBED_MODEL,
                distance=0.0,
                similarity=1.0,
                cached_response=resp,
                metadata=tampered_dict,
                visibility="tenant_shared",
            ),
            req,
            request_metadata=meta,
        )
        self.assertFalse(decision.is_compatible)

    def test_resolve_context_cache_scope_isolation(self) -> None:
        priv_ctx_req = GenerationRequest(
            model="m",
            provider="mock",
            conversation_id="conv_secret",
            messages=[
                ChatMessage(role="user", content="CREATE TABLE secret_ledger (id INT);"),
                ChatMessage(role="user", content="Write a SQL query for secret_ledger rows"),
            ],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_carol",
        )
        ass = analyze_request_context(priv_ctx_req)
        scope = resolve_context_cache_scope(priv_ctx_req, ass, "fp_raw")
        self.assertEqual(scope.effective_visibility, "private")
        self.assertEqual(scope.effective_owner, "p_carol")

    def test_postgres_sql_context_prefilter_uppercases_context_class(self) -> None:
        vs = PostgresVectorStore(
            pool=None,  # type: ignore[arg-type]
            default_namespace="default",
            default_model=FASTEMBED_MODEL,
            dimensions=FASTEMBED_DIM,
        )
        variant_where, _legacy_where, params, _allow_legacy = vs._build_search_queries(
            visibility="tenant_shared",
            context_class="context_independent",
            context_fingerprint="ctx-v1:independent",
        )
        self.assertIn("context_contract'->>'context_class'", variant_where)
        self.assertIn("CONTEXT_INDEPENDENT", params)
        self.assertIn("ctx-v1:independent", params)

    async def test_defense_in_depth_scope_and_budget_metrics_on_lookup(self) -> None:
        ext = CompatibilityExtractor()

        # Candidate 1: another principal's private entry returned by a mock leaky store -> PRIVACY_SCOPE_MISMATCH
        bob_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to sort a list in Python?")],
            temperature=0.0,
            namespace="team_alpha",
            visibility="private",
            principal_id="p_bob",
        )
        bob_meta = ext.extract(bob_req, FASTEMBED_MODEL)
        bob_resp = "```python\nsorted(items)\n```"
        bob_ver = self.verifier.verify(response_text=bob_resp, request_metadata=bob_meta, finish_reason="stop")
        bob_ev = self.authority.mint_response_verified_evidence(
            cache_namespace="team_alpha",
            fingerprint="fp_bob_leak",
            response_text=bob_resp,
            metadata=bob_meta,
            verification=bob_ver,
            context_independent=True,
            visibility="private",
            owner_principal_id="p_bob",
        )
        bob_full = SemanticCompatibilityMetadata(
            schema_version=bob_meta.schema_version,
            extractor_version=bob_meta.extractor_version,
            task_kind=bob_meta.task_kind,
            dimensions=bob_meta.dimensions,
            generation_contract=bob_meta.generation_contract,
            embedding_contract=bob_meta.embedding_contract,
            compatibility_evidence=bob_ev,
            context_contract=bob_meta.context_contract,
        )
        bob_cand = VectorSearchResult(
            cache_namespace="team_alpha",
            fingerprint="fp_bob_leak",
            query_text="How to sort a list in Python?",
            model=FASTEMBED_MODEL,
            distance=0.0,
            similarity=1.0,
            cached_response=bob_resp,
            metadata=bob_full.to_dict(),
            visibility="private",
            owner_principal_id="p_bob",
        )

        # Candidate 2: tenant_shared entry rejected when caller has allow_shared_reads=False -> SHARED_READS_NOT_ALLOWED
        carol_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to sort a list in Python?")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
        )
        carol_meta = ext.extract(carol_req, FASTEMBED_MODEL)
        carol_ver = self.verifier.verify(response_text=bob_resp, request_metadata=carol_meta, finish_reason="stop")
        carol_ev = self.authority.mint_response_verified_evidence(
            cache_namespace="team_beta",
            fingerprint="fp_carol_shared",
            response_text=bob_resp,
            metadata=carol_meta,
            verification=carol_ver,
            context_independent=True,
            visibility="tenant_shared",
        )
        carol_full = SemanticCompatibilityMetadata(
            schema_version=carol_meta.schema_version,
            extractor_version=carol_meta.extractor_version,
            task_kind=carol_meta.task_kind,
            dimensions=carol_meta.dimensions,
            generation_contract=carol_meta.generation_contract,
            embedding_contract=carol_meta.embedding_contract,
            compatibility_evidence=carol_ev,
            context_contract=carol_meta.context_contract,
        )
        carol_cand = VectorSearchResult(
            cache_namespace="team_beta",
            fingerprint="fp_carol_shared",
            query_text="How to sort a list in Python?",
            model=FASTEMBED_MODEL,
            distance=0.0,
            similarity=1.0,
            cached_response=bob_resp,
            metadata=carol_full.to_dict(),
            visibility="tenant_shared",
        )

        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=2,
            configured_model=FASTEMBED_MODEL,
            retrieval_budget=2,
            evidence_authority=self.authority,
        )

        # Check private_scope_rejections
        svc_priv = SemanticCacheService(
            embedding_provider=_FixedEmbedder(),
            vector_store=_MockLeakyVectorStore([bob_cand]),  # type: ignore[arg-type]
            policy=policy,
            default_namespace="team_alpha",
        )
        alice_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to sort a list in Python?")],
            temperature=0.0,
            namespace="team_alpha",
            visibility="private",
            principal_id="p_alice",
        )
        self.assertIsNone(await svc_priv.lookup(alice_req))
        self.assertGreaterEqual(svc_priv.stats.private_scope_rejections, 1)

        # Check tenant_shared_authorization_rejections + retrieval_budget_exhaustions (2 rejected rows == budget 2)
        svc_shared = SemanticCacheService(
            embedding_provider=_FixedEmbedder(),
            vector_store=_MockLeakyVectorStore([carol_cand, carol_cand]),  # type: ignore[arg-type]
            policy=policy,
            default_namespace="team_beta",
        )
        fay_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to sort a list in Python?")],
            temperature=0.0,
            namespace="team_beta",
            visibility="private",
            principal_id="p_fay",
            allow_shared_reads=False,
        )
        self.assertIsNone(await svc_shared.lookup(fay_req))
        self.assertGreaterEqual(svc_shared.stats.tenant_shared_authorization_rejections, 1)
        self.assertGreaterEqual(svc_shared.stats.retrieval_budget_exhaustions, 1)


class TestStage36HttpLifecycleInMemory(unittest.IsolatedAsyncioTestCase):
    """End-to-end Stage 3.6 HTTP verification across all 24 CTX scenarios (in-memory)."""

    async def asyncSetUp(self) -> None:
        self._saved_cache = get_query_cache()
        self._saved_sem = get_semantic_cache()
        self._saved_vs = get_vector_store()
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)

    async def asyncTearDown(self) -> None:
        set_query_cache(self._saved_cache)
        set_semantic_cache(self._saved_sem)
        set_vector_store(self._saved_vs)
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()
        reload_settings()

    async def test_stage_3_6_all_24_scenarios_http_in_memory(self) -> None:
        report = await run_stage3_6_evaluation(use_postgres=False, use_real_fastembed=False)
        self.assertEqual(report["scenarios_total"], 24)
        self.assertEqual(report["scenarios_passed"], 24, report)
        for scenario_idx in range(1, 25):
            expected_id = f"CTX-{scenario_idx:03d}"
            found = next((s for s in report["scenarios"] if s["id"] == expected_id), None)
            self.assertIsNotNone(found, f"Missing scenario {expected_id}")
            self.assertTrue(found["passed"], f"Scenario {expected_id} failed: {found}")

        metrics = report["mandatory_metrics"]
        for key in MANDATORY_21_METRICS:
            self.assertIn(key, metrics, f"Missing mandatory metric: {key}")

        self.assertGreater(metrics["context_independent_requests"], 0)
        self.assertGreater(metrics["context_dependent_requests"], 0)
        self.assertGreater(metrics["context_uncertain_requests"], 0)
        self.assertGreater(metrics["context_compatibility_matches"], 0)
        self.assertGreater(metrics["context_compatibility_rejections"], 0)
        self.assertGreater(metrics["context_metadata_missing"], 0)
        self.assertGreater(metrics["context_fingerprint_mismatches"], 0)
        self.assertGreater(metrics["cross_conversation_semantic_hits"], 0)
        self.assertGreater(metrics["semantic_admission_successes"], 0)
        self.assertGreater(metrics["semantic_admission_rejections"], 0)
        self.assertGreater(metrics["semantic_hits"], 0)
        self.assertGreater(metrics["coalesced_semantic_hits"], 0)
        self.assertGreater(metrics["provider_calls_avoided"], 0)
        self.assertGreater(metrics["semantic_fallbacks"], 0)
        self.assertGreater(metrics["ambiguous_candidate_rejections"], 0)
        self.assertGreater(metrics["evidence_validation_rejections"], 0)
        self.assertGreater(metrics["context_validation_latency_ms"]["sample_count"], 0)
        self.assertGreater(metrics["compatibility_validation_latency_ms"]["sample_count"], 0)


class TestStage36RealFastEmbedPostgresHttpEvaluation(unittest.IsolatedAsyncioTestCase):
    """
    Opt-in live PostgreSQL 16 + pgvector + real FastEmbed (BAAI/bge-small-en-v1.5, 384-d)
    verification suite for Stage 3.6.
    """

    async def asyncSetUp(self) -> None:
        self._saved_cache = get_query_cache()
        self._saved_sem = get_semantic_cache()
        self._saved_vs = get_vector_store()
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)

        strict = os.environ.get("REQUIRE_FASTEMBED_PGVECTOR_TEST") == "1"
        enabled = (
            os.environ.get("RUN_FASTEMBED_PGVECTOR_TEST") == "1"
            or os.environ.get("RUN_FASTEMBED_SMOKE") == "1"
            or strict
        )
        if not enabled:
            self.skipTest("Opt-in test: set RUN_FASTEMBED_PGVECTOR_TEST=1 to run Stage 3.6 FastEmbed + Postgres HTTP suite.")

        try:
            self.db_url = await resolve_fastembed_database_url()
        except Exception as exc:
            if strict:
                self.fail(f"Required PostgreSQL setup failed: {exc}")
            self.skipTest(f"PostgreSQL unavailable: {exc}")

    async def asyncTearDown(self) -> None:
        set_query_cache(self._saved_cache)
        set_semantic_cache(self._saved_sem)
        set_vector_store(self._saved_vs)
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()
        reload_settings()

    async def test_real_fastembed_postgres_all_24_scenarios_and_indexes(self) -> None:
        report = await run_stage3_6_evaluation(
            use_postgres=True,
            db_url=self.db_url,
            use_real_fastembed=True,
        )
        self.assertEqual(report["backend"], "postgres+pgvector")
        self.assertEqual(report["embedding_mode"], "real_fastembed_bge_small_en_v1_5")
        self.assertEqual(report["scenarios_total"], 24)
        self.assertEqual(report["scenarios_passed"], 24, report)

        pg = report["postgres_benchmark"]
        self.assertIsNotNone(pg)
        self.assertTrue(pg["app_sql_context_prefilter_enabled"])
        self.assertTrue(pg["required_context_indexes_present"])
        self.assertEqual(pg["starvation_check"]["unfiltered_budget_25_returned"], 25)
        self.assertFalse(pg["starvation_check"]["unfiltered_found_target"])
        self.assertEqual(pg["starvation_check"]["sql_prefilter_returned"], 1)
        self.assertTrue(pg["starvation_check"]["sql_prefilter_found_target"])
        http_starv = pg.get("http_starvation_recovery")
        self.assertIsNotNone(http_starv)
        self.assertTrue(http_starv["http_semantic_hit"])
        self.assertTrue(http_starv["app_sql_context_prefilter"])
        self.assertEqual(http_starv["search_call_context_class"], ContextClass.CONTEXT_DEPENDENT.value)
        self.assertTrue(http_starv["search_call_has_fingerprint"])
        self.assertIn(
            "Index Scan(idx_semantic_variant_records_context_scope)",
            pg["explain_context_indexed"]["plan_nodes"],
        )

        conv = report["server_managed_conversations"]
        self.assertIsNotNone(conv)
        self.assertEqual(conv["cross_principal_blocked_status"], 404)
        self.assertEqual(conv["persisted_messages"], 4)
        self.assertEqual(conv["turn_2_context_status"], "complete")

        conc = report["concurrency"]
        self.assertEqual(conc["concurrent_batch_size"], 8)
        self.assertEqual(conc["upstream_calls"], 1)
        self.assertEqual(conc["coalesced_hits"], 7)

        metrics = report["mandatory_metrics"]
        for key in MANDATORY_21_METRICS:
            self.assertIn(key, metrics, f"Missing mandatory metric: {key}")
        self.assertGreater(metrics["ambiguous_candidate_rejections"], 0)
        self.assertGreater(metrics["evidence_validation_rejections"], 0)
        # Active SQL context prefiltering keeps missing-context rows out of retrieval completely.
        self.assertEqual(metrics["context_metadata_missing"], 0)

    async def test_postgres_app_sql_context_prefilter_e2e_and_exact_privacy_audit(self) -> None:
        """
        Verifies in the real FastAPI + PostgreSQL 16 + FastEmbed application:
        1. Application-created SemanticCacheService enables sql_context_prefilter=True.
        2. Captured live SQL from PostgresVectorStore._build_search_queries binds
           context_class and context_fingerprint predicates for both CONTEXT_INDEPENDENT
           and CONTEXT_DEPENDENT HTTP lookups.
        3. Missing context metadata rows remain inaccessible at SQL level.
        4. EXPLAIN ANALYZE confirms partial JSONB context index usage under mixed workload.
        5. Direct PostgreSQL cache_entries audit proves sensitive/DSN requests never persist
           and personal tenant_shared requests downgrade strictly to private owner scope
           across both streaming and non-streaming requests.
        """
        await reset_pg_tables(self.db_url)
        provider = ScriptedStage36Provider(delay_seconds=0.0, tag="RF_PG")
        env = {
            "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
            "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
            "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
            "CONVERSATION_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
            "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "true",
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": self.db_url,
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": FASTEMBED_MODEL,
            "VECTOR_DIMENSIONS": str(FASTEMBED_DIM),
            "LLM_PROVIDER": "mock",
        }
        carol_prin = derive_principal_id("tok_carol", STAGE_3_5_5_IDENTITY_SECRET)

        with patch.dict(os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: provider
            async with app.router.lifespan_context(app):
                sem_svc = app.state.semantic_cache
                vs = app.state.vector_store
                self.assertIsNotNone(sem_svc)
                self.assertTrue(sem_svc.sql_context_prefilter)

                captured_sql_calls: List[Dict[str, Any]] = []
                orig_build = vs._build_search_queries

                def _spy_build(*args: Any, **kwargs: Any):
                    res = orig_build(*args, **kwargs)
                    captured_sql_calls.append(
                        {
                            "context_class": kwargs.get("context_class"),
                            "context_fingerprint": kwargs.get("context_fingerprint"),
                            "single_query": res[0],
                            "combined_query": res[1],
                            "params": list(res[2]),
                        }
                    )
                    return res

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    # 1. Seed a CONTEXT_DEPENDENT entry on Python list sorting in team_beta
                    r_dep = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_carol"},
                        json={
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": "CREATE TABLE custom_sort_jobs (job_id INT, priority INT);"},
                                {"role": "user", "content": "Write a SQL query to sort a Python list"},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    self.assertEqual(r_dep.status_code, 200)

                    # Seed a missing-context row directly in DB to prove SQL excludes it
                    raw_vec = (await sem_svc.embedding_provider.embed("How do I reverse a Python tuple cleanly?", EmbeddingTask.DOCUMENT)).vector
                    ext = CompatibilityExtractor()
                    tmp_req = GenerationRequest(
                        model="mock-qwen-27b",
                        provider="mock",
                        messages=[ChatMessage(role="user", content="How do I reverse a Python tuple cleanly?")],
                        temperature=0.0,
                        namespace="team_beta",
                        visibility="tenant_shared",
                    )
                    tmp_md = ext.extract(tmp_req, FASTEMBED_MODEL).to_dict()
                    tmp_md.pop("context_contract", None)
                    async with vs.pool.acquire() as conn:
                        await conn.execute(
                            """
                            INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, visibility, owner_principal_id, prompt_tokens, completion_tokens, total_tokens, latency_ms)
                            VALUES ('team_beta', 'fp_missing_ctx_pg', 'stale missing ctx', 'mock-qwen-27b', 'tenant_shared', NULL, 5, 5, 10, 5.0)
                            ON CONFLICT DO NOTHING;
                            """
                        )
                        await conn.execute(
                            """
                            INSERT INTO vector_records (cache_namespace, fingerprint, query_text, vector, model, metadata, visibility, owner_principal_id)
                            VALUES ('team_beta', 'fp_missing_ctx_pg', 'How do I reverse a Python tuple cleanly?', $1::vector, $2, $3::jsonb, 'tenant_shared', NULL)
                            ON CONFLICT DO NOTHING;
                            """,
                            f"[{','.join(str(x) for x in raw_vec)}]",
                            FASTEMBED_MODEL,
                            json.dumps(tmp_md),
                        )

                    with patch.object(vs, "_build_search_queries", side_effect=_spy_build):
                        # Standalone CONTEXT_INDEPENDENT query -> binds CONTEXT_INDEPENDENT + ctx-v1:independent,
                        # excludes missing-context row at SQL level
                        r_indep = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={
                                "model": "mock-qwen-27b",
                                "messages": [{"role": "user", "content": "How do I reverse a Python tuple cleanly?"}],
                                "temperature": 0.0,
                            },
                        )
                        self.assertEqual(r_indep.status_code, 200)
                        self.assertFalse(r_indep.json()["cached"])

                        # CONTEXT_DEPENDENT follow-up with DIFFERENT schema -> binds CONTEXT_DEPENDENT + ctx-v1:<fp>, misses cleanly
                        r_dep_diff = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={
                                "model": "mock-qwen-27b",
                                "messages": [
                                    {"role": "user", "content": "CREATE TABLE unrelated_audit_logs (log_id INT);"},
                                    {"role": "user", "content": "Write a SQL query to sort a Python list"},
                                ],
                                "temperature": 0.0,
                            },
                        )
                        self.assertEqual(r_dep_diff.status_code, 200)
                        self.assertFalse(r_dep_diff.json()["cached"])

                    self.assertEqual(len(captured_sql_calls), 2)
                    indep_sql = captured_sql_calls[0]
                    self.assertEqual(indep_sql["context_class"], "CONTEXT_INDEPENDENT")
                    self.assertEqual(indep_sql["context_fingerprint"], "ctx-v1:independent")
                    self.assertIn("context_contract'->>'context_class'", indep_sql["combined_query"])
                    self.assertIn("context_contract'->>'context_fingerprint'", indep_sql["combined_query"])
                    self.assertIn("CONTEXT_INDEPENDENT", indep_sql["params"])
                    self.assertIn("ctx-v1:independent", indep_sql["params"])

                    dep_sql = captured_sql_calls[1]
                    self.assertEqual(dep_sql["context_class"], "CONTEXT_DEPENDENT")
                    self.assertTrue(str(dep_sql["context_fingerprint"]).startswith("ctx-v1:"))
                    self.assertIn("CONTEXT_DEPENDENT", dep_sql["params"])

                    q_vec_str = f"[{','.join(str(float(x)) for x in raw_vec)}]"
                    async with vs.pool.acquire() as conn:
                        idx_rows = await conn.fetch(
                            """
                            SELECT indexname
                            FROM pg_indexes
                            WHERE schemaname = 'public'
                              AND tablename IN ('vector_records', 'semantic_variant_records')
                            """
                        )
                        idx_names = {r["indexname"] for r in idx_rows}
                        for req_idx in (
                            "idx_vector_records_context_scope",
                            "idx_vector_records_private_context_scope",
                            "idx_semantic_variant_records_context_scope",
                            "idx_semantic_variant_records_private_context_scope",
                        ):
                            self.assertIn(req_idx, idx_names)
                        async with conn.transaction():
                            await conn.execute("SET LOCAL enable_seqscan = off;")
                            plan_json_raw = await conn.fetchval(
                                f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)\n{indep_sql['combined_query']}",
                                q_vec_str,
                                "team_beta",
                                FASTEMBED_MODEL,
                                25,
                                *indep_sql["params"],
                            )
                    plan_text = json.dumps(json.loads(plan_json_raw))
                    self.assertIn("context_contract", plan_text)
                    self.assertIn("ctx-v1:independent", plan_text)
                    self.assertIn("Index Scan", plan_text)
                    self.assertTrue(
                        "idx_semantic_variant_records_context_scope" in plan_text
                        or "idx_semantic_variant_records_lookup" in plan_text
                    )

                    # 2. Exercise exact-cache privacy over HTTP + PG cache_entries inspection
                    api_secret_prompt = "Validate Python token api_key=sk-proj-secret1234567890abcdefghij"
                    dsn_secret_prompt = "Connect using postgresql://svc_acct:TopSecretPg99@db.internal:5432/prod_db in Python"
                    personal_prompt = "Debug my config at /home/carol/work/private_token_cfg.py in Python"

                    for p in (api_secret_prompt, dsn_secret_prompt):
                        r_s = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_carol"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": p}], "temperature": 0.0},
                        )
                        self.assertEqual(r_s.status_code, 200)
                        self.assertFalse(r_s.json()["cached"])

                    r_pers = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_carol"},
                        json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": personal_prompt}], "temperature": 0.0},
                    )
                    self.assertEqual(r_pers.status_code, 200)
                    self.assertFalse(r_pers.json()["cached"])

                    # Stream sensitive query as Carol -> must also bypass cache
                    r_stream = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_carol"},
                        json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": dsn_secret_prompt}], "stream": True, "temperature": 0.0},
                    )
                    self.assertEqual(r_stream.status_code, 200)
                    self.assertIn('"cached": false', r_stream.text.lower())

                    # Dave (tenant_shared) and Erin (allow_shared_reads=true) MUST miss on personal & secret prompts
                    for tok in ("tok_dave", "tok_erin"):
                        for p in (api_secret_prompt, dsn_secret_prompt, personal_prompt):
                            rep = await client.post(
                                "/query",
                                headers={"X-Client-Token": tok},
                                json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": p}], "temperature": 0.0},
                            )
                            self.assertEqual(rep.status_code, 200)
                            self.assertFalse(rep.json()["cached"], f"{tok} unexpectedly cached {p!r}")

                    # Inspect live PostgreSQL cache_entries table directly!
                    api_fp = generate_fingerprint([ChatMessage(role="user", content=api_secret_prompt)], "mock-qwen-27b", 0.0, provider="mock")
                    dsn_fp = generate_fingerprint([ChatMessage(role="user", content=dsn_secret_prompt)], "mock-qwen-27b", 0.0, provider="mock")
                    pers_fp = generate_fingerprint([ChatMessage(role="user", content=personal_prompt)], "mock-qwen-27b", 0.0, provider="mock")
                    pers_priv_fp = derive_scoped_exact_key(pers_fp, "private", carol_prin)

                    async with vs.pool.acquire() as conn:
                        rows = await conn.fetch(
                            """
                            SELECT cache_namespace, fingerprint, visibility, owner_principal_id
                            FROM cache_entries
                            WHERE cache_namespace = 'team_beta'
                            """
                        )
                        by_fp = {r["fingerprint"]: r for r in rows}
                        self.assertNotIn(api_fp, by_fp, "API credential leaked to tenant_shared cache_entries!")
                        self.assertNotIn(dsn_fp, by_fp, "PostgreSQL DSN credential leaked to tenant_shared cache_entries!")
                        self.assertNotIn(
                            derive_scoped_exact_key(api_fp, "private", carol_prin),
                            by_fp,
                            "Sensitive request should bypass private cache_entries under full-bypass policy!",
                        )
                        self.assertNotIn(pers_fp, by_fp, "Personal request leaked to tenant_shared cache_entries!")
                        self.assertIn(pers_priv_fp, by_fp, "Personal request should downgrade to carol's private key!")
                        self.assertEqual(by_fp[pers_priv_fp]["visibility"], "private")
                        self.assertEqual(by_fp[pers_priv_fp]["owner_principal_id"], carol_prin)


class _DummyStreamingProvider(LLMProvider):
    """Minimal mock provider supporting both generate() and stream() for core privacy tests."""

    def __init__(self) -> None:
        self.calls: List[str] = []

    @property
    def provider_name(self) -> str:
        return "mock"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        q = request.messages[-1].content if request.messages else ""
        self.calls.append(f"gen:{q}")
        return GenerationResult(
            text=f"Answer-{len(self.calls)}: ```python\nreturn True\n```",
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            latency_ms=5.0,
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        q = request.messages[-1].content if request.messages else ""
        self.calls.append(f"stream:{q}")
        idx = len(self.calls)
        yield StreamChunk(delta=f"Stream-{idx}: ", done=False, model=request.model)
        yield StreamChunk(
            delta="",
            done=True,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=8, completion_tokens=8, total_tokens=16),
        )

    async def close(self) -> None:
        return None


class TestStage361ExactCachePrivacyAndStreaming(unittest.IsolatedAsyncioTestCase):
    """Stage 3.6.1 S36-RF-01 & in-memory regression coverage for exact-cache privacy boundaries."""

    def setUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(True)

    def tearDown(self) -> None:
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()

    def _build_core(self) -> tuple[ExactCache, SemanticCacheService, IntelliCacheCore, _DummyStreamingProvider]:
        cache = ExactCache(max_entries=200, ttl_seconds=3600)
        authority = EvidenceAuthority("stage361-unit-secret", allow_ephemeral=False)
        vstore = InMemoryVectorStore(dimensions=FASTEMBED_DIM, default_model=FASTEMBED_MODEL, exact_cache=cache)
        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model=FASTEMBED_MODEL,
            evidence_authority=authority,
        )
        sem = SemanticCacheService(
            embedding_provider=_FixedEmbedder(),
            vector_store=vstore,
            policy=policy,
            default_namespace="team_beta",
            sql_context_prefilter=False,
        )
        ctrl = get_semantic_serving_controller()
        ctrl.set_serving_enabled(True, reason="stage361_test")
        core = IntelliCacheCore(
            cache=cache,
            cache_enabled=True,
            semantic_cache=sem,
            semantic_serving_controller=ctrl,
            semantic_cache_startup_enabled=True,
        )
        return cache, sem, core, _DummyStreamingProvider()

    def test_resolve_context_cache_scope_all_privacy_branches(self) -> None:
        fp = "f" * 64
        cases = [
            (
                "unknown_visibility",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="Sort a list")], visibility="unknown", principal_id="p1"),
                ("unknown", None, False, False, False, None),
            ),
            (
                "tenant_shared_api_credential_full_bypass",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="Use api_key=sk-1234567890abcdefghijklmnop in Python")], visibility="tenant_shared", principal_id="p_carol"),
                ("private", "p_carol", False, False, False, None),
            ),
            (
                "tenant_shared_dsn_credential_full_bypass",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="Connect postgresql://admin:SuperPass123@pg.local:5432/app in Python")], visibility="tenant_shared", principal_id="p_carol"),
                ("private", "p_carol", False, False, False, None),
            ),
            (
                "private_sensitive_credential_full_bypass",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="Use postgres://admin:SecretPass123@db:5432/prod in Python")], visibility="private", principal_id="p_alice"),
                ("private", "p_alice", False, False, False, None),
            ),
            (
                "tenant_shared_personal_downgraded_with_principal",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="Debug my company script at /home/carol/app.py")], visibility="tenant_shared", principal_id="p_carol"),
                ("private", "p_carol", False, True, True, derive_scoped_exact_key(fp, "private", "p_carol")),
            ),
            (
                "tenant_shared_context_dep_conv_missing_principal_bypassed",
                GenerationRequest(
                    model="m",
                    provider="mock",
                    conversation_id="conv_missing_p",
                    messages=[
                        ChatMessage(role="user", content="CREATE TABLE orders (id INT);"),
                        ChatMessage(role="user", content="Write SQL to list orders"),
                    ],
                    visibility="tenant_shared",
                    principal_id=None,
                ),
                ("private", None, False, False, False, None),
            ),
            (
                "ordinary_independent_tenant_shared_preserved",
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content="How to sort a Python list?")], visibility="tenant_shared", principal_id="p_carol"),
                ("tenant_shared", None, False, True, True, derive_scoped_exact_key(fp, "tenant_shared", None)),
            ),
        ]
        for label, req, expected in cases:
            with self.subTest(branch=label):
                ass = analyze_request_context(req)
                scope = resolve_context_cache_scope(req, ass, fp)
                self.assertEqual(
                    (
                        scope.effective_visibility,
                        scope.effective_owner,
                        scope.allow_shared_reads,
                        scope.can_cache_exact,
                        scope.can_admit_semantic,
                        scope.exact_storage_key,
                    ),
                    expected,
                )

    async def test_core_exact_and_semantic_privacy_boundaries_end_to_end(self) -> None:
        cache, sem, core, prov = self._build_core()

        # 1. Tenant-shared API credential & DSN credentials bypass BOTH exact and semantic cache
        for secret_q in (
            "Write a Python client using api_key=sk-1234567890abcdefghijklmnop",
            "Query using postgresql://admin:SecretPass99@db.local:5432/prod in Python",
            "Query using postgres://admin:SecretPass99@db.local:5432/prod in Python",
        ):
            r1 = await core.execute_query(
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=secret_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared", principal_id="p_carol"),
                prov,
            )
            self.assertFalse(r1.cached)
            # Second tenant_shared reader & same writer miss -> zero entries created
            r2 = await core.execute_query(
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=secret_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared", principal_id="p_dave"),
                prov,
            )
            self.assertFalse(r2.cached)
        self.assertEqual(len(cache._entries), 0)

        # 2. Sensitive private request follows documented full-bypass policy
        priv_secret_q = "Check my Python DSN postgresql://user:PrivPass88@internal:5432/app"
        ps1 = await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=priv_secret_q)], temperature=0.0, namespace="team_beta", visibility="private", principal_id="p_alice"),
            prov,
        )
        ps2 = await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=priv_secret_q)], temperature=0.0, namespace="team_beta", visibility="private", principal_id="p_alice"),
            prov,
        )
        self.assertFalse(ps1.cached)
        self.assertFalse(ps2.cached)
        self.assertEqual(len(cache._entries), 0)

        # 3. Tenant-shared personal context downgrades to private owned by carol; dave & erin (allow_shared_reads=True) cannot hit
        personal_q = "How can I fix my Python script at /home/carol/app/config.py?"
        pers_r = await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=personal_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared", principal_id="p_carol"),
            prov,
        )
        self.assertFalse(pers_r.cached)
        self.assertEqual(len(cache._entries), 1)
        stored_entry = next(iter(cache._entries.values()))
        self.assertEqual(stored_entry.visibility, "private")
        self.assertEqual(stored_entry.owner_principal_id, "p_carol")

        for probe_vis, probe_own, probe_shared in (("tenant_shared", "p_dave", False), ("private", "p_erin", True)):
            probe_r = await core.execute_query(
                GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=personal_q)], temperature=0.0, namespace="team_beta", visibility=probe_vis, principal_id=probe_own, allow_shared_reads=probe_shared),
                prov,
            )
            self.assertFalse(probe_r.cached, f"{probe_vis}/{probe_own} should not retrieve carol's personal entry")

        # 4. Exact-cache hits must not bypass privacy restrictions even when same-fingerprint shared entry exists
        shared_q = "How do I reverse a Python list?"
        await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=shared_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared", principal_id="p_carol"),
            prov,
        )
        # Ordinary tenant_shared reuse succeeds for dave & erin
        dave_hit = await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=shared_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared", principal_id="p_dave"),
            prov,
        )
        self.assertTrue(dave_hit.cached)
        self.assertEqual(dave_hit.cache_type, "exact")
        erin_hit = await core.execute_query(
            GenerationRequest(model="m", provider="mock", messages=[ChatMessage(role="user", content=shared_q)], temperature=0.0, namespace="team_beta", visibility="private", principal_id="p_erin", allow_shared_reads=True),
            prov,
        )
        self.assertTrue(erin_hit.cached)
        self.assertEqual(erin_hit.cache_type, "exact")

        # If a personal-context request from frank (allow_shared_reads=True, no private entries)
        # probes while tenant_shared exact & semantic entries exist (even seeding a colliding shared exact key),
        # resolve_context_cache_scope must keep allow_shared_reads=False so shared cache cannot be read!
        pers_shared_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="My account question: " + shared_q)],
            temperature=0.0,
            namespace="team_beta",
            visibility="private",
            principal_id="p_frank",
            allow_shared_reads=True,
        )
        pers_fp = derive_variant_exact_fingerprint(
            generate_fingerprint(
                messages=pers_shared_req.messages,
                model=pers_shared_req.model,
                temperature=pers_shared_req.temperature,
                max_tokens=pers_shared_req.max_tokens,
                provider=pers_shared_req.provider,
            ),
            pers_shared_req.metadata,
        )
        await cache.set(
            pers_fp,
            CacheEntry(
                response="FORBIDDEN_SHARED_LEAK",
                model="m",
                usage=UsageMetrics(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                latency_ms=1.0,
                provider="mock",
                visibility="tenant_shared",
                owner_principal_id=None,
            ),
            namespace="team_beta",
        )
        pers_frank = await core.execute_query(pers_shared_req, prov)
        self.assertFalse(pers_frank.cached)
        self.assertNotIn("FORBIDDEN_SHARED_LEAK", pers_frank.text)

        # 5. Tenant-shared context-dependent + conversation_id + missing principal bypasses caching completely
        before_cnt = len(cache._entries)
        conv_miss_p_req = GenerationRequest(
            model="m",
            provider="mock",
            conversation_id="conv_anonymous_tenant_shared",
            messages=[
                ChatMessage(role="user", content="CREATE TABLE secret_keys (kid INT PRIMARY KEY);"),
                ChatMessage(role="user", content="Write a SQL query to inspect secret_keys"),
            ],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id=None,
        )
        c_no_p_1 = await core.execute_query(conv_miss_p_req, prov)
        c_no_p_2 = await core.execute_query(conv_miss_p_req, prov)
        self.assertFalse(c_no_p_1.cached)
        self.assertFalse(c_no_p_2.cached)
        self.assertEqual(len(cache._entries), before_cnt)

    async def test_streaming_and_non_streaming_preserve_equivalent_privacy_boundaries(self) -> None:
        cache, _sem, core, prov = self._build_core()

        async def _collect_stream(req: GenerationRequest) -> tuple[bool, str]:
            cached = False
            cache_type = "none"
            async for ch in core.execute_stream(req, prov):
                if ch.done:
                    cached = ch.cached
                    cache_type = ch.cache_type
            return cached, cache_type

        # 1. Streaming tenant_shared request with API key bypasses cache completely
        sec_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="Test Python script api_key=sk-1234567890abcdefghijklmnop")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_carol",
            stream=True,
        )
        self.assertEqual(await _collect_stream(sec_req), (False, "none"))
        self.assertEqual(await _collect_stream(sec_req), (False, "none"))
        self.assertEqual(len(cache._entries), 0)

        # 2. Streaming tenant_shared with personal context downgrades to private (dave misses; carol repeats hit)
        pers_stream_carol = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="Explain my Python log file at /home/carol/app.log")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_carol",
            stream=True,
        )
        pers_stream_dave = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="Explain my Python log file at /home/carol/app.log")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_dave",
            stream=True,
        )
        self.assertEqual(await _collect_stream(pers_stream_carol), (False, "none"))
        self.assertEqual(await _collect_stream(pers_stream_dave), (False, "none"))
        self.assertEqual(await _collect_stream(pers_stream_carol), (True, "exact"))

        # 3. Streaming tenant_shared context-dependent + conversation_id + missing principal bypasses cache
        entries_before = len(cache._entries)
        conv_stream_no_p = GenerationRequest(
            model="m",
            provider="mock",
            conversation_id="conv_stream_no_p",
            messages=[
                ChatMessage(role="user", content="CREATE TABLE stream_cfg (k TEXT);"),
                ChatMessage(role="user", content="Query stream_cfg rows in SQL"),
            ],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id=None,
            stream=True,
        )
        self.assertEqual(await _collect_stream(conv_stream_no_p), (False, "none"))
        self.assertEqual(await _collect_stream(conv_stream_no_p), (False, "none"))
        self.assertEqual(len(cache._entries), entries_before)

        # 4. Ordinary benign streaming tenant_shared request still reuses across principals
        ok_carol = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to flatten Python lists?")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_carol",
            stream=True,
        )
        ok_dave = GenerationRequest(
            model="m",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to flatten Python lists?")],
            temperature=0.0,
            namespace="team_beta",
            visibility="tenant_shared",
            principal_id="p_dave",
            stream=True,
        )
        self.assertEqual(await _collect_stream(ok_carol), (False, "none"))
        self.assertEqual(await _collect_stream(ok_dave), (True, "exact"))


if __name__ == "__main__":
    unittest.main()
