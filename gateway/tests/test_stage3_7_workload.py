"""
Stage 3.7 Real-World Workload Evaluation, Optimization & Reliability Test Suite.

Two test layers:
1. ``TestStage37DeterministicRegressions`` (executes in all Python 3.11 / 3.12 unit & CI runs):
   - Dataset schema, held-out disjointness, and minimum scenario-coverage invariants.
   - Held-out context classification, chitchat-referent fail-closed safety, inline
     signature identifier tracking, and multi-word stack decision invalidation.
   - Optimization #1: Exact-cache read & streaming fail-open recovery (``REL-04`` fix).
   - Optimization #2: PostgreSQL ``CONTEXT_INDEPENDENT`` SQL specialization preserving HNSW
     plans across generic prepared-statement transitions while keeping ``_build_search_queries``
     backward compatibility and strict ``CONTEXT_DEPENDENT`` parameterization.
   - Optimization #4: Policy batch context/scope precomputation equivalence and call-count check.
   - Baseline safe default configuration verification (semantic cache disabled by default).
2. ``TestStage37LiveFastEmbedPostgresEvaluation`` (executes when ``RUN_FASTEMBED_PGVECTOR_TEST=1``
   or ``RUN_FASTEMBED_SMOKE=1`` with live PostgreSQL 16 + pgvector + FastEmbed):
   - End-to-end Phases 1-7 execution over PostgreSQL + ``BAAI/bge-small-en-v1.5`` (384-d),
     verifying 24/24 context held-out, 26/26 compatibility (0 false semantic hits), 24/24 HTTP
     workload, PG prefilter vs starvation recall, A/B/C caching gains, 13/13 failure recovery,
     and strict secret/principal non-disclosure.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Dict, List
import unittest
from unittest.mock import patch

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.config import Settings
from app.core.contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    UsageMetrics,
)
from app.core.service import IntelliCacheCore
from app.providers.interfaces import LLMProvider
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.context import (
    ContextAssessment,
    ContextClass,
    analyze_request_context,
    resolve_context_cache_scope,
)
from app.semantic_cache.contracts import (
    CompatibilityReasonCode,
    ResponseVerificationResult,
    SemanticCompatibilityMetadata,
    TruncationStatus,
    VerificationStatus,
)
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import VectorSearchResult
from app.vector_store.postgres_store import PostgresVectorStore
from scripts.evaluate_stage3_5_5_operational import FASTEMBED_DIM, FASTEMBED_MODEL
from scripts.evaluate_stage3_7_workload import (
    run_phase2_context_evaluation,
    run_stage37_evaluation,
)


GATEWAY_ROOT = Path(__file__).resolve().parent.parent
DATASET_DIR = GATEWAY_ROOT / "evaluation" / "datasets"


class _FailingReadCache(ExactCache):
    """ExactCache double raising OSError on exact reads and miss counters."""

    def __init__(self, *, fail_get: bool = True, fail_set: bool = False) -> None:
        super().__init__(max_entries=100, ttl_seconds=60)
        self.fail_get = fail_get
        self.fail_set = fail_set

    async def get(self, key: str, **kwargs: Any) -> CacheEntry | None:
        if self.fail_get:
            raise OSError("Simulated PostgreSQL read outage")
        return await super().get(key, **kwargs)

    async def set(self, key: str, entry: CacheEntry, **kwargs: Any) -> None:
        if self.fail_set:
            raise OSError("Simulated PostgreSQL write outage")
        await super().set(key, entry, **kwargs)

    async def record_miss(self) -> None:
        if self.fail_get:
            raise OSError("Simulated PostgreSQL counter outage")
        await super().record_miss()


class _DeterministicProvider(LLMProvider):
    def __init__(self, reply: str = "```python\ndef f():\n    return 1\n```") -> None:
        self.reply = reply
        self.calls: int = 0

    @property
    def provider_name(self) -> str:
        return "mock"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls += 1
        return GenerationResult(
            text=self.reply,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=12, total_tokens=22),
            latency_ms=4.0,
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        self.calls += 1
        from app.core.contracts import StreamChunk

        yield StreamChunk(delta=self.reply, done=False, model=request.model)
        yield StreamChunk(
            delta="",
            done=True,
            model=request.model,
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=12, total_tokens=22),
            latency_ms=4.0,
        )

    async def close(self) -> None:
        return None


class TestStage37DeterministicRegressions(unittest.IsolatedAsyncioTestCase):
    """Deterministic CI regression checks for Stage 3.7 datasets, policy, and optimizations."""

    def test_stage37_datasets_schema_and_split_disjointness(self) -> None:
        with open(DATASET_DIR / "stage3_7_context_classification.json", "r", encoding="utf-8") as fh:
            ctx_ds = json.load(fh)
        with open(DATASET_DIR / "stage3_7_compatibility_heldout.json", "r", encoding="utf-8") as fh:
            compat_ds = json.load(fh)
        with open(DATASET_DIR / "stage3_7_workload.json", "r", encoding="utf-8") as fh:
            wl_ds = json.load(fh)

        dev_prompts = {
            m["content"].strip().lower()
            for item in ctx_ds["splits"]["development"]
            for m in item["messages"]
            if m["role"] == "user"
        }
        ho_prompts = {
            m["content"].strip().lower()
            for item in ctx_ds["splits"]["held_out"]
            for m in item["messages"]
            if m["role"] == "user"
        }
        self.assertGreaterEqual(len(ctx_ds["splits"]["development"]), 8)
        self.assertGreaterEqual(len(ctx_ds["splits"]["held_out"]), 24)
        self.assertEqual(dev_prompts.intersection(ho_prompts), set())
        self.assertGreaterEqual(len(compat_ds["scenarios"]), 26)
        self.assertGreaterEqual(sum(len(ph["operations"]) for ph in wl_ds["phases"]), 20)

    def test_safe_application_defaults_remain_disabled_and_strict(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Settings()
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertFalse(cfg.semantic_cache_serving_enabled)
        self.assertFalse(cfg.shadow_evaluation_enabled)
        self.assertEqual(cfg.semantic_cache_default_visibility, "private")
        self.assertEqual(cfg.semantic_cache_min_similarity, 0.92)
        self.assertEqual(cfg.semantic_cache_min_margin, 0.03)
        self.assertEqual(cfg.semantic_cache_top_k, 5)
        self.assertEqual(cfg.semantic_cache_max_retrieval_candidates, 25)

    def test_phase2_context_classification_dev_heldout_and_regressed_boundaries(self) -> None:
        with open(DATASET_DIR / "stage3_7_context_classification.json", "r", encoding="utf-8") as fh:
            ctx_ds = json.load(fh)
        report = run_phase2_context_evaluation(ctx_ds)
        self.assertEqual(report["development"]["accuracy"], 1.0, report["development"])
        self.assertEqual(report["held_out"]["accuracy"], 1.0, report["held_out"])
        self.assertEqual(report["fingerprint_checks"]["stability_passed"], 2)
        self.assertEqual(report["fingerprint_checks"]["invalidation_passed"], 4)

        # Direct regression: chitchat assistant filler must NOT satisfy back-reference cue
        chitchat_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[
                ChatMessage(role="user", content="Hi"),
                ChatMessage(role="assistant", content="Hello! Ready when you are."),
                ChatMessage(role="user", content="Fix the above code and add error handling"),
            ],
        )
        chitchat_ass = analyze_request_context(chitchat_req)
        self.assertEqual(chitchat_ass.context_class, ContextClass.CONTEXT_UNCERTAIN)
        self.assertEqual(chitchat_ass.contract.classification_reason, "FOLLOWUP_MISSING_OR_UNVERIFIABLE_HISTORY")

        # Direct regression: inline signature identifier reference across turns -> CONTEXT_DEPENDENT
        sig_req = GenerationRequest(
            model="m",
            provider="mock",
            messages=[
                ChatMessage(role="user", content="We have def verify_token(raw: str) -> bool:"),
                ChatMessage(role="user", content="Write pytest unit tests for verify_token handling expired tokens."),
            ],
        )
        sig_ass = analyze_request_context(sig_req)
        self.assertEqual(sig_ass.context_class, ContextClass.CONTEXT_DEPENDENT)
        self.assertIsNotNone(sig_ass.context_fingerprint)

    async def test_optimization1_exact_cache_read_fail_open_sync_and_stream(self) -> None:
        cache = _FailingReadCache(fail_get=True, fail_set=False)
        prov = _DeterministicProvider()
        core = IntelliCacheCore(cache=cache, cache_enabled=True)

        req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How do I reverse a Python list?")],
            namespace="team_beta",
            visibility="tenant_shared",
        )
        res = await core.execute_query(req, prov)
        self.assertFalse(res.cached)
        self.assertEqual(res.cache_type, "none")
        self.assertEqual(prov.calls, 1)

        req_stream = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How do I reverse a Python tuple?")],
            namespace="team_beta",
            visibility="tenant_shared",
            stream=True,
        )
        chunks = [c async for c in core.execute_stream(req_stream, prov)]
        self.assertTrue(any(c.done and not c.cached for c in chunks))
        self.assertEqual(prov.calls, 2)

    def test_optimization2_postgres_specializes_only_fixed_independent_constants(self) -> None:
        vs = PostgresVectorStore(
            pool=None,  # type: ignore[arg-type]
            default_namespace="default",
            default_model=FASTEMBED_MODEL,
            dimensions=FASTEMBED_DIM,
        )
        for vis, own, expected_base_len in (("tenant_shared", None, 4), ("private", "prin_a", 5)):
            single_q, combined_q, scope_params, fail_closed = vs._build_search_queries(
                visibility=vis,
                principal_id=own,
                allow_shared_reads=False,
                context_class="CONTEXT_INDEPENDENT",
                context_fingerprint="ctx-v1:independent",
            )
            self.assertFalse(fail_closed)
            self.assertIn("CONTEXT_INDEPENDENT", scope_params)
            self.assertIn("ctx-v1:independent", scope_params)
            base_params: List[Any] = ["[0.0]", "default", FASTEMBED_MODEL, 25, *scope_params]
            spec_sql, spec_params = vs._specialize_low_selectivity_context_sql(
                combined_q or single_q,
                base_params,
                "CONTEXT_INDEPENDENT",
                "ctx-v1:independent",
            )
            self.assertEqual(len(spec_params), expected_base_len)
            self.assertIn("->>'context_class') = 'CONTEXT_INDEPENDENT'", spec_sql)
            self.assertIn("->>'context_fingerprint') = 'ctx-v1:independent'", spec_sql)

            # CONTEXT_DEPENDENT must remain strictly bound via parameters (B-tree context index path)
            dep_fp = "ctx-v1:" + ("a" * 64)
            dep_single, dep_combined, dep_scope_params, _ = vs._build_search_queries(
                visibility=vis,
                principal_id=own,
                allow_shared_reads=False,
                context_class="CONTEXT_DEPENDENT",
                context_fingerprint=dep_fp,
            )
            dep_base_params: List[Any] = ["[0.0]", "default", FASTEMBED_MODEL, 25, *dep_scope_params]
            kept_sql, kept_params = vs._specialize_low_selectivity_context_sql(
                dep_combined or dep_single,
                dep_base_params,
                "CONTEXT_DEPENDENT",
                dep_fp,
            )
            self.assertEqual(kept_sql, dep_combined or dep_single)
            self.assertEqual(kept_params, dep_base_params)
            self.assertIn(dep_fp, kept_params)

    def test_optimization4_policy_precomputes_request_context_once_per_batch(self) -> None:
        auth = EvidenceAuthority("stage37-test-evidence-secret", allow_ephemeral=False)
        ext = CompatibilityExtractor()
        req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="How to sort a Python list?")],
            namespace="team_beta",
            visibility="tenant_shared",
        )
        req_meta = ext.extract(req, FASTEMBED_MODEL)
        pol = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model=FASTEMBED_MODEL,
            evidence_authority=auth,
        )
        candidates: List[VectorSearchResult] = []
        for idx in range(5):
            fp = f"fp_batch_{idx}"
            resp = f"```python\ndef sort_{idx}(xs):\n    return sorted(xs)\n```"
            c_req = GenerationRequest(
                model="mock-qwen-27b",
                provider="mock",
                messages=[ChatMessage(role="user", content=f"How do I sort a list #{idx} in JavaScript?")],
                namespace="team_beta",
                visibility="tenant_shared",
            )
            c_meta = ext.extract(c_req, FASTEMBED_MODEL)
            ev = auth.mint_response_verified_evidence(
                cache_namespace="team_beta",
                fingerprint=fp,
                response_text=resp,
                metadata=c_meta,
                verification=ResponseVerificationResult(
                    status=VerificationStatus.VERIFIED,
                    truncation_status=TruncationStatus.COMPLETE,
                    reason_code=CompatibilityReasonCode.COMPATIBLE,
                    message="ok",
                ),
                context_independent=True,
                visibility="tenant_shared",
            )
            full_md = SemanticCompatibilityMetadata(
                schema_version=c_meta.schema_version,
                extractor_version=c_meta.extractor_version,
                task_kind=c_meta.task_kind,
                dimensions=c_meta.dimensions,
                generation_contract=c_meta.generation_contract,
                embedding_contract=c_meta.embedding_contract,
                compatibility_evidence=ev,
                context_contract=c_meta.context_contract,
            ).to_dict()
            candidates.append(
                VectorSearchResult(
                    cache_namespace="team_beta",
                    fingerprint=fp,
                    query_text="js",
                    model=FASTEMBED_MODEL,
                    distance=0.04,
                    similarity=0.96,
                    cached_response=resp,
                    metadata=full_md,
                    visibility="tenant_shared",
                )
            )

        ctx_calls = 0
        orig_analyze = analyze_request_context

        def _spy_analyze(r: GenerationRequest) -> ContextAssessment:
            nonlocal ctx_calls
            ctx_calls += 1
            return orig_analyze(r)

        with patch("app.semantic_cache.policy.analyze_request_context", side_effect=_spy_analyze):
            winner, reason = pol.evaluate_candidates(candidates, req, request_metadata=req_meta)
        self.assertIsNone(winner)
        self.assertIsNotNone(reason)
        # Analyze should run once for the batch instead of 5 times (one per candidate)
        self.assertEqual(ctx_calls, 1)


def _should_run_live_fastembed_pg() -> bool:
    return (
        os.environ.get("RUN_FASTEMBED_PGVECTOR_TEST") == "1"
        or os.environ.get("RUN_FASTEMBED_SMOKE") == "1"
    )


@unittest.skipUnless(
    _should_run_live_fastembed_pg(),
    "Set RUN_FASTEMBED_PGVECTOR_TEST=1 to execute live PostgreSQL 16 + FastEmbed Stage 3.7 evaluation.",
)
class TestStage37LiveFastEmbedPostgresEvaluation(unittest.IsolatedAsyncioTestCase):
    """Live PostgreSQL 16 + pgvector + real FastEmbed (BAAI/bge-small-en-v1.5) Stage 3.7 suite."""

    async def test_stage37_end_to_end_evaluation_gates(self) -> None:
        report = await run_stage37_evaluation(
            db_url=os.environ.get("TEST_DATABASE_URL"),
            scalability_sizes=(100, 1000),
            perf_iterations=8,
            synthetic_delay_ms=40.0,
        )
        p1 = report["phase1_audit"]
        self.assertTrue(p1["application_defaults"]["defaults_safe_and_disabled"])
        self.assertTrue(p1["effective_stage37_config"]["semantic_cache_enabled"])
        self.assertTrue(p1["effective_stage37_config"]["sql_context_prefilter"])

        p2 = report["phase2_context_classification"]
        self.assertEqual(p2["development"]["accuracy"], 1.0)
        self.assertEqual(p2["held_out"]["accuracy"], 1.0)
        self.assertEqual(p2["fingerprint_checks"]["stability_passed"], 2)
        self.assertEqual(p2["fingerprint_checks"]["invalidation_passed"], 4)

        p3 = report["phase3_compatibility"]
        self.assertEqual(p3["scenarios_passed"], p3["scenarios_total"])
        self.assertEqual(p3["false_semantic_hits"], 0)
        self.assertEqual(p3["false_rejections"], 0)

        p4 = report["phase4_workload"]
        self.assertEqual(p4["operations_passed"], p4["operations_total"])
        self.assertGreaterEqual(p4["metrics_aggregate"]["exact_cache_hits"], 1)
        self.assertGreaterEqual(p4["metrics_aggregate"]["semantic_hits"], 5)
        self.assertGreaterEqual(p4["metrics_aggregate"]["cross_conversation_semantic_hits"], 4)
        self.assertGreaterEqual(p4["metrics_aggregate"]["privacy_violation_attempts"], 1)

        p5 = report["phase5_scalability"]
        scale_1k = p5["scales"][-1]
        self.assertEqual(scale_1k["unfiltered_context_dependent"]["recall_at_budget"], 0.0)
        self.assertEqual(scale_1k["prefiltered_context_dependent"]["recall_at_budget"], 1.0)
        self.assertTrue(scale_1k["prefiltered_context_dependent"]["explain"]["context_index_used"])
        self.assertEqual(scale_1k["prefiltered_context_independent"]["recall_at_budget"], 1.0)

        p6 = report["phase6_performance"]["modes"]
        self.assertEqual(p6["B_exact_plus_semantic"]["cache_hit_ratio"], 1.0)
        self.assertGreater(p6["B_exact_plus_semantic"]["cache_hit_ratio"], p6["A_exact_only"]["cache_hit_ratio"])
        self.assertEqual(p6["C_upstream_only"]["cache_hit_ratio"], 0.0)

        p7 = report["phase7_failure_recovery"]
        self.assertEqual(p7["scenarios_passed"], 13, p7["scenarios"])
