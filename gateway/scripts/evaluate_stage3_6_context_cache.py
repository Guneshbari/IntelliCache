#!/usr/bin/env python3
"""
Stage 3.6 Context-Aware Centralized Semantic Cache Evaluation & Benchmark Runner.

Executes end-to-end HTTP, FastEmbed (384-d), and PostgreSQL + pgvector verification
across all 24 mandatory scenarios (CTX-001 through CTX-024), all 21 mandatory
observability metrics, PostgreSQL vector context index / EXPLAIN ANALYZE plans,
and multi-conversation concurrency benchmarks.

Usage:
    cd gateway
    TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test" \\
        python scripts/evaluate_stage3_6_context_cache.py --output-dir /tmp/stage3_6_eval
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
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

from app.api.routes import (  # noqa: E402
    get_llm_provider,
    get_query_cache,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from app.config import reload_settings  # noqa: E402
from app.conversations.identity import derive_principal_id  # noqa: E402
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics  # noqa: E402
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask  # noqa: E402
from app.embeddings.errors import EmbeddingTimeoutError  # noqa: E402
from app.embeddings.interfaces import EmbeddingProvider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.metrics import get_metrics_collector  # noqa: E402
from app.privacy import derive_scoped_exact_key  # noqa: E402
from app.providers.interfaces import LLMProvider  # noqa: E402
from app.semantic_cache.compatibility import CompatibilityExtractor  # noqa: E402
from app.semantic_cache.contracts import (  # noqa: E402
    CompatibilityReasonCode,
    ContextClass,
    SemanticCompatibilityMetadata,
    compute_variant_key,
)
from app.semantic_cache.control import reset_semantic_serving_controller  # noqa: E402
from app.semantic_cache.verification import ResponseArtifactVerifier  # noqa: E402
from app.vector_store.contracts import VectorRecord  # noqa: E402
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
)

MANDATORY_21_METRICS: Tuple[str, ...] = (
    "context_independent_requests",
    "context_dependent_requests",
    "context_uncertain_requests",
    "context_compatibility_matches",
    "context_compatibility_rejections",
    "context_metadata_missing",
    "context_fingerprint_mismatches",
    "cross_conversation_semantic_hits",
    "cross_conversation_semantic_misses",
    "private_scope_rejections",
    "tenant_shared_authorization_rejections",
    "semantic_admission_successes",
    "semantic_admission_rejections",
    "semantic_hits",
    "coalesced_semantic_hits",
    "provider_calls_avoided",
    "semantic_fallbacks",
    "retrieval_budget_exhaustions",
    "semantic_lookup_latency_ms",
    "context_validation_latency_ms",
    "compatibility_validation_latency_ms",
)


class Stage36TopicEmbedder(EmbeddingProvider):
    """
    Deterministic multi-topic 384-d unit-vector embedder for fast offline Stage 3.6 CI unit tests.
    Real FastEmbed BAAI/bge-small-en-v1.5 is used when ``use_real_fastembed=True``.
    """

    def __init__(self, *, dimensions: int = FASTEMBED_DIM, model_name: str = FASTEMBED_MODEL) -> None:
        self._dim = dimensions
        self._model = model_name
        self.embed_calls = 0

    @property
    def provider_name(self) -> str:
        return "stage36_deterministic_topic"

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def expected_dimensions(self) -> int:
        return self._dim

    def _vector_for_prompt(self, text: str) -> List[float]:
        lower = text.lower()
        topic_dims = (
            ("reverse a string", 0),
            ("reverse", 0),
            ("sort a list", 1),
            ("sort a python list", 1),
            ("merge two dict", 2),
            ("refactor the previous implementation", 3),
            ("empty lists", 4),
            ("parse json", 5),
            ("parse a json", 5),
            ("active records", 6),
            ("timeout and retry", 7),
            ("health check route", 8),
            ("file exists", 9),
            ("read a file", 10),
            ("read lines from a file", 10),
            ("format a user object", 11),
            ("log invalid inputs", 12),
            ("virtual environment", 13),
            ("decorator execution order", 14),
            ("required keys: id, status", 15),
            ("flatten a nested list", 16),
            ("flatten nested lists", 16),
            ("format a datetime", 17),
            ("format a python datetime", 17),
            ("pagination helper", 18),
            ("aggregate active payroll rows", 19),
            ("custom_calc_z9", 20),
            ("check if a key is in a python dict", 21),
            ("list length", 22),
            ("palindrome string", 23),
            ("count unique items", 24),
            ("custom_tamper_probe_alpha", 25),
        )
        vec = [0.0] * self._dim
        for marker, idx in topic_dims:
            if marker in lower:
                vec[idx % self._dim] = 1.0
                return vec
        stable_hash = sum((i + 1) * ord(ch) for i, ch in enumerate(lower))
        fallback_idx = 40 + (stable_hash % max(1, self._dim - 50))
        vec[fallback_idx] = 1.0
        return vec

    async def embed(
        self,
        text: str,
        task: EmbeddingTask = EmbeddingTask.QUERY,
        model: Optional[str] = None,
    ) -> EmbeddingResult:
        self.embed_calls += 1
        return EmbeddingResult(
            vector=self._vector_for_prompt(text),
            model=model or self._model,
            dimensions=self._dim,
        )

    async def embed_batch(
        self,
        texts: List[str],
        task: EmbeddingTask = EmbeddingTask.DOCUMENT,
        model: Optional[str] = None,
    ) -> List[EmbeddingResult]:
        return [await self.embed(t, task=task, model=model) for t in texts]

    async def close(self) -> None:
        return None


class ScriptedStage36Provider(ControlledEvalProvider):
    """Deterministic upstream mock provider generating format-valid answers per Stage 3.6 prompt."""

    def __init__(self, *, delay_seconds: float = 0.0, tag: str = "S36_UPSTREAM") -> None:
        super().__init__(delay_seconds=delay_seconds, tag=tag)
        self.next_finish_reason: Optional[str] = None
        self.next_response_override: Optional[str] = None

    def _build_answer(self, prompt: str) -> str:
        lower = prompt.lower()
        if "timeout and retry" in lower:
            return f"```python\n# {self.tag} config recommendation\nTIMEOUT = 5\nMAX_RETRIES = 2\n```"
        if "active records" in lower or "sql query" in lower:
            return f"```sql\n-- {self.tag}\nSELECT id, email FROM users WHERE is_active = TRUE;\n```"
        if "decorator execution order" in lower:
            return "In Python, outer decorators wrap inner decorators bottom-up and execute top-down at call time."
        if "datetime" in lower:
            return f"```python\n# {self.tag} datetime\nfrom datetime import datetime\ndef fmt(dt: datetime) -> str:\n    return dt.isoformat()\n```"
        if "flatten" in lower:
            return f"```python\n# {self.tag} flatten\ndef flatten(items):\n    return [x for sub in items for x in sub]\n```"
        if "plain text" in lower:
            return f"Plain helper output for user formatting ({self.tag})."
        if "json" in lower and "parse" not in lower:
            return json.dumps({"id": 1, "status": "ok", "tag": self.tag})
        return super()._build_answer(prompt)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        res = await super().generate(request)
        if self.next_response_override is not None:
            res.text = self.next_response_override
            self.next_response_override = None
        if self.next_finish_reason is not None:
            res.finish_reason = self.next_finish_reason
            self.next_finish_reason = None
        return res


def load_stage36_dataset(path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads the Stage 3.6 evaluation dataset."""
    target = path or (GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_6_context_dataset.json")
    with open(target, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


def _hdr(token: str, extra: Optional[Dict[str, str]] = None) -> Dict[str, str]:
    headers = {"X-Client-Token": token}
    if extra:
        headers.update(extra)
    return headers


def _verify_no_disclosures(payload: Any, forbidden_values: Sequence[str], context_label: str) -> None:
    serialized = json.dumps(payload, default=str) if not isinstance(payload, str) else payload
    for secret in forbidden_values:
        _assert(secret not in serialized, f"Disclosure violation in {context_label}: sensitive secret exposed")
    if isinstance(payload, dict):
        for key in FORBIDDEN_INTERNAL_RESPONSE_KEYS:
            _assert(key not in payload, f"Forbidden internal key '{key}' exposed in {context_label}")


@dataclass
class ScenarioResult:
    """Recorded execution outcome for a single CTX-* evaluation scenario."""

    id: str
    passed: bool
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)


async def _run_pg_context_index_benchmark(
    app: Any,
    embedder: EmbeddingProvider,
    resolved_db_url: str,
    *,
    client: Optional[httpx.AsyncClient] = None,
) -> Dict[str, Any]:
    """
    Executes Phase 3.6.6 PostgreSQL vector context index checks, EXPLAIN ANALYZE plans,
    mixed-workload starvation comparison (unfiltered budget=25 vs SQL context prefilter),
    and HTTP-level starvation immunity through POST /query with sql_context_prefilter=True.
    """
    vs = app.state.vector_store
    cache = get_query_cache()
    pool = vs.pool
    model_name = app.state.semantic_cache.policy.configured_model
    ext = CompatibilityExtractor()
    verifier = ResponseArtifactVerifier()
    ns = "team_gamma" if client is not None else "bench_ctx_perf"

    # Seed 30 wrong-context rows (ctx-v1:noise_i) sharing prompt wording + 1 target row at lower similarity
    target_ctx = [
        ChatMessage(role="user", content="We use CREATE TABLE ledger_entries (entry_id BIGINT PRIMARY KEY, posted_at TIMESTAMPTZ);"),
        ChatMessage(role="user", content="Add a structured pagination helper for the previous implementation"),
    ]
    target_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=target_ctx,
        temperature=0.0,
        namespace=ns,
        visibility="tenant_shared",
    )
    target_meta = ext.extract(target_req, model_name)
    target_ver = verifier.verify(
        response_text="```sql\nSELECT entry_id, posted_at FROM ledger_entries ORDER BY posted_at DESC LIMIT 25;\n```",
        request_metadata=target_meta,
        finish_reason="stop",
    )
    base_target_vec = (await embedder.embed("Add a structured pagination helper for the previous implementation", EmbeddingTask.DOCUMENT)).vector

    async with pool.acquire() as conn:
        await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1;", ns)
        await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1;", ns)
        await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1;", ns)

    from app.cache.interfaces import CacheEntry
    authority = app.state.semantic_cache.evidence_authority

    for i in range(30):
        noise_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[
                ChatMessage(role="user", content=f"We use CREATE TABLE shard_{i} (shard_{i}_id INT PRIMARY KEY, state TEXT);"),
                ChatMessage(role="user", content="Add a structured pagination helper for the previous implementation"),
            ],
            temperature=0.0,
            namespace=ns,
            visibility="tenant_shared",
        )
        noise_meta = ext.extract(noise_req, model_name)
        noise_resp = f"```sql\nSELECT shard_{i}_id FROM shard_{i} LIMIT 10;\n```"
        noise_ver = verifier.verify(response_text=noise_resp, request_metadata=noise_meta, finish_reason="stop")
        fp = f"bench_noise_{i:03d}"
        ev = authority.mint_response_verified_evidence(
            cache_namespace=ns,
            fingerprint=fp,
            response_text=noise_resp,
            metadata=noise_meta,
            verification=noise_ver,
            visibility="tenant_shared",
        )
        meta_obj = SemanticCompatibilityMetadata(
            schema_version=noise_meta.schema_version,
            extractor_version=noise_meta.extractor_version,
            task_kind=noise_meta.task_kind,
            dimensions=noise_meta.dimensions,
            generation_contract=noise_meta.generation_contract,
            embedding_contract=noise_meta.embedding_contract,
            compatibility_evidence=ev,
            context_contract=noise_meta.context_contract,
        )
        md = meta_obj.to_dict()
        vk = compute_variant_key(noise_meta, visibility="tenant_shared")
        md["variant_key"] = vk
        await cache.set(
            fp,
            CacheEntry(response=noise_resp, model="mock-qwen-27b", usage=UsageMetrics(10, 12, 22), latency_ms=25.0, visibility="tenant_shared"),
            namespace=ns,
        )
        # Keep closer angle so noise ranks ahead of target without context prefiltering
        await vs.upsert(
            VectorRecord(
                cache_namespace=ns,
                fingerprint=fp,
                query_text="Add a structured pagination helper for the previous implementation",
                vector=base_target_vec,
                model=model_name,
                metadata=md,
                variant_key=vk,
                response_text=noise_resp,
                visibility="tenant_shared",
            )
        )

    # Seed target with slight perturbation so it ranks 31st in unfiltered vector search
    target_vec = list(base_target_vec)
    target_vec[0] = target_vec[0] * 0.992
    target_vec[2] = math.sqrt(max(0.0, 1.0 - sum(v * v for v in target_vec)))
    t_resp = "```sql\nSELECT entry_id, posted_at FROM ledger_entries ORDER BY posted_at DESC LIMIT 25;\n```"
    t_fp = "bench_target_000"
    t_ev = authority.mint_response_verified_evidence(
        cache_namespace=ns,
        fingerprint=t_fp,
        response_text=t_resp,
        metadata=target_meta,
        verification=target_ver,
        visibility="tenant_shared",
    )
    t_meta_obj = SemanticCompatibilityMetadata(
        schema_version=target_meta.schema_version,
        extractor_version=target_meta.extractor_version,
        task_kind=target_meta.task_kind,
        dimensions=target_meta.dimensions,
        generation_contract=target_meta.generation_contract,
        embedding_contract=target_meta.embedding_contract,
        compatibility_evidence=t_ev,
        context_contract=target_meta.context_contract,
    )
    t_md = t_meta_obj.to_dict()
    t_vk = compute_variant_key(target_meta, visibility="tenant_shared")
    t_md["variant_key"] = t_vk
    await cache.set(
        t_fp,
        CacheEntry(response=t_resp, model="mock-qwen-27b", usage=UsageMetrics(10, 12, 22), latency_ms=25.0, visibility="tenant_shared"),
        namespace=ns,
    )
    await vs.upsert(
        VectorRecord(
            cache_namespace=ns,
            fingerprint=t_fp,
            query_text="Add a structured pagination helper for the previous implementation",
            vector=target_vec,
            model=model_name,
            metadata=t_md,
            variant_key=t_vk,
            response_text=t_resp,
            visibility="tenant_shared",
        )
    )

    unfiltered_t0 = time.perf_counter()
    unfiltered_res = await vs.search(
        base_target_vec,
        top_k=25,
        namespace=ns,
        model=model_name,
        visibility="tenant_shared",
    )
    unfiltered_ms = (time.perf_counter() - unfiltered_t0) * 1000.0
    unfiltered_found_target = any(r.fingerprint == t_fp for r in unfiltered_res)

    prefiltered_t0 = time.perf_counter()
    prefiltered_res = await vs.search(
        base_target_vec,
        top_k=25,
        namespace=ns,
        model=model_name,
        visibility="tenant_shared",
        context_class=target_meta.context_contract.context_class.value,
        context_fingerprint=target_meta.context_contract.context_fingerprint,
    )
    prefiltered_ms = (time.perf_counter() - prefiltered_t0) * 1000.0
    prefiltered_found_target = any(r.fingerprint == t_fp for r in prefiltered_res)

    _assert(not unfiltered_found_target, "Expected unfiltered top-25 over 31 rows to suffer rank-31 starvation")
    _assert(prefiltered_found_target and len(prefiltered_res) == 1, "Expected SQL context prefilter to recover rank-31 match")

    http_starvation_report: Optional[Dict[str, Any]] = None
    if client is not None:
        _assert(
            bool(getattr(app.state.semantic_cache, "sql_context_prefilter", False)) is True,
            "Expected app.state.semantic_cache.sql_context_prefilter=True for PostgreSQL app",
        )
        captured_search_calls: List[Dict[str, Any]] = []
        orig_vs_search = vs.search

        async def _spy_search(*args: Any, **kwargs: Any):
            captured_search_calls.append(
                {
                    "context_class": kwargs.get("context_class"),
                    "context_fingerprint": kwargs.get("context_fingerprint"),
                    "visibility": kwargs.get("visibility"),
                    "top_k": kwargs.get("top_k"),
                }
            )
            return await orig_vs_search(*args, **kwargs)

        with patch.object(vs, "search", side_effect=_spy_search):
            http_resp = await client.post(
                "/query",
                headers=_hdr("tok_frank"),
                json={
                    "model": "mock-qwen-27b",
                    "messages": [
                        {"role": "user", "content": "We use CREATE TABLE ledger_entries (entry_id BIGINT PRIMARY KEY, posted_at TIMESTAMPTZ);"},
                        {"role": "user", "content": "Add a structured pagination helper for the previous implementation"},
                    ],
                    "temperature": 0.0,
                },
            )
        _assert(http_resp.status_code == 200, f"HTTP starvation check failed: {http_resp.text}")
        http_body = http_resp.json()
        _assert(
            http_body["cached"] is True
            and http_body["cache_type"] == "semantic"
            and "ledger_entries" in http_body["response"],
            f"HTTP-level POST /query did not recover rank-31 target via SQL prefilter: {http_body}",
        )
        _assert(
            len(captured_search_calls) == 1
            and captured_search_calls[0]["context_class"] == ContextClass.CONTEXT_DEPENDENT.value
            and captured_search_calls[0]["context_fingerprint"] == target_meta.context_contract.context_fingerprint,
            f"Expected app lookup to pass SQL context prefilter args: {captured_search_calls}",
        )
        http_starvation_report = {
            "http_semantic_hit": True,
            "cache_type": http_body["cache_type"],
            "app_sql_context_prefilter": True,
            "search_call_context_class": captured_search_calls[0]["context_class"],
            "search_call_has_fingerprint": bool(captured_search_calls[0]["context_fingerprint"]),
        }

    # Inspect PostgreSQL indexes and EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
    q_vec_str = f"[{','.join(str(float(x)) for x in base_target_vec)}]"
    async with pool.acquire() as conn:
        pg_idx_rows = await conn.fetch(
            """
            SELECT tablename, indexname
            FROM pg_indexes
            WHERE schemaname = 'public'
              AND tablename IN ('vector_records', 'semantic_variant_records')
            ORDER BY tablename, indexname;
            """
        )
        index_names = [f"{r['tablename']}.{r['indexname']}" for r in pg_idx_rows]
        explain_unfiltered = json.loads(
            await conn.fetchval(
                """
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT v.fingerprint, (v.vector <=> $1::vector) AS distance
                FROM vector_records v
                JOIN cache_entries c ON v.cache_namespace = c.cache_namespace AND v.fingerprint = c.fingerprint
                WHERE v.cache_namespace = $2 AND v.model = $3 AND v.visibility = 'tenant_shared'
                ORDER BY distance ASC
                LIMIT 25;
                """,
                q_vec_str,
                ns,
                model_name,
            )
        )
        explain_context_default = json.loads(
            await conn.fetchval(
                """
                EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                SELECT sv.fingerprint, (sv.vector <=> $1::vector) AS distance
                FROM semantic_variant_records sv
                JOIN cache_entries c ON sv.cache_namespace = c.cache_namespace AND sv.fingerprint = c.fingerprint
                WHERE sv.cache_namespace = $2
                  AND sv.model = $3
                  AND sv.visibility = 'tenant_shared'
                  AND (sv.metadata->'context_contract'->>'context_class') = $4
                  AND (sv.metadata->'context_contract'->>'context_fingerprint') = $5
                ORDER BY distance ASC
                LIMIT 25;
                """,
                q_vec_str,
                ns,
                model_name,
                target_meta.context_contract.context_class.value,
                target_meta.context_contract.context_fingerprint,
            )
        )
        async with conn.transaction():
            await conn.execute("SET LOCAL enable_seqscan = off;")
            explain_context_idx = json.loads(
                await conn.fetchval(
                    """
                    EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)
                    SELECT sv.fingerprint, (sv.vector <=> $1::vector) AS distance
                    FROM semantic_variant_records sv
                    JOIN cache_entries c ON sv.cache_namespace = c.cache_namespace AND sv.fingerprint = c.fingerprint
                    WHERE sv.cache_namespace = $2
                      AND sv.model = $3
                      AND sv.visibility = 'tenant_shared'
                      AND (sv.metadata->'context_contract'->>'context_class') = $4
                      AND (sv.metadata->'context_contract'->>'context_fingerprint') = $5
                    ORDER BY distance ASC
                    LIMIT 25;
                    """,
                    q_vec_str,
                    ns,
                    model_name,
                    target_meta.context_contract.context_class.value,
                    target_meta.context_contract.context_fingerprint,
                )
            )

    def _collect_nodes(plan_node: Dict[str, Any]) -> List[str]:
        nodes = [str(plan_node.get("Node Type") or "")]
        if plan_node.get("Index Name"):
            nodes[-1] += f"({plan_node['Index Name']})"
        for sub in plan_node.get("Plans", []):
            nodes.extend(_collect_nodes(sub))
        return nodes

    return {
        "database_url_redacted": redact_database_url(resolved_db_url),
        "app_sql_context_prefilter_enabled": bool(getattr(app.state.semantic_cache, "sql_context_prefilter", False)),
        "indexes_present": index_names,
        "required_context_indexes_present": all(
            any(req_idx in name for name in index_names)
            for req_idx in (
                "idx_vector_records_context_scope",
                "idx_vector_records_private_context_scope",
                "idx_semantic_variant_records_context_scope",
                "idx_semantic_variant_records_private_context_scope",
            )
        ),
        "starvation_check": {
            "total_seeded_rows": 31,
            "unfiltered_budget_25_returned": len(unfiltered_res),
            "unfiltered_found_target": unfiltered_found_target,
            "unfiltered_latency_ms": round(unfiltered_ms, 3),
            "sql_prefilter_returned": len(prefiltered_res),
            "sql_prefilter_found_target": prefiltered_found_target,
            "sql_prefilter_latency_ms": round(prefiltered_ms, 3),
        },
        "http_starvation_recovery": http_starvation_report,
        "explain_unfiltered": {
            "execution_time_ms": explain_unfiltered[0].get("Execution Time"),
            "plan_nodes": _collect_nodes(explain_unfiltered[0]["Plan"]),
        },
        "explain_context_default": {
            "execution_time_ms": explain_context_default[0].get("Execution Time"),
            "plan_nodes": _collect_nodes(explain_context_default[0]["Plan"]),
        },
        "explain_context_indexed": {
            "execution_time_ms": explain_context_idx[0].get("Execution Time"),
            "plan_nodes": _collect_nodes(explain_context_idx[0]["Plan"]),
        },
    }


