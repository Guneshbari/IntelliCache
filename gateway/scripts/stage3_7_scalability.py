#!/usr/bin/env python3
"""
Stage 3.7 Phase 5 & Phase 6 Benchmarks: PostgreSQL/pgvector Scalability & A/B/C Performance Comparison.

Runs real PostgreSQL 16 + pgvector + FastEmbed benchmarks across:
- Phase 5: 100 / 1,000 / 5,000 / 10,000 semantic_variant_records scaling, multi-variant /
  multi-context / multi-visibility distributions, EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON) plans,
  HNSW vs B-tree/context index utilization, retrieval budget exhaustion, and (top_k, budget)
  plus hnsw.iterative_scan comparisons.
- Phase 6: Controlled A/B/C comparison:
  (A) Exact caching only
  (B) Exact + Semantic caching enabled
  (C) Upstream-only baseline
  with end-to-end p50/p95/p99 and component latency breakdowns cleanly separating
  synthetic upstream delay from real FastEmbed + PostgreSQL processing time.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import math
import statistics
import time
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from unittest.mock import patch

import asyncpg
import httpx

from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.embeddings.contracts import EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.main import create_app
from app.metrics import get_metrics_collector
from app.privacy import derive_scoped_exact_key
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.context import analyze_request_context
from app.semantic_cache.contracts import (
    ContextClass,
    ResponseVerificationResult,
    SemanticCompatibilityMetadata,
    TruncationStatus,
    VerificationStatus,
    compute_variant_key,
)
from app.semantic_cache.control import reset_semantic_serving_controller
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import VectorRecord
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore
from app.api.routes import get_llm_provider
from scripts.evaluate_semantic_cache import redact_database_url
from scripts.evaluate_stage3_5_5_operational import (
    ControlledEvalProvider,
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    reset_pg_tables,
)


def compute_full_percentiles(values: Sequence[float]) -> Dict[str, Any]:
    """Computes summary statistics (count, min, mean, p50, p95, p99, max) in ms."""
    if not values:
        return {
            "count": 0,
            "min_ms": 0.0,
            "mean_ms": 0.0,
            "p50_ms": 0.0,
            "p95_ms": 0.0,
            "p99_ms": 0.0,
            "max_ms": 0.0,
        }
    ordered = sorted(float(v) for v in values)

    def _pct(p: float) -> float:
        idx = (len(ordered) - 1) * (p / 100.0)
        lo = math.floor(idx)
        hi = math.ceil(idx)
        if lo == hi:
            return ordered[lo]
        return ordered[lo] + (ordered[hi] - ordered[lo]) * (idx - lo)

    return {
        "count": len(ordered),
        "min_ms": round(ordered[0], 3),
        "mean_ms": round(statistics.mean(ordered), 3),
        "p50_ms": round(_pct(50.0), 3),
        "p95_ms": round(_pct(95.0), 3),
        "p99_ms": round(_pct(99.0), 3),
        "max_ms": round(ordered[-1], 3),
    }


def _extract_plan_details(plan_root: Dict[str, Any]) -> Dict[str, Any]:
    """Extracts structured node names, indexes, scan types, and timings from PG EXPLAIN JSON."""
    nodes: List[str] = []
    indexes_used: List[str] = []
    scan_types: List[str] = []

    def _walk(node: Dict[str, Any]) -> None:
        ntype = str(node.get("Node Type") or "")
        idx_name = node.get("Index Name")
        label = f"{ntype}({idx_name})" if idx_name else ntype
        nodes.append(label)
        if idx_name:
            indexes_used.append(str(idx_name))
        if "Scan" in ntype:
            scan_types.append(label)
        for child in node.get("Plans", []):
            _walk(child)

    top_plan = plan_root.get("Plan") or {}
    _walk(top_plan)
    return {
        "execution_time_ms": round(float(plan_root.get("Execution Time") or 0.0), 3),
        "planning_time_ms": round(float(plan_root.get("Planning Time") or 0.0), 3),
        "actual_rows": int(top_plan.get("Actual Rows") or 0),
        "plan_nodes": nodes,
        "scan_types": scan_types,
        "indexes_used": sorted(set(indexes_used)),
        "hnsw_used": any("hnsw" in idx.lower() for idx in indexes_used),
        "context_index_used": any("context_scope" in idx.lower() for idx in indexes_used),
    }


def _perturb_unit_vector(base: Sequence[float], seed_idx: int, *, eps: float) -> List[float]:
    """Produces a deterministically normalized vector near `base` with controlled angle offset."""
    vec = list(float(x) for x in base)
    dim = len(vec)
    slot_a = (seed_idx * 7 + 3) % dim
    slot_b = (seed_idx * 13 + 19) % dim
    sign_a = 1.0 if (seed_idx % 2 == 0) else -1.0
    vec[slot_a] += sign_a * eps
    vec[slot_b] -= sign_a * (eps * 0.5)
    norm = math.sqrt(sum(v * v for v in vec)) or 1.0
    return [v / norm for v in vec]


def _vec_literal(vector: Sequence[float]) -> str:
    return "[" + ",".join(f"{float(v):.6f}" for v in vector) + "]"


async def _seed_scalability_corpus(
    pool: asyncpg.Pool,
    embedder: EmbeddingProvider,
    authority: EvidenceAuthority,
    *,
    namespace: str,
    total_rows: int,
) -> Dict[str, Any]:
    """
    Populates `cache_entries` + `semantic_variant_records` cleanly with `total_rows` entries
    spanning multiple programming languages, framework versions, output formats, constraints,
    context fingerprints, and private/shared scopes, plus deterministic planted starvation traps.
    """
    ext = CompatibilityExtractor()
    now = datetime.now(timezone.utc)

    # Probe 1: CONTEXT_INDEPENDENT Python target + paraphrase query
    indep_seed_prompt = "How do I reverse a string cleanly in Python?"
    indep_probe_prompt = "What is the Python syntax to cleanly reverse a string?"
    indep_target_resp = "```python\ndef reverse_str(s: str) -> str:\n    return s[::-1]\n```"

    # Probe 2: CONTEXT_DEPENDENT PostgreSQL schema target + high-similarity wrong-context distractors
    dep_target_ctx = "CREATE TABLE billing_ledger (entry_id BIGINT PRIMARY KEY, posted_at TIMESTAMPTZ, amount_cents INT);"
    dep_distractor_ctx = "CREATE TABLE staging_logs (log_id BIGINT PRIMARY KEY, message TEXT, level INT);"
    dep_followup = "Add a structured pagination helper for the previous implementation"
    dep_target_resp = "```sql\nSELECT entry_id, posted_at, amount_cents FROM billing_ledger ORDER BY posted_at DESC LIMIT 25;\n```"

    anchor_prompts = [
        indep_seed_prompt,
        indep_probe_prompt,
        dep_followup,
        "Write a Go helper to merge two sorted slices",
        "Write a Rust helper to parse configuration YAML",
        "Write a FastAPI 0.110 async health endpoint in Python",
        "Write a React 18 data fetching hook in JavaScript",
        "Return strict JSON schema user summary in Python",
        "Configure Nginx reverse proxy timeouts for SSE streaming",
        "Write a Kubernetes readiness probe in YAML for FastAPI",
        "Optimize PostgreSQL B-tree composite index ordering",
        "Implement TypeScript generic retry wrapper with exponential backoff",
        "Debug Docker multi-stage build layer caching cleanly",
        "Write a SQLite WAL mode checkpoint helper in Python",
        "Implement OAuth 2.1 PKCE challenge verifier in Python",
        "Write pytest async fixture for PostgreSQL connection pools",
        "Compare gRPC protobuf streaming vs REST JSON pagination",
        "Implement asyncio bounded Queue worker pool in Python",
        "Configure GitHub Actions pip and npm cache invalidation",
        "Parse structured NDJSON server logs in Go safely",
        "Implement token bucket rate limiter in Java cleanly",
        "Write a SQL window function running total query in PostgreSQL",
    ]
    anchor_vecs = [
        res.vector
        for res in await embedder.embed_batch(anchor_prompts, task=EmbeddingTask.DOCUMENT)
    ]
    indep_seed_vec = anchor_vecs[0]
    indep_probe_vec = (await embedder.embed(indep_probe_prompt, task=EmbeddingTask.QUERY)).vector
    dep_query_vec = (await embedder.embed(dep_followup, task=EmbeddingTask.QUERY)).vector

    def _build_signed_row(
        req: GenerationRequest,
        fp_raw: str,
        resp_text: str,
        vec: Sequence[float],
        vis: str,
        owner: Optional[str],
        *,
        out_fmt: Optional[str] = None,
        schema_dig: Optional[str] = None,
    ) -> Tuple[Tuple[Any, ...], Tuple[Any, ...]]:
        from app.semantic_cache.contracts import CompatibilityReasonCode

        meta = ext.extract(req, FASTEMBED_MODEL)
        ver = ResponseVerificationResult(
            status=VerificationStatus.VERIFIED,
            truncation_status=TruncationStatus.COMPLETE,
            reason_code=CompatibilityReasonCode.COMPATIBLE,
            message="Seeded verified entry",
            verified_output_format=out_fmt,
            verified_json_valid=(out_fmt == "json"),
            verified_schema_digest=schema_dig,
            verified_schema_version=1 if schema_dig else None,
        )
        scoped_fp = derive_scoped_exact_key(fp_raw, vis, owner)
        vk = compute_variant_key(meta, schema_digest=schema_dig, visibility=vis, owner_principal_id=owner)
        is_ctx_indep = bool(meta.context_contract and meta.context_contract.context_class == ContextClass.CONTEXT_INDEPENDENT)
        ev = authority.mint_response_verified_evidence(
            cache_namespace=namespace,
            fingerprint=scoped_fp,
            response_text=resp_text,
            metadata=meta,
            verification=ver,
            context_independent=is_ctx_indep,
            visibility=vis,
            owner_principal_id=owner,
        )
        full_meta = SemanticCompatibilityMetadata(
            schema_version=meta.schema_version,
            extractor_version=meta.extractor_version,
            task_kind=meta.task_kind,
            dimensions=meta.dimensions,
            generation_contract=meta.generation_contract,
            embedding_contract=meta.embedding_contract,
            indexed_at=meta.indexed_at,
            compatibility_evidence=ev,
            context_contract=meta.context_contract,
        )
        md = full_meta.to_dict()
        md["variant_key"] = vk
        cache_row = (
            namespace,
            scoped_fp,
            resp_text,
            "mock-qwen-27b",
            "mock",
            20,
            24,
            44,
            12.0,
            vis,
            owner,
            json.dumps({}),
        )
        sv_row = (
            namespace,
            scoped_fp,
            vk,
            req.messages[-1].content,
            FASTEMBED_MODEL,
            _vec_literal(vec),
            json.dumps(md),
            resp_text,
            now,
            vis,
            owner,
        )
        return cache_row, sv_row

    # Build base templates we can clone cheaply with distinct fingerprints/contexts
    cache_rows: List[Tuple[Any, ...]] = []
    sv_rows: List[Tuple[Any, ...]] = []

    # 1. Plant exact compatible targets
    # 1a. Independent shared target (rank behind high-similarity wrong-language/wrong-visibility distractors if >=35 distractors exist)
    indep_target_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=indep_seed_prompt)],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    indep_target_meta = ext.extract(indep_target_req, FASTEMBED_MODEL)
    c_r, sv_r = _build_signed_row(
        indep_target_req,
        "scale_target_indep",
        indep_target_resp,
        indep_seed_vec,
        "tenant_shared",
        None,
    )
    cache_rows.append(c_r)
    sv_rows.append(sv_r)

    # 1b. Context-dependent shared target slightly perturbed from dep_query_vec so closer wrong-context noise outranks without SQL prefilter
    dep_target_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[
            ChatMessage(role="user", content=dep_target_ctx),
            ChatMessage(role="user", content=dep_followup),
        ],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    dep_target_meta = ext.extract(dep_target_req, FASTEMBED_MODEL)
    dep_target_vec = _perturb_unit_vector(dep_query_vec, 1, eps=0.045)
    c_r, sv_r = _build_signed_row(
        dep_target_req,
        "scale_target_dep",
        dep_target_resp,
        dep_target_vec,
        "tenant_shared",
        None,
    )
    cache_rows.append(c_r)
    sv_rows.append(sv_r)

    # 2. Plant 35 closer wrong-context distractors (ranked 1..35 ahead of rank-36 dep_target in unfiltered vector search)
    distractor_count = min(35, max(10, total_rows // 3))
    dep_distractor_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[
            ChatMessage(role="user", content=dep_distractor_ctx),
            ChatMessage(role="user", content=dep_followup),
        ],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    c_tpl, sv_tpl = _build_signed_row(
        dep_distractor_req,
        "scale_dist_tpl",
        "```sql\nSELECT log_id FROM staging_logs LIMIT 10;\n```",
        dep_query_vec,
        "tenant_shared",
        None,
    )
    dist_meta_base = json.loads(sv_tpl[6])
    for idx in range(distractor_count):
        fp = f"scale_dist_{idx:05d}"
        scoped_fp = derive_scoped_exact_key(fp, "tenant_shared", None)
        d_vec = _perturb_unit_vector(dep_query_vec, idx + 10, eps=0.008)
        d_meta = dict(dist_meta_base)
        d_meta["context_contract"] = dict(dist_meta_base["context_contract"])
        d_meta["context_contract"]["context_fingerprint"] = f"ctx-v1:wrong_schema_{idx:05d}"
        d_vk = hashlib.sha256(f"{fp}:vk".encode()).hexdigest()[:64]
        d_meta["variant_key"] = d_vk
        cache_rows.append((namespace, scoped_fp, c_tpl[2], "mock-qwen-27b", "mock", 18, 20, 38, 10.0, "tenant_shared", None, "{}"))
        sv_rows.append((namespace, scoped_fp, d_vk, dep_followup, FASTEMBED_MODEL, _vec_literal(d_vec), json.dumps(d_meta), c_tpl[2], now, "tenant_shared", None))

    # 3. Fill remainder up to total_rows using realistic diverse multi-variant & multi-scope templates
    variant_templates_raw = [
        ("How do I format a JSON response in Python?", "tenant_shared", None, "json", "{\"ok\": true}"),
        ("How do I format a YAML response in Python?", "tenant_shared", None, None, "ok: true"),
        ("How do I write an async endpoint in FastAPI 0.110 using Python?", "tenant_shared", None, None, "```python\nasync def ok(): return True\n```"),
        ("How do I write a route in FastAPI 0.95 using Python?", "tenant_shared", None, None, "```python\ndef ok_095(): return True\n```"),
        ("Write a Go slice deduplication function", "tenant_shared", None, None, "```go\nfunc dedupe() {}\n```"),
        ("Write a Rust TOML config reader", "tenant_shared", None, None, "```rust\nfn read_cfg() {}\n```"),
        ("Write a React 18 effect hook in JavaScript", "tenant_shared", None, None, "```javascript\nfunction hook() {}\n```"),
        ("Explain binary tree balancing in Java", "private", "prin_alpha", None, "```java\nclass Tree {}\n```"),
        ("Write a private Python script helper", "private", "prin_beta", None, "```python\ndef priv(): pass\n```"),
    ]
    prepared_templates = []
    bg_anchor_count = max(1, len(anchor_vecs) - 3)
    for idx in range(bg_anchor_count):
        base_v = anchor_vecs[3 + idx]
        tpl_raw = variant_templates_raw[idx % len(variant_templates_raw)]
        prompt_txt, vis, own, out_fmt, resp_txt = tpl_raw
        req_t = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content=anchor_prompts[3 + idx])],
            temperature=0.0,
            namespace=namespace,
            visibility=vis,
            principal_id=own,
        )
        c_t, sv_t = _build_signed_row(req_t, f"tpl_{idx}", resp_txt, base_v, vis, own, out_fmt=out_fmt)
        prepared_templates.append((c_t, sv_t, base_v, json.loads(sv_t[6]), anchor_prompts[3 + idx], resp_txt, vis, own))

    remaining = max(0, total_rows - len(sv_rows))
    for i in range(remaining):
        tpl = prepared_templates[i % len(prepared_templates)]
        c_t, _, base_v, meta_t, q_txt, resp_txt, vis, own = tpl
        fp = f"scale_bg_{total_rows}_{i:06d}"
        scoped_fp = derive_scoped_exact_key(fp, vis, own)
        # Alternate CONTEXT_INDEPENDENT and distinct CONTEXT_DEPENDENT rows across background pool
        meta_copy = json.loads(json.dumps(meta_t))
        if i % 4 == 0:
            meta_copy["context_contract"]["context_class"] = "CONTEXT_DEPENDENT"
            meta_copy["context_contract"]["context_fingerprint"] = f"ctx-v1:bg_{i % 64:03d}"
        vk = hashlib.sha256(f"bg:{total_rows}:{i}".encode()).hexdigest()[:64]
        meta_copy["variant_key"] = vk
        vec_bg = _perturb_unit_vector(base_v, i + 100, eps=0.08 + 0.01 * (i % 7))
        cache_rows.append((namespace, scoped_fp, resp_txt, "mock-qwen-27b", "mock", 16, 20, 36, 11.0, vis, own, "{}"))
        sv_rows.append((namespace, scoped_fp, vk, f"{q_txt} [case {i}]", FASTEMBED_MODEL, _vec_literal(vec_bg), json.dumps(meta_copy), resp_txt, now, vis, own))

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO cache_entries (
                    cache_namespace, fingerprint, response, model, provider,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms,
                    visibility, owner_principal_id, metadata
                )
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12::jsonb)
                ON CONFLICT DO NOTHING;
                """,
                cache_rows,
            )
            await conn.executemany(
                """
                INSERT INTO semantic_variant_records (
                    cache_namespace, fingerprint, variant_key, query_text, model,
                    vector, metadata, cached_response, created_at, visibility, owner_principal_id
                )
                VALUES ($1, $2, $3, $4, $5, $6::vector, $7::jsonb, $8, $9, $10, $11)
                ON CONFLICT DO NOTHING;
                """,
                sv_rows,
            )
        await conn.execute("ANALYZE cache_entries;")
        await conn.execute("ANALYZE semantic_variant_records;")

    return {
        "seeded_rows": len(sv_rows),
        "distractors_planted": distractor_count,
        "indep_probe_vec": indep_probe_vec,
        "indep_target_fp": derive_scoped_exact_key("scale_target_indep", "tenant_shared", None),
        "dep_query_vec": dep_query_vec,
        "dep_target_fp": derive_scoped_exact_key("scale_target_dep", "tenant_shared", None),
        "dep_target_ctx_fp": dep_target_meta.context_contract.context_fingerprint,
        "indep_target_ctx_fp": indep_target_meta.context_contract.context_fingerprint,
    }


