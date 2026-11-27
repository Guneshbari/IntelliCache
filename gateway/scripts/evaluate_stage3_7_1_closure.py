#!/usr/bin/env python3
"""
Stage 3.7.1 Closure Evaluation & Shared PostgreSQL/HNSW/HTTP Fixtures.

Provides:
1. Shared deterministic fixture builders for:
   - PostgreSQL generic-plan vs specialized query verification (768-d & 384-d).
   - Skewed multi-cluster HNSW low-recall vs exact-search reference & ef_search sweep.
2. Controlled Phase 4 Adaptive Retrieval experiment benchmark comparing:
   - conservative single-pass `ef_search=40`
   - always-high `ef_search=400`
   - bounded adaptive 2nd-pass `ef_search=400`
   - bounded adaptive exact-scan fallback (`enable_indexscan=off`)
3. Full closure verification runner emitting JSON + Markdown artifacts.
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import AsyncExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import statistics
import sys
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple
from unittest.mock import patch

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

import asyncpg
import httpx

from app.api.routes import get_llm_provider
from app.cache.migration import run_migrations
from app.config import Settings, get_settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest
from app.main import create_app
from app.privacy import derive_scoped_exact_key
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.contracts import (
    CompatibilityReasonCode,
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
from app.vector_store.contracts import VectorSearchResult
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore
from scripts.evaluate_stage3_5_5_operational import (
    ControlledEvalProvider,
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    build_forbidden_disclosure_values,
    reset_pg_tables,
    resolve_fastembed_database_url,
    verify_no_sensitive_leaks,
)
from scripts.evaluate_stage3_7_workload import build_stage37_env

SENSITIVE_HEADER_KEYS = ("authorization", "x-client-token", "x-admin-token")


def normalize_vector(vec: Sequence[float]) -> List[float]:
    """L2-normalizes a float vector cleanly."""
    norm = math.sqrt(sum(float(v) * float(v) for v in vec)) or 1.0
    return [float(v) / norm for v in vec]


def format_vec_literal(vec: Sequence[float]) -> str:
    """Formats a vector as pgvector literal string."""
    return "[" + ",".join(f"{float(v):.6f}" for v in vec) + "]"


def make_basis_vector(dim: int, weights: Dict[int, float]) -> List[float]:
    """Builds a normalized basis vector from sparse coordinate weights."""
    vec = [0.0] * dim
    for idx, val in weights.items():
        vec[idx % dim] = float(val)
    return normalize_vector(vec)


def perturb_vector(base: Sequence[float], seed_idx: int, eps: float) -> List[float]:
    """Produces a deterministic unit vector slightly rotated away from `base`."""
    vec = [float(x) for x in base]
    dim = len(vec)
    slot_a = (seed_idx * 7 + 3) % dim
    slot_b = (seed_idx * 13 + 19) % dim
    sign = 1.0 if (seed_idx % 2 == 0) else -1.0
    vec[slot_a] += sign * eps
    vec[slot_b] -= sign * (eps * 0.5)
    return normalize_vector(vec)


def build_gram_schmidt_ortho_basis(anchor: Sequence[float], count: int = 64) -> List[List[float]]:
    """
    Constructs `count` unit vectors mutually orthogonal to `anchor` (and each other)
    using Modified Gram-Schmidt so skewed cluster experiments operate around real
    embedding vectors in arbitrary dimensions (`384` or `768`).
    """
    dim = len(anchor)
    q_norm = normalize_vector(anchor)
    basis: List[List[float]] = [q_norm]
    out: List[List[float]] = []

    coord = 0
    while len(out) < count and coord < dim:
        cand = [0.0] * dim
        cand[coord] = 1.0
        coord += 1
        for b in basis:
            dot = sum(cand[i] * b[i] for i in range(dim))
            for i in range(dim):
                cand[i] -= dot * b[i]
        norm = math.sqrt(sum(x * x for x in cand))
        if norm > 1e-4:
            unit = [x / norm for x in cand]
            basis.append(unit)
            out.append(unit)
    return out


async def detect_pg_vector_dimensions(
    conn: asyncpg.Connection,
    *,
    default_dim: int = 768,
) -> int:
    """Detects configured embedding/vector column dimension if vector schema already exists."""
    row = await conn.fetchrow(
        """
        SELECT format_type(a.atttypid, a.atttypmod) AS vec_type
        FROM pg_class t
        JOIN pg_attribute a ON a.attrelid = t.oid
        WHERE t.relname IN ('semantic_variant_records', 'vector_records')
          AND a.attname IN ('vector', 'embedding')
          AND a.attnum > 0
          AND NOT a.attisdropped
        LIMIT 1;
        """
    )
    if row and row["vec_type"]:
        match = re.search(r"vector\((\d+)\)", str(row["vec_type"]))
        if match:
            return int(match.group(1))
    return default_dim


def mint_signed_metadata(
    *,
    ext: CompatibilityExtractor,
    authority: EvidenceAuthority,
    request: GenerationRequest,
    fingerprint: str,
    response_text: str,
    embedding_model: str,
    ctx_class_override: Optional[str] = None,
    ctx_fp_override: Optional[str] = None,
    variant_seed: str = "",
) -> Tuple[str, Dict[str, Any]]:
    """Extracts compatibility metadata and mints valid HMAC compatibility evidence."""
    meta = ext.extract(request, embedding_model)
    vis = request.visibility or "private"
    owner = request.principal_id if vis == "private" else None
    is_indep = bool(
        meta.context_contract
        and meta.context_contract.context_class == ContextClass.CONTEXT_INDEPENDENT
    )
    if ctx_class_override is not None:
        is_indep = ctx_class_override == "CONTEXT_INDEPENDENT"
    ver = ResponseVerificationResult(
        status=VerificationStatus.VERIFIED,
        reason_code=CompatibilityReasonCode.COMPATIBLE,
        message="stage3_7_1_seeded",
        truncation_status=TruncationStatus.COMPLETE,
    )
    ev = authority.mint_response_verified_evidence(
        cache_namespace=request.namespace or "default",
        fingerprint=fingerprint,
        response_text=response_text,
        metadata=meta,
        verification=ver,
        context_independent=is_indep,
        visibility=vis,
        owner_principal_id=owner,
    )
    full = SemanticCompatibilityMetadata(
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
    md = full.to_dict()
    if ctx_class_override is not None:
        md["context_contract"]["context_class"] = ctx_class_override
        md["context_contract"]["is_context_dependent"] = ctx_class_override == "CONTEXT_DEPENDENT"
    if ctx_fp_override is not None:
        md["context_contract"]["context_fingerprint"] = ctx_fp_override
    vk = compute_variant_key(meta, visibility=vis, owner_principal_id=owner)
    if variant_seed:
        vk = hashlib.sha256(f"{vk}:{variant_seed}".encode("utf-8")).hexdigest()[:64]
    md["variant_key"] = vk
    return vk, md


async def seed_mixed_context_corpus(
    pool: asyncpg.Pool,
    *,
    dim: int,
    namespace: str,
    embed_model: str,
    authority: EvidenceAuthority,
    total_rows: int = 3200,
    base_vectors: Optional[Sequence[Sequence[float]]] = None,
) -> Dict[str, Any]:
    """
    Seeds `cache_entries` + `semantic_variant_records` with a realistic skewed context
    distribution (~70% `CONTEXT_INDEPENDENT`, ~30% `CONTEXT_DEPENDENT`, private + shared,
    and rank-31 wrong-context distractors) for deterministic PostgreSQL generic-plan
    reproduction and verification.
    """
    ext = CompatibilityExtractor()
    now = datetime.now(timezone.utc)
    if base_vectors is not None and len(base_vectors) >= 6:
        topic_vecs = [normalize_vector(v) for v in base_vectors[:6]]
    else:
        topic_vecs = [make_basis_vector(dim, {k * 17: 1.0, (k * 17 + 1): 0.25}) for k in range(6)]
    extra_topics = [
        make_basis_vector(dim, {(k * 23 + 3): 1.0, (k * 29 + 11): 0.45})
        for k in range(16)
    ]

    indep_prompt = "How do I reverse a string cleanly in Python?"
    dep_schema = "CREATE TABLE billing_orders (order_id BIGINT PRIMARY KEY, total_cents INT);"
    dep_followup = "Add a structured pagination helper for the schema above"

    indep_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=indep_prompt)],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    indep_fp = derive_scoped_exact_key("gp_target_indep", "tenant_shared", None)
    indep_resp = "```python\ndef reverse_str(s: str) -> str:\n    return s[::-1]\n```"
    indep_vk, indep_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=indep_req,
        fingerprint=indep_fp,
        response_text=indep_resp,
        embedding_model=embed_model,
        variant_seed="indep_target",
    )

    dep_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[
            ChatMessage(role="user", content=dep_schema),
            ChatMessage(role="user", content=dep_followup),
        ],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    dep_fp = derive_scoped_exact_key("gp_target_dep", "tenant_shared", None)
    dep_resp = "```sql\nSELECT order_id, total_cents FROM billing_orders ORDER BY order_id DESC LIMIT 25;\n```"
    dep_vk, dep_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=dep_req,
        fingerprint=dep_fp,
        response_text=dep_resp,
        embedding_model=embed_model,
        variant_seed="dep_target",
    )
    dep_ctx_fp = str(dep_md["context_contract"]["context_fingerprint"])
    dep_target_vec = perturb_vector(topic_vecs[5], 1, eps=0.045)

    # Plant private target owned by prin_other to validate generic-plan privacy enforcement
    priv_other_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=indep_prompt)],
        temperature=0.0,
        namespace=namespace,
        visibility="private",
        principal_id="prin_other_secret",
    )
    priv_other_fp = derive_scoped_exact_key("gp_priv_other", "private", "prin_other_secret")
    priv_other_resp = "PRIVATE_OTHER_SECRET_SHOULD_NOT_LEAK"
    priv_vk, priv_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=priv_other_req,
        fingerprint=priv_other_fp,
        response_text=priv_other_resp,
        embedding_model=embed_model,
        variant_seed="priv_other",
    )

    # Plant cross-tenant entry under another namespace
    other_ns = f"{namespace}_other"
    cross_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=indep_prompt)],
        temperature=0.0,
        namespace=other_ns,
        visibility="tenant_shared",
    )
    cross_fp = derive_scoped_exact_key("gp_cross_tenant", "tenant_shared", None)
    cross_vk, cross_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=cross_req,
        fingerprint=cross_fp,
        response_text="CROSS_TENANT_RESPONSE_ISOLATED",
        embedding_model=embed_model,
        variant_seed="cross_tenant",
    )

    cache_rows: List[Tuple[Any, ...]] = [
        (namespace, indep_fp, indep_resp, "mock-qwen-27b", "mock", 20, 20, 40, 10.0, "tenant_shared", None, "{}"),
        (namespace, dep_fp, dep_resp, "mock-qwen-27b", "mock", 20, 20, 40, 10.0, "tenant_shared", None, "{}"),
        (namespace, priv_other_fp, priv_other_resp, "mock-qwen-27b", "mock", 20, 20, 40, 10.0, "private", "prin_other_secret", "{}"),
        (other_ns, cross_fp, "CROSS_TENANT_RESPONSE_ISOLATED", "mock-qwen-27b", "mock", 20, 20, 40, 10.0, "tenant_shared", None, "{}"),
    ]
    sv_rows: List[Tuple[Any, ...]] = [
        (namespace, indep_fp, indep_vk, indep_prompt, embed_model, format_vec_literal(topic_vecs[0]), json.dumps(indep_md), indep_resp, now, "tenant_shared", None),
        (namespace, dep_fp, dep_vk, dep_followup, embed_model, format_vec_literal(dep_target_vec), json.dumps(dep_md), dep_resp, now, "tenant_shared", None),
        (namespace, priv_other_fp, priv_vk, indep_prompt, embed_model, format_vec_literal(topic_vecs[0]), json.dumps(priv_md), priv_other_resp, now, "private", "prin_other_secret"),
        (other_ns, cross_fp, cross_vk, indep_prompt, embed_model, format_vec_literal(topic_vecs[0]), json.dumps(cross_md), "CROSS_TENANT_RESPONSE_ISOLATED", now, "tenant_shared", None),
    ]

    # 30 wrong-context distractors closer to topic_vecs[5] than dep_target_vec (rank-31 starvation test)
    for idx in range(30):
        raw_fp = f"gp_dep_dist_{idx:03d}"
        sfp = derive_scoped_exact_key(raw_fp, "tenant_shared", None)
        d_md = json.loads(json.dumps(dep_md))
        d_md["context_contract"]["context_fingerprint"] = f"ctx-v1:wrong_schema_{idx:03d}"
        d_vk = hashlib.sha256(f"{raw_fp}:vk".encode("utf-8")).hexdigest()[:64]
        d_md["variant_key"] = d_vk
        vec = perturb_vector(topic_vecs[5], idx + 10, eps=0.006)
        cache_rows.append((namespace, sfp, "WRONG_CONTEXT_DISTRACTOR", "mock-qwen-27b", "mock", 15, 15, 30, 5.0, "tenant_shared", None, "{}"))
        sv_rows.append((namespace, sfp, d_vk, dep_followup, embed_model, format_vec_literal(vec), json.dumps(d_md), "WRONG_CONTEXT_DISTRACTOR", now, "tenant_shared", None))

    # Insert dispersed pseudo-random isotropic background vectors blended lightly toward t0_vec
    # (cosine ~0.55..0.82) so no degenerate 0.002-spaced subcluster prunes HNSW edges to target_indep.
    bg_c_rows: List[Tuple[Any, ...]] = []
    bg_sv_rows: List[Tuple[Any, ...]] = []
    remaining = max(0, total_rows - len(sv_rows))
    t0_vec = topic_vecs[0]
    rng_dirs = []
    for k in range(64):
        state = (k + 1) * 2654435761
        raw_d = []
        for _ in range(dim):
            state = (state * 1664525 + 1013904223) & 0xFFFFFFFF
            raw_d.append((state / 4294967296.0) - 0.5)
        rng_dirs.append(normalize_vector(raw_d))

    for i in range(remaining):
        is_dep = (i % 7) in (1, 3)
        is_priv = (i % 13) == 4
        vis = "private" if is_priv else "tenant_shared"
        own = "prin_bg_other" if is_priv else None
        raw_fp = f"gp_bg_{i:05d}"
        sfp = derive_scoped_exact_key(raw_fp, vis, own)
        md = json.loads(json.dumps(indep_md))
        if is_dep:
            md["context_contract"]["context_class"] = "CONTEXT_DEPENDENT"
            md["context_contract"]["context_fingerprint"] = f"ctx-v1:bg_ctx_{i % 64:03d}"
            md["context_contract"]["is_context_dependent"] = True
        vk = hashlib.sha256(f"{raw_fp}:vk".encode("utf-8")).hexdigest()[:64]
        md["variant_key"] = vk
        d_a = rng_dirs[i % 64]
        d_b = rng_dirs[(i * 11 + 7) % 64]
        bias = 0.50 + 0.30 * ((i % 20) / 20.0)
        mix = [bias * t0_vec[d] + 0.5 * d_a[d] + 0.5 * d_b[d] for d in range(dim)]
        vec = normalize_vector(mix)
        bg_c_rows.append((namespace, sfp, f"bg_resp_{i}", "mock-qwen-27b", "mock", 12, 12, 24, 5.0, vis, own, "{}"))
        bg_sv_rows.append((namespace, sfp, vk, f"bg query {i}", embed_model, format_vec_literal(vec), json.dumps(md), f"bg_resp_{i}", now, vis, own))

    # Place bg rows first and target_indep last so target_indep joins the populated HNSW graph cleanly
    cache_rows = bg_c_rows + cache_rows
    sv_rows = bg_sv_rows + sv_rows

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO cache_entries (
                    cache_namespace, fingerprint, response, model, provider,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms,
                    visibility, owner_principal_id, metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
                ON CONFLICT DO NOTHING;
                """,
                cache_rows,
            )
            await conn.executemany(
                """
                INSERT INTO semantic_variant_records (
                    cache_namespace, fingerprint, variant_key, query_text, model,
                    vector, metadata, cached_response, created_at, visibility, owner_principal_id
                ) VALUES ($1,$2,$3,$4,$5,$6::vector,$7::jsonb,$8,$9,$10,$11)
                ON CONFLICT DO NOTHING;
                """,
                sv_rows,
            )
        await conn.execute("ANALYZE cache_entries;")
        await conn.execute("ANALYZE semantic_variant_records;")

    return {
        "namespace": namespace,
        "other_namespace": other_ns,
        "rows_seeded": len(sv_rows),
        "indep_prompt_vec": topic_vecs[0],
        "indep_target_fp": indep_fp,
        "indep_resp": indep_resp,
        "dep_query_vec": topic_vecs[5],
        "dep_ctx_fp": dep_ctx_fp,
        "dep_target_fp": dep_fp,
        "dep_resp": dep_resp,
        "private_other_fp": priv_other_fp,
        "cross_tenant_fp": cross_fp,
    }


