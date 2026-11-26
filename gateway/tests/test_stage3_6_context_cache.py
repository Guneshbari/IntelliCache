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
from typing import Any, Dict, List
import unittest

from app.config import reload_settings
from app.core.contracts import ChatMessage, GenerationRequest
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.metrics import get_metrics_collector
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
from app.semantic_cache.control import reset_semantic_serving_controller
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.verification import ResponseArtifactVerifier
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.postgres_store import PostgresVectorStore
from app.api.routes import (
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from scripts.evaluate_stage3_5_5_operational import FASTEMBED_DIM, FASTEMBED_MODEL, resolve_fastembed_database_url
from scripts.evaluate_stage3_6_context_cache import MANDATORY_21_METRICS, run_stage3_6_evaluation


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
        self.assertTrue(pg["required_context_indexes_present"])
        self.assertEqual(pg["starvation_check"]["unfiltered_budget_25_returned"], 25)
        self.assertFalse(pg["starvation_check"]["unfiltered_found_target"])
        self.assertEqual(pg["starvation_check"]["sql_prefilter_returned"], 1)
        self.assertTrue(pg["starvation_check"]["sql_prefilter_found_target"])
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
        self.assertGreater(metrics["context_metadata_missing"], 0)


if __name__ == "__main__":
    unittest.main()