async def _measure_search_mode(
    vs: PostgresVectorStore,
    pool: asyncpg.Pool,
    *,
    query_vector: Sequence[float],
    namespace: str,
    model: str,
    visibility: str,
    top_k: int,
    context_class: Optional[str],
    context_fingerprint: Optional[str],
    expected_fp: str,
    trials: int = 15,
    iterative_scan_mode: Optional[str] = None,
) -> Dict[str, Any]:
    latencies: List[float] = []
    found_hits = 0
    budget_exhaustions = 0
    returned_counts: List[int] = []

    single_q, combined_q, scope_params, _ = vs._build_search_queries(
        visibility=visibility,
        principal_id=None,
        allow_shared_reads=False,
        context_class=context_class,
        context_fingerprint=context_fingerprint,
    )
    raw_sql = combined_q or single_q
    base_params: List[Any] = [_vec_literal(query_vector), namespace, model, top_k, *scope_params]
    sql_to_run, params = vs._specialize_low_selectivity_context_sql(
        raw_sql, base_params, context_class, context_fingerprint
    )

    for _ in range(trials):
        t0 = time.perf_counter()
        async with pool.acquire() as conn:
            if iterative_scan_mode:
                async with conn.transaction(readonly=True):
                    await conn.execute(f"SET LOCAL hnsw.iterative_scan = '{iterative_scan_mode}';")
                    rows = await conn.fetch(sql_to_run, *params)
            else:
                rows = await conn.fetch(sql_to_run, *params)
        lat_ms = (time.perf_counter() - t0) * 1000.0
        latencies.append(lat_ms)
        fps = [r["fingerprint"] for r in rows]
        returned_counts.append(len(rows))
        if expected_fp in fps:
            found_hits += 1
        elif len(rows) >= top_k:
            budget_exhaustions += 1

    async with pool.acquire() as conn:
        async with conn.transaction(readonly=True):
            if iterative_scan_mode:
                await conn.execute(f"SET LOCAL hnsw.iterative_scan = '{iterative_scan_mode}';")
            plan_raw = await conn.fetchval(f"EXPLAIN (ANALYZE, BUFFERS, FORMAT JSON)\n{sql_to_run}", *params)
    plan_info = _extract_plan_details(json.loads(plan_raw)[0])

    return {
        "trials": trials,
        "top_k_budget": top_k,
        "iterative_scan": iterative_scan_mode or "off",
        "recall_at_budget": round(found_hits / max(1, trials), 4),
        "target_found": found_hits > 0,
        "budget_exhaustion_rate": round(budget_exhaustions / max(1, trials), 4),
        "avg_candidates_returned": round(statistics.mean(returned_counts), 2) if returned_counts else 0.0,
        "latency": compute_full_percentiles(latencies),
        "explain": plan_info,
    }