async def run_stage3_6_evaluation(
    *,
    dataset: Optional[Dict[str, Any]] = None,
    use_postgres: bool = False,
    db_url: Optional[str] = None,
    use_real_fastembed: bool = False,
) -> Dict[str, Any]:
    """
    Runs all 24 mandatory Stage 3.6 scenarios (CTX-001 through CTX-024) through real
    FastAPI HTTP endpoints, verifies all 21 mandatory metrics, validates zero sensitive
    disclosures, and optionally runs Phase 3.6.6 PostgreSQL index/EXPLAIN benchmarks.
    """
    ds = dataset or load_stage36_dataset()
    scenarios_by_id = {s["id"]: s for s in ds.get("scenarios", [])}
    provider = ScriptedStage36Provider(delay_seconds=0.0, tag="S36_HTTP")

    saved_cache = None
    saved_sem = None
    saved_vs = None

    resolved_db_url: Optional[str] = None
    if use_postgres:
        resolved_db_url = db_url or await resolve_fastembed_database_url()
        await reset_pg_tables(resolved_db_url)

    env_vars: Dict[str, str] = {
        "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
        "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
        "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "CONVERSATION_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
        "SEMANTIC_CACHE_ENABLED": "true",
        "SEMANTIC_CACHE_SERVING_ENABLED": "true",
        "SHADOW_EVALUATION_ENABLED": "false",
        "SEMANTIC_CACHE_DEFAULT_VISIBILITY": "private",
        "SEMANTIC_CACHE_MIN_SIMILARITY": "0.92",
        "SEMANTIC_CACHE_MIN_MARGIN": "0.03",
        "SEMANTIC_CACHE_TOP_K": "5",
        "SEMANTIC_CACHE_MAX_RETRIEVAL_CANDIDATES": "25",
        "LLM_PROVIDER": "mock",
        "EMBEDDING_PROVIDER": "fastembed",
        "EMBEDDING_MODEL": FASTEMBED_MODEL,
        "VECTOR_DIMENSIONS": str(FASTEMBED_DIM),
        "CACHE_BACKEND": "postgres" if use_postgres else "memory",
        "VECTOR_STORE_BACKEND": "postgres" if use_postgres else "memory",
        "DB_POOL_MIN_SIZE": "2",
        "DB_POOL_MAX_SIZE": "10",
    }
    if resolved_db_url:
        env_vars["DATABASE_URL"] = resolved_db_url

    derived_principals, forbidden_secrets = build_forbidden_disclosure_values(
        list(DEFAULT_CLIENT_TOKENS.keys()),
        STAGE_3_5_5_IDENTITY_SECRET,
        extra_secrets=[STAGE_3_5_5_EVIDENCE_SECRET, STAGE_3_5_5_ADMIN_TOKEN],
    )

    outcomes: List[ScenarioResult] = []
    pg_benchmark_report: Optional[Dict[str, Any]] = None
    concurrency_benchmark: Dict[str, Any] = {}

    get_metrics_collector().reset()
    reset_semantic_serving_controller(False)

    set_query_cache(None)
    set_semantic_cache(None)
    set_vector_store(None)

    topic_embedder: Optional[Stage36TopicEmbedder] = None
    with patch.dict(os.environ, env_vars, clear=False):
        reload_settings()
        if not use_real_fastembed:
            topic_embedder = Stage36TopicEmbedder()
            patch_ctx: Any = patch("app.main.EmbeddingProviderFactory.create", return_value=topic_embedder)
        else:
            patch_ctx = nullcontext()

        with patch_ctx:
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: provider
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    async def _post_query(
                        token: str,
                        payload: Dict[str, Any],
                        extra_headers: Optional[Dict[str, str]] = None,
                        expected_status: int = 200,
                    ) -> Dict[str, Any]:
                        r = await client.post("/query", headers=_hdr(token, extra_headers), json=payload)
                        _assert(
                            r.status_code == expected_status,
                            f"Expected HTTP {expected_status}, got {r.status_code}: {r.text}",
                        )
                        body = r.json()
                        _verify_no_disclosures(body, forbidden_secrets, "POST /query response")
                        return body

                    # CTX-001: Equivalent standalone technical question in two independent conversations
                    s = scenarios_by_id["CTX-001"]
                    c1_seed = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["seed_prompt"]}],
                            "conversation_id": "ctx001_conv_a",
                            "temperature": 0.0,
                        },
                    )
                    c1_probe = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["probe_prompt"]}],
                            "conversation_id": "ctx001_conv_b",
                            "temperature": 0.0,
                        },
                    )
                    _assert(c1_seed["cached"] is False, "CTX-001 seed must miss")
                    _assert(c1_probe["cached"] is True and c1_probe["cache_type"] == "semantic", f"CTX-001 expected cross-conversation semantic hit: {c1_probe}")
                    outcomes.append(ScenarioResult("CTX-001", True, "Standalone query reused across independent conversations", {"probe_cache_type": c1_probe["cache_type"]}))

                    # CTX-002: Same-tenant authorized shared reuse vs private isolation
                    s = scenarios_by_id["CTX-002"]
                    await _post_query("tok_carol", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    c2_dave = await _post_query("tok_dave", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    _assert(c2_dave["cached"] is True and c2_dave["cache_type"] == "semantic", "CTX-002 tenant_shared reader Dave must hit Carol's entry")
                    await _post_query("tok_alice", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to check if a key is in a Python dict?"}], "temperature": 0.0})
                    c2_bob = await _post_query("tok_bob", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to check if a key is in a Python dict?"}], "temperature": 0.0})
                    _assert(c2_bob["cached"] is False, "CTX-002 private entry must remain isolated from Bob")
                    outcomes.append(ScenarioResult("CTX-002", True, "Same-tenant shared reuse succeeded while private isolation held"))

                    # CTX-003: Cross-tenant isolation (team_beta vs team_gamma)
                    s = scenarios_by_id["CTX-003"]
                    await _post_query("tok_carol", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    c3_frank = await _post_query("tok_frank", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    _assert(c3_frank["cached"] is False, "CTX-003 cross-tenant request must never hit")
                    outcomes.append(ScenarioResult("CTX-003", True, "Cross-tenant boundary blocked semantic reuse"))

                    # CTX-004: Same wording, different previous conversation context
                    s = scenarios_by_id["CTX-004"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["context_a"]},
                                {"role": "assistant", "content": "Here is `parse_records_v1`."},
                                {"role": "user", "content": s["shared_followup"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    c4_diff = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["context_b"]},
                                {"role": "assistant", "content": "Here is `fetchInvoices`."},
                                {"role": "user", "content": s["shared_followup"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c4_diff["cached"] is False, f"CTX-004 mismatched code context must miss: {c4_diff}")
                    outcomes.append(ScenarioResult("CTX-004", True, "Identical wording under different code context rejected"))

                    # CTX-005: Follow-up referencing previous code -> context-dependent reuse under matching context
                    s = scenarios_by_id["CTX-005"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["prior_user"]},
                                {"role": "assistant", "content": s["prior_assistant"]},
                                {"role": "user", "content": s["seed_followup"]},
                            ],
                            "conversation_id": "ctx005_conv_a",
                            "temperature": 0.0,
                        },
                    )
                    c5_hit = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["prior_user"]},
                                {"role": "assistant", "content": s["prior_assistant"]},
                                {"role": "user", "content": s["probe_followup"]},
                            ],
                            "conversation_id": "ctx005_conv_b",
                            "temperature": 0.0,
                        },
                    )
                    _assert(c5_hit["cached"] is True and c5_hit["cache_type"] == "semantic", f"CTX-005 matching context follow-up expected semantic hit: {c5_hit}")
                    outcomes.append(ScenarioResult("CTX-005", True, "Matching context-dependent follow-up hit semantically"))

                    # CTX-006: Follow-up request with missing conversation history -> CONTEXT_UNCERTAIN fail closed
                    s = scenarios_by_id["CTX-006"]
                    before_calls = len(provider.calls)
                    c6 = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["probe_followup"]}],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c6["cached"] is False and len(provider.calls) == before_calls + 1, "CTX-006 missing history must miss to upstream")
                    # Repeat once more -> still misses because CONTEXT_UNCERTAIN is never admitted
                    c6_rep = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["probe_followup"]}],
                            "temperature": 0.0,
                        },
                    )
                    # Note exact cache may still cache exact message string unless distinct; verify semantic cache did NOT admit!
                    st_after_c6 = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                    _assert(st_after_c6["context_uncertain_requests"] >= 1, "CTX-006 must increment context_uncertain_requests")
                    _assert(c6_rep["cache_type"] != "semantic", "CTX-006 must never serve semantic hit")
                    outcomes.append(ScenarioResult("CTX-006", True, "Missing-history follow-up classified CONTEXT_UNCERTAIN and failed closed"))

                    # CTX-007: Standalone technical question with irrelevant prior chatter
                    s = scenarios_by_id["CTX-007"]
                    await _post_query(
                        "tok_alice",
                        {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0},
                    )
                    c7 = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [*s["prior_chatter"], {"role": "user", "content": s["probe_prompt"]}],
                            "conversation_id": "ctx007_chatter_conv",
                            "temperature": 0.0,
                        },
                    )
                    _assert(c7["cached"] is True and c7["cache_type"] == "semantic", f"CTX-007 irrelevant chatter must allow standalone hit: {c7}")
                    outcomes.append(ScenarioResult("CTX-007", True, "Irrelevant prior greetings excluded cleanly for standalone hit"))

                    # CTX-008: Different database schemas
                    s = scenarios_by_id["CTX-008"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["schema_a"]},
                                {"role": "user", "content": s["question"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    c8_diff = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["schema_b"]},
                                {"role": "user", "content": s["question"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c8_diff["cached"] is False, "CTX-008 different DDL schema must reject candidate")
                    outcomes.append(ScenarioResult("CTX-008", True, "Different PostgreSQL DDL schema rejected safely"))

                    # CTX-009: Different configuration settings
                    s = scenarios_by_id["CTX-009"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["config_a"]}, {"role": "user", "content": s["question"]}],
                            "temperature": 0.0,
                        },
                    )
                    c9_diff = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["config_b"]}, {"role": "user", "content": s["question"]}],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c9_diff["cached"] is False, "CTX-009 different YAML config must reject candidate")
                    outcomes.append(ScenarioResult("CTX-009", True, "Different configuration values rejected safely"))

                    # CTX-010: Different framework versions
                    s = scenarios_by_id["CTX-010"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["context_fastapi_110"]}, {"role": "user", "content": s["question"]}],
                            "temperature": 0.0,
                        },
                    )
                    c10_diff = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": s["context_fastapi_095"]}, {"role": "user", "content": s["question"]}],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c10_diff["cached"] is False, "CTX-010 different FastAPI framework version must miss")
                    outcomes.append(ScenarioResult("CTX-010", True, "Framework version mismatch blocked cross-version hit"))

                    # CTX-011 & CTX-012: Missing/invalid context_contract & tampered context fingerprint via direct + live HTTP lookup
                    sem_svc = app.state.semantic_cache
                    alice_prin = derive_principal_id("tok_alice", STAGE_3_5_5_IDENTITY_SECRET)
                    missing_ctx_req = GenerationRequest(
                        model="mock-qwen-27b",
                        provider="mock",
                        messages=[
                            ChatMessage(role="user", content="We define `def custom_calc_z9(x): return x + 1`"),
                            ChatMessage(role="assistant", content="def custom_calc_z9(x): return x + 1"),
                            ChatMessage(role="user", content="Update the previous implementation custom_calc_z9 to validate integers"),
                        ],
                        temperature=0.0,
                        namespace="team_alpha",
                        visibility="private",
                        principal_id=alice_prin,
                    )
                    ext = CompatibilityExtractor()
                    base_c11_meta = ext.extract(missing_ctx_req, FASTEMBED_MODEL)
                    c11_dict = base_c11_meta.to_dict()
                    c11_dict.pop("context_contract", None)
                    c11_fp = derive_scoped_exact_key("ctx011_missing_fp", "private", alice_prin)
                    c11_cand = VectorRecord(
                        cache_namespace="team_alpha",
                        fingerprint=c11_fp,
                        query_text="Update the previous implementation custom_calc_z9 to validate integers",
                        vector=(await app.state.semantic_cache.embedding_provider.embed("Update the previous implementation custom_calc_z9 to validate integers", EmbeddingTask.DOCUMENT)).vector,
                        model=FASTEMBED_MODEL,
                        metadata=c11_dict,
                        variant_key="v3:c11_missing",
                        response_text="def custom_calc_z9(x: int): return x + 1",
                        visibility="private",
                        owner_principal_id=alice_prin,
                    )
                    from app.vector_store.contracts import VectorSearchResult
                    from app.cache.interfaces import CacheEntry
                    dec_c11 = sem_svc.policy.validate_candidate(
                        VectorSearchResult(
                            cache_namespace=c11_cand.cache_namespace,
                            fingerprint=c11_cand.fingerprint,
                            query_text=c11_cand.query_text,
                            model=c11_cand.model,
                            distance=0.01,
                            similarity=0.99,
                            cached_response=c11_cand.response_text,
                            metadata=c11_dict,
                            visibility="private",
                            owner_principal_id=alice_prin,
                        ),
                        missing_ctx_req,
                    )
                    _assert(dec_c11.reason_code == CompatibilityReasonCode.CONTEXT_METADATA_MISSING, f"CTX-011 expected CONTEXT_METADATA_MISSING, got {dec_c11.reason_code}")
                    await get_query_cache().set(
                        c11_fp,
                        CacheEntry(response=c11_cand.response_text, model="mock-qwen-27b", usage=UsageMetrics(5, 5, 10), latency_ms=5.0, visibility="private", owner_principal_id=alice_prin),
                        namespace="team_alpha",
                    )
                    await app.state.vector_store.upsert(c11_cand)
                    c11_http = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": "We define `def custom_calc_z9(x): return x + 1`"},
                                {"role": "assistant", "content": "def custom_calc_z9(x): return x + 1"},
                                {"role": "user", "content": "Update the previous implementation custom_calc_z9 to validate integers"},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c11_http["cached"] is False, "CTX-011 HTTP lookup against missing context_contract must miss")
                    outcomes.append(ScenarioResult("CTX-011", True, "Missing candidate context_contract failed closed"))

                    # CTX-012: Tampered context_fingerprint rejected by EvidenceAuthority HMAC binding (direct + live HTTP)
                    tamper_prompt = "Write a Python function custom_tamper_probe_alpha that returns true"
                    tamper_req = GenerationRequest(
                        model="mock-qwen-27b",
                        provider="mock",
                        messages=[ChatMessage(role="user", content=tamper_prompt)],
                        temperature=0.0,
                        namespace="team_alpha",
                        visibility="private",
                        principal_id=alice_prin,
                    )
                    tamper_meta = ext.extract(tamper_req, FASTEMBED_MODEL)
                    tamper_resp = "```python\ndef custom_tamper_probe_alpha() -> bool:\n    return True\n```"
                    ver12 = sem_svc.verifier.verify(
                        response_text=tamper_resp,
                        request_metadata=tamper_meta,
                        finish_reason="stop",
                    )
                    c12_fp = derive_scoped_exact_key("ctx012_tampered_fp", "private", alice_prin)
                    ev12 = sem_svc.evidence_authority.mint_response_verified_evidence(
                        cache_namespace="team_alpha",
                        fingerprint=c12_fp,
                        response_text=tamper_resp,
                        metadata=tamper_meta,
                        verification=ver12,
                        context_independent=True,
                        visibility="private",
                        owner_principal_id=alice_prin,
                    )
                    signed_meta12 = SemanticCompatibilityMetadata(
                        schema_version=tamper_meta.schema_version,
                        extractor_version=tamper_meta.extractor_version,
                        task_kind=tamper_meta.task_kind,
                        dimensions=tamper_meta.dimensions,
                        generation_contract=tamper_meta.generation_contract,
                        embedding_contract=tamper_meta.embedding_contract,
                        compatibility_evidence=ev12,
                        context_contract=tamper_meta.context_contract,
                    )
                    signed_dict12 = signed_meta12.to_dict()
                    tampered_fp_meta12 = json.loads(json.dumps(signed_dict12))
                    tampered_fp_meta12["context_contract"]["context_fingerprint"] = "ctx-v1:" + "0" * 64
                    vk12 = compute_variant_key(signed_meta12, visibility="private", owner_principal_id=alice_prin)
                    dec_c12 = sem_svc.policy.validate_candidate(
                        VectorSearchResult(
                            cache_namespace="team_alpha",
                            fingerprint=c12_fp,
                            query_text=tamper_prompt,
                            model=FASTEMBED_MODEL,
                            distance=0.0,
                            similarity=1.0,
                            cached_response=tamper_resp,
                            metadata=tampered_fp_meta12,
                            visibility="private",
                            owner_principal_id=alice_prin,
                        ),
                        tamper_req,
                    )
                    _assert(not dec_c12.is_compatible, "CTX-012 tampered context fingerprint must never pass validation")
                    # Also verify EvidenceAuthority HMAC signature check on matching context_contract via HTTP lookup
                    tampered_sig_meta12 = json.loads(json.dumps(signed_dict12))
                    tampered_sig_meta12["compatibility_evidence"]["evidence_signature"] = "0" * 64
                    tampered_sig_meta12["variant_key"] = vk12
                    await get_query_cache().set(
                        c12_fp,
                        CacheEntry(response=tamper_resp, model="mock-qwen-27b", usage=UsageMetrics(5, 5, 10), latency_ms=5.0, visibility="private", owner_principal_id=alice_prin),
                        namespace="team_alpha",
                    )
                    await app.state.vector_store.upsert(
                        VectorRecord(
                            cache_namespace="team_alpha",
                            fingerprint=c12_fp,
                            query_text=tamper_prompt,
                            vector=(await app.state.semantic_cache.embedding_provider.embed(tamper_prompt, EmbeddingTask.DOCUMENT)).vector,
                            model=FASTEMBED_MODEL,
                            metadata=tampered_sig_meta12,
                            variant_key=vk12,
                            response_text=tamper_resp,
                            visibility="private",
                            owner_principal_id=alice_prin,
                        )
                    )
                    c12_http = await _post_query(
                        "tok_alice",
                        {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": tamper_prompt}], "temperature": 0.0},
                    )
                    _assert(c12_http["cached"] is False, "CTX-012 tampered HTTP lookup must miss and not serve forged record")
                    # Also verify tenant_shared + conversation_id context isolation (Carol -> Dave cannot read)
                    await _post_query(
                        "tok_carol",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": "CREATE TABLE payroll_secrets (emp_id INT, salary INT);"},
                                {"role": "user", "content": "Write a SQL query to aggregate active payroll rows"},
                            ],
                            "conversation_id": "conv_carol_private_thread",
                            "temperature": 0.0,
                        },
                    )
                    c12_dave = await _post_query(
                        "tok_dave",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": "CREATE TABLE payroll_secrets (emp_id INT, salary INT);"},
                                {"role": "user", "content": "Write a SQL query to aggregate active payroll rows"},
                            ],
                            "conversation_id": "conv_dave_thread",
                            "temperature": 0.0,
                        },
                    )
                    _assert(c12_dave["cached"] is False, "CTX-012 conversation-bound context-dependent entry must isolate from Dave")
                    outcomes.append(ScenarioResult("CTX-012", True, "Tampered context metadata rejected and conversation privacy preserved"))

                    # CTX-013: Private response retrieved by another principal (identical query)
                    s = scenarios_by_id["CTX-013"]
                    await _post_query("tok_alice", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    c13_bob = await _post_query("tok_bob", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    _assert(c13_bob["cached"] is False, "CTX-013 private entry must reject Bob regardless of similarity")
                    outcomes.append(ScenarioResult("CTX-013", True, "Private principal isolation enforced"))

                    # CTX-014: Tenant-shared entry queried by unauthorized private reader Fay (False) vs Erin (True)
                    s = scenarios_by_id["CTX-014"]
                    await _post_query("tok_carol", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    c14_fay = await _post_query("tok_fay", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    c14_erin = await _post_query("tok_erin", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    _assert(c14_fay["cached"] is False, "CTX-014 private reader with allow_shared_reads=false must miss")
                    _assert(c14_erin["cached"] is True and c14_erin["cache_type"] == "semantic", "CTX-014 private reader with allow_shared_reads=true must hit")
                    outcomes.append(ScenarioResult("CTX-014", True, "allow_shared_reads authorization strictly enforced"))

                    # CTX-015: Spoofing principal, context scope, or visibility -> HTTP 422 & header isolation
                    s = scenarios_by_id["CTX-015"]
                    for spoof_key in s["spoof_fields"]:
                        r_spoof = await client.post(
                            "/query",
                            headers=_hdr("tok_alice"),
                            json={
                                "model": "mock-qwen-27b",
                                "messages": [{"role": "user", "content": "How do I reverse a string in Python?"}],
                                spoof_key: "spoofed_val",
                            },
                        )
                        _assert(r_spoof.status_code == 422, f"CTX-015 expected 422 on spoofed '{spoof_key}', got {r_spoof.status_code}")
                    r_hdr_spoof = await client.post(
                        "/query",
                        headers=_hdr("tok_alice", {"X-Tenant-Namespace": "team_beta"}),
                        json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How do I check Python list length?"}]},
                    )
                    _assert(r_hdr_spoof.status_code == 200, "CTX-015 ignored header spoof should not leak team_beta")
                    outcomes.append(ScenarioResult("CTX-015", True, "Client context/scope spoofing blocked with HTTP 422"))

                    # CTX-016: Two compatible variants (json vs plain_text) share base prompt cleanly
                    s = scenarios_by_id["CTX-016"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": f"{s['prompt']} in Python and output strict JSON"}],
                            "temperature": 0.0,
                        },
                    )
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": f"{s['prompt']} in Python as plain text"}],
                            "temperature": 0.0,
                        },
                    )
                    c16_json_hit = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": "Write a Python helper to format a user object cleanly in strict JSON"}],
                            "temperature": 0.0,
                        },
                    )
                    c16_txt_hit = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [{"role": "user", "content": "Write a Python helper to format a user object cleanly as plain text"}],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c16_json_hit["cached"] and "status" in c16_json_hit["response"], "CTX-016 JSON variant mismatch or lost")
                    _assert(c16_txt_hit["cached"] and "Plain helper output" in c16_txt_hit["response"], "CTX-016 plain_text variant mismatch or lost")
                    outcomes.append(ScenarioResult("CTX-016", True, "Multiple compatible variants coexisted without overwrite"))

                    # CTX-017: Context changes after response cached -> new fingerprint misses old answer
                    s = scenarios_by_id["CTX-017"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["turn_1"]},
                                {"role": "assistant", "content": "```python\ndef process_order(order_id: int) -> bool:\n    return order_id > 0\n```"},
                                {"role": "user", "content": s["turn_2_followup"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    c17_changed = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [
                                {"role": "user", "content": s["turn_1"]},
                                {"role": "assistant", "content": "```python\ndef process_order(order_id: int) -> bool:\n    return order_id > 0\n```"},
                                {"role": "user", "content": s["turn_3_change"]},
                                {"role": "assistant", "content": "Updated with asyncpg advisory locks."},
                                {"role": "user", "content": s["turn_2_followup"]},
                            ],
                            "temperature": 0.0,
                        },
                    )
                    _assert(c17_changed["cached"] is False, "CTX-017 mutated conversation context must invalidate old context match")
                    outcomes.append(ScenarioResult("CTX-017", True, "Dynamic context mutation invalidates prior context entry"))

                    # CTX-018: Independent responses reused despite different irrelevant chatter in both conversations
                    s = scenarios_by_id["CTX-018"]
                    await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [*s["seed_chatter"], {"role": "user", "content": s["seed_prompt"]}],
                            "conversation_id": "ctx018_seed",
                            "temperature": 0.0,
                        },
                    )
                    c18_hit = await _post_query(
                        "tok_alice",
                        {
                            "model": "mock-qwen-27b",
                            "messages": [*s["probe_chatter"], {"role": "user", "content": s["probe_prompt"]}],
                            "conversation_id": "ctx018_probe",
                            "temperature": 0.0,
                        },
                    )
                    _assert(c18_hit["cached"] is True and c18_hit["cache_type"] == "semantic", "CTX-018 independent prompt with different chatter should hit")
                    outcomes.append(ScenarioResult("CTX-018", True, "Different irrelevant histories did not block independent reuse"))

                    # CTX-019: Embedding provider outage fails open cleanly
                    with patch.object(app.state.semantic_cache.embedding_provider, "embed", side_effect=EmbeddingTimeoutError("Simulated embed timeout")):
                        c19 = await _post_query(
                            "tok_alice",
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to check palindrome string in Python?"}], "temperature": 0.0},
                        )
                        _assert(c19["cached"] is False and c19["cache_type"] == "none", "CTX-019 embedding failure must fail open")
                    outcomes.append(ScenarioResult("CTX-019", True, "Embedding unavailability failed open to upstream"))

                    # CTX-020: pgvector/vector store outage fails open cleanly
                    with patch.object(app.state.semantic_cache.vector_store, "search", side_effect=VectorStoreError("Simulated pgvector failure")):
                        c20 = await _post_query(
                            "tok_alice",
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to count unique items in a Python list?"}], "temperature": 0.0},
                        )
                        _assert(c20["cached"] is False and c20["cache_type"] == "none", "CTX-020 vector store failure must fail open")
                    outcomes.append(ScenarioResult("CTX-020", True, "Vector store retrieval outage failed open to upstream"))

                    # CTX-021: Ambiguous semantic candidates with different answers / context requirements -> rejected cleanly
                    q_amb = "Explain Python decorator execution order in detail"
                    amb_vec = (await app.state.semantic_cache.embedding_provider.embed(q_amb, EmbeddingTask.DOCUMENT)).vector
                    # Seed two near-identical entries with different answers at tiny distance delta (< 0.03 margin)
                    for idx_amb, txt_amb in ((1, "Decorator answer variant ALPHA"), (2, "Decorator answer variant BETA")):
                        r_amb = GenerationRequest(
                            model="mock-qwen-27b",
                            provider="mock",
                            messages=[ChatMessage(role="user", content=f"{q_amb} case {idx_amb}")],
                            temperature=0.0,
                            namespace="team_alpha",
                            visibility="private",
                            principal_id=alice_prin,
                        )
                        m_amb = ext.extract(r_amb, FASTEMBED_MODEL)
                        v_amb = sem_svc.verifier.verify(response_text=txt_amb, request_metadata=m_amb, finish_reason="stop")
                        fp_amb = derive_scoped_exact_key(f"ctx021_amb_{idx_amb}", "private", alice_prin)
                        ev_amb = sem_svc.evidence_authority.mint_response_verified_evidence(
                            cache_namespace="team_alpha",
                            fingerprint=fp_amb,
                            response_text=txt_amb,
                            metadata=m_amb,
                            verification=v_amb,
                            context_independent=True,
                            visibility="private",
                            owner_principal_id=alice_prin,
                        )
                        m_amb_full = SemanticCompatibilityMetadata(
                            schema_version=m_amb.schema_version,
                            extractor_version=m_amb.extractor_version,
                            task_kind=m_amb.task_kind,
                            dimensions=m_amb.dimensions,
                            generation_contract=m_amb.generation_contract,
                            embedding_contract=m_amb.embedding_contract,
                            compatibility_evidence=ev_amb,
                            context_contract=m_amb.context_contract,
                        )
                        md_amb = m_amb_full.to_dict()
                        vk_amb = compute_variant_key(m_amb_full, visibility="private", owner_principal_id=alice_prin)
                        md_amb["variant_key"] = vk_amb
                        await get_query_cache().set(fp_amb, CacheEntry(response=txt_amb, model="mock-qwen-27b", usage=UsageMetrics(5, 5, 10), latency_ms=5.0, visibility="private", owner_principal_id=alice_prin), namespace="team_alpha")
                        v2_amb = list(amb_vec)
                        if idx_amb == 2:
                            v2_amb[-1] = v2_amb[-1] + 0.005
                            norm_v2 = math.sqrt(sum(x * x for x in v2_amb)) or 1.0
                            v2_amb = [x / norm_v2 for x in v2_amb]
                        await app.state.vector_store.upsert(VectorRecord(cache_namespace="team_alpha", fingerprint=fp_amb, query_text=f"{q_amb} case {idx_amb}", vector=v2_amb, model=FASTEMBED_MODEL, metadata=md_amb, variant_key=vk_amb, response_text=txt_amb, visibility="private", owner_principal_id=alice_prin))
                    c21 = await _post_query("tok_alice", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q_amb}], "temperature": 0.0})
                    _assert(c21["cached"] is False, "CTX-021 ambiguous close candidates with differing answers must miss to upstream")
                    c21_stats = (await client.get("/cache/semantic/stats", headers={"Authorization": f"Bearer {STAGE_3_5_5_ADMIN_TOKEN}"})).json()
                    _assert(c21_stats["ambiguous_candidates"] >= 1, f"CTX-021 expected ambiguous_candidates >= 1, got {c21_stats['ambiguous_candidates']}")
                    outcomes.append(ScenarioResult("CTX-021", True, "Ambiguous candidates within margin rejected safely"))

                    # CTX-022: Truncated/incomplete upstream response refused by admission verifier
                    s = scenarios_by_id["CTX-022"]
                    provider.next_response_override = s["truncated_response"]
                    provider.next_finish_reason = s["finish_reason"]
                    c22_first = await _post_query("tok_alice", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    _assert(c22_first["cached"] is False, "CTX-022 seed misses")
                    c22_second = await _post_query("tok_alice", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "Write a Python function outputting JSON with required keys: id, status"}], "temperature": 0.0})
                    _assert(c22_second["cached"] is False, "CTX-022 truncated artifact must NOT be admitted semantically")
                    outcomes.append(ScenarioResult("CTX-022", True, "Truncated/incomplete response rejected from semantic admission"))

                    # CTX-023: Simultaneous identical requests preserve SingleFlight & semantic/coalesced hit accounting
                    s = scenarios_by_id["CTX-023"]
                    provider.set_delay_ms(80.0)
                    before_sf_calls = len(provider.calls)
                    sf_t0 = time.perf_counter()
                    sf_tasks = [
                        _post_query("tok_sf_a", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["prompt"]}], "temperature": 0.0})
                        for _ in range(int(s["concurrency"]))
                    ]
                    sf_results = await asyncio.gather(*sf_tasks)
                    sf_elapsed_ms = (time.perf_counter() - sf_t0) * 1000.0
                    provider.set_delay_ms(0.0)
                    _assert(len(provider.calls) - before_sf_calls == 1, f"CTX-023 expected 1 upstream call for 8 concurrent identical requests, got {len(provider.calls) - before_sf_calls}")
                    sf_cached_count = sum(1 for r in sf_results if r["cached"])
                    _assert(sf_cached_count == int(s["concurrency"]) - 1, f"CTX-023 expected {int(s['concurrency']) - 1} coalesced hits, got {sf_cached_count}")
                    # Now issue concurrent semantic hits (leader serves semantically + followers coalesce cleanly!)
                    orig_embed_sf = app.state.semantic_cache.embedding_provider.embed

                    async def _delayed_embed_for_coalesce(text: str, *a: Any, **kw: Any):
                        await asyncio.sleep(0.03)
                        return await orig_embed_sf(text, *a, **kw)

                    with patch.object(app.state.semantic_cache.embedding_provider, "embed", _delayed_embed_for_coalesce):
                        sf_sem_tasks = [
                            _post_query("tok_sf_a", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to flatten nested lists in Python?"}], "temperature": 0.0})
                            for _ in range(4)
                        ]
                        sf_sem_results = await asyncio.gather(*sf_sem_tasks)
                    _assert(all(r["cached"] is True and r["cache_type"] == "semantic" for r in sf_sem_results), "CTX-023 simultaneous semantic hits should serve + coalesce")
                    concurrency_benchmark = {
                        "concurrent_batch_size": int(s["concurrency"]),
                        "upstream_calls": len(provider.calls) - before_sf_calls,
                        "coalesced_hits": sf_cached_count,
                        "elapsed_ms": round(sf_elapsed_ms, 2),
                    }
                    outcomes.append(ScenarioResult("CTX-023", True, "SingleFlight coalesced simultaneous misses and semantic hits accurately", concurrency_benchmark))

                    # CTX-024: Runtime semantic serving kill switch disables lookups cleanly
                    s = scenarios_by_id["CTX-024"]
                    await _post_query("tok_ctrl", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["seed_prompt"]}], "temperature": 0.0})
                    r_disable = await client.post(
                        "/admin/semantic-cache/serving",
                        headers={"Authorization": f"Bearer {STAGE_3_5_5_ADMIN_TOKEN}"},
                        json={"enabled": False, "namespace": "team_ctrl"},
                    )
                    _assert(r_disable.status_code == 200, "Failed to disable runtime serving")
                    c24_off = await _post_query("tok_ctrl", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": s["probe_prompt"]}], "temperature": 0.0})
                    _assert(c24_off["cached"] is False and c24_off["cache_type"] == "none", "CTX-024 disabled runtime switch must bypass semantic lookup")
                    r_enable = await client.post(
                        "/admin/semantic-cache/serving",
                        headers={"Authorization": f"Bearer {STAGE_3_5_5_ADMIN_TOKEN}"},
                        json={"enabled": True, "namespace": "team_ctrl"},
                    )
                    _assert(r_enable.status_code == 200, "Failed to re-enable runtime serving")
                    c24_on = await _post_query("tok_ctrl", {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How to format a datetime in Python?"}], "temperature": 0.0})
                    _assert(c24_on["cached"] is True and c24_on["cache_type"] == "semantic", "CTX-024 re-enabled runtime switch must resume semantic reuse")
                    outcomes.append(ScenarioResult("CTX-024", True, "Runtime kill switch toggled semantic lookups safely without restart"))

                    # Phase 3.6.6 PostgreSQL Index + EXPLAIN + Server-Managed Conversations check
                    server_conv_report: Optional[Dict[str, Any]] = None
                    if use_postgres and resolved_db_url:
                        _assert(
                            bool(getattr(app.state.semantic_cache, "sql_context_prefilter", False)) is True,
                            "PostgreSQL SemanticCacheService must enable sql_context_prefilter=True",
                        )
                        pg_benchmark_report = await _run_pg_context_index_benchmark(
                            app,
                            app.state.semantic_cache.embedding_provider,
                            resolved_db_url,
                            client=client,
                        )
                        # Verify server-managed persistent conversation turns via POST /conversations -> POST /query
                        conv_create = await client.post(
                            "/conversations",
                            headers=_hdr("tok_alice"),
                            json={"title": "Stage 3.6 Server Managed Verification"},
                        )
                        _assert(conv_create.status_code in (200, 201), f"POST /conversations failed: {conv_create.status_code} {conv_create.text}")
                        conv_id = conv_create.json()["id"]
                        conv_t1 = await _post_query(
                            "tok_alice",
                            {"model": "mock-qwen-27b", "conversation_id": conv_id, "prompt": "How do I reverse a Python list?", "temperature": 0.0},
                            extra_headers={"Idempotency-Key": "s36-server-turn-1"},
                        )
                        _assert(conv_t1["conversation_id"] == conv_id and conv_t1["turn_id"] and conv_t1["context_status"] == "complete", f"Server turn 1 invalid: {conv_t1}")
                        conv_t2 = await _post_query(
                            "tok_alice",
                            {"model": "mock-qwen-27b", "conversation_id": conv_id, "prompt": "Refactor that helper to handle empty inputs safely.", "temperature": 0.0},
                            extra_headers={"Idempotency-Key": "s36-server-turn-2"},
                        )
                        _assert(conv_t2["conversation_id"] == conv_id and conv_t2["turn_id"] and conv_t2["context_status"] == "complete", f"Server turn 2 invalid: {conv_t2}")
                        # Verify another principal cannot read Alice's server-managed conversation
                        conv_bob = await client.get(f"/conversations/{conv_id}", headers=_hdr("tok_bob"))
                        _assert(conv_bob.status_code == 404, f"Bob must not see Alice's conversation, got {conv_bob.status_code}")
                        conv_msgs = await client.get(f"/conversations/{conv_id}/messages", headers=_hdr("tok_alice"))
                        _assert(conv_msgs.status_code == 200 and len(conv_msgs.json().get("messages", [])) == 4, "Expected 4 persisted messages in server conversation")
                        server_conv_report = {
                            "conversation_id": conv_id,
                            "turn_1_cached": conv_t1["cached"],
                            "turn_1_cache_type": conv_t1["cache_type"],
                            "turn_2_cached": conv_t2["cached"],
                            "turn_2_context_status": conv_t2["context_status"],
                            "cross_principal_blocked_status": conv_bob.status_code,
                            "persisted_messages": len(conv_msgs.json().get("messages", [])),
                        }

                    # Collect global admin metrics + per-namespace checks and verify all 21 mandatory metrics
                    admin_metrics_resp = await client.get(
                        "/admin/metrics",
                        headers={"Authorization": f"Bearer {STAGE_3_5_5_ADMIN_TOKEN}"},
                    )
                    _assert(admin_metrics_resp.status_code == 200, "GET /admin/metrics failed")
                    admin_metrics = admin_metrics_resp.json()
                    _verify_no_disclosures(admin_metrics, forbidden_secrets, "GET /admin/metrics")

                    sem_stats_resp = await client.get(
                        "/cache/semantic/stats",
                        headers={"Authorization": f"Bearer {STAGE_3_5_5_ADMIN_TOKEN}"},
                    )
                    _assert(sem_stats_resp.status_code == 200, "GET /cache/semantic/stats failed")
                    sem_stats_global = sem_stats_resp.json()
                    _verify_no_disclosures(sem_stats_global, forbidden_secrets, "GET /cache/semantic/stats")

                    agg = admin_metrics["aggregate"]
                    flat_21_metrics: Dict[str, Any] = {
                        **{k: agg["semantic"][k] for k in agg["semantic"] if k != "semantic_rejection_reasons"},
                        "provider_calls_avoided": agg["provider_and_savings"]["provider_calls_avoided"],
                        "semantic_lookup_latency_ms": agg["latencies"]["semantic_lookup_latency_ms"],
                        "context_validation_latency_ms": agg["latencies"]["context_validation_latency_ms"],
                        "compatibility_validation_latency_ms": agg["latencies"]["compatibility_validation_latency_ms"],
                    }
                    missing_21 = [m for m in MANDATORY_21_METRICS if m not in flat_21_metrics]
                    _assert(not missing_21, f"Missing mandatory Stage 3.6 metrics: {missing_21}")
                    _assert(flat_21_metrics["context_independent_requests"] > 0, "Expected context_independent_requests > 0")
                    _assert(flat_21_metrics["context_dependent_requests"] > 0, "Expected context_dependent_requests > 0")
                    _assert(flat_21_metrics["context_uncertain_requests"] > 0, "Expected context_uncertain_requests > 0")
                    _assert(flat_21_metrics["context_compatibility_matches"] > 0, "Expected context_compatibility_matches > 0")
                    if not use_postgres:
                        _assert(flat_21_metrics["context_compatibility_rejections"] > 0, "Expected context_compatibility_rejections > 0")
                        _assert(flat_21_metrics["context_metadata_missing"] > 0, "Expected context_metadata_missing > 0")
                        _assert(flat_21_metrics["context_fingerprint_mismatches"] > 0, "Expected context_fingerprint_mismatches > 0")
                    else:
                        _assert(flat_21_metrics["context_metadata_missing"] == 0, "SQL prefilter must keep missing-context rows out of retrieval")
                    _assert(agg["semantic"]["ambiguous_candidate_rejections"] > 0, "Expected ambiguous_candidate_rejections > 0")
                    _assert(agg["semantic"]["evidence_validation_rejections"] > 0, "Expected evidence_validation_rejections > 0")
                    _assert(flat_21_metrics["cross_conversation_semantic_hits"] > 0, "Expected cross_conversation_semantic_hits > 0")
                    _assert(flat_21_metrics["semantic_admission_successes"] > 0, "Expected semantic_admission_successes > 0")
                    _assert(flat_21_metrics["semantic_admission_rejections"] > 0, "Expected semantic_admission_rejections > 0")
                    _assert(flat_21_metrics["semantic_hits"] > 0, "Expected semantic_hits > 0")
                    _assert(flat_21_metrics["coalesced_semantic_hits"] > 0, "Expected coalesced_semantic_hits > 0")
                    _assert(flat_21_metrics["semantic_fallbacks"] > 0, "Expected semantic_fallbacks > 0")
                    _assert(flat_21_metrics["context_validation_latency_ms"]["sample_count"] > 0, "Expected context_validation_latency_ms samples > 0")
                    _assert(flat_21_metrics["compatibility_validation_latency_ms"]["sample_count"] > 0, "Expected compatibility_validation_latency_ms samples > 0")

                    report = {
                        "stage": "3.6",
                        "timestamp": datetime.now(timezone.utc).isoformat(),
                        "backend": "postgres+pgvector" if use_postgres else "in_memory",
                        "embedding_mode": "real_fastembed_bge_small_en_v1_5" if use_real_fastembed else "deterministic_topic_384d",
                        "database_url": redact_database_url(resolved_db_url) if resolved_db_url else None,
                        "scenarios_total": len(outcomes),
                        "scenarios_passed": sum(1 for o in outcomes if o.passed),
                        "scenarios": [
                            {"id": o.id, "passed": o.passed, "summary": o.summary, "details": o.details}
                            for o in outcomes
                        ],
                        "mandatory_metrics": flat_21_metrics,
                        "latencies": agg["latencies"],
                        "semantic_stats_global": sem_stats_global,
                        "postgres_benchmark": pg_benchmark_report,
                        "server_managed_conversations": server_conv_report,
                        "concurrency": concurrency_benchmark,
                    }

    set_query_cache(saved_cache)
    set_semantic_cache(saved_sem)
    set_vector_store(saved_vs)
    return report


def _render_markdown_report(report: Dict[str, Any]) -> str:
    lines = [
        "# Stage 3.6 Context-Aware Semantic Cache Evaluation Report",
        "",
        f"- **Timestamp:** `{report['timestamp']}`",
        f"- **Backend:** `{report['backend']}`",
        f"- **Embeddings:** `{report['embedding_mode']}`",
        f"- **Scenarios Passed:** `{report['scenarios_passed']} / {report['scenarios_total']}`",
        "",
        "## Scenario Matrix (CTX-001 .. CTX-024)",
        "",
        "| ID | Status | Summary |",
        "| :--- | :---: | :--- |",
    ]
    for s in report["scenarios"]:
        status = "PASS" if s["passed"] else "FAIL"
        lines.append(f"| `{s['id']}` | **{status}** | {s['summary']} |")

    lines.extend([
        "",
        "## Mandatory Metrics Snapshot",
        "",
        "```json",
        json.dumps(report["mandatory_metrics"], indent=2),
        "```",
    ])
    if report.get("postgres_benchmark"):
        lines.extend([
            "",
            "## PostgreSQL Index & EXPLAIN ANALYZE Verification",
            "",
            "```json",
            json.dumps(report["postgres_benchmark"], indent=2),
            "```",
        ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.6 Context-Aware Semantic Cache Evaluator")
    parser.add_argument("--memory-only", action="store_true", help="Run with InMemory backend + deterministic embedder")
    parser.add_argument("--test-database-url", type=str, default=None, help="PostgreSQL test DB URL")
    parser.add_argument("--output-dir", type=str, default="/tmp/stage3_6_eval", help="Report output directory")
    args = parser.parse_args()

    use_pg = not args.memory_only
    report = asyncio.run(
        run_stage3_6_evaluation(
            use_postgres=use_pg,
            db_url=args.test_database_url,
            use_real_fastembed=use_pg,
        )
    )

    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "stage3_6_context_evaluation.json"
    md_path = out_dir / "stage3_6_context_evaluation.md"
    json_path.write_text(json.dumps(report, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown_report(report), encoding="utf-8")
    print(f"Stage 3.6 evaluation complete: {report['scenarios_passed']}/{report['scenarios_total']} scenarios passed.")
    print(f"JSON report: {json_path}")
    print(f"Markdown report: {md_path}")
    return 0 if report["scenarios_passed"] == report["scenarios_total"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
