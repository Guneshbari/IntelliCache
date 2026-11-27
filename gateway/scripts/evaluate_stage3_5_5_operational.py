#!/usr/bin/env python3
"""
Stage 3.5.5 Local-First Semantic Serving Operational Evaluation Runner.

Executes end-to-end HTTP evaluation of IntelliCache's real semantic-serving request
path (`POST /query`, `/admin/semantic-cache/serving`, `/cache/semantic/stats`,
`/metrics/me`, `/admin/metrics`, `/cache/clear`) backed by real PostgreSQL + pgvector
and real FastEmbed (`BAAI/bge-small-en-v1.5`, 384-d) using a deterministic mock
upstream LLM provider (zero external LLM keys required).

Usage:
    cd gateway
    TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test" \\
        python scripts/evaluate_stage3_5_5_operational.py --iterations 25
"""

from __future__ import annotations

import argparse
import asyncio
from contextlib import nullcontext
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sys
import time
from typing import Any, Awaitable, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlsplit, urlunsplit
from unittest.mock import patch

import asyncpg
import httpx

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.api.routes import (  # noqa: E402
    get_llm_provider,
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from app.config import reload_settings  # noqa: E402
from app.conversations.identity import derive_principal_id  # noqa: E402
from app.core.contracts import GenerationRequest, GenerationResult, StreamChunk, UsageMetrics  # noqa: E402
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask  # noqa: E402
from app.embeddings.errors import EmbeddingTimeoutError  # noqa: E402
from app.embeddings.interfaces import EmbeddingProvider  # noqa: E402
from app.main import create_app  # noqa: E402
from app.metrics import get_metrics_collector  # noqa: E402
from app.providers.interfaces import LLMProvider  # noqa: E402
from app.semantic_cache.control import (  # noqa: E402
    get_semantic_serving_controller,
    reset_semantic_serving_controller,
)
from app.vector_store.errors import VectorStoreError  # noqa: E402
from scripts.evaluate_semantic_cache import redact_database_url, validate_test_database_url  # noqa: E402


STAGE_3_5_5_IDENTITY_SECRET = "stage355-operational-identity-secret-hex-9f2c71a4"
STAGE_3_5_5_EVIDENCE_SECRET = "stage355-operational-evidence-secret-hex-4a8e19d0"
STAGE_3_5_5_ADMIN_TOKEN = "stage355-admin-token"
FASTEMBED_MODEL = "BAAI/bge-small-en-v1.5"
FASTEMBED_DIM = 384

DEFAULT_CLIENT_TOKENS: Dict[str, Dict[str, Any]] = {
    "tok_alice": {"namespace": "team_alpha", "visibility": "private", "allow_shared_reads": False},
    "tok_bob": {"namespace": "team_alpha", "visibility": "private", "allow_shared_reads": False},
    "tok_carol": {"namespace": "team_beta", "visibility": "tenant_shared", "allow_shared_reads": True},
    "tok_dave": {"namespace": "team_beta", "visibility": "tenant_shared", "allow_shared_reads": True},
    "tok_erin": {"namespace": "team_beta", "visibility": "private", "allow_shared_reads": True},
    "tok_fay": {"namespace": "team_beta", "visibility": "private", "allow_shared_reads": False},
    "tok_frank": {"namespace": "team_gamma", "visibility": "tenant_shared", "allow_shared_reads": True},
    "tok_sf_a": {"namespace": "team_sf", "visibility": "private", "allow_shared_reads": False},
    "tok_sf_b": {"namespace": "team_sf", "visibility": "private", "allow_shared_reads": False},
    "tok_ctrl": {"namespace": "team_ctrl", "visibility": "private", "allow_shared_reads": False},
    "tok_bench": {"namespace": "team_bench", "visibility": "private", "allow_shared_reads": False},
}

FORBIDDEN_INTERNAL_RESPONSE_KEYS = frozenset(
    {
        "principal_id",
        "owner_principal_id",
        "evidence_signature",
        "client_token",
        "identity_secret",
        "evidence_secret",
    }
)


class ControlledEvalProvider(LLMProvider):
    """
    Deterministic upstream mock provider for controlled HTTP gateway evaluation.

    Generates valid code or strict JSON artifacts matching each prompt so
    ``ResponseArtifactVerifier`` admits legitimate responses into the cache, while
    optionally applying deterministic artificial latency or fault injection.
    """

    def __init__(
        self,
        *,
        delay_seconds: float = 0.0,
        tag: str = "UPSTREAM",
        response_map: Optional[Dict[str, str]] = None,
    ) -> None:
        self.delay_seconds = max(0.0, float(delay_seconds))
        self.tag = tag
        self.response_map: Dict[str, str] = dict(response_map or {})
        self.calls: List[Tuple[str, str]] = []
        self.fail_next_error: Optional[Exception] = None

    def set_delay_ms(self, delay_ms: float) -> None:
        """Sets simulated upstream inference latency in milliseconds."""
        self.delay_seconds = max(0.0, float(delay_ms) / 1000.0)

    def _build_answer(self, prompt: str) -> str:
        if prompt in self.response_map:
            return self.response_map[prompt]
        lower = prompt.lower()
        if "json" in lower and "markdown" not in lower:
            if "email" in lower:
                return json.dumps({"id": 1, "email": "alice@example.com", "tag": self.tag})
            if "name" in lower:
                return json.dumps({"id": 1, "name": "alice", "tag": self.tag})
            return json.dumps({"status": "ok", "tag": self.tag})
        if "markdown" in lower:
            return f"```python\n# Markdown response ({self.tag})\ndef serialize_md(data: dict) -> str:\n    return str(data)\n```"
        if "java" in lower:
            return f"```java\n// {self.tag}\nimport java.util.*;\npublic static <T extends Comparable<? super T>> List<T> sortList(List<T> items) {{ Collections.sort(items); return items; }}\n```"
        if "javascript" in lower or "react" in lower:
            return f"```javascript\n// {self.tag}\nimport {{ useEffect, useState }} from 'react';\nexport function useData(url) {{ const [d, setD] = useState(null); useEffect(() => {{ fetch(url).then(r => r.json()).then(setD); }}, [url]); return d; }}\n```"
        return f"```python\n# {self.tag} response for: {prompt[:64]}\ndef answer() -> str:\n    return 'ok'\n```"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        prompt = request.messages[-1].content if request.messages else ""
        self.calls.append((request.namespace, prompt))
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_next_error is not None:
            err = self.fail_next_error
            self.fail_next_error = None
            raise err
        text = self._build_answer(prompt)
        prompt_tokens = max(12, len(prompt.split()) * 2 + 6)
        completion_tokens = max(24, len(text.split()) * 2 + 12)
        return GenerationResult(
            text=text,
            model=request.model,
            usage=UsageMetrics(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=prompt_tokens + completion_tokens,
            ),
            latency_ms=round(max(self.delay_seconds * 1000.0, 2.5), 2),
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        res = await self.generate(request)
        yield StreamChunk(delta=res.text, done=False, model=request.model)
        yield StreamChunk(
            delta="",
            done=True,
            model=request.model,
            usage=res.usage,
            latency_ms=res.latency_ms,
        )

    async def close(self) -> None:
        return None


class TopicDeterministicEmbedder(EmbeddingProvider):
    """
    Lightweight deterministic topic-cluster embedder for offline unit tests only.
    Never used when verifying real FastEmbed inference.
    """

    def __init__(self, *, dimensions: int = FASTEMBED_DIM, model_name: str = FASTEMBED_MODEL) -> None:
        self._dim = dimensions
        self._model = model_name
        self.embed_calls = 0

    @property
    def provider_name(self) -> str:
        return "deterministic_topic"

    @property
    def default_model(self) -> str:
        return self._model

    @property
    def expected_dimensions(self) -> int:
        return self._dim

    def _angle_for_prompt(self, text: str) -> float:
        lower = text.lower()
        if "reverse" in lower and "string" in lower:
            return 0.05
        if "sort" in lower and "list" in lower:
            return 0.65
        if "merge" in lower and "dict" in lower:
            return 1.15
        if "serialize" in lower or ("json" in lower and "dict" in lower):
            return 1.65
        if "useeffect" in lower or "react" in lower:
            return 2.15
        if "file exists" in lower:
            if "os.path" in lower:
                return 2.64
            if "pathlib" in lower:
                return 2.66
            return 2.65
        if "sha-256" in lower or "sha256" in lower:
            return 3.05
        if "asyncpg" in lower or "postgresql" in lower:
            return 3.55
        # Unique benchmark/other text gets distinct orthogonal-ish angles
        stable_hash = sum((idx + 1) * ord(ch) for idx, ch in enumerate(lower))
        return 4.0 + ((stable_hash % 1000) / 1000.0) * 1.9

    async def embed(
        self,
        text: str,
        task: EmbeddingTask = EmbeddingTask.QUERY,
        model: Optional[str] = None,
    ) -> EmbeddingResult:
        self.embed_calls += 1
        angle = self._angle_for_prompt(text)
        vec = [0.0] * self._dim
        vec[0] = math.cos(angle)
        vec[1] = math.sin(angle)
        return EmbeddingResult(
            vector=vec,
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


def build_response_map_from_dataset(dataset: Dict[str, Any]) -> Dict[str, str]:
    """Maps dataset seed and query prompts to verified canonical response artifacts."""
    mapping: Dict[str, str] = {}
    for entry in dataset.get("positive", []):
        mapping[entry["seed_prompt"]] = entry["seed_response"]
    for entry in dataset.get("negative", []):
        if "seed_prompt" in entry and "seed_response" in entry:
            mapping[entry["seed_prompt"]] = entry["seed_response"]
        if "query_prompt" in entry and "query_response" in entry:
            mapping[entry["query_prompt"]] = entry["query_response"]
        for cand in entry.get("seed_candidates", []):
            mapping[cand["prompt"]] = cand["response"]
    return mapping


def load_stage355_dataset(path: Optional[Path] = None) -> Dict[str, Any]:
    """Loads the Stage 3.5.5 operational evaluation dataset."""
    target = path or (GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_5_5_operational_dataset.json")
    with open(target, "r", encoding="utf-8") as handle:
        return json.load(handle)


async def resolve_fastembed_database_url() -> str:
    """
    Resolves a live PostgreSQL database URL suitable for 384-d FastEmbed evaluation.

    Uses ``TEST_FASTEMBED_DATABASE_URL`` when set, otherwise uses ``TEST_DATABASE_URL``
    (auto-creating isolated ``intellicache_fastembed_test`` when pointing to a 768-d DB).
    """
    raw_url = os.environ.get("TEST_FASTEMBED_DATABASE_URL")
    if not raw_url:
        base_url = os.environ.get("TEST_DATABASE_URL")
        if not base_url:
            raise RuntimeError(
                "TEST_FASTEMBED_DATABASE_URL or TEST_DATABASE_URL must be set for PostgreSQL evaluation."
            )
        parsed = urlsplit(base_url)
        db_name = (parsed.path or "").lstrip("/")
        if "fastembed" not in db_name.lower():
            admin_url = urlunsplit((parsed.scheme, parsed.netloc, "/postgres", "", ""))
            admin_conn = await asyncpg.connect(admin_url)
            try:
                exists = await admin_conn.fetchval(
                    "SELECT 1 FROM pg_database WHERE datname = $1",
                    "intellicache_fastembed_test",
                )
                if not exists:
                    await admin_conn.execute("CREATE DATABASE intellicache_fastembed_test")
            finally:
                await admin_conn.close()
            raw_url = urlunsplit(
                (parsed.scheme, parsed.netloc, "/intellicache_fastembed_test", parsed.query, parsed.fragment)
            )
        else:
            raw_url = base_url
    return validate_test_database_url(raw_url)


async def reset_pg_tables(db_url: str, *, include_runtime_control: bool = False) -> None:
    """Truncates cache, vector, variant, and conversation tables (and optionally shared runtime control)."""
    tables = [
        "conversation_messages",
        "conversation_turns",
        "conversations",
        "shadow_evaluations",
        "semantic_variant_records",
        "vector_records",
        "cache_entries",
    ]
    if include_runtime_control:
        tables = ["semantic_runtime_namespace_overrides", "semantic_runtime_control", *tables]

    conn = await asyncpg.connect(db_url)
    try:
        for table in tables:
            exists = await conn.fetchval(
                """
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = 'public' AND table_name = $1
                """,
                table,
            )
            if exists:
                await conn.execute(f"TRUNCATE TABLE {table} CASCADE")
    finally:
        await conn.close()


def build_forbidden_disclosure_values(
    tokens: Sequence[str],
    identity_secret: str,
    *,
    extra_secrets: Sequence[str] = (),
) -> Tuple[List[str], List[str]]:
    """
    Computes derived principal identifiers using the production HMAC helper
    together with raw credential tokens and secret strings for non-disclosure checks.
    """
    derived_principals = [derive_principal_id(tok, identity_secret) for tok in tokens]
    forbidden_substrings: List[str] = [
        identity_secret,
        *extra_secrets,
        *FORBIDDEN_INTERNAL_RESPONSE_KEYS,
        *tokens,
        *derived_principals,
    ]
    return derived_principals, forbidden_substrings


def _find_forbidden_keys(obj: Any, path: str = "$") -> Optional[str]:
    if isinstance(obj, dict):
        for key, value in obj.items():
            key_lower = str(key).strip().lower()
            if key_lower in FORBIDDEN_INTERNAL_RESPONSE_KEYS:
                return f"{path}.{key}"
            nested = _find_forbidden_keys(value, f"{path}.{key}")
            if nested is not None:
                return nested
    elif isinstance(obj, list):
        for idx, item in enumerate(obj):
            nested = _find_forbidden_keys(item, f"{path}[{idx}]")
            if nested is not None:
                return nested
    return None


def verify_no_sensitive_leaks(
    payload: Any,
    forbidden_substrings: Sequence[str],
    context: str,
    *,
    check_forbidden_keys: bool = True,
) -> None:
    """Asserts no token, derived principal ID, secret, signature, or internal identity key leaks."""
    if check_forbidden_keys:
        forbidden_key_path = _find_forbidden_keys(payload)
        if forbidden_key_path is not None:
            raise AssertionError(
                f"Forbidden internal identity/signature key '{forbidden_key_path}' exposed during {context}"
            )
    serialized = payload if isinstance(payload, str) else json.dumps(payload, sort_keys=True)
    for forbidden in forbidden_substrings:
        if forbidden and forbidden in serialized:
            raise AssertionError(
                f"Sensitive value leaked in {context}: matched redacted length={len(forbidden)}"
            )


def compute_percentiles(values: Sequence[float]) -> Dict[str, Any]:
    """Computes summary latency statistics including count, mean, min, max, p50, and p95."""
    if not values:
        return {"count": 0, "mean_ms": 0.0, "min_ms": 0.0, "max_ms": 0.0, "p50_ms": 0.0, "p95_ms": 0.0}
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
        "mean_ms": round(sum(ordered) / len(ordered), 3),
        "min_ms": round(ordered[0], 3),
        "max_ms": round(ordered[-1], 3),
        "p50_ms": round(_pct(50.0), 3),
        "p95_ms": round(_pct(95.0), 3),
    }


def _query_payload(prompt: str, *, temperature: float = 0.0, model: str = "mock-qwen-27b") -> Dict[str, Any]:
    return {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": temperature,
    }


def _hdr(token: str) -> Dict[str, str]:
    return {"X-Client-Token": token}


def _admin_hdr() -> Dict[str, str]:
    return {"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}


async def _raw_post_query(
    client: httpx.AsyncClient,
    token: str,
    prompt: str,
) -> Tuple[httpx.Response, float]:
    t0 = time.perf_counter()
    resp = await client.post("/query", headers=_hdr(token), json=_query_payload(prompt))
    elapsed_ms = (time.perf_counter() - t0) * 1000.0
    return resp, elapsed_ms


def _assert(cond: bool, msg: str) -> None:
    if not cond:
        raise AssertionError(msg)


@dataclass
class ScenarioOutcome:
    """Recorded pass/fail verification status for an individual evaluation scenario."""

    scenario_id: str
    passed: bool
    summary: str
    details: Dict[str, Any] = field(default_factory=dict)


async def run_operational_evaluation(
    *,
    dataset: Optional[Dict[str, Any]] = None,
    use_postgres: bool = True,
    db_url: Optional[str] = None,
    use_real_fastembed: bool = True,
    benchmark_iterations: int = 25,
    simulated_upstream_delay_ms: float = 120.0,
) -> Dict[str, Any]:
    """
    Runs all Stage 3.5.5 HTTP semantic-serving scenarios (A-J, privacy, SingleFlight,
    tampering/unknown-visibility DB isolation) and latency/savings benchmarks.
    """
    ds = dataset or load_stage355_dataset()
    response_map = build_response_map_from_dataset(ds)
    provider = ControlledEvalProvider(delay_seconds=0.0, tag="HTTP_EVAL", response_map=response_map)

    saved_cache = get_query_cache()
    saved_sem = get_semantic_cache()
    saved_vs = get_vector_store()

    if use_postgres:
        resolved_db_url = db_url or await resolve_fastembed_database_url()
        await reset_pg_tables(resolved_db_url)
    else:
        resolved_db_url = None

    env_vars: Dict[str, str] = {
        "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
        "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
        "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
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
    }
    if resolved_db_url:
        env_vars["DATABASE_URL"] = resolved_db_url

    derived_principals, forbidden_secrets = build_forbidden_disclosure_values(
        list(DEFAULT_CLIENT_TOKENS.keys()),
        STAGE_3_5_5_IDENTITY_SECRET,
        extra_secrets=[STAGE_3_5_5_EVIDENCE_SECRET, STAGE_3_5_5_ADMIN_TOKEN],
    )

    outcomes: List[ScenarioOutcome] = []
    benchmark_report: Dict[str, Any] = {}

    get_metrics_collector().reset()
    if resolved_db_url:
        await reset_pg_tables(resolved_db_url, include_runtime_control=True)
    reset_semantic_serving_controller(False)

    patch_ctx: Any
    topic_embedder: Optional[TopicDeterministicEmbedder] = None
    with patch.dict(os.environ, env_vars, clear=False):
        reload_settings()
        if not use_real_fastembed:
            topic_embedder = TopicDeterministicEmbedder()
            patch_ctx = patch("app.main.EmbeddingProviderFactory.create", return_value=topic_embedder)
        else:
            patch_ctx = nullcontext()

        test_app = create_app()
        test_app.dependency_overrides[get_llm_provider] = lambda: provider

        try:
            async def _execute_with_app() -> None:
                async with test_app.router.lifespan_context(test_app):
                    sem_svc = test_app.state.semantic_cache
                    _assert(sem_svc is not None, "SemanticCacheService must be initialized at startup.")
                    ctrl = get_semantic_serving_controller()
                    _assert(ctrl.is_serving_enabled(None), "Runtime serving should initialize from SEMANTIC_CACHE_SERVING_ENABLED=true.")

                    async with httpx.AsyncClient(
                        transport=httpx.ASGITransport(app=test_app),
                        base_url="http://testserver",
                    ) as client:
                        async def _post_query(
                            httpx_client: httpx.AsyncClient,
                            token: str,
                            prompt: str,
                        ) -> Tuple[httpx.Response, float]:
                            resp, elapsed_ms = await _raw_post_query(httpx_client, token, prompt)
                            if "application/json" in resp.headers.get("content-type", ""):
                                verify_no_sensitive_leaks(resp.json(), forbidden_secrets, "POST /query response")
                            return resp, elapsed_ms

                        async def _reset_state() -> None:
                            for ns in {entry["namespace"] for entry in DEFAULT_CLIENT_TOKENS.values()}:
                                r = await client.post(f"/cache/clear?namespace={ns}", headers=_admin_hdr())
                                _assert(r.status_code == 200, f"Clear namespace {ns} failed: {r.text}")
                            if resolved_db_url:
                                await reset_pg_tables(resolved_db_url)
                            r_m = await client.post("/admin/metrics/reset", headers=_admin_hdr())
                            _assert(r_m.status_code == 200, f"Reset metrics failed: {r_m.text}")
                            ctrl.set_serving_enabled(True, clear_namespace_override=True, reason="eval_reset")
                            provider.calls.clear()

                        # ------------------------------------------------------------------
                        # Scenario A & B: Exact Hit + Real Paraphrase Semantic Hits + Savings
                        # ------------------------------------------------------------------
                        await _reset_state()
                        seed_prompt = ds["positive"][0]["seed_prompt"]
                        seed_r, _ = await _post_query(client, "tok_alice", seed_prompt)
                        _assert(seed_r.status_code == 200, f"Seed query failed: {seed_r.text}")
                        seed_j = seed_r.json()
                        verify_no_sensitive_leaks(seed_j, forbidden_secrets, "Scenario A/B seed")
                        _assert(seed_j["cached"] is False and seed_j["cache_type"] == "none", f"Expected miss on seed: {seed_j}")

                        exact_r, _ = await _post_query(client, "tok_alice", seed_prompt)
                        exact_j = exact_r.json()
                        verify_no_sensitive_leaks(exact_j, forbidden_secrets, "Scenario A exact hit")
                        _assert(exact_j["cached"] is True and exact_j["cache_type"] == "exact", f"Scenario A exact miss: {exact_j}")
                        _assert(len(provider.calls) == 1, f"Exact hit triggered upstream call: {provider.calls}")

                        para_hits = []
                        for pos in ds["positive"]:
                            # Seed non-first entries before probing their paraphrases
                            if pos["seed_prompt"] != seed_prompt:
                                sr, _ = await _post_query(client, "tok_alice", pos["seed_prompt"])
                                _assert(sr.status_code == 200 and not sr.json()["cached"], f"Seed expected miss: {sr.json()}")
                            for para in pos["paraphrases"]:
                                calls_before = len(provider.calls)
                                pr, _ = await _post_query(client, "tok_alice", para)
                                _assert(pr.status_code == 200, f"Paraphrase HTTP error: {pr.text}")
                                pj = pr.json()
                                verify_no_sensitive_leaks(pj, forbidden_secrets, f"Scenario B paraphrase {para}")
                                _assert(pj["cached"] is True and pj["cache_type"] == "semantic", f"Scenario B expected semantic hit for {para!r}: {pj}")
                                _assert(len(provider.calls) == calls_before, f"Scenario B called upstream for {para!r}")
                                para_hits.append(pj)

                        stats_a = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                        me_a = (await client.get("/metrics/me", headers=_hdr("tok_alice"))).json()
                        verify_no_sensitive_leaks(stats_a, forbidden_secrets, "Scenario A/B semantic stats")
                        verify_no_sensitive_leaks(me_a, forbidden_secrets, "Scenario A/B metrics/me")
                        _assert(stats_a["hits"] == len(para_hits), f"Expected {len(para_hits)} semantic hits, got {stats_a}")
                        _assert(me_a["request_and_cache"]["exact_cache_hits"] == 1, f"Expected 1 exact hit: {me_a}")
                        _assert(me_a["provider_and_savings"]["provider_calls_avoided"] == 1 + len(para_hits), f"Avoided mismatch: {me_a}")
                        _assert(me_a["provider_and_savings"]["total_tokens_saved"] > 0, f"Tokens saved missing: {me_a}")
                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_a_b_exact_and_semantic_reuse",
                                True,
                                f"1 exact hit + {len(para_hits)} HTTP semantic paraphrase hits; 0 spurious provider calls.",
                                {"exact_hits": 1, "semantic_hits": len(para_hits), "tokens_saved": me_a["provider_and_savings"]["total_tokens_saved"]},
                            )
                        )

                        # ------------------------------------------------------------------
                        # Scenario C: High-Similarity Incompatible + Threshold + Ambiguity
                        # ------------------------------------------------------------------
                        await _reset_state()
                        neg_results: List[Dict[str, Any]] = []
                        for neg in ds["negative"]:
                            await _reset_state()
                            if "seed_prompt" in neg:
                                sr, _ = await _post_query(client, "tok_alice", neg["seed_prompt"])
                                _assert(sr.status_code == 200 and not sr.json()["cached"], "Negative seed fail")
                            else:
                                # Dark-launch seeding (runtime serving disabled while indexing) ensures both
                                # competing candidates coexist before ambiguity evaluation.
                                await client.post(
                                    "/admin/semantic-cache/serving",
                                    headers=_admin_hdr(),
                                    json={"enabled": False, "reason": "seed_ambiguous_pool"},
                                )
                                for cand in neg["seed_candidates"]:
                                    sr, _ = await _post_query(client, "tok_alice", cand["prompt"])
                                    _assert(sr.status_code == 200 and not sr.json()["cached"], "Ambiguity candidate seed fail")
                                await client.post(
                                    "/admin/semantic-cache/serving",
                                    headers=_admin_hdr(),
                                    json={"enabled": True, "reason": "probe_ambiguous_query"},
                                )
                            calls_before = len(provider.calls)
                            qr, _ = await _post_query(client, "tok_alice", neg["query_prompt"])
                            _assert(qr.status_code == 200, f"Negative query HTTP error: {qr.text}")
                            qj = qr.json()
                            _assert(qj["cached"] is False and qj["cache_type"] == "none", f"Scenario C false hit on {neg['id']}: {qj}")
                            _assert(len(provider.calls) == calls_before + 1, f"Scenario C must fall back to upstream for {neg['id']}")
                            st_c = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                            neg_results.append({
                                "id": neg["id"],
                                "rejected_candidates": st_c["rejected_candidates"],
                                "ambiguous_candidates": st_c["ambiguous_candidates"],
                                "threshold_rejections": st_c["threshold_rejections"],
                                "upstream_fallbacks": st_c["upstream_fallbacks_after_miss"],
                            })
                            if neg["expected_rejection_reason"] == "AMBIGUOUS":
                                _assert(st_c["ambiguous_candidates"] >= 1, f"Expected ambiguity rejection on {neg['id']}: {st_c}")
                            elif neg["expected_rejection_reason"] == "THRESHOLD_MISS":
                                _assert(st_c["threshold_rejections"] >= 1, f"Expected threshold rejection on {neg['id']}: {st_c}")
                            else:
                                _assert(st_c["rejected_candidates"] >= 1, f"Expected metadata rejection on {neg['id']}: {st_c}")
                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_c_incompatible_and_ambiguous_fallback",
                                True,
                                f"All {len(neg_results)} negative/trap/ambiguous HTTP queries rejected & fell back safely.",
                                {"cases": neg_results},
                            )
                        )

                        # ------------------------------------------------------------------
                        # Scenario D: Admin Kill Switch (Pre/Mid-Lookup, Global & Namespace)
                        # ------------------------------------------------------------------
                        await _reset_state()
                        ctrl_item = ds["control"][1]
                        _assert((await client.get("/admin/semantic-cache/serving")).status_code == 403, "Admin unauth must 403")
                        _assert(
                            (await client.post("/admin/semantic-cache/serving", headers=_admin_hdr(), json={"enabled": "true"})).status_code == 422,
                            "Admin non-bool must 422",
                        )
                        s1, _ = await _post_query(client, "tok_alice", ctrl_item["seed_prompt"])
                        s2, _ = await _post_query(client, "tok_ctrl", ctrl_item["seed_prompt"])
                        _assert(not s1.json()["cached"] and not s2.json()["cached"], "Seeds failed")

                        # Disable team_alpha override only: team_alpha falls back, team_ctrl still hits!
                        srv_get = await client.get("/admin/semantic-cache/serving", headers=_admin_hdr())
                        _assert(srv_get.status_code == 200, f"GET /admin/semantic-cache/serving failed: {srv_get.text}")
                        verify_no_sensitive_leaks(srv_get.json(), forbidden_secrets, "GET /admin/semantic-cache/serving")
                        ns_off = await client.post(
                            "/admin/semantic-cache/serving",
                            headers=_admin_hdr(),
                            json={"enabled": False, "namespace": "team_alpha", "reason": "canary_disable"},
                        )
                        _assert(ns_off.status_code == 200 and ns_off.json()["effective_enabled"] is False, f"Namespace override error: {ns_off.text}")
                        verify_no_sensitive_leaks(ns_off.json(), forbidden_secrets, "POST /admin/semantic-cache/serving")
                        a_par, _ = await _post_query(client, "tok_alice", ctrl_item["paraphrase_prompt"])
                        c_par, _ = await _post_query(client, "tok_ctrl", ctrl_item["paraphrase_prompt"])
                        a_ex, _ = await _post_query(client, "tok_alice", ctrl_item["exact_prompt"])
                        _assert(a_par.json()["cached"] is False and a_par.json()["cache_type"] == "none", "Namespace kill switch didn't block semantic")
                        _assert(a_ex.json()["cached"] is True and a_ex.json()["cache_type"] == "exact", "Exact cache should stay intact when semantic kill switch off")
                        _assert(c_par.json()["cached"] is True and c_par.json()["cache_type"] == "semantic", "Other namespace unaffected by team_alpha override")

                        # Clear override + global off: query an unseen paraphrase ("Reverse a string in Python")
                        # so exact cache does not mask semantic runtime kill-switch enforcement.
                        await client.post(
                            "/admin/semantic-cache/serving",
                            headers=_admin_hdr(),
                            json={"enabled": True, "namespace": "team_alpha", "clear_namespace_override": True},
                        )
                        await client.post("/admin/semantic-cache/serving", headers=_admin_hdr(), json={"enabled": False, "reason": "global_off"})
                        g_off, _ = await _post_query(client, "tok_alice", "Reverse a string in Python")
                        _assert(g_off.json()["cached"] is False, "Global kill switch must block semantic")
                        await client.post("/admin/semantic-cache/serving", headers=_admin_hdr(), json={"enabled": True, "reason": "re_enable"})

                        # Mid-lookup Checkpoint 2 toggle over real HTTP (seed fresh sort-list prompt)
                        s_sort, _ = await _post_query(client, "tok_alice", "How do I sort a list in Python?")
                        _assert(not s_sort.json()["cached"], "Checkpoint 2 seed sort-list fail")
                        in_lookup = asyncio.Event()
                        allow_return = asyncio.Event()
                        orig_lookup = sem_svc.lookup

                        async def gated_lookup(req: GenerationRequest):
                            res = await orig_lookup(req)
                            in_lookup.set()
                            await allow_return.wait()
                            return res

                        sem_svc.lookup = gated_lookup  # type: ignore[method-assign]
                        try:
                            inflight_task = asyncio.create_task(
                                _post_query(client, "tok_alice", "How to sort a Python list?")
                            )
                            await in_lookup.wait()
                            flip_r = await client.post(
                                "/admin/semantic-cache/serving",
                                headers=_admin_hdr(),
                                json={"enabled": False, "reason": "mid_lookup_kill"},
                            )
                            _assert(flip_r.status_code == 200, f"Flip failed: {flip_r.text}")
                            allow_return.set()
                            mid_resp, _ = await inflight_task
                            _assert(mid_resp.status_code == 200 and mid_resp.json()["cached"] is False, f"Checkpoint 2 did not fail closed: {mid_resp.json()}")
                        finally:
                            sem_svc.lookup = orig_lookup  # type: ignore[method-assign]

                        st_d = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                        _assert(st_d["runtime_disabled_skips"] >= 3, f"Expected runtime_disabled_skips >= 3: {st_d}")
                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_d_runtime_kill_switch_http",
                                True,
                                "Global off, namespace override, exact preservation, and mid-lookup Checkpoint 2 disable verified over HTTP.",
                                {"runtime_disabled_skips": st_d["runtime_disabled_skips"]},
                            )
                        )

                        # ------------------------------------------------------------------
                        # Scenario E, F, G: Privacy Isolation, Tenant Sharing, Spoofing
                        # ------------------------------------------------------------------
                        await _reset_state()
                        priv_iso, priv_shared, priv_cross = ds["privacy"]

                        # Scenario E: Private Alice -> Bob same tenant blocked (distinct exact & semantic seeds
                        # so Bob's own upstream fallback does not populate Bob's private cache for Alice's paraphrase)
                        await _post_query(client, priv_iso["seed_actor"]["token"], priv_iso["seed_prompt"])
                        await _post_query(client, priv_iso["seed_actor"]["token"], priv_iso["semantic_seed_prompt"])
                        bob_ex, _ = await _post_query(client, priv_iso["probe_actor"]["token"], priv_iso["exact_probe_prompt"])
                        bob_sem, _ = await _post_query(client, priv_iso["probe_actor"]["token"], priv_iso["semantic_probe_prompt"])
                        _assert(bob_ex.json()["cached"] is False, "Private exact leak Alice -> Bob!")
                        _assert(bob_sem.json()["cached"] is False, "Private semantic leak Alice -> Bob!")

                        # Scenario F: Carol shared -> Dave shared hits; Erin private(allow_shared_reads=true) hits;
                        # Fay private(allow_shared_reads=false) in same tenant MUST NOT hit Carol's shared entry;
                        # Erin's private write NOT readable by Carol; Frank cross-tenant denied!
                        await _post_query(client, priv_shared["seed_actor"]["token"], priv_shared["seed_prompt"])
                        dave_sem, _ = await _post_query(client, priv_shared["shared_reader"]["token"], priv_shared["semantic_probe_prompt"])
                        erin_sem, _ = await _post_query(client, priv_shared["opt_in_private_reader"]["token"], priv_shared["semantic_probe_prompt"])
                        _assert(dave_sem.json()["cached"] is True and dave_sem.json()["cache_type"] == "semantic", f"Shared reader Dave missed: {dave_sem.json()}")
                        _assert(erin_sem.json()["cached"] is True and erin_sem.json()["cache_type"] == "semantic", f"Opt-in private reader Erin missed: {erin_sem.json()}")

                        # Same-tenant private reader with allow_shared_reads=false MUST miss & fall back to upstream
                        disallowed_reader = priv_shared["private_disallowed_shared_reader"]
                        calls_before_fay = len(provider.calls)
                        fay_sem, _ = await _post_query(client, disallowed_reader["token"], priv_shared["semantic_probe_prompt"])
                        _assert(
                            fay_sem.status_code == 200
                            and fay_sem.json()["cached"] is False
                            and fay_sem.json()["cache_type"] == "none",
                            f"Same-tenant private reader (allow_shared_reads=false) leaked tenant_shared entry: {fay_sem.json()}",
                        )
                        _assert(
                            len(provider.calls) == calls_before_fay + 1,
                            "Disallowed same-tenant private reader must fall back to upstream provider",
                        )

                        # Erin writes private entry; Carol must NOT see it!
                        await _post_query(client, priv_shared["opt_in_private_reader"]["token"], priv_shared["private_seed_prompt_by_opt_in"])
                        carol_probe_priv, _ = await _post_query(client, priv_shared["seed_actor"]["token"], "How to sort a Python list?")
                        _assert(carol_probe_priv.json()["cached"] is False, f"Shared reader Carol read Erin's private entry: {carol_probe_priv.json()}")

                        # Frank (team_gamma) cannot read Carol's team_beta shared entry
                        frank_cross, _ = await _post_query(client, priv_cross["probe_actor"]["token"], priv_cross["semantic_probe_prompt"])
                        _assert(frank_cross.json()["cached"] is False, f"Cross-tenant semantic leak to team_gamma: {frank_cross.json()}")

                        # Scenario G: Spoof attempts rejected (422) & ignored headers increment privacy_violation_attempts
                        spoof_error_forbidden = [
                            STAGE_3_5_5_IDENTITY_SECRET,
                            STAGE_3_5_5_EVIDENCE_SECRET,
                            STAGE_3_5_5_ADMIN_TOKEN,
                            *DEFAULT_CLIENT_TOKENS.keys(),
                            *derived_principals,
                        ]
                        for spoof_body in (
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "hi"}], "principal_id": "forged"},
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "hi"}], "visibility": "tenant_shared"},
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "hi"}], "allow_shared_reads": True},
                            {"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "hi"}], "metadata": {"owner_principal_id": "forged"}},
                        ):
                            sp_r = await client.post("/query", headers=_hdr("tok_alice"), json=spoof_body)
                            _assert(sp_r.status_code == 422, f"Spoof body must 422: {spoof_body} -> {sp_r.status_code}")
                            verify_no_sensitive_leaks(
                                sp_r.json(),
                                spoof_error_forbidden,
                                "POST /query spoof 422 detail",
                                check_forbidden_keys=False,
                            )

                        hdr_spoof = await client.post(
                            "/query",
                            headers={**_hdr("tok_alice"), "X-Cache-Visibility": "tenant_shared"},
                            json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "How do I merge two dictionaries in Python?"}], "namespace": "team_beta"},
                        )
                        _assert(hdr_spoof.status_code == 200, "Ignored namespace/header should still complete in caller namespace")
                        verify_no_sensitive_leaks(hdr_spoof.json(), forbidden_secrets, "Scenario G header spoof response")
                        st_alpha = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                        verify_no_sensitive_leaks(st_alpha, forbidden_secrets, "Scenario G semantic stats")
                        _assert(st_alpha["privacy_violation_attempts"] >= 1, f"Expected privacy_violation_attempts increment: {st_alpha}")

                        # DB Legacy visibility='unknown' and Tampered Signature Fail-Closed Check (when PG active)
                        if resolved_db_url:
                            await _reset_state()
                            # Seed two prompts, downgrade all rows to legacy visibility='unknown',
                            # and verify BOTH exact (merge dicts) and semantic (sort list -> sort a Python list) miss.
                            await _post_query(client, "tok_carol", "How do I merge two dictionaries in Python?")
                            await _post_query(client, "tok_carol", "How do I sort a list in Python?")
                            conn = await asyncpg.connect(resolved_db_url)
                            try:
                                await conn.execute(
                                    "UPDATE vector_records SET visibility = 'unknown', owner_principal_id = NULL WHERE cache_namespace = 'team_beta'"
                                )
                                await conn.execute(
                                    "UPDATE semantic_variant_records SET visibility = 'unknown', owner_principal_id = NULL WHERE cache_namespace = 'team_beta'"
                                )
                                await conn.execute(
                                    "UPDATE cache_entries SET visibility = 'unknown', owner_principal_id = NULL WHERE cache_namespace = 'team_beta'"
                                )
                            finally:
                                await conn.close()
                            unk_exact, _ = await _post_query(client, "tok_dave", "How do I merge two dictionaries in Python?")
                            unk_sem, _ = await _post_query(client, "tok_dave", "How to sort a Python list?")
                            _assert(unk_exact.json()["cached"] is False, "Legacy 'unknown' exact DB row leaked!")
                            _assert(unk_sem.json()["cached"] is False, "Legacy 'unknown' semantic DB row leaked!")

                            # Tamper evidence signature on a fresh DB entry
                            await _reset_state()
                            await _post_query(client, "tok_alice", "How do I sort a list in Python?")
                            conn = await asyncpg.connect(resolved_db_url)
                            try:
                                await conn.execute(
                                    "UPDATE vector_records SET metadata = jsonb_set(metadata::jsonb, '{compatibility_evidence,evidence_signature}', '\"tampered_sig\"'::jsonb) WHERE cache_namespace = 'team_alpha'"
                                )
                                await conn.execute(
                                    "UPDATE semantic_variant_records SET metadata = jsonb_set(metadata::jsonb, '{compatibility_evidence,evidence_signature}', '\"tampered_sig\"'::jsonb) WHERE cache_namespace = 'team_alpha'"
                                )
                            finally:
                                await conn.close()
                            tamp_sem, _ = await _post_query(client, "tok_alice", "How to sort a Python list?")
                            _assert(tamp_sem.json()["cached"] is False, f"Tampered evidence signature served over HTTP: {tamp_sem.json()}")
                            st_tamp = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                            _assert(st_tamp["evidence_validation_rejections"] >= 1, f"Expected evidence_validation_rejections >= 1: {st_tamp}")

                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_e_f_g_privacy_isolation_and_db_safety",
                                True,
                                "Private/shared/opt-in/disallowed-shared-read/cross-tenant isolation, spoof rejection, derived principal non-disclosure, legacy 'unknown' SQL block, and tampered evidence rejection verified.",
                                {
                                    "derived_principals_checked": len(derived_principals),
                                    "credentials_checked": len(DEFAULT_CLIENT_TOKENS),
                                },
                            )
                        )

                        # ------------------------------------------------------------------
                        # Scenario H: HTTP SingleFlight Semantic Leader & Coalesced Follower
                        # ------------------------------------------------------------------
                        await _reset_state()
                        sf_seed, _ = await _post_query(client, "tok_sf_a", "How do I reverse a string in Python?")
                        _assert(not sf_seed.json()["cached"], "SF seed fail")
                        calls_before_sf = len(provider.calls)

                        sf_in_lookup = asyncio.Event()
                        sf_release = asyncio.Event()
                        orig_sf_lookup = sem_svc.lookup

                        async def sf_paused_lookup(req: GenerationRequest):
                            res = await orig_sf_lookup(req)
                            if req.namespace == "team_sf":
                                sf_in_lookup.set()
                                await sf_release.wait()
                            return res

                        sem_svc.lookup = sf_paused_lookup  # type: ignore[method-assign]
                        try:
                            leader_coro = _post_query(client, "tok_sf_a", "What is the Python syntax to reverse a string?")
                            follower_coro = _post_query(client, "tok_sf_a", "What is the Python syntax to reverse a string?")
                            other_principal_coro = _post_query(client, "tok_sf_b", "What is the Python syntax to reverse a string?")

                            leader_t = asyncio.create_task(leader_coro)
                            await sf_in_lookup.wait()
                            # Start same-principal follower AND different-principal request concurrently
                            follower_t = asyncio.create_task(follower_coro)
                            other_t = asyncio.create_task(other_principal_coro)
                            await asyncio.sleep(0.05)  # yield so both enter SingleFlight registry
                            sf_release.set()
                            (l_r, _), (f_r, _), (o_r, _) = await asyncio.gather(leader_t, follower_t, other_t)
                        finally:
                            sem_svc.lookup = orig_sf_lookup  # type: ignore[method-assign]

                        lj, fj, oj = l_r.json(), f_r.json(), o_r.json()
                        _assert(lj["cached"] is True and lj["cache_type"] == "semantic", f"SF leader failed: {lj}")
                        _assert(fj["cached"] is True and fj["cache_type"] == "semantic", f"SF follower failed: {fj}")
                        _assert(lj["response"] == fj["response"], "SF leader and follower response diverged")
                        # Private principal tok_sf_b MUST NOT share Alice's private semantic entry or coalesce onto Alice's flight!
                        _assert(oj["cached"] is False and oj["cache_type"] == "none", f"Cross-principal SF/semantic leak to tok_sf_b: {oj}")
                        _assert(len(provider.calls) == calls_before_sf + 1, f"Expected 1 provider call (tok_sf_b only): {provider.calls}")

                        sf_me = (await client.get("/metrics/me", headers=_hdr("tok_sf_a"))).json()
                        sf_st = (await client.get("/cache/semantic/stats", headers=_hdr("tok_sf_a"))).json()
                        sf_adm = (await client.get("/admin/metrics", headers=_admin_hdr())).json()
                        verify_no_sensitive_leaks(sf_me, forbidden_secrets, "Scenario H /metrics/me")
                        verify_no_sensitive_leaks(sf_st, forbidden_secrets, "Scenario H /cache/semantic/stats")
                        verify_no_sensitive_leaks(sf_adm, forbidden_secrets, "Scenario H /admin/metrics")
                        _assert(sf_me["semantic"]["semantic_hits"] == 1, f"Expected 1 semantic leader hit: {sf_me}")
                        _assert(sf_me["request_and_cache"]["coalesced_joins"] == 1, f"Expected 1 coalesced join: {sf_me}")
                        _assert(sf_me["request_and_cache"]["coalesced_successes"] == 1, f"Expected 1 coalesced success: {sf_me}")
                        _assert(sf_me["request_and_cache"]["coalesced_semantic_hits"] == 1, f"Expected 1 coalesced semantic hit: {sf_me}")
                        _assert(sf_st["coalesced_semantic_hits"] == 1, f"Expected 1 coalesced semantic hit in stats endpoint: {sf_st}")
                        _assert(sf_adm["aggregate"]["request_and_cache"]["coalesced_semantic_hits"] == 1, f"Admin aggregate request_and_cache coalesced_semantic_hits mismatch: {sf_adm}")
                        _assert(sf_adm["aggregate"]["semantic"]["coalesced_semantic_hits"] == 1, f"Admin aggregate semantic coalesced_semantic_hits mismatch: {sf_adm}")
                        _assert(sf_me["provider_and_savings"]["provider_calls_avoided"] == 2, f"Expected 2 provider calls avoided (leader+follower): {sf_me}")

                        # Post-Checkpoint-2 flip over HTTP (Stage 3.8B.1 follower enforcement):
                        # once leader record_semantic_hit runs (post-CP2), toggling serving off lets the
                        # authorized leader finish cleanly, while the waiting follower independently enforces
                        # Checkpoint 2, rejects the semantic response, and falls back to upstream.
                        await _reset_state()
                        sf2_seed, _ = await _post_query(client, "tok_sf_a", "How do I reverse a string in Python?")
                        _assert(not sf2_seed.json()["cached"], "Post-CP2 seed fail")
                        sf2_in_lookup = asyncio.Event()
                        sf2_release = asyncio.Event()
                        active_cache = get_query_cache()
                        orig_cache_rec = active_cache.record_semantic_hit

                        async def post_cp2_lookup(req: GenerationRequest):
                            res = await orig_sf_lookup(req)
                            if req.namespace == "team_sf":
                                sf2_in_lookup.set()
                                await sf2_release.wait()
                            return res

                        async def flip_after_cp2(tokens_saved: int, latency_saved_ms: float):
                            await orig_cache_rec(tokens_saved, latency_saved_ms)
                            ctrl.set_serving_enabled(False, updated_by="eval_post_cp2", reason="post_cp2_http_flip")

                        sem_svc.lookup = post_cp2_lookup  # type: ignore[method-assign]
                        active_cache.record_semantic_hit = flip_after_cp2  # type: ignore[method-assign]
                        try:
                            l2_t = asyncio.create_task(_post_query(client, "tok_sf_a", "Reverse a string in Python"))
                            await sf2_in_lookup.wait()
                            f2_t = asyncio.create_task(_post_query(client, "tok_sf_a", "Reverse a string in Python"))
                            await asyncio.sleep(0.05)
                            sf2_release.set()
                            (l2_r, _), (f2_r, _) = await asyncio.gather(l2_t, f2_t)
                        finally:
                            sem_svc.lookup = orig_sf_lookup  # type: ignore[method-assign]
                            active_cache.record_semantic_hit = orig_cache_rec  # type: ignore[method-assign]

                        _assert(l2_r.json()["cached"] is True and l2_r.json()["cache_type"] == "semantic", "Post-CP2 leader must finish")
                        _assert(f2_r.json()["cached"] is False and f2_r.json()["cache_type"] == "none", "Post-CP2 follower must independently fail closed to upstream")
                        # Immediate next request sees disabled state and falls back
                        post_flip_next, _ = await _post_query(client, "tok_sf_a", "How do I sort a list in Python?")
                        _assert(post_flip_next.json()["cached"] is False, "Request after post-CP2 flip must fall back")
                        sf2_st = (await client.get("/cache/semantic/stats", headers=_hdr("tok_sf_a"))).json()
                        _assert(sf2_st["runtime_disabled_skips"] >= 2, f"Expected runtime_disabled_skips >= 2 (follower + next) after post-CP2 flip: {sf2_st}")
                        _assert(sf2_st["coalesced_semantic_hits"] == 0, f"Expected coalesced_semantic_hits == 0 when follower rejected semantic hit: {sf2_st}")
                        ctrl.set_serving_enabled(True, reason="restore_after_sf")

                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_h_http_singleflight_semantic_coalescing",
                                True,
                                "HTTP SingleFlight leader+follower served identical semantic response (0 upstream calls), distinct private principal isolated, and post-CP2 follower Checkpoint 2 enforcement verified.",
                                {
                                    "semantic_hits": sf_me["semantic"]["semantic_hits"],
                                    "coalesced_joins": sf_me["request_and_cache"]["coalesced_joins"],
                                    "coalesced_semantic_hits": sf_me["request_and_cache"]["coalesced_semantic_hits"],
                                    "provider_calls_avoided": sf_me["provider_and_savings"]["provider_calls_avoided"],
                                    "post_cp2_runtime_disabled_skips": sf2_st["runtime_disabled_skips"],
                                },
                            )
                        )

                        # ------------------------------------------------------------------
                        # Scenario I & J: Embedding Failure & Vector Store Failure Fallback
                        # ------------------------------------------------------------------
                        await _reset_state()
                        await _post_query(client, "tok_alice", "How do I reverse a string in Python?")

                        orig_embed = sem_svc.embedding_provider.embed
                        async def failing_embed(*args, **kwargs):
                            raise EmbeddingTimeoutError("Simulated FastEmbed runtime timeout", provider="fastembed")

                        sem_svc.embedding_provider.embed = failing_embed  # type: ignore[method-assign]
                        try:
                            emb_fail_r, _ = await _post_query(client, "tok_alice", "What is the Python syntax to reverse a string?")
                            _assert(emb_fail_r.status_code == 200 and emb_fail_r.json()["cached"] is False, f"Embedding fail fallback error: {emb_fail_r.json()}")
                        finally:
                            sem_svc.embedding_provider.embed = orig_embed  # type: ignore[method-assign]

                        orig_vs_search = sem_svc.vector_store.search
                        async def failing_vs_search(*args, **kwargs):
                            raise VectorStoreError("Simulated PostgreSQL vector connection lost")

                        sem_svc.vector_store.search = failing_vs_search  # type: ignore[method-assign]
                        try:
                            vs_fail_r, _ = await _post_query(client, "tok_alice", "Reverse a string in Python")
                            _assert(vs_fail_r.status_code == 200 and vs_fail_r.json()["cached"] is False, f"VectorStore fail fallback error: {vs_fail_r.json()}")
                        finally:
                            sem_svc.vector_store.search = orig_vs_search  # type: ignore[method-assign]

                        st_ij = (await client.get("/cache/semantic/stats", headers=_hdr("tok_alice"))).json()
                        _assert(st_ij["embedding_failures"] >= 1, f"Missing embedding_failures counter: {st_ij}")
                        _assert(st_ij["vector_store_failures"] >= 1, f"Missing vector_store_failures counter: {st_ij}")
                        _assert(st_ij["upstream_fallbacks_after_miss"] >= 2, f"Missing upstream fallback counters: {st_ij}")
                        outcomes.append(
                            ScenarioOutcome(
                                "scenario_i_j_degraded_dependency_fallback",
                                True,
                                "Embedding and vector-store failures fell back to HTTP 200 upstream with accurate failure counters.",
                                {
                                    "embedding_failures": st_ij["embedding_failures"],
                                    "vector_store_failures": st_ij["vector_store_failures"],
                                    "upstream_fallbacks_after_miss": st_ij["upstream_fallbacks_after_miss"],
                                },
                            )
                        )

                        # ------------------------------------------------------------------
                        # Reproducible Latency & Inference Savings Benchmark (>= 20 samples)
                        # ------------------------------------------------------------------
                        await _reset_state()
                        n_iter = max(1, int(benchmark_iterations))

                        # Warm-up one FastEmbed inference call to remove cold ONNX load skew
                        await _post_query(client, "tok_bench", "How do I reverse a string in Python?")
                        await _reset_state()

                        # 1) Baseline (runtime serving OFF + unique prompts -> exact misses + upstream generation)
                        await client.post("/admin/semantic-cache/serving", headers=_admin_hdr(), json={"enabled": False, "reason": "bench_baseline"})
                        provider.set_delay_ms(simulated_upstream_delay_ms)
                        baseline_latencies_ms: List[float] = []
                        for i in range(n_iter):
                            prompt_i = f"How do I reverse a string in Python (request #{i})?"
                            br, bl = await _post_query(client, "tok_bench", prompt_i)
                            _assert(br.status_code == 200 and br.json()["cached"] is False, f"Baseline unexpected hit: {br.json()}")
                            baseline_latencies_ms.append(bl)
                        baseline_me = (await client.get("/metrics/me", headers=_hdr("tok_bench"))).json()

                        # 2) Pure zero-delay gateway overhead baseline (separates gateway + auth + exact miss cost from mock delay)
                        provider.set_delay_ms(0.0)
                        zero_delay_baseline_ms: List[float] = []
                        for i in range(n_iter):
                            prompt_z = f"How do I compute square root in Python (overhead #{i})?"
                            zr, zl = await _post_query(client, "tok_bench", prompt_z)
                            _assert(zr.status_code == 200 and zr.json()["cached"] is False, "Zero-delay baseline hit")
                            zero_delay_baseline_ms.append(zl)

                        # 3) Exact Hit benchmark
                        await _reset_state()
                        provider.set_delay_ms(simulated_upstream_delay_ms)
                        await _post_query(client, "tok_bench", "How do I reverse a string in Python?")
                        exact_latencies_ms: List[float] = []
                        for _ in range(n_iter):
                            er, el = await _post_query(client, "tok_bench", "How do I reverse a string in Python?")
                            ej = er.json()
                            _assert(er.status_code == 200 and ej["cached"] is True and ej["cache_type"] == "exact", f"Benchmark exact miss: {ej}")
                            exact_latencies_ms.append(el)

                        # 4) Semantic Hit benchmark (semantic paraphrase never promoted to exact cache, hits vector/compat pipeline each time)
                        await _reset_state()
                        provider.set_delay_ms(simulated_upstream_delay_ms)
                        calls_seed_before = len(provider.calls)
                        seed_b, _ = await _post_query(client, "tok_bench", "How do I reverse a string in Python?")
                        _assert(not seed_b.json()["cached"], "Semantic benchmark seed must miss")
                        _assert(len(provider.calls) == calls_seed_before + 1, "Seed must call provider once")

                        # Disable provider delay for extra safety proof — semantic hits shouldn't call provider at all
                        calls_before_sem_loop = len(provider.calls)
                        semantic_latencies_ms: List[float] = []
                        for _ in range(n_iter):
                            sr, sl = await _post_query(client, "tok_bench", "What is the Python syntax to reverse a string?")
                            sj = sr.json()
                            _assert(sr.status_code == 200 and sj["cached"] is True and sj["cache_type"] == "semantic", f"Benchmark semantic expected hit: {sj}")
                            semantic_latencies_ms.append(sl)
                        _assert(len(provider.calls) == calls_before_sem_loop, "Semantic benchmark called upstream provider!")

                        sem_me = (await client.get("/metrics/me", headers=_hdr("tok_bench"))).json()
                        adm_me = (await client.get("/admin/metrics", headers=_admin_hdr())).json()
                        verify_no_sensitive_leaks(adm_me, forbidden_secrets, "Admin metrics benchmark")

                        base_stats = compute_percentiles(baseline_latencies_ms)
                        gw_overhead_stats = compute_percentiles(zero_delay_baseline_ms)
                        exact_stats = compute_percentiles(exact_latencies_ms)
                        sem_stats = compute_percentiles(semantic_latencies_ms)

                        speedup_p50_ratio = (
                            round(base_stats["p50_ms"] / sem_stats["p50_ms"], 2)
                            if sem_stats["p50_ms"] > 0
                            else 0.0
                        )
                        latency_reduction_p50_pct = (
                            round((base_stats["p50_ms"] - sem_stats["p50_ms"]) / base_stats["p50_ms"] * 100.0, 2)
                            if base_stats["p50_ms"] > 0
                            else 0.0
                        )
                        benchmark_report.update({
                            "iterations_per_mode": n_iter,
                            "simulated_upstream_delay_ms": simulated_upstream_delay_ms,
                            "baseline_with_simulated_upstream": base_stats,
                            "gateway_zero_delay_overhead": gw_overhead_stats,
                            "exact_hit": exact_stats,
                            "semantic_hit": sem_stats,
                            "semantic_vs_simulated_baseline": {
                                "p50_speedup_x": speedup_p50_ratio,
                                "p50_latency_reduction_pct": latency_reduction_p50_pct,
                                "p50_saved_ms": round(base_stats["p50_ms"] - sem_stats["p50_ms"], 3),
                            },
                            "metrics_snapshot_semantic_phase": {
                                "total_authenticated_requests": sem_me["request_and_cache"]["total_authenticated_requests"],
                                "semantic_lookups": sem_me["request_and_cache"]["semantic_lookups"],
                                "semantic_hits": sem_me["semantic"]["semantic_hits"],
                                "provider_calls_attempted": sem_me["provider_and_savings"]["provider_calls_attempted"],
                                "provider_calls_avoided": sem_me["provider_and_savings"]["provider_calls_avoided"],
                                "provider_total_tokens_used": sem_me["provider_and_savings"]["provider_total_tokens_used"],
                                "total_tokens_saved": sem_me["provider_and_savings"]["total_tokens_saved"],
                            },
                            "baseline_provider_calls_attempted": baseline_me["provider_and_savings"]["provider_calls_attempted"],
                            "baseline_total_tokens_used": baseline_me["provider_and_savings"]["provider_total_tokens_used"],
                        })

            with patch_ctx:
                await _execute_with_app()
        finally:
            set_query_cache(saved_cache)
            set_semantic_cache(saved_sem)
            set_vector_store(saved_vs)
            if resolved_db_url:
                await reset_pg_tables(resolved_db_url, include_runtime_control=True)
            reset_semantic_serving_controller(False)
            get_metrics_collector().reset()
            reload_settings()

    all_passed = all(o.passed for o in outcomes)
    report_payload: Dict[str, Any] = {
        "stage": "3.5.5",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "environment": {
            "backend_cache": "postgres" if use_postgres else "memory",
            "backend_vector": "postgres" if use_postgres else "memory",
            "database_url": redact_database_url(resolved_db_url) if resolved_db_url else None,
            "embedding_mode": "real_fastembed" if use_real_fastembed else "deterministic_topic_test_double",
            "embedding_model": FASTEMBED_MODEL,
            "vector_dimensions": FASTEMBED_DIM,
            "upstream_provider": "ControlledEvalProvider(mock)",
            "min_similarity": 0.92,
            "min_margin": 0.03,
            "top_k": 5,
            "max_retrieval_candidates": 25,
        },
        "all_scenarios_passed": all_passed,
        "scenarios": [
            {"scenario_id": o.scenario_id, "passed": o.passed, "summary": o.summary, "details": o.details}
            for o in outcomes
        ],
        "benchmark": benchmark_report,
    }
    verify_no_sensitive_leaks(report_payload, forbidden_secrets, "evaluation JSON report")
    verify_no_sensitive_leaks(render_markdown_report(report_payload), forbidden_secrets, "evaluation Markdown report")
    return report_payload