async def run_phase5_scalability_evaluation(
    *,
    resolved_db_url: str,
    embedder: EmbeddingProvider,
    sizes: Sequence[int] = (100, 1000, 5000, 10000),
    trials_per_probe: int = 15,
) -> Dict[str, Any]:
    """
    Runs Phase 5 PostgreSQL + pgvector scalability benchmarks across specified entry counts
    plus (top_k, retrieval_budget) and HNSW iterative_scan comparisons.
    """
    pool = await asyncpg.create_pool(dsn=resolved_db_url, min_size=2, max_size=10)
    await run_migrations(pool)
    await run_vector_migrations(pool, dimensions=FASTEMBED_DIM)
    await reset_pg_tables(resolved_db_url)
    cache = PostgresCache(pool=pool, default_namespace="eval_s37_scale")
    vs = PostgresVectorStore(
        pool=pool,
        dimensions=FASTEMBED_DIM,
        default_namespace="eval_s37_scale",
        default_model=FASTEMBED_MODEL,
        readonly_transaction=True,
    )
    await vs.verify_schema()
    authority = EvidenceAuthority(STAGE_3_5_5_EVIDENCE_SECRET, allow_ephemeral=False)

    # Measure FastEmbed warm query embedding overhead independently
    await embedder.embed("Warmup query embedding", task=EmbeddingTask.QUERY)
    embed_lats: List[float] = []
    for i in range(trials_per_probe):
        t0 = time.perf_counter()
        await embedder.embed(f"What is the Python syntax to reverse a string cleanly? #{i}", task=EmbeddingTask.QUERY)
        embed_lats.append((time.perf_counter() - t0) * 1000.0)
    embed_overhead = compute_full_percentiles(embed_lats)

    scale_results: List[Dict[str, Any]] = []
    topk_budget_experiments: List[Dict[str, Any]] = []

    try:
        for target_size in sizes:
            ns = f"eval_s37_scale_{target_size}"
            await vs.clear(ns)
            await cache.clear(ns)
            seeded = await _seed_scalability_corpus(
                cache.pool,
                embedder,
                authority,
                namespace=ns,
                total_rows=int(target_size),
            )

            # Compare unfiltered vs context-prefiltered on CONTEXT_DEPENDENT starvation workload
            unfiltered_dep = await _measure_search_mode(
                vs,
                cache.pool,
                query_vector=seeded["dep_query_vec"],
                namespace=ns,
                model=FASTEMBED_MODEL,
                visibility="tenant_shared",
                top_k=25,
                context_class=None,
                context_fingerprint=None,
                expected_fp=seeded["dep_target_fp"],
                trials=trials_per_probe,
            )
            prefiltered_dep = await _measure_search_mode(
                vs,
                cache.pool,
                query_vector=seeded["dep_query_vec"],
                namespace=ns,
                model=FASTEMBED_MODEL,
                visibility="tenant_shared",
                top_k=25,
                context_class=ContextClass.CONTEXT_DEPENDENT.value,
                context_fingerprint=seeded["dep_target_ctx_fp"],
                expected_fp=seeded["dep_target_fp"],
                trials=trials_per_probe,
            )
            prefiltered_indep = await _measure_search_mode(
                vs,
                cache.pool,
                query_vector=seeded["indep_probe_vec"],
                namespace=ns,
                model=FASTEMBED_MODEL,
                visibility="tenant_shared",
                top_k=25,
                context_class=ContextClass.CONTEXT_INDEPENDENT.value,
                context_fingerprint=seeded["indep_target_ctx_fp"],
                expected_fp=seeded["indep_target_fp"],
                trials=trials_per_probe,
            )

            scale_results.append(
                {
                    "dataset_size": int(seeded["seeded_rows"]),
                    "distractors_planted": int(seeded["distractors_planted"]),
                    "unfiltered_context_dependent": unfiltered_dep,
                    "prefiltered_context_dependent": prefiltered_dep,
                    "prefiltered_context_independent": prefiltered_indep,
                }
            )

            # Experimental top-k / retrieval_budget & HNSW comparison at largest & 1000-entry tiers
            if target_size in (min(max(sizes), 1000), max(sizes)):
                for budget_cfg, iter_mode in ((5, None), (10, None), (25, None), (50, None), (25, "strict_order")):
                    exp_res = await _measure_search_mode(
                        vs,
                        cache.pool,
                        query_vector=seeded["indep_probe_vec"],
                        namespace=ns,
                        model=FASTEMBED_MODEL,
                        visibility="tenant_shared",
                        top_k=budget_cfg,
                        context_class=ContextClass.CONTEXT_INDEPENDENT.value,
                        context_fingerprint=seeded["indep_target_ctx_fp"],
                        expected_fp=seeded["indep_target_fp"],
                        trials=trials_per_probe,
                        iterative_scan_mode=iter_mode,
                    )
                    topk_budget_experiments.append(
                        {
                            "dataset_size": int(seeded["seeded_rows"]),
                            "retrieval_budget": budget_cfg,
                            "iterative_scan": iter_mode or "off",
                            "recall_at_budget": exp_res["recall_at_budget"],
                            "budget_exhaustion_rate": exp_res["budget_exhaustion_rate"],
                            "latency_p50_ms": exp_res["latency"]["p50_ms"],
                            "latency_p95_ms": exp_res["latency"]["p95_ms"],
                            "explain_indexes": exp_res["explain"]["indexes_used"],
                            "hnsw_used": exp_res["explain"]["hnsw_used"],
                            "context_index_used": exp_res["explain"]["context_index_used"],
                        }
                    )

            await vs.clear(ns)
            await cache.clear(ns)

        async with cache.pool.acquire() as conn:
            idx_rows = await conn.fetch(
                """
                SELECT tablename, indexname
                FROM pg_indexes
                WHERE schemaname = 'public'
                  AND tablename IN ('vector_records', 'semantic_variant_records')
                ORDER BY tablename, indexname;
                """
            )
            pg_ver = await conn.fetchval("SELECT version();")
    finally:
        await vs.close()
        await cache.close()
        await pool.close()

    return {
        "database_url_redacted": redact_database_url(resolved_db_url),
        "postgres_version": str(pg_ver),
        "embedding_overhead_ms": embed_overhead,
        "indexes_present": [f"{r['tablename']}.{r['indexname']}" for r in idx_rows],
        "scales": scale_results,
        "topk_budget_and_hnsw_experiments": topk_budget_experiments,
    }