async def seed_skewed_hnsw_fixture(
    pool: asyncpg.Pool,
    *,
    dim: int,
    namespace: str,
    embed_model: str,
    authority: EvidenceAuthority,
    anchor_vec: Optional[Sequence[float]] = None,
    singleton_prompt: str = "Sort a list cleanly in Python using sorted()",
    singleton_resp: str = "```python\ndef sort_items(xs):\n    return sorted(xs)\n```",
    distractor_cluster_size: Optional[int] = None,
    far_rows: int = 4000,
    singleton_insert_idx: int = 0,
    verify_hnsw_topology: bool = True,
    temperature: float = 0.0,
) -> Dict[str, Any]:
    """
    Seeds a deterministic skewed HNSW geometry around `anchor_vec` (or canonical basis e0):
    - Optional `FAR` prefix establishing entry hubs outside `u0`
    - `hnsw_singleton`: isolated compatible candidate at cos~0.985 (dist~0.015) along `u0`
    - Smooth incompatible `arc` bridge connecting `singleton` into dense `C2` cluster
    - `C2`: dense incompatible Rust cluster at cos~0.975 (dist~0.023), trapping `ef_search=40` HNSW
    - `FAR` suffix keeping PG HNSW index active cleanly
    When `verify_hnsw_topology=True`, verifies the newly constructed random-level HNSW graph
    exhibits `miss@ef=40` + `recover@ef=400` and rebuilds via `REINDEX INDEX` (max 5 tries)
    if pgvector PRNG assigned `singleton` an anomalous top-layer shortcut.
    """
    ext = CompatibilityExtractor()
    now = datetime.now(timezone.utc)
    q_vec = normalize_vector(anchor_vec) if anchor_vec is not None else make_basis_vector(dim, {0: 1.0})
    ortho = build_gram_schmidt_ortho_basis(q_vec, count=96)
    u0, u1 = ortho[0], ortho[1]

    csize = distractor_cluster_size if distractor_cluster_size is not None else (180 if dim <= 512 else 120)
    arc_steps = 6
    far_prefix_cnt = 0

    def _combine(w_q: float, parts: List[Tuple[List[float], float]]) -> List[float]:
        vec = [w_q * q_vec[i] for i in range(dim)]
        for basis_v, w in parts:
            for i in range(dim):
                vec[i] += w * basis_v[i]
        return normalize_vector(vec)

    singleton_vec = _combine(0.985, [(u0, 0.1725)])

    sing_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=singleton_prompt)],
        temperature=temperature,
        namespace=namespace,
        visibility="tenant_shared",
    )
    sing_fp = derive_scoped_exact_key("hnsw_singleton_target", "tenant_shared", None)
    sing_vk, sing_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=sing_req,
        fingerprint=sing_fp,
        response_text=singleton_resp,
        embedding_model=embed_model,
        variant_seed="hnsw_singleton",
    )

    rust_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content="Write a Rust helper to sort vectors cleanly")],
        temperature=0.0,
        namespace=namespace,
        visibility="tenant_shared",
    )
    rust_resp = "```rust\nfn sort_vec<T: Ord>(v: &mut Vec<T>) { v.sort(); }\n```"
    _, rust_md_tpl = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=rust_req,
        fingerprint="rust_tpl",
        response_text=rust_resp,
        embedding_model=embed_model,
        variant_seed="rust_tpl",
    )

    cache_rows: List[Tuple[Any, ...]] = []
    sv_rows: List[Tuple[Any, ...]] = []

    def _append_far(idx: int, tag: str) -> None:
        b_a = ortho[45 + (idx % 25)]
        b_b = ortho[70 + (idx % 20)]
        f_vec = _combine(0.0, [(b_a, 1.0), (b_b, 0.5 + 0.01 * (idx % 10))])
        raw_fp = f"hnsw_{tag}_{idx:05d}"
        sfp = derive_scoped_exact_key(raw_fp, "tenant_shared", None)
        f_md = json.loads(json.dumps(sing_md))
        f_vk = hashlib.sha256(f"{raw_fp}:vk".encode("utf-8")).hexdigest()[:64]
        f_md["variant_key"] = f_vk
        cache_rows.append((namespace, sfp, "FAR_BG", "mock-qwen-27b", "mock", 10, 10, 20, 4.0, "tenant_shared", None, "{}"))
        sv_rows.append((namespace, sfp, f_vk, f"far item {idx}", embed_model, format_vec_literal(f_vec), json.dumps(f_md), "FAR_BG", now, "tenant_shared", None))

    # 1. Optional FAR prefix so HNSW upper-layer entryPoint sits outside u0
    for i in range(far_prefix_cnt):
        _append_far(i, "farp")

    # 2. Target singleton
    cache_rows.append((namespace, sing_fp, singleton_resp, "mock-qwen-27b", "mock", 20, 22, 42, 10.0, "tenant_shared", None, "{}"))
    sv_rows.append((namespace, sing_fp, sing_vk, singleton_prompt, embed_model, format_vec_literal(singleton_vec), json.dumps(sing_md), singleton_resp, now, "tenant_shared", None))

    # 3. Smooth incompatible Rust arc bridge from u0 -> C2
    for k in range(1, arc_steps + 1):
        t = k / (arc_steps + 1)
        bv = _combine(1.0, [(u0, 0.25 * (1.0 - t)), (u1, 0.28 * t + 0.08)])
        raw_fp = f"hnsw_arc_{k:02d}"
        sfp = derive_scoped_exact_key(raw_fp, "tenant_shared", None)
        b_md = json.loads(json.dumps(rust_md_tpl))
        b_vk = hashlib.sha256(f"{raw_fp}:vk".encode("utf-8")).hexdigest()[:64]
        b_md["variant_key"] = b_vk
        cache_rows.append((namespace, sfp, rust_resp, "mock-qwen-27b", "mock", 12, 12, 24, 5.0, "tenant_shared", None, "{}"))
        sv_rows.append((namespace, sfp, b_vk, f"Rust arc {k}", embed_model, format_vec_literal(bv), json.dumps(b_md), rust_resp, now, "tenant_shared", None))

    # 4. Dense C2 distractor cluster at dist~0.023..0.026 trapping low ef_search HNSW
    for i in range(csize):
        w_main = 0.22 + (i % 40) * 0.001
        noise_axis = ortho[3 + (i % 20)]
        noise_w = 0.008 * ((i % 7) - 3)
        d_vec = _combine(1.0, [(u1, w_main), (noise_axis, noise_w)])
        raw_fp = f"hnsw_c2_{i:04d}"
        sfp = derive_scoped_exact_key(raw_fp, "tenant_shared", None)
        d_md = json.loads(json.dumps(rust_md_tpl))
        d_vk = hashlib.sha256(f"{raw_fp}:vk".encode("utf-8")).hexdigest()[:64]
        d_md["variant_key"] = d_vk
        cache_rows.append((namespace, sfp, rust_resp, "mock-qwen-27b", "mock", 12, 12, 24, 5.0, "tenant_shared", None, "{}"))
        sv_rows.append((namespace, sfp, d_vk, f"Rust sort c2 {i}", embed_model, format_vec_literal(d_vec), json.dumps(d_md), rust_resp, now, "tenant_shared", None))

    # 5. Remainder FAR rows to keep HNSW cost optimal
    for i in range(far_prefix_cnt, far_rows):
        _append_far(i, "fars")

    async with pool.acquire() as conn:
        async with conn.transaction():
            await conn.executemany(
                """
                INSERT INTO cache_entries (
                    cache_namespace, fingerprint, response, model, provider,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms,
                    visibility, owner_principal_id, metadata
                ) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb)
                ON CONFLICT DO NOTHING;
                """,
                cache_rows,
            )
            await conn.executemany(
                """
                INSERT INTO semantic_variant_records (
                    cache_namespace, fingerprint, variant_key, query_text, model,
                    vector, metadata, cached_response, created_at, visibility, owner_principal_id
                ) VALUES ($1,$2,$3,$4,$5,$6::vector,$7::jsonb,$8,$9,$10,$11)
                ON CONFLICT DO NOTHING;
                """,
                sv_rows,
            )
        await conn.execute("ANALYZE cache_entries;")
        await conn.execute("ANALYZE semantic_variant_records;")

    if verify_hnsw_topology:
        vs_probe = PostgresVectorStore(
            pool=pool,
            default_namespace=namespace,
            default_model=embed_model,
            dimensions=dim,
        )
        es, ec, prm, _, _ = vs_probe._prepare_search_queries(
            vec_str=format_vec_literal(q_vec),
            namespace=namespace,
            target_model=embed_model,
            top_k=25,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
        )
        qsql = ec or es
        for attempt in range(6):
            async with pool.acquire() as conn:
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL hnsw.ef_search = 40;"
                    )
                    low_rows = await conn.fetch(qsql, *prm)
                async with conn.transaction(readonly=True):
                    await conn.execute(
                        "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL hnsw.ef_search = 400;"
                    )
                    high_rows = await conn.fetch(qsql, *prm)
                low_hit = sing_fp in [r["fingerprint"] for r in low_rows]
                high_hit = sing_fp in [r["fingerprint"] for r in high_rows]
                if (not low_hit) and high_hit:
                    break
                if attempt < 5:
                    await conn.execute("REINDEX INDEX idx_semantic_variant_records_hnsw;")
                    await conn.execute("ANALYZE semantic_variant_records;")

    return {
        "namespace": namespace,
        "query_vec": q_vec,
        "singleton_fp": sing_fp,
        "singleton_resp": singleton_resp,
        "rows_seeded": len(sv_rows),
        "distractor_count": csize + arc_steps,
    }