def render_markdown_report(result: Dict[str, Any]) -> str:
    """Formats the Stage 3.5.5 operational evaluation dictionary as a readable Markdown report."""
    env = result["environment"]
    b = result["benchmark"]
    base = b["baseline_with_simulated_upstream"]
    gw0 = b["gateway_zero_delay_overhead"]
    ex = b["exact_hit"]
    sem = b["semantic_hit"]
    sav = b["semantic_vs_simulated_baseline"]
    snap = b["metrics_snapshot_semantic_phase"]
    lines = [
        "# Stage 3.5.5 Local-First Semantic Serving Operational Evaluation",
        "",
        f"- Generated at: `{result['generated_at']}`",
        f"- Backends: cache=`{env['backend_cache']}`, vector=`{env['backend_vector']}`, embedder=`{env['embedding_mode']}` (`{env['embedding_model']}`, {env['vector_dimensions']}-d)",
        f"- Production thresholds: `min_similarity={env['min_similarity']}`, `min_margin={env['min_margin']}`, `top_k={env['top_k']}`, `budget={env['max_retrieval_candidates']}`",
        f"- All Scenarios Passed: **{result['all_scenarios_passed']}**",
        "",
        "## Scenario Outcomes",
        "",
        "| Scenario | Status | Summary |",
        "|---|---|---|",
    ]
    for s in result["scenarios"]:
        status = "PASS" if s["passed"] else "FAIL"
        lines.append(f"| `{s['scenario_id']}` | {status} | {s['summary']} |")
    lines.extend([
        "",
        f"## Latency & Savings Benchmark (`iterations={b['iterations_per_mode']}`, `simulated_upstream_delay_ms={b['simulated_upstream_delay_ms']}`)",
        "",
        "| Mode | Count | p50 (ms) | p95 (ms) | Mean (ms) | Min (ms) | Max (ms) |",
        "|---|---:|---:|---:|---:|---:|---:|",
        f"| Baseline (semantic off, simulated upstream) | {base['count']} | {base['p50_ms']} | {base['p95_ms']} | {base['mean_ms']} | {base['min_ms']} | {base['max_ms']} |",
        f"| Gateway Overhead Only (0 ms upstream delay) | {gw0['count']} | {gw0['p50_ms']} | {gw0['p95_ms']} | {gw0['mean_ms']} | {gw0['min_ms']} | {gw0['max_ms']} |",
        f"| Exact Cache Hit (HTTP POST /query) | {ex['count']} | {ex['p50_ms']} | {ex['p95_ms']} | {ex['mean_ms']} | {ex['min_ms']} | {ex['max_ms']} |",
        f"| Semantic Cache Hit (FastEmbed + pgvector + HTTP) | {sem['count']} | {sem['p50_ms']} | {sem['p95_ms']} | {sem['mean_ms']} | {sem['min_ms']} | {sem['max_ms']} |",
        "",
        "### Savings Summary",
        "",
        f"- Semantic vs Simulated-Upstream p50 latency reduction: **{sav['p50_latency_reduction_pct']}%** (`{sav['p50_speedup_x']}x` faster, `{sav['p50_saved_ms']} ms` saved)",
        f"- Semantic phase requests: `{snap['total_authenticated_requests']}` (1 seed + {b['iterations_per_mode']} paraphrases)",
        f"- Provider calls attempted: `{snap['provider_calls_attempted']}` vs avoided: `{snap['provider_calls_avoided']}`",
        f"- Tokens used: `{snap['provider_total_tokens_used']}` vs tokens saved: `{snap['total_tokens_saved']}`",
        "",
        "> Note: Baseline inference latency includes synthetic upstream mock delay (`simulated_upstream_delay_ms`) so it is separated explicitly from unmocked FastEmbed + PostgreSQL/pgvector gateway processing time.",
    ])
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description="Stage 3.5.5 Local-First HTTP Semantic Serving Operational Evaluation")
    parser.add_argument("--iterations", type=int, default=25, help="Number of iterations per latency benchmark mode (default: 25)")
    parser.add_argument("--upstream-delay-ms", type=float, default=120.0, help="Simulated upstream latency in ms (default: 120.0)")
    parser.add_argument("--output-dir", type=Path, default=Path("/tmp/intellicache_stage3_5_5_report"), help="Directory to write JSON/MD evaluation outputs")
    args = parser.parse_args()

    result = asyncio.run(
        run_operational_evaluation(
            use_postgres=True,
            use_real_fastembed=True,
            benchmark_iterations=args.iterations,
            simulated_upstream_delay_ms=args.upstream_delay_ms,
        )
    )
    args.output_dir.mkdir(parents=True, exist_ok=True)
    json_path = args.output_dir / "stage3_5_5_operational_evaluation.json"
    md_path = args.output_dir / "stage3_5_5_operational_evaluation.md"
    json_path.write_text(json.dumps(result, indent=2), encoding="utf-8")
    md_text = render_markdown_report(result)
    md_path.write_text(md_text, encoding="utf-8")
    print(md_text)
    print(f"Wrote JSON artifact: {json_path}")
    print(f"Wrote Markdown artifact: {md_path}")
    return 0 if result["all_scenarios_passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