async def run_phase6_performance_comparison(
    *,
    resolved_db_url: str,
    build_env_fn: Callable[..., Dict[str, str]],
    iterations: int = 20,
    synthetic_upstream_delay_ms: float = 120.0,
) -> Dict[str, Any]:
    """
    Executes controlled A/B/C latency comparison over HTTP POST /query with PG 16 + FastEmbed:
    A) Exact caching only
    B) Exact + Semantic caching enabled
    C) Upstream-only baseline
    """
    exact_prompt = "How do I encode and decode JSON strings cleanly in Python?"
    # Mixed realistic stream: 1 seed + repeated exact + 4 distinct paraphrase requests repeated across turns
    workload_prompts = [
        ("How do I reverse a string in Python?", "What is the Python syntax to reverse a string?"),
        ("How do I sort a list in Python?", "How to sort a Python list?"),
        ("How do I merge two dictionaries in Python?", "How to merge two dicts in Python?"),
        ("How do I read lines from a file in Python?", "How to read a file line by line in Python?"),
    ]
    provider = ControlledEvalProvider(delay_seconds=synthetic_upstream_delay_ms / 1000.0, tag="S37_PERF")

    async def _run_mode(
        mode_name: str,
        *,
        cache_enabled: bool,
        semantic_enabled: bool,
    ) -> Dict[str, Any]:
        await reset_pg_tables(resolved_db_url)
        get_metrics_collector().reset()
        reset_semantic_serving_controller(semantic_enabled)
        provider.calls.clear()
        env = build_env_fn(
            resolved_db_url,
            cache_enabled=cache_enabled,
            semantic_enabled=semantic_enabled,
            semantic_serving_enabled=semantic_enabled,
        )
        from unittest.mock import patch as _patch
        import os as _os

        latencies: List[float] = []
        exact_hits = 0
        semantic_hits = 0
        errors = 0

        with _patch.dict(_os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: provider
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    # Seed exact & base semantic topics first (warmup outside measured steady-state window)
                    seed_prompts = [exact_prompt, *[pair[0] for pair in workload_prompts]]
                    for sp in seed_prompts:
                        r_seed = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_carol"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": sp}], "temperature": 0.0},
                        )
                        if r_seed.status_code != 200:
                            errors += 1
                    provider.calls.clear()
                    # Measured steady-state workload: mix exact repeats + paraphrase requests
                    for idx in range(iterations):
                        if idx % 3 == 0:
                            q = exact_prompt
                        else:
                            q = workload_prompts[idx % len(workload_prompts)][1]
                        t0 = time.perf_counter()
                        r = await client.post(
                            "/query",
                            headers={"X-Client-Token": "tok_dave"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": q}], "temperature": 0.0},
                        )
                        latencies.append((time.perf_counter() - t0) * 1000.0)
                        if r.status_code != 200:
                            errors += 1
                            continue
                        body = r.json()
                        if body.get("cached"):
                            if body.get("cache_type") == "exact":
                                exact_hits += 1
                            elif body.get("cache_type") == "semantic":
                                semantic_hits += 1

                    metrics_resp = await client.get("/admin/metrics", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN})
                    snap = metrics_resp.json()["namespaces"].get("team_beta", {})
                    lat_snap = snap.get("latencies", {})

        total_hits = exact_hits + semantic_hits
        calls_made = len(provider.calls)
        return {
            "mode": mode_name,
            "measured_requests": iterations,
            "exact_hits": exact_hits,
            "semantic_hits": semantic_hits,
            "cache_hit_ratio": round(total_hits / max(1, iterations), 4),
            "upstream_calls_made": calls_made,
            "upstream_calls_avoided": max(0, iterations - calls_made),
            "error_rate": round(errors / max(1, iterations), 4),
            "fallback_rate": round(calls_made / max(1, iterations), 4),
            "end_to_end_latency_ms": compute_full_percentiles(latencies),
            "component_latencies_ms": {
                "exact_lookup": lat_snap.get("exact_lookup_latency_ms", {}),
                "semantic_lookup": lat_snap.get("semantic_lookup_latency_ms", {}),
                "embedding": lat_snap.get("embedding_latency_ms", {}),
                "vector_search": lat_snap.get("vector_search_latency_ms", {}),
                "compatibility_validation": lat_snap.get("compatibility_validation_latency_ms", {}),
                "provider_generation": lat_snap.get("provider_generation_latency_ms", {}),
            },
        }

    mode_a = await _run_mode("A_exact_only", cache_enabled=True, semantic_enabled=False)
    mode_b = await _run_mode("B_exact_plus_semantic", cache_enabled=True, semantic_enabled=True)
    mode_c = await _run_mode("C_upstream_only_baseline", cache_enabled=False, semantic_enabled=False)

    return {
        "measurement_type": "controlled_mock_provider_with_real_fastembed_and_postgres",
        "synthetic_upstream_delay_ms": synthetic_upstream_delay_ms,
        "real_external_provider_tested": False,
        "iterations_per_mode": iterations,
        "modes": {
            "A_exact_only": mode_a,
            "B_exact_plus_semantic": mode_b,
            "C_upstream_only": mode_c,
        },
        "additional_experiment_required": (
            "Measure with LLM_PROVIDER=groq against real external inference endpoints before claiming production upstream latency reduction."
        ),
    }