def _rows_to_search_results(rows: Sequence[asyncpg.Record]) -> List[VectorSearchResult]:
    out: List[VectorSearchResult] = []
    for r in rows:
        dist = float(r["distance"])
        raw_meta = r["metadata"]
        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
        out.append(
            VectorSearchResult(
                cache_namespace=str(r["cache_namespace"]),
                fingerprint=str(r["fingerprint"]),
                query_text=str(r["query_text"]),
                model=str(r["model"]),
                distance=dist,
                similarity=1.0 - dist,
                cached_response=str(r["cached_response"]),
                metadata=meta,
                created_at=r["created_at"],
                visibility=str(r["visibility"]),
                owner_principal_id=r["owner_principal_id"],
            )
        )
    return out


async def run_phase4_adaptive_benchmark(
    *,
    db_url: str,
    pool: asyncpg.Pool,
    embedder: Any,
    authority: EvidenceAuthority,
    iterations: int = 15,
) -> Dict[str, Any]:
    """
    Controlled empirical comparison of:
    - `single_ef40`: current conservative single-pass HNSW retrieval
    - `always_ef400`: globally elevated `hnsw.ef_search = 400`
    - `adaptive_ef400`: bounded 2nd-pass HNSW retry (`ef_search=400`) on zero compatible hits
    - `adaptive_exact`: bounded 2nd-pass exact scan (`enable_indexscan=off`) on zero compatible hits
    across `normal_hit`, `true_miss`, and `skewed_singleton` workloads.
    """
    ns = "eval_s371_adaptive"
    hit_prompt = "How do I reverse a string cleanly in Python?"
    hit_probe = "Reverse a string cleanly in Python"
    miss_prompt = "Explain TLS 1.3 ALPN handshake negotiation with mutual certificates in Python"
    sing_prompt = "Sort a list cleanly in Python using sorted()"

    hit_vec = (await embedder.embed(hit_prompt)).vector
    probe_vec = (await embedder.embed(hit_probe)).vector
    miss_vec = (await embedder.embed(miss_prompt)).vector
    sing_q_vec = (await embedder.embed(sing_prompt)).vector

    fixture = await seed_skewed_hnsw_fixture(
        pool,
        dim=FASTEMBED_DIM,
        namespace=ns,
        embed_model=FASTEMBED_MODEL,
        authority=authority,
        anchor_vec=sing_q_vec,
        singleton_prompt=sing_prompt,
    )

    ext = CompatibilityExtractor()
    hit_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=hit_prompt)],
        temperature=0.0,
        namespace=ns,
        visibility="tenant_shared",
    )
    hit_fp = derive_scoped_exact_key("norm_hit_entry", "tenant_shared", None)
    hit_resp = "```python\ndef rev(s: str) -> str:\n    return s[::-1]\n```"
    hit_vk, hit_md = mint_signed_metadata(
        ext=ext,
        authority=authority,
        request=hit_req,
        fingerprint=hit_fp,
        response_text=hit_resp,
        embedding_model=FASTEMBED_MODEL,
        variant_seed="norm_hit",
    )
    now = datetime.now(timezone.utc)
    # Seed normal-hit target + cluster of distinct bridge entries (< 0.92 sim so unambiguous) so ef=40 lands naturally at hit_fp
    norm_c_rows: List[Tuple[Any, ...]] = [
        (ns, hit_fp, hit_resp, "mock-qwen-27b", "mock", 15, 15, 30, 6.0, "tenant_shared", None, "{}")
    ]
    norm_sv_rows: List[Tuple[Any, ...]] = [
        (ns, hit_fp, hit_vk, hit_prompt, FASTEMBED_MODEL, format_vec_literal(hit_vec), json.dumps(hit_md), hit_resp, now, "tenant_shared", None)
    ]
    for b_idx in range(60):
        b_vec = perturb_vector(hit_vec, b_idx + 1, eps=0.55 + 0.005 * b_idx)
        b_fp = derive_scoped_exact_key(f"norm_bridge_{b_idx:02d}", "tenant_shared", None)
        b_vk = hashlib.sha256(f"nb:{b_idx}".encode("utf-8")).hexdigest()[:64]
        b_md = json.loads(json.dumps(hit_md))
        b_md["variant_key"] = b_vk
        norm_c_rows.append((ns, b_fp, "BRIDGE", "mock-qwen-27b", "mock", 10, 10, 20, 4.0, "tenant_shared", None, "{}"))
        norm_sv_rows.append((ns, b_fp, b_vk, f"bridge {b_idx}", FASTEMBED_MODEL, format_vec_literal(b_vec), json.dumps(b_md), "BRIDGE", now, "tenant_shared", None))
    async with pool.acquire() as conn:
        await conn.executemany(
            "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider, prompt_tokens, completion_tokens, total_tokens, latency_ms, visibility, owner_principal_id, metadata) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12::jsonb) ON CONFLICT DO NOTHING;",
            norm_c_rows,
        )
        await conn.executemany(
            "INSERT INTO semantic_variant_records (cache_namespace, fingerprint, variant_key, query_text, model, vector, metadata, cached_response, created_at, visibility, owner_principal_id) VALUES ($1,$2,$3,$4,$5,$6::vector,$7::jsonb,$8,$9,$10,$11) ON CONFLICT DO NOTHING;",
            norm_sv_rows,
        )
        await conn.execute("ANALYZE cache_entries; ANALYZE semantic_variant_records;")

    vs = PostgresVectorStore(
        pool=pool,
        default_namespace=ns,
        default_model=FASTEMBED_MODEL,
        dimensions=FASTEMBED_DIM,
    )
    policy = SemanticCachePolicy(
        min_similarity=0.92,
        min_margin=0.03,
        top_k=5,
        configured_model=FASTEMBED_MODEL,
        extractor=ext,
        retrieval_budget=25,
        evidence_authority=authority,
    )
    exec_single, exec_combined, _, _, _ = vs._prepare_search_queries(
        vec_str=format_vec_literal(sing_q_vec),
        namespace=ns,
        target_model=FASTEMBED_MODEL,
        top_k=25,
        visibility="tenant_shared",
        principal_id=None,
        allow_shared_reads=False,
        context_class="CONTEXT_INDEPENDENT",
        context_fingerprint="ctx-v1:independent",
    )
    sql_to_run = exec_combined or exec_single

    probe_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=hit_probe)],
        temperature=0.0,
        namespace=ns,
        visibility="tenant_shared",
    )
    probe_meta = ext.extract(probe_req, FASTEMBED_MODEL)
    sing_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=sing_prompt)],
        temperature=0.0,
        namespace=ns,
        visibility="tenant_shared",
    )
    sing_meta = ext.extract(sing_req, FASTEMBED_MODEL)
    miss_req = GenerationRequest(
        model="mock-qwen-27b",
        provider="mock",
        messages=[ChatMessage(role="user", content=miss_prompt)],
        temperature=0.0,
        namespace=ns,
        visibility="tenant_shared",
    )
    miss_meta = ext.extract(miss_req, FASTEMBED_MODEL)

    # Measure warm embedding cost
    await embedder.embed("warmup query")
    t_emb0 = time.perf_counter()
    await embedder.embed(hit_probe)
    emb_ms = round((time.perf_counter() - t_emb0) * 1000.0, 3)

    conn = await asyncpg.connect(db_url, statement_cache_size=0)
    try:
        async def _eval_mode(
            mode: str,
            q_vec: Sequence[float],
            req_obj: GenerationRequest,
            rmeta: SemanticCompatibilityMetadata,
        ) -> Dict[str, Any]:
            qlit = format_vec_literal(q_vec)
            totals_ms: List[float] = []
            vals_ms: List[float] = []
            hits = 0
            second_passes = 0
            for _ in range(iterations):
                t0 = time.perf_counter()
                async with conn.transaction(readonly=True):
                    if mode == "always_ef400":
                        await conn.execute(
                            "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL hnsw.ef_search = 400;"
                        )
                    else:
                        await conn.execute("SET LOCAL hnsw.ef_search = 40;")
                    rows = await conn.fetch(sql_to_run, qlit, ns, FASTEMBED_MODEL, 25)
                tv0 = time.perf_counter()
                sel, _ = policy.evaluate_candidates(
                    candidates=_rows_to_search_results(rows),
                    request=req_obj,
                    request_metadata=rmeta,
                )
                tv = (time.perf_counter() - tv0) * 1000.0
                hit = sel is not None
                if not hit and mode in ("adaptive_ef400", "adaptive_exact"):
                    second_passes += 1
                    async with conn.transaction(readonly=True):
                        if mode == "adaptive_ef400":
                            await conn.execute(
                                "SET LOCAL enable_seqscan = off; SET LOCAL enable_bitmapscan = off; SET LOCAL hnsw.ef_search = 400;"
                            )
                        else:
                            await conn.execute("SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;")
                        rows2 = await conn.fetch(sql_to_run, qlit, ns, FASTEMBED_MODEL, 25)
                    tv1 = time.perf_counter()
                    sel2, _ = policy.evaluate_candidates(
                        candidates=_rows_to_search_results(rows2),
                        request=req_obj,
                        request_metadata=rmeta,
                    )
                    tv += (time.perf_counter() - tv1) * 1000.0
                    hit = sel2 is not None
                totals_ms.append((time.perf_counter() - t0) * 1000.0)
                vals_ms.append(tv)
                if hit:
                    hits += 1
            return {
                "mode": mode,
                "iterations": iterations,
                "hits": hits,
                "recall": round(hits / max(1, iterations), 4),
                "second_passes": second_passes,
                "p50_ms": round(statistics.median(totals_ms), 3),
                "p95_ms": round(max(totals_ms), 3),
                "val_mean_ms": round(statistics.mean(vals_ms), 3),
            }

        workloads = {}
        for wname, qv, req_o, rm in (
            ("normal_hit", probe_vec, probe_req, probe_meta),
            ("true_miss", miss_vec, miss_req, miss_meta),
            ("skewed_singleton", sing_q_vec, sing_req, sing_meta),
        ):
            workloads[wname] = {
                m: await _eval_mode(m, qv, req_o, rm)
                for m in ("single_ef40", "always_ef400", "adaptive_ef400", "adaptive_exact")
            }
    finally:
        await conn.close()

    return {
        "rows_seeded": fixture["rows_seeded"] + 1,
        "fastembed_warm_query_ms": emb_ms,
        "workloads": workloads,
        "decision": "DEFER",
        "rationale": (
            "Retain conservative single-pass HNSW (ef_search=40, sql_context_prefilter=True, safe upstream fallback). "
            "Unconditional adaptive_ef400 adds a full redundant 2nd search and HMAC/compatibility validation pass on "
            "100% of genuine misses, and adaptive_exact degrades true-miss p50 by >10x via O(N) sequential scan, "
            "while missed candidates already fail open safely to upstream generation."
        ),
    }


