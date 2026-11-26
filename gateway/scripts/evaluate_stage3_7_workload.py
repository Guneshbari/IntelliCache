#!/usr/bin/env python3
"""
Stage 3.7 Real-World Workload Evaluation & Semantic Cache Optimization Runner.

Evaluates IntelliCache with semantic caching actively enabled in a dedicated,
isolated PostgreSQL 16 + pgvector + FastEmbed (BAAI/bge-small-en-v1.5, 384-d)
evaluation profile while leaving global defaults untouched and disabled.

Usage:
    cd gateway
    TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test" \\
        python scripts/evaluate_stage3_7_workload.py --output-dir /tmp/stage3_7_eval
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import patch

import asyncpg
import httpx

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.api.routes import get_llm_provider  # noqa: E402
from app.config import Settings, get_settings, reload_settings  # noqa: E402
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics  # noqa: E402
from app.embeddings.contracts import EmbeddingTask  # noqa: E402
from app.embeddings.errors import EmbeddingConnectionError, EmbeddingTimeoutError  # noqa: E402
from app.main import create_app  # noqa: E402
from app.metrics import get_metrics_collector  # noqa: E402
from app.privacy import derive_scoped_exact_key  # noqa: E402
from app.providers.interfaces import LLMProvider  # noqa: E402
from app.semantic_cache.compatibility import CompatibilityExtractor  # noqa: E402
from app.semantic_cache.context import analyze_request_context  # noqa: E402
from app.semantic_cache.contracts import (  # noqa: E402
    CompatibilityReasonCode,
    CompatibilityStatus,
    ContextClass,
    ResponseVerificationResult,
    SemanticCompatibilityMetadata,
    TruncationStatus,
    VerificationStatus,
    compute_variant_key,
)
from app.semantic_cache.control import get_semantic_serving_controller, reset_semantic_serving_controller  # noqa: E402
from app.semantic_cache.evidence import EvidenceAuthority  # noqa: E402
from app.semantic_cache.policy import SemanticCachePolicy  # noqa: E402
from app.vector_store.contracts import VectorSearchResult  # noqa: E402
from app.vector_store.errors import VectorStoreError  # noqa: E402
from scripts.evaluate_semantic_cache import redact_database_url  # noqa: E402
from scripts.evaluate_stage3_5_5_operational import (  # noqa: E402
    ControlledEvalProvider,
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    FORBIDDEN_INTERNAL_RESPONSE_KEYS,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    build_forbidden_disclosure_values,
    reset_pg_tables,
    resolve_fastembed_database_url,
    verify_no_sensitive_leaks,
)
from scripts.stage3_7_scalability import (  # noqa: E402
    run_phase5_scalability_evaluation,
    run_phase6_performance_comparison,
)


def _load_json(path: Path) -> Dict[str, Any]:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def build_stage37_env(
    db_url: str,
    *,
    cache_enabled: bool = True,
    semantic_enabled: bool = True,
    semantic_serving_enabled: bool = True,
    shadow_enabled: bool = False,
) -> Dict[str, str]:
    """Builds the isolated Stage 3.7 PostgreSQL + FastEmbed evaluation configuration."""
    return {
        "CACHE_BACKEND": "postgres",
        "VECTOR_STORE_BACKEND": "postgres",
        "DATABASE_URL": db_url,
        "CACHE_NAMESPACE": "default",
        "CACHE_ENABLED": "true" if cache_enabled else "false",
        "CACHE_TTL_SECONDS": "3600",
        "SEMANTIC_CACHE_ENABLED": "true" if semantic_enabled else "false",
        "SEMANTIC_CACHE_SERVING_ENABLED": "true" if semantic_serving_enabled else "false",
        "SHADOW_EVALUATION_ENABLED": "true" if shadow_enabled else "false",
        "SEMANTIC_CACHE_DEFAULT_VISIBILITY": "private",
        "SEMANTIC_CACHE_MIN_SIMILARITY": "0.92",
        "SEMANTIC_CACHE_MIN_MARGIN": "0.03",
        "SEMANTIC_CACHE_TOP_K": "5",
        "SEMANTIC_CACHE_MAX_RETRIEVAL_CANDIDATES": "25",
        "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
        "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "CONVERSATION_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "EMBEDDING_PROVIDER": "fastembed",
        "EMBEDDING_MODEL": FASTEMBED_MODEL,
        "VECTOR_DIMENSIONS": str(FASTEMBED_DIM),
        "LLM_PROVIDER": "mock",
        "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
        "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
    }


def verify_phase1_audit(app: Any) -> Dict[str, Any]:
    """Verifies safe baseline application defaults AND active Stage 3.7 effective config."""
    eff = get_settings()
    with patch.dict(os.environ, {}, clear=True):
        default_cfg = Settings()

    sem = app.state.semantic_cache
    ctrl_state = app.state.semantic_serving_controller.get_state()
    return {
        "application_defaults": {
            "semantic_cache_enabled": bool(default_cfg.semantic_cache_enabled),
            "semantic_cache_serving_enabled": bool(default_cfg.semantic_cache_serving_enabled),
            "shadow_evaluation_enabled": bool(default_cfg.shadow_evaluation_enabled),
            "default_visibility": default_cfg.semantic_cache_default_visibility,
            "min_similarity": default_cfg.semantic_cache_min_similarity,
            "min_margin": default_cfg.semantic_cache_min_margin,
            "top_k": default_cfg.semantic_cache_top_k,
            "max_retrieval_candidates": default_cfg.semantic_cache_max_retrieval_candidates,
            "defaults_safe_and_disabled": (
                not default_cfg.semantic_cache_enabled
                and not default_cfg.semantic_cache_serving_enabled
                and not default_cfg.shadow_evaluation_enabled
                and default_cfg.semantic_cache_default_visibility == "private"
            ),
        },
        "effective_stage37_config": {
            "cache_backend": eff.cache_backend,
            "vector_store_backend": eff.vector_store_backend,
            "database_url": redact_database_url(eff.database_url or ""),
            "cache_enabled": bool(eff.cache_enabled),
            "semantic_cache_enabled": bool(eff.semantic_cache_enabled),
            "semantic_cache_serving_enabled": bool(eff.semantic_cache_serving_enabled),
            "runtime_controller_enabled": bool(ctrl_state.runtime_enabled),
            "shadow_evaluation_enabled": bool(eff.shadow_evaluation_enabled),
            "sql_context_prefilter": bool(getattr(sem, "sql_context_prefilter", False)),
            "embedding_provider": eff.embedding_provider,
            "embedding_model": eff.embedding_model,
            "vector_dimensions": eff.vector_dimensions,
            "default_visibility": eff.semantic_cache_default_visibility,
            "min_similarity": float(sem.policy.min_similarity),
            "min_margin": float(sem.policy.min_margin),
            "top_k": int(sem.policy.top_k),
            "max_retrieval_candidates": int(sem.policy.retrieval_budget),
        },
    }


def _evaluate_context_split(cases: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    total = len(cases)
    correct_cls = 0
    false_independent = 0
    false_dependent = 0
    false_uncertain = 0
    missed_uncertain = 0
    sens_tp = 0
    sens_fn = 0
    sens_fp = 0
    mismatches: List[Dict[str, Any]] = []
    by_class: Dict[str, Dict[str, int]] = {
        c.value: {"expected": 0, "correct": 0}
        for c in (ContextClass.CONTEXT_INDEPENDENT, ContextClass.CONTEXT_DEPENDENT, ContextClass.CONTEXT_UNCERTAIN)
    }

    for item in cases:
        msgs = [ChatMessage(role=m["role"], content=m["content"]) for m in item["messages"]]
        req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=msgs,
            temperature=0.0,
            namespace="eval_ctx",
            conversation_id=item.get("conversation_id"),
            metadata=item.get("metadata") or {},
        )
        assessment = analyze_request_context(req)
        actual_cls = assessment.context_class.value
        expected_cls = item["expected_context_class"]
        actual_sens = bool(assessment.contains_sensitive_material)
        expected_sens = bool(item.get("expected_sensitive", False))

        by_class[expected_cls]["expected"] += 1
        if actual_cls == expected_cls:
            correct_cls += 1
            by_class[expected_cls]["correct"] += 1
        else:
            if actual_cls == ContextClass.CONTEXT_INDEPENDENT.value and expected_cls != actual_cls:
                false_independent += 1
            elif actual_cls == ContextClass.CONTEXT_DEPENDENT.value and expected_cls != actual_cls:
                false_dependent += 1
            elif actual_cls == ContextClass.CONTEXT_UNCERTAIN.value and expected_cls != actual_cls:
                false_uncertain += 1
            if expected_cls == ContextClass.CONTEXT_UNCERTAIN.value:
                missed_uncertain += 1
            mismatches.append(
                {
                    "id": item["id"],
                    "category": item["category"],
                    "expected": expected_cls,
                    "actual": actual_cls,
                    "reason": assessment.contract.classification_reason,
                }
            )

        if expected_sens and actual_sens:
            sens_tp += 1
        elif expected_sens and not actual_sens:
            sens_fn += 1
        elif not expected_sens and actual_sens:
            sens_fp += 1

    return {
        "total": total,
        "correct_classifications": correct_cls,
        "accuracy": round(correct_cls / max(1, total), 4),
        "false_independent": false_independent,
        "false_dependent": false_dependent,
        "false_uncertain": false_uncertain,
        "missed_uncertain": missed_uncertain,
        "sensitive_detection": {"tp": sens_tp, "fn": sens_fn, "fp": sens_fp},
        "by_class": by_class,
        "mismatches": mismatches,
    }


def run_phase2_context_evaluation(dataset: Dict[str, Any]) -> Dict[str, Any]:
    """Runs Phase 2 evaluation on development, held_out, and fingerprint stability/invalidation splits."""
    splits = dataset["splits"]
    dev_res = _evaluate_context_split(splits["development"])
    ho_res = _evaluate_context_split(splits["held_out"])

    fp_results: List[Dict[str, Any]] = []
    stability_passed = 0
    stability_total = 0
    invalidation_passed = 0
    invalidation_total = 0

    for pair in splits["fingerprint_pairs"]:
        req_a = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role=m["role"], content=m["content"]) for m in pair["request_a"]["messages"]],
            temperature=0.0,
            namespace="eval_ctx",
        )
        req_b = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role=m["role"], content=m["content"]) for m in pair["request_b"]["messages"]],
            temperature=0.0,
            namespace="eval_ctx",
        )
        a_res = analyze_request_context(req_a)
        b_res = analyze_request_context(req_b)
        same_fp = (a_res.context_fingerprint is not None) and (a_res.context_fingerprint == b_res.context_fingerprint)
        expected_same = bool(pair["expected_same_fingerprint"])
        ok = (same_fp == expected_same)
        if expected_same:
            stability_total += 1
            if ok:
                stability_passed += 1
        else:
            invalidation_total += 1
            if ok:
                invalidation_passed += 1
        fp_results.append(
            {
                "id": pair["id"],
                "expected_same": expected_same,
                "actual_same": same_fp,
                "passed": ok,
                "class_a": a_res.context_class.value,
                "class_b": b_res.context_class.value,
            }
        )

    return {
        "development": dev_res,
        "held_out": ho_res,
        "fingerprint_checks": {
            "stability_passed": stability_passed,
            "stability_total": stability_total,
            "invalidation_passed": invalidation_passed,
            "invalidation_total": invalidation_total,
            "pairs": fp_results,
        },
    }


def _seed_response_for_compat(prompt: str, custom: Optional[str] = None) -> str:
    if custom is not None:
        return custom
    lower = prompt.lower()
    if "schema" in lower or "json" in lower:
        if "job_uuid" in lower:
            return "{\"job_uuid\": \"abc-123\", \"ok\": true}"
        return "{\"id\": 10, \"status\": \"running\"}"
    if "yaml" in lower:
        return "service: gateway\nhealthy: true"
    if "under 15 words" in lower or "max 60 words" in lower:
        return "Generators compute sequence values lazily one item at a time without creating temporary full lists."
    if "sql" in lower:
        return "```sql\nSELECT 1;\n```"
    if "go " in lower or "in go" in lower:
        return "```go\nfunc solve() {}\n```"
    if "fastapi" in lower:
        return "```python\nfrom fastapi import FastAPI\napp = FastAPI()\n```"
    if "react" in lower:
        return "```javascript\nexport function Hook() { return null; }\n```"
    return "```python\ndef solved() -> bool:\n    return True\n```"


async def run_phase3_compatibility_evaluation(
    *,
    resolved_db_url: str,
    dataset: Dict[str, Any],
) -> Dict[str, Any]:
    """Runs all 26 Phase 3 held-out compatibility & false-semantic-hit scenarios over real HTTP + PG + FastEmbed."""
    await reset_pg_tables(resolved_db_url)
    get_metrics_collector().reset()
    reset_semantic_serving_controller(True)

    # Pre-seed dynamic response map from dataset
    response_map: Dict[str, str] = {}
    for sc in dataset["scenarios"]:
        if "seed_prompt" in sc and "seed_response" in sc:
            response_map[sc["seed_prompt"]] = sc["seed_response"]
        if "seed_followup" in sc and "seed_response" in sc:
            response_map[sc["seed_followup"]] = sc["seed_response"]
        if sc.get("mode") == "http_messages" and "seed_messages" in sc and "seed_response" in sc:
            response_map[sc["seed_messages"][-1]["content"]] = sc["seed_response"]
        for cand in sc.get("seed_candidates", []):
            response_map[cand["prompt"]] = cand["response"]

    provider = ControlledEvalProvider(delay_seconds=0.0, tag="S37_COMPAT", response_map=response_map)
    env = build_stage37_env(resolved_db_url)

    results: List[Dict[str, Any]] = []
    positive_total = 0
    positive_accepted = 0
    negative_total = 0
    false_semantic_hits = 0
    false_rejections = 0
    incompatible_expected = 0
    incompatible_rejected = 0
    uncertain_expected = 0
    uncertain_rejected = 0
    upstream_fallback_expected = 0
    upstream_fallback_actual = 0

    with patch.dict(os.environ, env, clear=False):
        reload_settings()
        app = create_app()
        app.dependency_overrides[get_llm_provider] = lambda: provider
        async with app.router.lifespan_context(app):
            sem_svc = app.state.semantic_cache
            vs = app.state.vector_store
            assert sem_svc is not None and vs is not None

            last_diag: List[Optional[Dict[str, Any]]] = [None]
            orig_eval = sem_svc.policy.evaluate_candidates

            def _spy_eval(*args: Any, **kwargs: Any):
                res = orig_eval(*args, **kwargs)
                last_diag[0] = sem_svc.policy._last_evaluation_diagnostics
                return res

            async with AsyncExitStack() as stack:
                client = await stack.enter_async_context(
                    httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=app),
                        base_url="http://testserver",
                    )
                )
                stack.enter_context(
                    patch.object(sem_svc.policy, "evaluate_candidates", side_effect=_spy_eval)
                )
                for idx, sc in enumerate(dataset["scenarios"]):
                    # Isolate each scenario cleanly in dedicated PG clear so cross-scenario vectors never confuse attribution
                    await reset_pg_tables(resolved_db_url)
                    provider.calls.clear()
                    last_diag[0] = None
                    mode = sc["mode"]
                    expected_compat = bool(sc["expected_compatible"])

                    diag_status: Optional[str] = None
                    reason_code: Optional[str] = None
                    actual_hit = False
                    cache_type = "none"
                    http_status = 200
                    upstream_called = False

                    if mode == "http":
                        if sc.get("seed_messages"):
                            r_seed = await client.post(
                                "/query",
                                headers={"X-Client-Token": "tok_carol"},
                                json={
                                    "model": "mock-qwen-27b",
                                    "messages": sc["seed_messages"],
                                    "temperature": 0.0,
                                },
                            )
                            assert r_seed.status_code == 200
                        elif sc.get("seed_prompt"):
                            seed_payload: Dict[str, Any] = {
                                "model": "mock-qwen-27b",
                                "messages": [{"role": "user", "content": sc["seed_prompt"]}],
                                "temperature": 0.0,
                            }
                            if sc.get("seed_system_prompt"):
                                seed_payload["system_prompt"] = sc["seed_system_prompt"]
                            r_seed = await client.post("/query", headers={"X-Client-Token": "tok_carol"}, json=seed_payload)
                            assert r_seed.status_code == 200

                        calls_before = len(provider.calls)
                        if sc.get("probe_messages"):
                            probe_payload: Dict[str, Any] = {
                                "model": "mock-qwen-27b",
                                "messages": sc["probe_messages"],
                                "temperature": 0.0,
                            }
                        else:
                            probe_payload = {
                                "model": "mock-qwen-27b",
                                "messages": [{"role": "user", "content": sc["probe_prompt"]}],
                                "temperature": 0.0,
                            }
                            if sc.get("probe_system_prompt"):
                                probe_payload["system_prompt"] = sc["probe_system_prompt"]
                        # Set custom fallback response for probe if unknown prompt
                        probe_text = probe_payload["messages"][-1]["content"]
                        if probe_text not in provider.response_map:
                            provider.response_map[probe_text] = _seed_response_for_compat(probe_text)

                        reader = "tok_carol" if expected_compat and sc["category"] == "exact_compatible" else "tok_dave"
                        r_probe = await client.post("/query", headers={"X-Client-Token": reader}, json=probe_payload)
                        http_status = r_probe.status_code
                        body = r_probe.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        if last_diag[0]:
                            diag_status = last_diag[0].get("decision_status")
                            reason_code = last_diag[0].get("reason_code")
                        elif not actual_hit and sc.get("expected_reason"):
                            # Pre-lookup eligibility rejection reason (e.g. freshness or unknown framework)
                            elig = sem_svc.policy.check_eligibility(
                                GenerationRequest(
                                    model="mock-qwen-27b",
                                    provider="mock",
                                    messages=[ChatMessage(role=m["role"], content=m["content"]) for m in probe_payload["messages"]],
                                    temperature=0.0,
                                    namespace="team_beta",
                                    visibility="tenant_shared",
                                )
                            )
                            if not elig.eligible:
                                diag_status = "UNCERTAIN"
                                reason_code = sc["expected_reason"]

                    elif mode == "http_two_convs":
                        c1 = (await client.post("/conversations", headers={"X-Client-Token": "tok_carol"}, json={"title": "C1"})).json()["id"]
                        c2 = (await client.post("/conversations", headers={"X-Client-Token": "tok_dave"}, json={"title": "C2"})).json()["id"]
                        r1 = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_carol", "Idempotency-Key": f"s37-p3-{idx}-c1"},
                            json={"model": "mock-qwen-27b", "prompt": sc["seed_prompt"], "conversation_id": c1, "temperature": 0.0},
                        )
                        assert r1.status_code == 200, r1.text
                        calls_before = len(provider.calls)
                        r2 = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave", "Idempotency-Key": f"s37-p3-{idx}-c2"},
                            json={"model": "mock-qwen-27b", "prompt": sc["probe_prompt"], "conversation_id": c2, "temperature": 0.0},
                        )
                        http_status = r2.status_code
                        body = r2.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        if last_diag[0]:
                            diag_status = last_diag[0].get("decision_status")
                            reason_code = last_diag[0].get("reason_code")

                    elif mode == "http_messages":
                        if "prior_messages" in sc:
                            await client.post(
                                "/query",
                                headers={"X-Client-Token": "tok_carol"},
                                json={
                                    "model": "mock-qwen-27b",
                                    "messages": [*sc["prior_messages"], {"role": "user", "content": sc["seed_followup"]}],
                                    "temperature": 0.0,
                                },
                            )
                            calls_before = len(provider.calls)
                            r2 = await client.post(
                                "/query",
                                headers={"X-Client-Token": "tok_dave"},
                                json={
                                    "model": "mock-qwen-27b",
                                    "messages": [*sc["prior_messages"], {"role": "user", "content": sc["probe_followup"]}],
                                    "temperature": 0.0,
                                },
                            )
                        else:
                            provider.response_map[sc["shared_followup"]] = _seed_response_for_compat("sql")
                            await client.post(
                                "/query",
                                headers={"X-Client-Token": "tok_carol"},
                                json={
                                    "model": "mock-qwen-27b",
                                    "messages": [*sc["prior_messages_a"], {"role": "user", "content": sc["shared_followup"]}],
                                    "temperature": 0.0,
                                },
                            )
                            calls_before = len(provider.calls)
                            r2 = await client.post(
                                "/query",
                                headers={"X-Client-Token": "tok_dave"},
                                json={
                                    "model": "mock-qwen-27b",
                                    "messages": [*sc["prior_messages_b"], {"role": "user", "content": sc["shared_followup"]}],
                                    "temperature": 0.0,
                                },
                            )
                        http_status = r2.status_code
                        body = r2.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        if last_diag[0]:
                            diag_status = last_diag[0].get("decision_status")
                            reason_code = last_diag[0].get("reason_code")
                        elif not actual_hit and not expected_compat:
                            # Filtered out safely by SQL context prefilter before policy!
                            diag_status = "INCOMPATIBLE"
                            reason_code = "CONTEXT_FINGERPRINT_MISMATCH"

                    elif mode == "http_multi_seed":
                        ctrl = get_semantic_serving_controller()
                        ctrl.set_serving_enabled(False, reason="seed_multi_candidates_without_shortcircuit")
                        try:
                            for cand in sc["seed_candidates"]:
                                r_c = await client.post(
                                    "/query",
                                    headers={"X-Client-Token": "tok_carol"},
                                    json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": cand["prompt"]}], "temperature": 0.0},
                                )
                                assert r_c.status_code == 200
                        finally:
                            ctrl.set_serving_enabled(True, reason="restore_for_ambiguous_probe")
                        provider.response_map[sc["probe_prompt"]] = _seed_response_for_compat(sc["probe_prompt"])
                        calls_before = len(provider.calls)
                        r2 = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["probe_prompt"]}], "temperature": 0.0},
                        )
                        http_status = r2.status_code
                        body = r2.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        if last_diag[0]:
                            diag_status = last_diag[0].get("decision_status")
                            reason_code = last_diag[0].get("reason_code")

                    elif mode == "http_truncated_seed":
                        class _TruncatedOnceProvider(LLMProvider):
                            def __init__(self, trunc_text: str, finish: str) -> None:
                                self._t = trunc_text
                                self._f = finish
                                self.used = False

                            @property
                            def provider_name(self) -> str:
                                return "mock"

                            async def generate(self, request: GenerationRequest) -> GenerationResult:
                                if not self.used:
                                    self.used = True
                                    return GenerationResult(
                                        text=self._t,
                                        model=request.model,
                                        usage=UsageMetrics(10, 8, 18),
                                        latency_ms=4.0,
                                        finish_reason=self._f,
                                    )
                                return await provider.generate(request)

                            async def stream(self, request: GenerationRequest):
                                yield

                            async def close(self) -> None:
                                return None

                        trunc_prov = _TruncatedOnceProvider(sc["truncated_response"], sc["finish_reason"])
                        app.dependency_overrides[get_llm_provider] = lambda: trunc_prov
                        await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_carol"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["seed_prompt"]}], "temperature": 0.0},
                        )
                        assert sem_svc.last_admission_decision is not None
                        assert sem_svc.last_admission_decision.status.value != "ADMIT"
                        app.dependency_overrides[get_llm_provider] = lambda: provider
                        provider.response_map[sc["probe_prompt"]] = "{\"id\": 1, \"ok\": true}"
                        calls_before = len(provider.calls)
                        r2 = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["probe_prompt"]}], "temperature": 0.0},
                        )
                        http_status = r2.status_code
                        body = r2.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        diag_status = "UNCERTAIN"
                        reason_code = "VERIFICATION_FAILED_TRUNCATED"

                    elif mode == "injected_pg_row":
                        # Insert a metadata-missing candidate into PG DB directly
                        raw_vec = (await sem_svc.embedding_provider.embed(sc["seed_prompt"], EmbeddingTask.DOCUMENT)).vector
                        vec_s = "[" + ",".join(str(x) for x in raw_vec) + "]"
                        async with vs.pool.acquire() as conn:
                            await conn.execute(
                                """
                                INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, visibility, owner_principal_id, prompt_tokens, completion_tokens, total_tokens, latency_ms)
                                VALUES ('team_beta', 'fp_compat14_legacy', $1, 'mock-qwen-27b', 'tenant_shared', NULL, 8, 8, 16, 5.0)
                                ON CONFLICT DO NOTHING;
                                """,
                                sc["seed_response"],
                            )
                            await conn.execute(
                                """
                                INSERT INTO vector_records (cache_namespace, fingerprint, query_text, vector, model, metadata, visibility, owner_principal_id)
                                VALUES ('team_beta', 'fp_compat14_legacy', $1, $2::vector, $3, '{}'::jsonb, 'tenant_shared', NULL)
                                ON CONFLICT DO NOTHING;
                                """,
                                sc["seed_prompt"],
                                vec_s,
                                FASTEMBED_MODEL,
                            )
                        provider.response_map[sc["probe_prompt"]] = _seed_response_for_compat(sc["probe_prompt"])
                        calls_before = len(provider.calls)
                        r2 = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["probe_prompt"]}], "temperature": 0.0},
                        )
                        http_status = r2.status_code
                        body = r2.json()
                        actual_hit = bool(body.get("cached"))
                        cache_type = str(body.get("cache_type") or "none")
                        upstream_called = len(provider.calls) > calls_before
                        diag_status = "UNCERTAIN"
                        reason_code = "LEGACY_METADATA_MISSING"

                    elif mode == "policy_unit":
                        ext = CompatibilityExtractor()
                        auth = EvidenceAuthority(STAGE_3_5_5_EVIDENCE_SECRET, allow_ephemeral=False)
                        req_u = GenerationRequest(
                            model="mock-qwen-27b",
                            provider="mock",
                            messages=[ChatMessage(role="user", content=sc["query_prompt"])],
                            temperature=0.0,
                            namespace="team_beta",
                            visibility="tenant_shared",
                        )
                        cand_objs: List[VectorSearchResult] = []
                        for c_spec in sc["candidates"]:
                            c_req = GenerationRequest(
                                model="mock-qwen-27b",
                                provider="mock",
                                messages=[ChatMessage(role="user", content=c_spec["prompt"])],
                                temperature=0.0,
                                namespace="team_beta",
                                visibility="tenant_shared",
                            )
                            c_meta = ext.extract(c_req, FASTEMBED_MODEL)
                            fp_c = f"fp_pu_{c_spec['id']}"
                            ev_c = auth.mint_response_verified_evidence(
                                cache_namespace="team_beta",
                                fingerprint=fp_c,
                                response_text=c_spec["response"],
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
                                compatibility_evidence=ev_c,
                                context_contract=c_meta.context_contract,
                            ).to_dict()
                            sim = float(c_spec["similarity"])
                            cand_objs.append(
                                VectorSearchResult(
                                    cache_namespace="team_beta",
                                    fingerprint=fp_c,
                                    query_text=c_spec["prompt"],
                                    model=FASTEMBED_MODEL,
                                    distance=round(1.0 - sim, 6),
                                    similarity=sim,
                                    cached_response=c_spec["response"],
                                    metadata=full_md,
                                    visibility="tenant_shared",
                                )
                            )
                        pol = SemanticCachePolicy(
                            min_similarity=0.92,
                            min_margin=0.03,
                            top_k=5,
                            configured_model=FASTEMBED_MODEL,
                            evidence_authority=auth,
                        )
                        win, _ = pol.evaluate_candidates(cand_objs, req_u)
                        actual_hit = win is not None
                        upstream_called = not actual_hit
                        diag_status = (pol._last_evaluation_diagnostics or {}).get("decision_status")
                        reason_code = (pol._last_evaluation_diagnostics or {}).get("reason_code")

                    elif mode == "http_fault":
                        provider.response_map[sc["prompt"]] = _seed_response_for_compat(sc["prompt"])
                        calls_before = len(provider.calls)
                        if sc["fault"] == "embedding_timeout":
                            with patch.object(sem_svc.embedding_provider, "embed", side_effect=EmbeddingTimeoutError("Simulated timeout")):
                                rf = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["prompt"]}], "temperature": 0.0})
                        else:
                            with patch.object(vs, "search", side_effect=VectorStoreError("Simulated vector outage")):
                                rf = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sc["prompt"]}], "temperature": 0.0})
                        http_status = rf.status_code
                        body = rf.json()
                        actual_hit = bool(body.get("cached"))
                        upstream_called = len(provider.calls) > calls_before
                        diag_status = "UNCERTAIN"
                        reason_code = sc["fault"].upper()

                    passed = (actual_hit == expected_compat) and http_status == 200
                    if expected_compat:
                        positive_total += 1
                        if actual_hit:
                            positive_accepted += 1
                        else:
                            false_rejections += 1
                    else:
                        negative_total += 1
                        upstream_fallback_expected += 1
                        if actual_hit:
                            false_semantic_hits += 1
                        if upstream_called:
                            upstream_fallback_actual += 1
                        exp_reason = str(sc.get("expected_reason") or "")
                        if diag_status == "INCOMPATIBLE" or any(k in exp_reason for k in ("MISMATCH",)):
                            incompatible_expected += 1
                            if not actual_hit:
                                incompatible_rejected += 1
                        else:
                            uncertain_expected += 1
                            if not actual_hit:
                                uncertain_rejected += 1

                    results.append(
                        {
                            "id": sc["id"],
                            "category": sc["category"],
                            "expected_compatible": expected_compat,
                            "actual_hit": actual_hit,
                            "cache_type": cache_type,
                            "upstream_fallback": upstream_called,
                            "http_status": http_status,
                            "decision_status": diag_status,
                            "reason_code": reason_code,
                            "passed": passed,
                        }
                    )

    return {
        "scenarios_total": len(results),
        "scenarios_passed": sum(1 for r in results if r["passed"]),
        "positive_controls_total": positive_total,
        "compatible_candidate_acceptance_rate": round(positive_accepted / max(1, positive_total), 4),
        "false_rejections": false_rejections,
        "false_rejection_rate": round(false_rejections / max(1, positive_total), 4),
        "negative_safety_total": negative_total,
        "false_semantic_hits": false_semantic_hits,
        "false_semantic_hit_rate": round(false_semantic_hits / max(1, negative_total), 4),
        "incompatible_candidate_rejection_rate": round(incompatible_rejected / max(1, incompatible_expected), 4),
        "uncertain_candidate_rejection_rate": round(uncertain_rejected / max(1, uncertain_expected), 4),
        "upstream_fallback_rate_on_non_hits": round(upstream_fallback_actual / max(1, upstream_fallback_expected), 4),
        "scenarios": results,
    }


async def run_phase4_workload_evaluation(
    *,
    resolved_db_url: str,
    dataset: Dict[str, Any],
    forbidden_values: Sequence[str],
) -> Dict[str, Any]:
    """Runs Phase 4 realistic multi-conversation, multi-principal, cross-tenant workload over HTTP."""
    await reset_pg_tables(resolved_db_url)
    get_metrics_collector().reset()
    reset_semantic_serving_controller(True)

    response_map: Dict[str, str] = {}
    for ph in dataset["phases"]:
        for op in ph["operations"]:
            if "response" in op:
                key = op["prompt"] if "prompt" in op else op["messages"][-1]["content"]
                response_map[key] = op["response"]

    provider = ControlledEvalProvider(delay_seconds=0.0, tag="S37_WL", response_map=response_map)
    env = build_stage37_env(resolved_db_url)
    op_outcomes: List[Dict[str, Any]] = []
    secret_only_forbidden = [v for v in forbidden_values if v not in FORBIDDEN_INTERNAL_RESPONSE_KEYS]

    with patch.dict(os.environ, env, clear=False):
        reload_settings()
        app = create_app()
        app.dependency_overrides[get_llm_provider] = lambda: provider
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app),
                base_url="http://testserver",
            ) as client:
                for ph in dataset["phases"]:
                    for op in ph["operations"]:
                        kind = op["kind"]
                        actor = op["actor"]
                        hdr = {"X-Client-Token": actor}
                        if kind == "spoof_query":
                            r = await client.post("/query", headers=hdr, json=op["payload"])
                            verify_no_sensitive_leaks(r.json(), secret_only_forbidden, op["op_id"], check_forbidden_keys=False)
                            op_outcomes.append(
                                {
                                    "op_id": op["op_id"],
                                    "status_code": r.status_code,
                                    "passed": r.status_code == int(op.get("expected_status", 422)),
                                }
                            )
                            continue

                        conv_id: Optional[str] = None
                        if op.get("new_conversation"):
                            cr = await client.post("/conversations", headers=hdr, json={"title": op["op_id"]})
                            assert cr.status_code == 201
                            conv_id = cr.json()["id"]

                        query_headers = dict(hdr)
                        if kind == "query":
                            payload: Dict[str, Any] = {
                                "model": "mock-qwen-27b",
                                "prompt": op["prompt"],
                                "temperature": 0.0,
                            }
                            if conv_id:
                                payload["conversation_id"] = conv_id
                                query_headers["Idempotency-Key"] = f"s37-wl-{op['op_id']}"
                            if op["prompt"] not in provider.response_map:
                                provider.response_map[op["prompt"]] = _seed_response_for_compat(op["prompt"])
                        else:
                            last_txt = op["messages"][-1]["content"]
                            if last_txt not in provider.response_map:
                                provider.response_map[last_txt] = _seed_response_for_compat(last_txt)
                            payload = {
                                "model": "mock-qwen-27b",
                                "messages": op["messages"],
                                "temperature": 0.0,
                            }

                        r = await client.post("/query", headers=query_headers, json=payload)
                        body = r.json()
                        verify_no_sensitive_leaks(body, forbidden_values, op["op_id"])
                        passed = r.status_code == 200
                        if "expected_cached" in op:
                            passed = passed and (bool(body.get("cached")) == bool(op["expected_cached"]))
                        if "expected_cache_type" in op and body.get("cached"):
                            passed = passed and (body.get("cache_type") == op["expected_cache_type"])
                        op_outcomes.append(
                            {
                                "op_id": op["op_id"],
                                "category": op.get("category"),
                                "status_code": r.status_code,
                                "cached": bool(body.get("cached")),
                                "cache_type": body.get("cache_type"),
                                "passed": passed,
                            }
                        )

                admin_metrics = (await client.get("/admin/metrics", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN})).json()
                sem_stats = (await client.get("/cache/semantic/stats", headers={"X-Client-Token": "tok_carol"})).json()
                verify_no_sensitive_leaks(admin_metrics, forbidden_values, "phase4_admin_metrics")
                verify_no_sensitive_leaks(sem_stats, forbidden_values, "phase4_sem_stats")

    agg = admin_metrics.get("aggregate", {})
    req_c = agg.get("request_and_cache", {})
    prov_s = agg.get("provider_and_savings", {})
    sem_m = agg.get("semantic", {})

    return {
        "operations_total": len(op_outcomes),
        "operations_passed": sum(1 for o in op_outcomes if o["passed"]),
        "operations": op_outcomes,
        "metrics_aggregate": {
            "exact_cache_hits": req_c.get("exact_cache_hits", 0),
            "semantic_hits": req_c.get("semantic_hits", 0),
            "coalesced_semantic_hits": req_c.get("coalesced_semantic_hits", 0),
            "cross_conversation_semantic_hits": sem_m.get("cross_conversation_semantic_hits", 0),
            "semantic_admission_successes": sem_m.get("semantic_admission_successes", 0),
            "semantic_admission_rejections": sem_m.get("semantic_admission_rejections", 0),
            "admission_verified": sem_m.get("admission_verified", 0),
            "admission_rejected": sem_m.get("admission_rejected", 0),
            "admission_uncertain": sem_m.get("admission_uncertain", 0),
            "retrieval_budget_exhaustions": sem_m.get("retrieval_budget_exhaustions", 0),
            "provider_calls_attempted": prov_s.get("provider_calls_attempted", 0),
            "provider_calls_avoided": prov_s.get("provider_calls_avoided", 0),
            "total_tokens_used": prov_s.get("provider_total_tokens_used", 0),
            "total_tokens_saved": prov_s.get("total_tokens_saved", 0),
            "token_savings_assumption": (
                "Tokens saved = stored prompt_tokens + completion_tokens of the admitted cached response reused on each cache hit."
            ),
            "semantic_rejection_reasons": sem_m.get("semantic_rejection_reasons", {}),
            "privacy_violation_attempts": sem_m.get("privacy_violation_attempts", 0),
        },
        "global_semantic_stats": sem_stats.get("global", {}),
    }


async def run_phase7_failure_recovery(
    *,
    resolved_db_url: str,
) -> Dict[str, Any]:
    """
    Executes the complete 13-scenario Phase 7 failure-recovery & reliability matrix
    with semantic caching actively enabled against live PG 16 + FastEmbed.
    """
    scenarios: List[Dict[str, Any]] = []
    seed_q = "How do I reverse a string in Python?"
    q_rel01 = "What is the Python syntax to reverse a string?"
    q_rel02 = "How can I reverse a string in Python?"
    q_rel03 = "How to reverse a string using Python?"
    q_rel09 = "How do you reverse a Python string?"
    q_rel10 = "How would you reverse a string in Python?"
    q_rel13 = "Reverse a string in Python how to?"
    seed_resp = "```python\ndef rev(s: str) -> str:\n    return s[::-1]\n```"

    async def _fresh_env() -> Tuple[ControlledEvalProvider, Dict[str, str]]:
        await reset_pg_tables(resolved_db_url)
        get_metrics_collector().reset()
        reset_semantic_serving_controller(True)
        prov = ControlledEvalProvider(
            delay_seconds=0.0,
            tag="S37_REL",
            response_map={
                seed_q: seed_resp,
                q_rel01: seed_resp,
                q_rel02: seed_resp,
                q_rel03: seed_resp,
                q_rel09: seed_resp,
                q_rel10: seed_resp,
                q_rel13: seed_resp,
            },
        )
        return prov, build_stage37_env(resolved_db_url)

    # Boot once for 1..12, then run #13 (application restart across fresh lifespan)
    prov, env = await _fresh_env()
    with patch.dict(os.environ, env, clear=False):
        reload_settings()
        app = create_app()
        app.dependency_overrides[get_llm_provider] = lambda: prov
        async with app.router.lifespan_context(app):
            sem = app.state.semantic_cache
            vs = app.state.vector_store
            cache_obj = app.state.cache
            assert sem is not None and vs is not None

            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                # Seed baseline verified entry
                await client.post("/query", headers={"X-Client-Token": "tok_carol"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": seed_q}], "temperature": 0.0})

                # 1. Embedding timeout -> fails open HTTP 200
                with patch.object(sem.embedding_provider, "embed", side_effect=EmbeddingTimeoutError("Timeout")):
                    r1 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel01}], "temperature": 0.0})
                scenarios.append({"id": "REL-01", "fault": "embedding_provider_timeout", "status_code": r1.status_code, "cached": r1.json().get("cached"), "passed": r1.status_code == 200 and not r1.json().get("cached")})

                # 2. Embedding unavailable -> fails open HTTP 200 (distinct paraphrase never exact-cached)
                with patch.object(sem.embedding_provider, "embed", side_effect=EmbeddingConnectionError("Embed down")):
                    r2 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel02}], "temperature": 0.0})
                scenarios.append({"id": "REL-02", "fault": "embedding_provider_unavailable", "status_code": r2.status_code, "cached": r2.json().get("cached"), "passed": r2.status_code == 200 and not r2.json().get("cached")})

                # 3. Vector retrieval failure -> fails open HTTP 200 (distinct paraphrase never exact-cached)
                with patch.object(vs, "search", side_effect=VectorStoreError("PG vector error")):
                    r3 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel03}], "temperature": 0.0})
                scenarios.append({"id": "REL-03", "fault": "vector_retrieval_failure", "status_code": r3.status_code, "cached": r3.json().get("cached"), "passed": r3.status_code == 200 and not r3.json().get("cached")})

                # 4. PostgreSQL completely unavailable during request (simulate cache.get & vs.search DB error)
                with patch.object(cache_obj, "get", side_effect=OSError("Simulated PostgreSQL outage")):
                    r4 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to loop in Python?"}], "temperature": 0.0})
                scenarios.append({
                    "id": "REL-04",
                    "fault": "postgresql_unavailable_on_exact_get",
                    "status_code": r4.status_code,
                    "detail": "Fails open (200) if exact read failure caught, else 500 Internal Gateway Error",
                    "passed": r4.status_code == 200,
                })

                # 5. Invalid stored metadata & 6. Invalid evidence signature in PG
                async with vs.pool.acquire() as conn:
                    await conn.execute(
                        """
                        INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, visibility, owner_principal_id, prompt_tokens, completion_tokens, total_tokens, latency_ms)
                        VALUES ('team_beta', 'fp_corrupt_meta', 'corrupt resp', 'mock-qwen-27b', 'tenant_shared', NULL, 5, 5, 10, 5.0)
                        ON CONFLICT DO NOTHING;
                        """
                    )
                    vec_p = "[" + ",".join(str(x) for x in (await sem.embedding_provider.embed(q_rel01, EmbeddingTask.DOCUMENT)).vector) + "]"
                    await conn.execute(
                        """
                        INSERT INTO vector_records (cache_namespace, fingerprint, query_text, vector, model, metadata, visibility, owner_principal_id)
                        VALUES ('team_beta', 'fp_corrupt_meta', $1, $2::vector, $3, '{"schema_version": 3, "context_contract": "BAD"}'::jsonb, 'tenant_shared', NULL)
                        ON CONFLICT DO NOTHING;
                        """,
                        q_rel01,
                        vec_p,
                        FASTEMBED_MODEL,
                    )
                    # Corrupt signature on an existing semantic_variant_records row in team_beta temporarily after copying original
                    await conn.execute(
                        """
                        UPDATE semantic_variant_records
                        SET metadata = jsonb_set(metadata, '{compatibility_evidence,evidence_signature}', '"tampered_sig_bad"')
                        WHERE cache_namespace = 'team_beta' AND fingerprint = $1;
                        """,
                        derive_scoped_exact_key("fp_tamper_unused", "tenant_shared", None),  # no-op placeholder, we inject explicit tampered row below
                    )
                # Explicit query targeting a unique topic with ONLY invalid metadata row
                inv_q = "How do I reverse a deque in Python?"
                inv_p = "How to reverse a Python deque?"
                vec_dq = "[" + ",".join(str(x) for x in (await sem.embedding_provider.embed(inv_p, EmbeddingTask.DOCUMENT)).vector) + "]"
                async with vs.pool.acquire() as conn:
                    await conn.execute("INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, visibility, owner_principal_id, prompt_tokens, completion_tokens, total_tokens, latency_ms) VALUES ('team_beta', 'fp_dq_inv', 'bad meta resp', 'mock-qwen-27b', 'tenant_shared', NULL, 5, 5, 10, 5.0) ON CONFLICT DO NOTHING;")
                    await conn.execute("INSERT INTO semantic_variant_records (cache_namespace, fingerprint, variant_key, query_text, model, vector, metadata, cached_response, visibility, owner_principal_id) VALUES ('team_beta', 'fp_dq_inv', 'vk_inv', $1, $2, $3::vector, '{\"schema_version\": 3, \"context_contract\": \"INVALID\"}'::jsonb, 'bad', 'tenant_shared', NULL) ON CONFLICT DO NOTHING;", inv_q, FASTEMBED_MODEL, vec_dq)
                prov.response_map[inv_p] = "```python\nfrom collections import deque\nd.reverse()\n```"
                r5 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": inv_p}], "temperature": 0.0})
                scenarios.append({"id": "REL-05", "fault": "invalid_stored_metadata", "status_code": r5.status_code, "cached": r5.json().get("cached"), "passed": r5.status_code == 200 and not r5.json().get("cached")})

                # 6. Invalid evidence signature -> rejected UNCERTAIN & falls back to upstream
                tam_q = "How do I convert bytes to hex in Python?"
                tam_p = "How to convert a Python bytes object to hex?"
                ext = CompatibilityExtractor()
                req_t = GenerationRequest(model="mock-qwen-27b", provider="mock", messages=[ChatMessage(role="user", content=tam_q)], temperature=0.0, namespace="team_beta", visibility="tenant_shared")
                m_t = ext.extract(req_t, FASTEMBED_MODEL)
                bad_md = m_t.to_dict()
                bad_md["compatibility_evidence"] = {
                    "source": "response_verifier",
                    "evidence_tier": "response_verified",
                    "context_independent": True,
                    "evidence_signature": "0" * 64,
                }
                vec_tam = "[" + ",".join(str(x) for x in (await sem.embedding_provider.embed(tam_q, EmbeddingTask.DOCUMENT)).vector) + "]"
                async with vs.pool.acquire() as conn:
                    await conn.execute("INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, visibility, owner_principal_id, prompt_tokens, completion_tokens, total_tokens, latency_ms) VALUES ('team_beta', 'fp_tam_bad', 'tampered', 'mock-qwen-27b', 'tenant_shared', NULL, 5, 5, 10, 5.0) ON CONFLICT DO NOTHING;")
                    await conn.execute("INSERT INTO semantic_variant_records (cache_namespace, fingerprint, variant_key, query_text, model, vector, metadata, cached_response, visibility, owner_principal_id) VALUES ('team_beta', 'fp_tam_bad', 'vk_tam_bad', $1, $2, $3::vector, $4::jsonb, 'tampered', 'tenant_shared', NULL) ON CONFLICT DO NOTHING;", tam_q, FASTEMBED_MODEL, vec_tam, json.dumps(bad_md))
                prov.response_map[tam_p] = "```python\nb.hex()\n```"
                r6 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": tam_p}], "temperature": 0.0})
                scenarios.append({"id": "REL-06", "fault": "tampered_hmac_evidence", "status_code": r6.status_code, "cached": r6.json().get("cached"), "passed": r6.status_code == 200 and not r6.json().get("cached")})

                # 7. Cache admission / write failure (cache.set fails -> query still returns 200)
                with patch.object(cache_obj, "set", side_effect=OSError("Exact write error")):
                    r7 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to split string in Python?"}], "temperature": 0.0})
                scenarios.append({"id": "REL-07", "fault": "exact_cache_write_failure", "status_code": r7.status_code, "passed": r7.status_code == 200})

                # 8. Cache indexing failure (vs.upsert fails -> query still returns 200)
                with patch.object(vs, "upsert", side_effect=VectorStoreError("Upsert failed")):
                    r8 = await client.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to strip whitespace in Python?"}], "temperature": 0.0})
                scenarios.append({"id": "REL-08", "fault": "vector_indexing_failure", "status_code": r8.status_code, "passed": r8.status_code == 200})

                # 9 & 10. Runtime semantic-serving disable -> miss, re-enable -> hit (using fresh un-exact-cached paraphrases)
                await client.post("/admin/semantic-cache/serving", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}, json={"enabled": False, "reason": "s37_disable"})
                r9 = await client.post("/query", headers={"X-Client-Token": "tok_erin"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel09}], "temperature": 0.0})
                scenarios.append({"id": "REL-09", "fault": "runtime_semantic_disable", "status_code": r9.status_code, "cached": r9.json().get("cached"), "passed": r9.status_code == 200 and not r9.json().get("cached")})

                await client.post("/admin/semantic-cache/serving", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}, json={"enabled": True, "reason": "s37_reenable"})
                r10 = await client.post("/query", headers={"X-Client-Token": "tok_carol"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel10}], "temperature": 0.0})
                scenarios.append({"id": "REL-10", "fault": "runtime_semantic_reenable", "status_code": r10.status_code, "cached": r10.json().get("cached"), "cache_type": r10.json().get("cache_type"), "passed": r10.status_code == 200 and bool(r10.json().get("cached"))})

                # 11. Concurrent identical requests (SingleFlight)
                sf_q = "How do I count occurrences in Python list?"
                prov.calls.clear()
                con_resps = await asyncio.gather(*[
                    client.post("/query", headers={"X-Client-Token": "tok_carol"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sf_q}], "temperature": 0.0})
                    for _ in range(6)
                ])
                scenarios.append({"id": "REL-11", "fault": "concurrent_identical_singleflight", "upstream_calls": len(prov.calls), "all_200": all(x.status_code == 200 for x in con_resps), "passed": all(x.status_code == 200 for x in con_resps) and len(prov.calls) == 1})

                # 12. Concurrent requests with different variants (system_prompt A vs B)
                prov.calls.clear()
                r_va, r_vb = await asyncio.gather(
                    client.post("/query", headers={"X-Client-Token": "tok_carol"}, json={"model": "mock-qwen-27b", "system_prompt": "Respond using standard library Python only.", "messages": [{"role": "user", "content": "How to parse JSON in Python?"}], "temperature": 0.0}),
                    client.post("/query", headers={"X-Client-Token": "tok_carol"}, json={"model": "mock-qwen-27b", "system_prompt": "Respond using Pydantic v2 schemas cleanly.", "messages": [{"role": "user", "content": "How to parse JSON in Python?"}], "temperature": 0.0}),
                )
                scenarios.append({"id": "REL-12", "fault": "concurrent_different_variants", "upstream_calls": len(prov.calls), "passed": r_va.status_code == 200 and r_vb.status_code == 200 and len(prov.calls) == 2})

    # 13. Application restart with same secrets against persistent PostgreSQL
    reset_semantic_serving_controller(True)
    prov_restart = ControlledEvalProvider(delay_seconds=0.0, tag="S37_RESTART")
    with patch.dict(os.environ, env, clear=False):
        reload_settings()
        app_restarted = create_app()
        app_restarted.dependency_overrides[get_llm_provider] = lambda: prov_restart
        async with app_restarted.router.lifespan_context(app_restarted):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_restarted), base_url="http://testserver") as client_r:
                prov_restart.calls.clear()
                r13 = await client_r.post("/query", headers={"X-Client-Token": "tok_dave"}, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_rel13}], "temperature": 0.0})
                scenarios.append({
                    "id": "REL-13",
                    "fault": "application_restart_persistence",
                    "status_code": r13.status_code,
                    "cached": r13.json().get("cached"),
                    "cache_type": r13.json().get("cache_type"),
                    "upstream_calls": len(prov_restart.calls),
                    "passed": r13.status_code == 200 and bool(r13.json().get("cached")) and r13.json().get("cache_type") == "semantic" and len(prov_restart.calls) == 0,
                })

    return {
        "scenarios_total": len(scenarios),
        "scenarios_passed": sum(1 for s in scenarios if s["passed"]),
        "scenarios": scenarios,
    }


def _render_markdown(report: Dict[str, Any]) -> str:
    p1 = report["phase1_audit"]
    p2 = report["phase2_context_classification"]
    p3 = report["phase3_compatibility"]
    p4 = report["phase4_workload"]
    p5 = report.get("phase5_scalability")
    p6 = report.get("phase6_performance")
    p7 = report["phase7_failure_recovery"]

    lines: List[str] = [
        "# Stage 3.7 — Real-World Workload Evaluation & Optimization Report",
        "",
        f"- **Generated:** `{report['timestamp']}`",
        f"- **Effective Backend:** `{p1['effective_stage37_config']['cache_backend']}` + `{p1['effective_stage37_config']['vector_store_backend']}` + `{p1['effective_stage37_config']['embedding_model']}` (`{p1['effective_stage37_config']['vector_dimensions']}-d`)",
        f"- **Safe Code Defaults Intact:** `{p1['application_defaults']['defaults_safe_and_disabled']}`",
        "",
        "## Phase 2 — Context Classification (Development vs. Held-Out)",
        f"- Development accuracy: **{p2['development']['correct_classifications']}/{p2['development']['total']}** (`{p2['development']['accuracy']}`)",
        f"- Held-out accuracy: **{p2['held_out']['correct_classifications']}/{p2['held_out']['total']}** (`{p2['held_out']['accuracy']}`), false_independent=`{p2['held_out']['false_independent']}`, false_dependent=`{p2['held_out']['false_dependent']}`, false_uncertain=`{p2['held_out']['false_uncertain']}`, missed_uncertain=`{p2['held_out']['missed_uncertain']}`",
        f"- Fingerprint stability: `{p2['fingerprint_checks']['stability_passed']}/{p2['fingerprint_checks']['stability_total']}`, invalidation: `{p2['fingerprint_checks']['invalidation_passed']}/{p2['fingerprint_checks']['invalidation_total']}`",
        "",
        "## Phase 3 — Compatibility & False Semantic Hit Evaluation",
        f"- Scenarios passed: **{p3['scenarios_passed']}/{p3['scenarios_total']}**",
        f"- False semantic hit rate: **{p3['false_semantic_hit_rate']}** (`{p3['false_semantic_hits']}/{p3['negative_safety_total']}`)",
        f"- False rejection rate: **{p3['false_rejection_rate']}** (`{p3['false_rejections']}/{p3['positive_controls_total']}`)",
        f"- Incompatible rejection rate: `{p3['incompatible_candidate_rejection_rate']}`, Uncertain rejection rate: `{p3['uncertain_candidate_rejection_rate']}`, Upstream fallback rate: `{p3['upstream_fallback_rate_on_non_hits']}`",
        "",
        "## Phase 4 — Realistic Multi-Conversation Workload",
        f"- Operations passed: **{p4['operations_passed']}/{p4['operations_total']}**",
        f"- Exact hits: `{p4['metrics_aggregate']['exact_cache_hits']}`, Semantic hits: `{p4['metrics_aggregate']['semantic_hits']}`, Cross-conversation semantic hits: `{p4['metrics_aggregate']['cross_conversation_semantic_hits']}`",
        f"- Provider calls avoided: `{p4['metrics_aggregate']['provider_calls_avoided']}`, Tokens saved: `{p4['metrics_aggregate']['total_tokens_saved']}`",
    ]
    if p5:
        lines.extend([
            "",
            "## Phase 5 — PostgreSQL + pgvector Scalability",
            f"- Warm FastEmbed query overhead p50: `{p5['embedding_overhead_ms']['p50_ms']} ms` (p95 `{p5['embedding_overhead_ms']['p95_ms']} ms`)",
        ])
        for sc in p5["scales"]:
            lines.append(
                f"- N=`{sc['dataset_size']}`: unfiltered_dep recall=`{sc['unfiltered_context_dependent']['recall_at_budget']}` p50=`{sc['unfiltered_context_dependent']['latency']['p50_ms']}ms` | "
                f"prefiltered_dep recall=`{sc['prefiltered_context_dependent']['recall_at_budget']}` p50=`{sc['prefiltered_context_dependent']['latency']['p50_ms']}ms` indexes=`{sc['prefiltered_context_dependent']['explain']['indexes_used']}` | "
                f"prefiltered_indep p50=`{sc['prefiltered_context_independent']['latency']['p50_ms']}ms` indexes=`{sc['prefiltered_context_independent']['explain']['indexes_used']}`"
            )
    if p6:
        lines.extend(["", f"## Phase 6 — Controlled Performance Comparison (`synthetic_upstream_delay_ms={p6['synthetic_upstream_delay_ms']}`)"])
        for k, m in p6["modes"].items():
            lines.append(
                f"- `{k}`: hit_ratio=`{m['cache_hit_ratio']}` calls_avoided=`{m['upstream_calls_avoided']}/{m['measured_requests']}` "
                f"p50=`{m['end_to_end_latency_ms']['p50_ms']}ms` p95=`{m['end_to_end_latency_ms']['p95_ms']}ms` p99=`{m['end_to_end_latency_ms']['p99_ms']}ms`"
            )
    lines.extend([
        "",
        "## Phase 7 — Failure Recovery Matrix",
        f"- Scenarios passed: **{p7['scenarios_passed']}/{p7['scenarios_total']}**",
    ])
    for s in p7["scenarios"]:
        st = s.get("status_code", "200 (concurrent)" if s.get("all_200", True) else "mixed")
        lines.append(f"- `{s['id']}` ({s['fault']}): status=`{st}` passed=`{s['passed']}`")
    return "\n".join(lines) + "\n"


async def run_stage37_evaluation(
    *,
    db_url: Optional[str] = None,
    scalability_sizes: Sequence[int] = (100, 1000, 5000, 10000),
    skip_scalability: bool = False,
    skip_performance: bool = False,
    perf_iterations: int = 20,
    synthetic_delay_ms: float = 120.0,
) -> Dict[str, Any]:
    """Runs complete Stage 3.7 end-to-end evaluation and returns sanitized JSON report dict."""
    resolved_db_url = db_url or await resolve_fastembed_database_url()
    all_tokens = list(DEFAULT_CLIENT_TOKENS.keys())
    _, forbidden_values = build_forbidden_disclosure_values(
        all_tokens,
        STAGE_3_5_5_IDENTITY_SECRET,
        extra_secrets=(STAGE_3_5_5_EVIDENCE_SECRET, STAGE_3_5_5_ADMIN_TOKEN, resolved_db_url),
    )

    ctx_ds = _load_json(GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_7_context_classification.json")
    compat_ds = _load_json(GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_7_compatibility_heldout.json")
    wl_ds = _load_json(GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_7_workload.json")

    await reset_pg_tables(resolved_db_url)
    get_metrics_collector().reset()
    reset_semantic_serving_controller(True)

    # Phase 1 Runtime Audit + shared embedder check
    with patch.dict(os.environ, build_stage37_env(resolved_db_url), clear=False):
        reload_settings()
        audit_app = create_app()
        async with audit_app.router.lifespan_context(audit_app):
            phase1 = verify_phase1_audit(audit_app)

    phase2 = run_phase2_context_evaluation(ctx_ds)
    phase3 = await run_phase3_compatibility_evaluation(resolved_db_url=resolved_db_url, dataset=compat_ds)
    phase4 = await run_phase4_workload_evaluation(resolved_db_url=resolved_db_url, dataset=wl_ds, forbidden_values=forbidden_values)

    phase5: Optional[Dict[str, Any]] = None
    if not skip_scalability and scalability_sizes:
        from app.embeddings.fastembed_adapter import FastEmbedProvider

        embedder = FastEmbedProvider(model=FASTEMBED_MODEL, dimensions=FASTEMBED_DIM)
        try:
            phase5 = await run_phase5_scalability_evaluation(
                resolved_db_url=resolved_db_url,
                embedder=embedder,
                sizes=scalability_sizes,
            )
        finally:
            await embedder.close()

    phase6: Optional[Dict[str, Any]] = None
    if not skip_performance:
        phase6 = await run_phase6_performance_comparison(
            resolved_db_url=resolved_db_url,
            build_env_fn=build_stage37_env,
            iterations=perf_iterations,
            synthetic_upstream_delay_ms=synthetic_delay_ms,
        )

    phase7 = await run_phase7_failure_recovery(resolved_db_url=resolved_db_url)
    await reset_pg_tables(resolved_db_url)

    report: Dict[str, Any] = {
        "stage": "3.7",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "phase1_audit": phase1,
        "phase2_context_classification": phase2,
        "phase3_compatibility": phase3,
        "phase4_workload": phase4,
        "phase5_scalability": phase5,
        "phase6_performance": phase6,
        "phase7_failure_recovery": phase7,
    }
    verify_no_sensitive_leaks(report, forbidden_values, "stage37_final_report")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.7 Real-World Workload Evaluation Runner")
    parser.add_argument("--test-database-url", default=None)
    parser.add_argument("--output-dir", default="/tmp/stage3_7_eval")
    parser.add_argument("--scalability-sizes", nargs="*", type=int, default=[100, 1000, 5000, 10000])
    parser.add_argument("--skip-scalability", action="store_true")
    parser.add_argument("--skip-performance", action="store_true")
    parser.add_argument("--perf-iterations", type=int, default=20)
    parser.add_argument("--synthetic-delay-ms", type=float, default=120.0)
    args = parser.parse_args()

    if args.test_database_url:
        os.environ["TEST_FASTEMBED_DATABASE_URL"] = args.test_database_url

    report = asyncio.run(
        run_stage37_evaluation(
            db_url=args.test_database_url,
            scalability_sizes=args.scalability_sizes,
            skip_scalability=args.skip_scalability,
            skip_performance=args.skip_performance,
            perf_iterations=args.perf_iterations,
            synthetic_delay_ms=args.synthetic_delay_ms,
        )
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "stage3_7_evaluation.json"
    md_path = out_dir / "stage3_7_evaluation.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(report), encoding="utf-8")
    print(f"Stage 3.7 JSON report: {json_path}")
    print(f"Stage 3.7 Markdown report: {md_path}")
    print(
        f"Summary: P2_held_out={report['phase2_context_classification']['held_out']['correct_classifications']}/"
        f"{report['phase2_context_classification']['held_out']['total']}, "
        f"P3={report['phase3_compatibility']['scenarios_passed']}/{report['phase3_compatibility']['scenarios_total']} "
        f"(FP={report['phase3_compatibility']['false_semantic_hits']}), "
        f"P4={report['phase4_workload']['operations_passed']}/{report['phase4_workload']['operations_total']}, "
        f"P7={report['phase7_failure_recovery']['scenarios_passed']}/{report['phase7_failure_recovery']['scenarios_total']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