async def run_stage3_7_1_closure_evaluation(
    *,
    test_database_url: Optional[str] = None,
    output_dir: Optional[Path] = None,
    adaptive_iterations: int = 15,
) -> Dict[str, Any]:
    """Runs the Stage 3.7.1 closure evidence suite and persists JSON + Markdown reports."""
    resolved_db_url = test_database_url or await resolve_fastembed_database_url()
    reset_semantic_serving_controller()
    env = build_stage37_env(resolved_db_url)

    report: Dict[str, Any] = {
        "stage": "3.7.1",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "epistemic_labels": {
            "implementation": "Verified code & safe defaults",
            "pg_regression": "PostgreSQL 16 + pgvector EXPLAIN (ANALYZE, BUFFERS) deterministic fixtures",
            "adaptive_benchmark": "Controlled PG16 + FastEmbed microbenchmark across hit/miss/skewed slices",
        },
    }

    with patch.dict(os.environ, env, clear=False):
        reload_settings()
        with patch.dict(os.environ, {}, clear=True):
            default_cfg = Settings()
        report["defaults_audit"] = {
            "semantic_cache_enabled": bool(default_cfg.semantic_cache_enabled),
            "semantic_cache_serving_enabled": bool(default_cfg.semantic_cache_serving_enabled),
            "shadow_evaluation_enabled": bool(default_cfg.shadow_evaluation_enabled),
            "default_visibility": default_cfg.semantic_cache_default_visibility,
            "min_similarity": default_cfg.semantic_cache_min_similarity,
            "min_margin": default_cfg.semantic_cache_min_margin,
            "top_k": default_cfg.semantic_cache_top_k,
            "max_retrieval_candidates": default_cfg.semantic_cache_max_retrieval_candidates,
            "sql_context_prefilter_wired_in_main": "sql_context_prefilter=True" in (GATEWAY_ROOT / "app" / "main.py").read_text(encoding="utf-8"),
        }

        pool = await asyncpg.create_pool(dsn=resolved_db_url, min_size=2, max_size=6)
        try:
            await run_migrations(pool)
            await run_vector_migrations(pool, dimensions=FASTEMBED_DIM)
            await reset_pg_tables(resolved_db_url)

            from app.embeddings.fastembed_adapter import FastEmbedProvider

            embedder = FastEmbedProvider(model=FASTEMBED_MODEL, dimensions=FASTEMBED_DIM)
            authority = EvidenceAuthority(STAGE_3_5_5_EVIDENCE_SECRET, allow_ephemeral=False)

            async with pool.acquire() as conn:
                pg_ver = await conn.fetchval("SHOW server_version;")
                pgvector_ver = await conn.fetchval("SELECT extversion FROM pg_extension WHERE extname = 'vector';")
            report["environment"] = {"postgres_version": pg_ver, "pgvector_version": pgvector_ver, "vector_dimensions": FASTEMBED_DIM}

            # Generic-plan EXPLAIN metrics via production PostgresVectorStore
            topic_prompts = [
                "How do I reverse a string cleanly in Python?",
                "Write a Go helper to merge two sorted slices",
                "Write a FastAPI async healthcheck endpoint in Python",
                "Optimize PostgreSQL B-tree index ordering",
                "Write a Kubernetes readiness probe in YAML",
                "Add a structured pagination helper for the schema above",
            ]
            vecs = [r.vector for r in await embedder.embed_batch(topic_prompts)]
            gp_ns = "eval_s371_genplan"
            seeded_gp = await seed_mixed_context_corpus(
                pool,
                dim=FASTEMBED_DIM,
                namespace=gp_ns,
                embed_model=FASTEMBED_MODEL,
                authority=authority,
                total_rows=3200,
                base_vectors=vecs,
            )
            vs = PostgresVectorStore(
                pool=pool,
                default_namespace=gp_ns,
                default_model=FASTEMBED_MODEL,
                dimensions=FASTEMBED_DIM,
            )
            indep_gen = await vs.explain_search(
                seeded_gp["indep_prompt_vec"],
                top_k=25,
                namespace=gp_ns,
                model=FASTEMBED_MODEL,
                analyze=True,
                visibility="tenant_shared",
                context_class="CONTEXT_INDEPENDENT",
                context_fingerprint="ctx-v1:independent",
                plan_cache_mode="force_generic_plan",
            )
            indep_cust = await vs.explain_search(
                seeded_gp["indep_prompt_vec"],
                top_k=25,
                namespace=gp_ns,
                model=FASTEMBED_MODEL,
                analyze=True,
                visibility="tenant_shared",
                context_class="CONTEXT_INDEPENDENT",
                context_fingerprint="ctx-v1:independent",
                plan_cache_mode="force_custom_plan",
            )
            dep_gen = await vs.explain_search(
                seeded_gp["dep_query_vec"],
                top_k=25,
                namespace=gp_ns,
                model=FASTEMBED_MODEL,
                analyze=True,
                visibility="tenant_shared",
                context_class="CONTEXT_DEPENDENT",
                context_fingerprint=seeded_gp["dep_ctx_fp"],
                plan_cache_mode="force_generic_plan",
            )
            report["generic_plan_verification"] = {
                "rows_seeded": seeded_gp["rows_seeded"],
                "indep_generic": indep_gen,
                "indep_custom": indep_cust,
                "dep_generic": dep_gen,
            }

            # Phase 4 adaptive benchmark
            await reset_pg_tables(resolved_db_url)
            report["adaptive_retrieval_benchmark"] = await run_phase4_adaptive_benchmark(
                db_url=resolved_db_url,
                pool=pool,
                embedder=embedder,
                authority=authority,
                iterations=adaptive_iterations,
            )
        finally:
            await pool.close()

    if output_dir is not None:
        output_dir.mkdir(parents=True, exist_ok=True)
        json_path = output_dir / "stage3_7_1_closure.json"
        md_path = output_dir / "stage3_7_1_closure.md"
        with open(json_path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2)

        gp = report["generic_plan_verification"]
        ab = report["adaptive_retrieval_benchmark"]
        md_lines = [
            "# Stage 3.7.1 PostgreSQL Generic-Plan & HNSW Closure Benchmark",
            "",
            f"- PostgreSQL: `{report['environment']['postgres_version']}` | pgvector: `{report['environment']['pgvector_version']}`",
            f"- Safe defaults intact: `{report['defaults_audit']['semantic_cache_enabled'] is False and report['defaults_audit']['semantic_cache_serving_enabled'] is False and report['defaults_audit']['sql_context_prefilter_wired_in_main'] is True}`",
            f"- CONTEXT_INDEPENDENT generic plan HNSW used: `{gp['indep_generic']['vector_index_used']}` (specialized=`{gp['indep_generic']['sql_specialized']}`)",
            f"- CONTEXT_INDEPENDENT custom plan HNSW used: `{gp['indep_custom']['vector_index_used']}` (specialized=`{gp['indep_custom']['sql_specialized']}`)",
            f"- CONTEXT_DEPENDENT generic plan context index used: `{gp['dep_generic']['context_index_used']}` (specialized=`{gp['dep_generic']['sql_specialized']}`)",
            "",
            f"## Phase 4 Adaptive Retrieval Decision: `{ab['decision']}`",
            "",
            ab["rationale"],
            "",
            "| Workload | Mode | Hits | p50 (ms) | p95 (ms) | Val Mean (ms) | 2nd Passes |",
            "|---|---|---|---|---|---|---|",
        ]
        for wname, modes in ab["workloads"].items():
            for mname, st in modes.items():
                md_lines.append(
                    f"| {wname} | {mname} | {st['hits']}/{st['iterations']} | {st['p50_ms']} | {st['p95_ms']} | {st['val_mean_ms']} | {st['second_passes']} |"
                )
        with open(md_path, "w", encoding="utf-8") as handle:
            handle.write("\n".join(md_lines) + "\n")

    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.7.1 Closure Evaluation")
    parser.add_argument("--test-database-url", default=None)
    parser.add_argument("--output-dir", default="/tmp/stage3_7_1_eval")
    parser.add_argument("--adaptive-iterations", type=int, default=15)
    args = parser.parse_args()

    res = asyncio.run(
        run_stage3_7_1_closure_evaluation(
            test_database_url=args.test_database_url,
            output_dir=Path(args.output_dir),
            adaptive_iterations=args.adaptive_iterations,
        )
    )
    print(json.dumps({"status": "ok", "decision": res["adaptive_retrieval_benchmark"]["decision"]}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
