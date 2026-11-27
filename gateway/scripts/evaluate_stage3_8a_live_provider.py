#!/usr/bin/env python3
"""
Stage 3.8A Controlled Live Upstream Provider Evaluation Runner (Groq + PostgreSQL + FastEmbed).

Strictly requires explicit opt-in (`LIVE_PROVIDER_EVALUATION_ENABLED=true`), explicit non-placeholder
`GROQ_API_KEY`, explicit `LIVE_PROVIDER_MODEL`, and PostgreSQL + pgvector (384-d FastEmbed).
Never silently falls back to MockProvider when live evaluation is requested.

Usage:
    cd gateway
    LIVE_PROVIDER_EVALUATION_ENABLED=true LIVE_PROVIDER_MODEL=llama-3.3-70b-versatile \
        TEST_DATABASE_URL="postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test" \
        .venv/bin/python scripts/evaluate_stage3_8a_live_provider.py --confirm-live --output-dir /tmp/stage3_8a_eval
"""

from __future__ import annotations

import argparse
import asyncio
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import groq
import httpx

GATEWAY_ROOT = Path(__file__).resolve().parent.parent
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.api.routes import get_llm_provider  # noqa: E402
from app.cache.postgres_cache import PostgresCache  # noqa: E402
from app.config import Settings, reload_settings  # noqa: E402
from app.core.contracts import (  # noqa: E402
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from app.core.errors import (  # noqa: E402
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
    sanitize_error_message,
)
from app.core.service import IntelliCacheCore  # noqa: E402
from app.main import create_app  # noqa: E402
from app.metrics import RuntimeMetricsCollector, set_metrics_collector  # noqa: E402
from app.providers.groq_adapter import GroqProvider  # noqa: E402
from app.providers.interfaces import LLMProvider  # noqa: E402
from app.semantic_cache.compatibility import CompatibilityExtractor  # noqa: E402
from app.semantic_cache.contracts import ConstraintItem, MetadataStatus, VerificationStatus  # noqa: E402
from app.semantic_cache.control import reset_semantic_serving_controller  # noqa: E402
from app.semantic_cache.evidence import EvidenceAuthority  # noqa: E402
from app.semantic_cache.verification import ResponseArtifactVerifier  # noqa: E402
from app.vector_store.postgres_store import PostgresVectorStore  # noqa: E402
from scripts.evaluate_stage3_5_5_operational import (  # noqa: E402
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    build_forbidden_disclosure_values,
    reset_pg_tables,
    resolve_fastembed_database_url,
    verify_no_sensitive_leaks,
)
from scripts.stage3_7_scalability import compute_full_percentiles  # noqa: E402

logger = logging.getLogger("stage3_8a_live_eval")

DATASET_PATH = GATEWAY_ROOT / "evaluation" / "datasets" / "stage3_8a_live_provider_dataset.json"

STAGE38A_CLIENT_TOKENS: Dict[str, Any] = {
    "tok_s38a_alice": {"namespace": "s38a_alpha", "visibility": "private", "allow_shared_reads": False},
    "tok_s38a_bob": {"namespace": "s38a_alpha", "visibility": "private", "allow_shared_reads": False},
    "tok_s38a_carol": {"namespace": "s38a_beta", "visibility": "tenant_shared", "allow_shared_reads": True},
    "tok_s38a_dave": {"namespace": "s38a_beta", "visibility": "tenant_shared", "allow_shared_reads": True},
    "tok_s38a_frank": {"namespace": "s38a_gamma", "visibility": "tenant_shared", "allow_shared_reads": True},
}


class LiveEvalBudgetExceededError(ProviderError):
    """Raised when live-provider request count or spend ceiling is reached."""


class LiveEvalBudgetGuard:
    """Enforces hard upper bounds on live provider calls and optional spend."""

    def __init__(
        self,
        *,
        max_requests: int,
        max_cost: Optional[float] = None,
        input_cost_per_million: Optional[float] = None,
        output_cost_per_million: Optional[float] = None,
        pricing_source: Optional[str] = None,
    ) -> None:
        if max_requests < 1:
            raise ValueError(f"max_requests must be >= 1, got {max_requests}.")
        if max_cost is not None and max_cost <= 0.0:
            raise ValueError(f"max_cost must be > 0, got {max_cost}.")
        self.max_requests = int(max_requests)
        self.max_cost = float(max_cost) if max_cost is not None else None
        self.input_cost_per_million = input_cost_per_million
        self.output_cost_per_million = output_cost_per_million
        self.pricing_source = pricing_source
        self.requests_made = 0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.usage_reported_calls = 0
        self.usage_missing_calls = 0
        self.accumulated_cost: float = 0.0
        self.terminated_early_reason: Optional[str] = None
        self._lock = asyncio.Lock()

    @property
    def pricing_configured(self) -> bool:
        return (
            self.input_cost_per_million is not None
            and self.output_cost_per_million is not None
        )

    def calculate_cost(self, prompt_tokens: int, completion_tokens: int) -> Optional[float]:
        if not self.pricing_configured:
            return None
        return (
            (max(0, prompt_tokens) / 1_000_000.0) * float(self.input_cost_per_million or 0.0)
            + (max(0, completion_tokens) / 1_000_000.0) * float(self.output_cost_per_million or 0.0)
        )

    async def reserve_request(self) -> None:
        async with self._lock:
            if self.requests_made >= self.max_requests:
                self.terminated_early_reason = f"max_requests ({self.max_requests}) reached"
                raise LiveEvalBudgetExceededError(
                    f"Live evaluation budget exhausted: {self.terminated_early_reason}."
                )
            if self.max_cost is not None and self.pricing_configured and self.accumulated_cost >= self.max_cost:
                self.terminated_early_reason = f"max_cost ({self.max_cost:.6f}) reached"
                raise LiveEvalBudgetExceededError(
                    f"Live evaluation cost ceiling reached: {self.terminated_early_reason}."
                )
            self.requests_made += 1

    async def record_usage(self, usage: Optional[UsageMetrics], *, usage_available: bool) -> None:
        async with self._lock:
            if not usage_available or usage is None:
                self.usage_missing_calls += 1
                return
            self.usage_reported_calls += 1
            self.prompt_tokens += max(0, int(usage.prompt_tokens))
            self.completion_tokens += max(0, int(usage.completion_tokens))
            self.total_tokens += max(0, int(usage.total_tokens))
            call_cost = self.calculate_cost(usage.prompt_tokens, usage.completion_tokens)
            if call_cost is not None:
                self.accumulated_cost += call_cost

    def summary(self) -> Dict[str, Any]:
        return {
            "max_requests": self.max_requests,
            "requests_made": self.requests_made,
            "max_cost": self.max_cost,
            "pricing_configured": self.pricing_configured,
            "pricing_source": self.pricing_source,
            "prompt_tokens_reported": self.prompt_tokens if self.usage_reported_calls > 0 else None,
            "completion_tokens_reported": self.completion_tokens if self.usage_reported_calls > 0 else None,
            "total_tokens_reported": self.total_tokens if self.usage_reported_calls > 0 else None,
            "usage_reported_calls": self.usage_reported_calls,
            "usage_missing_calls": self.usage_missing_calls,
            "accumulated_cost_usd": round(self.accumulated_cost, 6) if self.pricing_configured and self.usage_reported_calls > 0 else None,
            "cost_status": "calculated_from_usage" if (self.pricing_configured and self.usage_reported_calls > 0) else "unavailable",
            "terminated_early_reason": self.terminated_early_reason,
        }


class BudgetedProviderProxy(LLMProvider):
    """Wraps an actual GroqProvider to enforce hard request/cost budget, pacing & record TTFT/usage."""

    def __init__(self, inner: GroqProvider, budget: LiveEvalBudgetGuard, *, min_interval_seconds: float = 0.0) -> None:
        self.inner = inner
        self.budget = budget
        self.min_interval_seconds = max(0.0, float(min_interval_seconds))
        self._last_call_mono: Optional[float] = None
        self.stream_observations: List[Dict[str, Any]] = []
        self.real_provider_errors: List[Dict[str, Any]] = []

    async def pace_before_upstream(self) -> None:
        """Applies inter-request OTPM cooldown OUTSIDE timed request execution spans."""
        if self.min_interval_seconds > 0.0 and self._last_call_mono is not None:
            elapsed = time.monotonic() - self._last_call_mono
            if elapsed < self.min_interval_seconds:
                await asyncio.sleep(self.min_interval_seconds - elapsed)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        await self.budget.reserve_request()
        try:
            res = await self.inner.generate(request)
        except ProviderError as err:
            self._last_call_mono = time.monotonic()
            self.real_provider_errors.append({
                "mode": "generate",
                "error_type": type(err).__name__,
                "retry_count": getattr(err, "retry_count", 0),
                "message": redact_sensitive_text(str(err)),
            })
            raise
        self._last_call_mono = time.monotonic()
        meta = getattr(res, "metadata", None) or {}
        usage_avail = bool(meta.get("usage_available", True))
        await self.budget.record_usage(res.usage, usage_available=usage_avail)
        return res

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        await self.budget.reserve_request()
        t0 = time.perf_counter()
        ttft_ms: Optional[float] = None
        chunk_count = 0
        final_chunk: Optional[StreamChunk] = None
        text_parts: List[str] = []
        try:
            async for chunk in self.inner.stream(request):
                if not chunk.done:
                    if chunk.delta:
                        text_parts.append(chunk.delta)
                        chunk_count += 1
                        if ttft_ms is None:
                            ttft_ms = (time.perf_counter() - t0) * 1000.0
                    yield chunk
                else:
                    final_chunk = chunk
                    self._last_call_mono = time.monotonic()
                    total_ms = (time.perf_counter() - t0) * 1000.0
                    meta = getattr(chunk, "metadata", None) or {}
                    usage_avail = bool(meta.get("usage_available", False))
                    await self.budget.record_usage(chunk.usage, usage_available=usage_avail)
                    self.stream_observations.append({
                        "model": chunk.model or request.model,
                        "ttft_ms": round(ttft_ms if ttft_ms is not None else total_ms, 2),
                        "total_ms": round(total_ms, 2),
                        "chunk_count": chunk_count,
                        "output_char_count": len("".join(text_parts)),
                        "finish_reason": chunk.finish_reason,
                        "usage_available": usage_avail,
                        "usage": chunk.usage.to_dict() if (usage_avail and chunk.usage) else None,
                    })
                    yield chunk
        except ProviderError as err:
            self._last_call_mono = time.monotonic()
            self.real_provider_errors.append({
                "mode": "stream",
                "error_type": type(err).__name__,
                "retry_count": getattr(err, "retry_count", 0),
                "message": redact_sensitive_text(str(err)),
            })
            raise

    async def close(self) -> None:
        await self.inner.close()


def validate_live_eval_profile(
    *,
    confirm_live: bool,
    live_enabled: Optional[bool] = None,
    provider_name: Optional[str] = None,
    model_name: Optional[str] = None,
    api_key: Optional[str] = None,
    timeout_seconds: Optional[float] = None,
    max_retries: Optional[int] = None,
    max_requests: Optional[int] = None,
    max_cost: Optional[float] = None,
    input_cost_per_million: Optional[float] = None,
    output_cost_per_million: Optional[float] = None,
    pricing_source: Optional[str] = None,
) -> Dict[str, Any]:
    """
    Validates explicit opt-in, credentials, provider/model configuration, and budget bounds.
    Raises ProviderConfigurationError on any missing/invalid requirement (zero mock fallback).
    """
    enabled = (
        live_enabled
        if live_enabled is not None
        else os.getenv("LIVE_PROVIDER_EVALUATION_ENABLED", "false").lower() in ("true", "1", "yes")
    )
    if not enabled or not confirm_live:
        raise ProviderConfigurationError(
            "Stage 3.8A live evaluation requires BOTH LIVE_PROVIDER_EVALUATION_ENABLED=true and explicit confirmation (--confirm-live)."
        )

    prov = (provider_name or os.getenv("LIVE_PROVIDER_NAME", "groq")).strip().lower()
    if prov != "groq":
        raise ProviderConfigurationError(f"Unsupported live evaluation provider '{prov}'; expected 'groq'.")

    model = (model_name if model_name is not None else os.getenv("LIVE_PROVIDER_MODEL", "")).strip()
    if not model:
        raise ProviderConfigurationError(
            "LIVE_PROVIDER_MODEL (or --model) must be explicitly set for Stage 3.8A live evaluation."
        )

    key = (api_key if api_key is not None else os.getenv("GROQ_API_KEY", "")).strip()
    if not key or key == "replace_with_your_key":
        raise ProviderConfigurationError(
            "Valid non-placeholder GROQ_API_KEY is required for Stage 3.8A live evaluation."
        )

    eff_timeout = float(timeout_seconds if timeout_seconds is not None else os.getenv("LIVE_PROVIDER_TIMEOUT_SECONDS", "30.0"))
    eff_retries = int(max_retries if max_retries is not None else os.getenv("LIVE_PROVIDER_MAX_RETRIES", "1"))
    eff_max_req = int(max_requests if max_requests is not None else os.getenv("LIVE_PROVIDER_MAX_REQUESTS", "45"))
    env_max_cost = os.getenv("LIVE_PROVIDER_MAX_COST", "").strip()
    eff_max_cost = float(max_cost) if max_cost is not None else (float(env_max_cost) if env_max_cost else None)

    if eff_timeout <= 0 or eff_timeout > 300.0:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_TIMEOUT_SECONDS out of bounds: {eff_timeout}")
    if eff_retries < 0 or eff_retries > 5:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_RETRIES out of bounds: {eff_retries}")
    if eff_max_req < 1 or eff_max_req > 500:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_REQUESTS out of bounds: {eff_max_req}")
    if eff_max_cost is not None and eff_max_cost <= 0:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_COST must be > 0, got {eff_max_cost}")

    env_in_cost = os.getenv("LIVE_PROVIDER_INPUT_COST_PER_MILLION", "").strip()
    env_out_cost = os.getenv("LIVE_PROVIDER_OUTPUT_COST_PER_MILLION", "").strip()
    in_cpm = input_cost_per_million if input_cost_per_million is not None else (float(env_in_cost) if env_in_cost else None)
    out_cpm = output_cost_per_million if output_cost_per_million is not None else (float(env_out_cost) if env_out_cost else None)
    src = pricing_source or (os.getenv("LIVE_PROVIDER_PRICING_SOURCE", "").strip() or None)

    return {
        "provider": prov,
        "model": model,
        "api_key": key,
        "timeout_seconds": eff_timeout,
        "max_retries": eff_retries,
        "max_requests": eff_max_req,
        "max_cost": eff_max_cost,
        "input_cost_per_million": in_cpm,
        "output_cost_per_million": out_cpm,
        "pricing_source": src,
    }


def redact_sensitive_text(text: str, extra_secrets: Sequence[str] = ()) -> str:
    """Redacts API keys, bearer tokens, URIs with passwords, paths, IPs, and extra secrets."""
    cleaned = sanitize_error_message(text or "")
    for sec in extra_secrets:
        if sec and len(sec) >= 4:
            cleaned = cleaned.replace(sec, "***REDACTED***")
    return cleaned


def sanitize_report_payload(obj: Any, *, extra_secrets: Sequence[str] = ()) -> Any:
    """Recursively strips secret fields/values so evaluation reports stay non-disclosing."""
    secret_keys = frozenset({
        "api_key", "groq_api_key", "authorization", "x-api-key", "x-client-token",
        "x-admin-token", "principal_id", "owner_principal_id", "evidence_secret",
        "principal_identity_secret", "database_url",
    })
    if isinstance(obj, dict):
        out: Dict[str, Any] = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in secret_keys:
                out[k] = "***REDACTED***"
            else:
                out[k] = sanitize_report_payload(v, extra_secrets=extra_secrets)
        return out
    if isinstance(obj, list):
        return [sanitize_report_payload(x, extra_secrets=extra_secrets) for x in obj]
    if isinstance(obj, str):
        return redact_sensitive_text(obj, extra_secrets=extra_secrets)
    return obj


def build_stage38a_env(
    db_url: str,
    *,
    profile: Dict[str, Any],
    semantic_enabled: bool,
    semantic_serving_enabled: bool,
) -> Dict[str, str]:
    """Builds isolated gateway env vars for Mode B or Mode C without changing repo defaults."""
    return {
        "LLM_PROVIDER": "groq",
        "GROQ_API_KEY": profile["api_key"],
        "GROQ_MODEL": profile["model"],
        "GROQ_TIMEOUT_SECONDS": str(profile["timeout_seconds"]),
        "GROQ_MAX_RETRIES": str(profile["max_retries"]),
        "LIVE_PROVIDER_EVALUATION_ENABLED": "true",
        "LIVE_PROVIDER_NAME": "groq",
        "LIVE_PROVIDER_MODEL": profile["model"],
        "LIVE_PROVIDER_TIMEOUT_SECONDS": str(profile["timeout_seconds"]),
        "LIVE_PROVIDER_MAX_RETRIES": str(profile["max_retries"]),
        "LIVE_PROVIDER_MAX_REQUESTS": str(profile["max_requests"]),
        "CLIENT_API_TOKENS": json.dumps(STAGE38A_CLIENT_TOKENS),
        "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
        "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "CONVERSATION_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
        "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
        "CACHE_BACKEND": "postgres",
        "VECTOR_STORE_BACKEND": "postgres",
        "DATABASE_URL": db_url,
        "CACHE_ENABLED": "true",
        "CACHE_TTL_SECONDS": "3600",
        "CACHE_DEFAULT_VISIBILITY": "private",
        "SEMANTIC_CACHE_DEFAULT_VISIBILITY": "private",
        "SEMANTIC_CACHE_ENABLED": "true" if semantic_enabled else "false",
        "SEMANTIC_CACHE_SERVING_ENABLED": "true" if semantic_serving_enabled else "false",
        "SHADOW_EVALUATION_ENABLED": "false",
        "EMBEDDING_PROVIDER": "fastembed",
        "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
        "VECTOR_DIMENSIONS": "384",
        "SEMANTIC_CACHE_MIN_SIMILARITY": "0.92",
        "SEMANTIC_CACHE_MIN_MARGIN": "0.03",
        "SEMANTIC_CACHE_TOP_K": "5",
        "SEMANTIC_CACHE_MAX_RETRIEVAL_CANDIDATES": "25",
    }


def _mode_stats(samples: Sequence[float]) -> Dict[str, Any]:
    if not samples:
        return {"count": 0, "p50_ms": None, "p95_ms": None, "p99_ms": None, "mean_ms": None, "min_ms": None, "max_ms": None}
    stats = compute_full_percentiles(samples)
    if len(samples) < 20:
        # Avoid claiming high-confidence p99 on sparse live request samples
        stats["p99_ms"] = None
    return stats


async def run_fault_injection_matrix(db_url: str) -> List[Dict[str, Any]]:
    """
    Executes controlled provider & database fault-injection checks (FAIL-01..FAIL-12).
    Safe offline/controlled tests explicitly distinguished from real Groq traffic.
    """
    extractor = CompatibilityExtractor()
    verifier = ResponseArtifactVerifier()
    results: List[Dict[str, Any]] = []

    req_sample = GenerationRequest(
        model="eval-model",
        messages=[ChatMessage(role="user", content="Explain Python sets in one sentence.")],
        temperature=0.0,
        namespace="s38a_faults",
        visibility="tenant_shared",
    )

    # FAIL-01/02: Missing/invalid API key + HTTP 401 Auth failure
    try:
        GroqProvider(api_key="replace_with_your_key")._create_client()
        ok_f1 = False
    except ProviderConfigurationError:
        ok_f1 = True
    results.append({"id": "FAIL-01", "source": "controlled_fault_injection", "scenario": "placeholder_or_missing_api_key", "passed": ok_f1})

    req_obj = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    c_auth = AsyncMock()
    c_auth.chat.completions.create = AsyncMock(
        side_effect=groq.AuthenticationError(message="Invalid API Key gsk_testsecret123", response=httpx.Response(401, request=req_obj), body=None)
    )
    c_auth.close = AsyncMock()
    p_auth = GroqProvider(api_key="gsk_test", client_factory=lambda: c_auth, max_retries=2, retry_backoff_seconds=0.0)
    try:
        await p_auth.generate(req_sample)
        ok_f2 = False
    except ProviderAuthenticationError as err:
        ok_f2 = (c_auth.chat.completions.create.await_count == 1) and ("gsk_***" in err.message)
    results.append({"id": "FAIL-02", "source": "controlled_fault_injection", "scenario": "authentication_failure_no_retry_redacted", "passed": ok_f2})

    # FAIL-03 & FAIL-11: Rate limit + retry exhaustion stops cleanly at max_retries
    collector = RuntimeMetricsCollector()
    from app.cache.exact_cache import ExactCache
    cache = ExactCache(max_entries=50)
    core = IntelliCacheCore(cache=cache, metrics_collector=collector)

    c_rl = AsyncMock()
    c_rl.chat.completions.create = AsyncMock(
        side_effect=groq.RateLimitError(message="Rate limit exceeded", response=httpx.Response(429, request=req_obj), body=None)
    )
    c_rl.close = AsyncMock()
    p_rl = GroqProvider(api_key="gsk_test", client_factory=lambda: c_rl, max_retries=1, retry_backoff_seconds=0.0)
    try:
        await core.execute_query(req_sample, p_rl)
        ok_f3 = False
    except ProviderRateLimitError:
        snap_rl = collector.get_namespace_snapshot("s38a_faults")
        ok_f3 = (
            c_rl.chat.completions.create.await_count == 2
            and snap_rl.provider_and_savings.provider_rate_limits_total == 1
            and snap_rl.provider_and_savings.provider_retries_total == 1
            and snap_rl.provider_and_savings.provider_calls_failed == 1
            and len(cache._entries) == 0
        )
    results.append({"id": "FAIL-03_FAIL-11", "source": "controlled_fault_injection", "scenario": "rate_limit_and_retry_exhaustion", "passed": ok_f3})

    # FAIL-04: Provider timeout
    c_to = AsyncMock()
    c_to.chat.completions.create = AsyncMock(side_effect=groq.APITimeoutError(request=req_obj))
    c_to.close = AsyncMock()
    p_to = GroqProvider(api_key="gsk_test", client_factory=lambda: c_to, max_retries=0, retry_backoff_seconds=0.0)
    try:
        await core.execute_query(req_sample, p_to)
        ok_f4 = False
    except ProviderTimeoutError:
        snap_to = collector.get_namespace_snapshot("s38a_faults")
        ok_f4 = snap_to.provider_and_savings.provider_timeouts_total == 1 and len(cache._entries) == 0
    results.append({"id": "FAIL-04", "source": "controlled_fault_injection", "scenario": "provider_timeout_metric_and_no_cache", "passed": ok_f4})

    # FAIL-05: 503 Service unavailable recovers via bounded retry
    ok_resp = MagicMock()
    ok_resp.choices = [MagicMock(message=MagicMock(content="Recovered response."), finish_reason="stop")]
    ok_resp.usage = MagicMock(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    c_503 = AsyncMock()
    c_503.chat.completions.create = AsyncMock(
        side_effect=[
            groq.APIStatusError(message="Service unavailable", response=httpx.Response(503, request=req_obj), body=None),
            ok_resp,
        ]
    )
    c_503.close = AsyncMock()
    p_503 = GroqProvider(api_key="gsk_test", client_factory=lambda: c_503, max_retries=1, retry_backoff_seconds=0.0)
    res_503 = await core.execute_query(req_sample, p_503)
    ok_f5 = res_503.text == "Recovered response." and c_503.chat.completions.create.await_count == 2
    results.append({"id": "FAIL-05", "source": "controlled_fault_injection", "scenario": "upstream_503_bounded_retry_recovery", "passed": ok_f5})

    # FAIL-06: Network connection error fails cleanly with 0 cache entries
    await cache.clear()
    c_conn = AsyncMock()
    c_conn.chat.completions.create = AsyncMock(side_effect=groq.APIConnectionError(request=req_obj, message="DNS failure"))
    c_conn.close = AsyncMock()
    p_conn = GroqProvider(api_key="gsk_test", client_factory=lambda: c_conn, max_retries=1, retry_backoff_seconds=0.0)
    try:
        await core.execute_query(req_sample, p_conn)
        ok_f6 = False
    except ProviderConnectionError:
        ok_f6 = c_conn.chat.completions.create.await_count == 2 and len(cache._entries) == 0
    results.append({"id": "FAIL-06", "source": "controlled_fault_injection", "scenario": "network_connection_interruption", "passed": ok_f6})

    # FAIL-07: Malformed/empty response blocked by verifier
    meta_json = extractor.extract(
        GenerationRequest(model="m", messages=[ChatMessage(role="user", content="Return JSON only")], temperature=0.0),
        "BAAI/bge-small-en-v1.5",
    )
    v_empty = verifier.verify(response_text="", request_metadata=meta_json)
    ok_f7 = v_empty.status == VerificationStatus.FAILED
    results.append({"id": "FAIL-07", "source": "controlled_fault_injection", "scenario": "malformed_empty_response_blocked", "passed": ok_f7})

    # FAIL-08: Missing usage metadata increments counter and avoids fabricated counts
    await cache.clear()
    c_nousg = AsyncMock()
    r_nousg = MagicMock()
    r_nousg.choices = [MagicMock(message=MagicMock(content="Answer without usage."), finish_reason="stop")]
    r_nousg.usage = None
    c_nousg.chat.completions.create = AsyncMock(return_value=r_nousg)
    c_nousg.close = AsyncMock()
    p_nousg = GroqProvider(api_key="gsk_test", client_factory=lambda: c_nousg)
    res_nu = await core.execute_query(req_sample, p_nousg)
    snap_nu = collector.get_namespace_snapshot("s38a_faults")
    ok_f8 = res_nu.usage.total_tokens == 0 and snap_nu.provider_and_savings.provider_usage_metadata_missing_total == 1
    results.append({"id": "FAIL-08", "source": "controlled_fault_injection", "scenario": "missing_usage_metadata_unavailable", "passed": ok_f8})

    # FAIL-09: Unexpected finish_reason -> UNCERTAIN prevents semantic admission
    v_fr = verifier.verify(
        response_text="Valid looking sentence.",
        request_metadata=extractor.extract(req_sample, "BAAI/bge-small-en-v1.5"),
        finish_reason="vendor_custom_abort",
    )
    ok_f9 = v_fr.status == VerificationStatus.UNCERTAIN
    results.append({"id": "FAIL-09", "source": "controlled_fault_injection", "scenario": "unexpected_finish_reason_uncertain", "passed": ok_f9})

    # FAIL-10: Mid-stream provider error never caches partial output and emits error chunk
    await cache.clear()
    async def failing_stream():
        c1 = MagicMock()
        c1.choices = [MagicMock(delta=MagicMock(content="Partial "))]
        yield c1
        raise groq.APIConnectionError(request=req_obj, message="Mid-stream drop")

    c_strm = AsyncMock()
    c_strm.chat.completions.create = AsyncMock(return_value=failing_stream())
    c_strm.close = AsyncMock()
    p_strm = GroqProvider(api_key="gsk_test", client_factory=lambda: c_strm, max_retries=2, retry_backoff_seconds=0.0)
    chunks_seen = [c async for c in core.execute_stream(req_sample, p_strm)]
    ok_f10 = (
        any(c.delta == "Partial " for c in chunks_seen)
        and chunks_seen[-1].error is not None
        and chunks_seen[-1].status_code == 503
        and len(cache._entries) == 0
        and c_strm.chat.completions.create.await_count == 1
    )
    results.append({"id": "FAIL-10", "source": "controlled_fault_injection", "scenario": "streaming_partial_failure_no_cache_no_retry", "passed": ok_f10})

    # FAIL-12: Database unavailable during live-like cache get/set fails open safely to provider
    broken_cache = AsyncMock()
    broken_cache.get = AsyncMock(side_effect=RuntimeError("PG connection lost on read"))
    broken_cache.set = AsyncMock(side_effect=RuntimeError("PG connection lost on write"))
    broken_cache.record_miss = AsyncMock()
    c_db = AsyncMock()
    c_db.chat.completions.create = AsyncMock(return_value=ok_resp)
    c_db.close = AsyncMock()
    core_db = IntelliCacheCore(cache=broken_cache, metrics_collector=collector)
    res_db = await core_db.execute_query(req_sample, GroqProvider(api_key="gsk_test", client_factory=lambda: c_db))
    ok_f12 = res_db.text == "Recovered response." and not res_db.cached
    results.append({"id": "FAIL-12", "source": "controlled_fault_injection", "scenario": "database_outage_fails_open_to_provider", "passed": ok_f12})

    # NEG-EXTRA: Ambiguous close candidates (< 0.03 margin) and missing compatibility metadata both fail closed
    from app.semantic_cache.policy import SemanticCachePolicy
    from app.vector_store.contracts import VectorSearchResult
    auth = EvidenceAuthority(STAGE_3_5_5_EVIDENCE_SECRET)
    policy = SemanticCachePolicy(
        min_similarity=0.92,
        min_margin=0.03,
        top_k=5,
        configured_model="BAAI/bge-small-en-v1.5",
        extractor=extractor,
        retrieval_budget=25,
        evidence_authority=auth,
    )
    req_amb = GenerationRequest(
        model="eval-model",
        messages=[ChatMessage(role="user", content="Explain Python sets briefly.")],
        temperature=0.0,
        namespace="s38a_faults",
        visibility="tenant_shared",
    )
    meta_ok = extractor.extract(req_amb, "BAAI/bge-small-en-v1.5")
    cand1 = VectorSearchResult(
        fingerprint="fp_amb_1", cache_namespace="s38a_faults", model="BAAI/bge-small-en-v1.5",
        query_text="Explain Python sets", cached_response="Sets store unique items.",
        prompt_tokens=5, completion_tokens=5, total_tokens=10, original_latency_ms=50.0,
        visibility="tenant_shared", distance=0.04, similarity=0.96, metadata=meta_ok.to_dict(),
    )
    cand2 = VectorSearchResult(
        fingerprint="fp_amb_2", cache_namespace="s38a_faults", model="BAAI/bge-small-en-v1.5",
        query_text="Explain sets in Python", cached_response="Sets deduplicate elements.",
        prompt_tokens=5, completion_tokens=5, total_tokens=10, original_latency_ms=50.0,
        visibility="tenant_shared", distance=0.05, similarity=0.95, metadata=meta_ok.to_dict(),
    )
    amb_win, amb_reason = policy.evaluate_candidates([cand1, cand2], req_amb)
    miss_meta_cand = VectorSearchResult(
        fingerprint="fp_no_meta", cache_namespace="s38a_faults", model="BAAI/bge-small-en-v1.5",
        query_text="Explain Python sets", cached_response="Sets store unique items.",
        prompt_tokens=5, completion_tokens=5, total_tokens=10, original_latency_ms=50.0,
        visibility="tenant_shared", distance=0.02, similarity=0.98, metadata={},
    )
    mm_win, _ = policy.evaluate_candidates([miss_meta_cand], req_amb)
    results.append({
        "id": "NEG-AMBIGUOUS-AND-MISSING-META",
        "source": "controlled_fault_injection",
        "scenario": "ambiguous_margin_and_missing_metadata_rejected",
        "passed": (amb_win is None and amb_reason is not None and mm_win is None),
    })

    return results


async def evaluate_artifact_verifier_live(
    *,
    groq_provider: LLMProvider,
    budget: LiveEvalBudgetGuard,
    model_name: str,
) -> Dict[str, Any]:
    """Validates ResponseArtifactVerifier against actual Groq completions (plus synthetic boundary checks)."""
    verifier = ResponseArtifactVerifier()
    extractor = CompatibilityExtractor()

    async def _call(prompt: str, max_tokens: int = 160, system: Optional[str] = None) -> GenerationResult:
        if hasattr(groq_provider, "pace_before_upstream"):
            await groq_provider.pace_before_upstream()
        msgs: List[ChatMessage] = []
        if system:
            msgs.append(ChatMessage(role="system", content=system))
        msgs.append(ChatMessage(role="user", content=prompt))
        req = GenerationRequest(model=model_name, messages=msgs, temperature=0.0, max_tokens=max_tokens, namespace="s38a_ver")
        return await groq_provider.generate(req)

    checks: List[Dict[str, Any]] = []

    # 1. Valid JSON + JSON Schema adherence on live Groq output
    schema_prompt = (
        'Return strictly raw JSON matching schema {"type":"object","required":["language","year"],'
        '"properties":{"language":{"type":"string"},"year":{"type":"integer"}}} for Python first release 1991. '
        'No markdown fences, no prose.'
    )
    r_json = await _call(schema_prompt, max_tokens=120)
    req_json = GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content=schema_prompt)], temperature=0.0)
    meta_json = extractor.extract(req_json, "BAAI/bge-small-en-v1.5")
    ver_json = verifier.verify(
        response_text=r_json.text,
        request_metadata=meta_json,
        query_text=schema_prompt,
        finish_reason=r_json.finish_reason,
        completion_tokens=r_json.usage.completion_tokens,
        request_max_tokens=120,
    )
    checks.append({
        "check_id": "ART-LIVE-JSON-SCHEMA",
        "source": "real_groq_api",
        "status": ver_json.status.value,
        "verified_json_valid": ver_json.verified_json_valid,
        "finish_reason": r_json.finish_reason,
        "admit_eligible": ver_json.status == VerificationStatus.VERIFIED,
    })

    # 2. Validate same live Groq JSON against incompatible required_keys -> missing key -> FAILED
    from app.semantic_cache.contracts import DimensionValue
    meta_missing_key = extractor.extract(
        GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content="Return JSON with language, year, and maintainer_email.")], temperature=0.0),
        "BAAI/bge-small-en-v1.5",
    )
    meta_missing_key.dimensions["output_constraints"] = DimensionValue(
        status=MetadataStatus.KNOWN,
        value=[ConstraintItem(key="required_keys", value="language,year,maintainer_email")],
    )
    ver_missing = verifier.verify(
        response_text=r_json.text,
        request_metadata=meta_missing_key,
        query_text="Return JSON with language, year, and maintainer_email.",
        finish_reason=r_json.finish_reason,
    )
    checks.append({
        "check_id": "ART-LIVE-MISSING-KEYS",
        "source": "real_groq_body_against_stricter_contract",
        "status": ver_missing.status.value,
        "reason_code": ver_missing.reason_code.value,
        "admit_eligible": ver_missing.status == VerificationStatus.VERIFIED,
    })

    # 3. Complete Markdown prose returned when raw JSON required -> FAILED (format mismatch, NOT truncation)
    r_md = await _call("Explain Python tuples in two short sentences with one tiny fenced python snippet.", max_tokens=450)
    ver_md_as_json = verifier.verify(
        response_text=r_md.text,
        request_metadata=meta_json,
        query_text=schema_prompt,
        finish_reason=r_md.finish_reason,
    )
    checks.append({
        "check_id": "ART-LIVE-MARKDOWN-WHEN-JSON-REQUIRED",
        "source": "real_groq_api",
        "status": ver_md_as_json.status.value,
        "reason_code": ver_md_as_json.reason_code.value,
        "admit_eligible": ver_md_as_json.status == VerificationStatus.VERIFIED,
    })

    # 4. Forced truncation via tiny max_tokens=8 -> finish_reason=length -> FAILED
    r_trunc = await _call("Write a detailed four-paragraph essay explaining TCP congestion control.", max_tokens=8)
    meta_plain = extractor.extract(GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content="Explain TCP.")], temperature=0.0), "BAAI/bge-small-en-v1.5")
    ver_trunc = verifier.verify(
        response_text=r_trunc.text,
        request_metadata=meta_plain,
        finish_reason=r_trunc.finish_reason,
        completion_tokens=r_trunc.usage.completion_tokens,
        request_max_tokens=8,
    )
    checks.append({
        "check_id": "ART-LIVE-TRUNCATED-MAX-TOKENS",
        "source": "real_groq_api",
        "status": ver_trunc.status.value,
        "reason_code": ver_trunc.reason_code.value,
        "finish_reason": r_trunc.finish_reason,
        "truncation_status": ver_trunc.truncation_status.value,
        "admit_eligible": ver_trunc.status == VerificationStatus.VERIFIED,
    })

    # 5. Live code_only + max_words boundary checks on real Groq output
    code_prompt = "Return ONLY a raw Python function def square(n): return n * n. Return ONLY raw Python code with no prose and no markdown code fences."
    r_code = await _call(code_prompt, max_tokens=200)
    meta_code = extractor.extract(GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content=code_prompt)], temperature=0.0), "BAAI/bge-small-en-v1.5")
    ver_code = verifier.verify(
        response_text=r_code.text,
        request_metadata=meta_code,
        query_text=code_prompt,
        finish_reason=r_code.finish_reason,
        completion_tokens=r_code.usage.completion_tokens,
        request_max_tokens=100,
    )
    checks.append({
        "check_id": "ART-LIVE-CODE-ONLY",
        "source": "real_groq_api",
        "status": ver_code.status.value,
        "reason_code": ver_code.reason_code.value,
        "admit_eligible": ver_code.status == VerificationStatus.VERIFIED,
    })

    meta_words = extractor.extract(
        GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content="Answer in at most 5 words and 40 characters.")], temperature=0.0),
        "BAAI/bge-small-en-v1.5",
    )
    ver_words_fail = verifier.verify(
        response_text=r_md.text,
        request_metadata=meta_words,
        finish_reason=r_md.finish_reason,
    )
    checks.append({
        "check_id": "ART-LIVE-MAX-WORDS-CHARS",
        "source": "real_groq_body_against_stricter_contract",
        "status": ver_words_fail.status.value,
        "reason_code": ver_words_fail.reason_code.value,
        "admit_eligible": ver_words_fail.status == VerificationStatus.VERIFIED,
    })

    # 6. Unsupported constraint (`required_sections`) & missing finish_reason -> honest documented boundaries
    meta_sections = extractor.extract(GenerationRequest(model=model_name, messages=[ChatMessage(role="user", content="Explain QUIC.")], temperature=0.0), "BAAI/bge-small-en-v1.5")
    from app.semantic_cache.contracts import DimensionValue
    meta_sections.dimensions["output_constraints"] = DimensionValue(
        status=MetadataStatus.KNOWN,
        value=[ConstraintItem(key="required_sections", value="Overview,Examples")],
    )
    ver_sec = verifier.verify(response_text="Overview: QUIC runs over UDP.", request_metadata=meta_sections, finish_reason="stop")
    ver_no_fr = verifier.verify(response_text="QUIC runs over UDP.", request_metadata=meta_plain, finish_reason=None)
    checks.append({
        "check_id": "ART-UNSUPPORTED-SECTIONS-UNCERTAIN",
        "source": "deterministic_verifier_contract",
        "status": ver_sec.status.value,
        "reason_code": ver_sec.reason_code.value,
        "admit_eligible": ver_sec.status == VerificationStatus.VERIFIED,
    })
    checks.append({
        "check_id": "ART-MISSING-FINISH-REASON-HONEST",
        "source": "deterministic_verifier_contract",
        "status": ver_no_fr.status.value,
        "truncation_status": ver_no_fr.truncation_status.value,
    })

    return {
        "checks": checks,
        "verifiable_constraints": ["json", "yaml", "xml", "csv", "strict_format=json_only", "schema_adherence_subset", "required_keys", "code_only", "markup=no_markdown", "max_words", "max_chars", "finish_reason_truncation"],
        "unsupported_constraints": ["required_sections (fails closed UNCERTAIN)", "arbitrary semantic accuracy"],
    }


async def run_live_provider_evaluation(
    *,
    confirm_live: bool,
    db_url: Optional[str] = None,
    output_dir: Optional[Path] = None,
    model_override: Optional[str] = None,
    max_requests_override: Optional[int] = None,
    case_ids: Optional[Sequence[str]] = None,
    skip_mode_comparison: bool = False,
    pacing_seconds: float = 5.5,
) -> Dict[str, Any]:
    """Executes the full Stage 3.8A controlled live-provider validation and persists JSON/MD reports."""
    profile = validate_live_eval_profile(
        confirm_live=confirm_live,
        model_name=model_override,
        max_requests=max_requests_override,
    )
    resolved_db_url = db_url or await resolve_fastembed_database_url()
    await reset_pg_tables(resolved_db_url)

    with open(DATASET_PATH, "r", encoding="utf-8") as fh:
        dataset = json.load(fh)

    budget = LiveEvalBudgetGuard(
        max_requests=profile["max_requests"],
        max_cost=profile["max_cost"],
        input_cost_per_million=profile["input_cost_per_million"],
        output_cost_per_million=profile["output_cost_per_million"],
        pricing_source=profile["pricing_source"],
    )

    _, forbidden_values = build_forbidden_disclosure_values(
        list(STAGE38A_CLIENT_TOKENS.keys()),
        STAGE_3_5_5_IDENTITY_SECRET,
        extra_secrets=(
            STAGE_3_5_5_EVIDENCE_SECRET,
            STAGE_3_5_5_ADMIN_TOKEN,
            profile["api_key"],
            resolved_db_url,
        ),
    )

    cases_by_id = {c["case_id"]: c for c in dataset["cases"]}
    bench_steps = dataset["benchmark_sequence"]["steps"]

    raw_groq = GroqProvider(
        api_key=profile["api_key"],
        default_model=profile["model"],
        timeout_seconds=profile["timeout_seconds"],
        max_retries=profile["max_retries"],
    )
    budgeted_provider = BudgetedProviderProxy(raw_groq, budget, min_interval_seconds=pacing_seconds)

    mode_a_latencies: List[float] = []
    mode_a_usage: List[Dict[str, Any]] = []
    mode_a_stream_obs: List[Dict[str, Any]] = []
    mode_b_results: Dict[str, Any] = {}
    mode_c_results: Dict[str, Any] = {}
    workload_outcomes: List[Dict[str, Any]] = []
    http_metrics_snap: Dict[str, Any] = {}
    artifact_report: Dict[str, Any] = {}

    reset_semantic_serving_controller()
    try:
        # 1. Mode A — Direct Groq upstream baseline + streaming TTFT measurement
        if not skip_mode_comparison:
            for st in bench_steps:
                case = cases_by_id[st["case_id"]]
                req = GenerationRequest(
                    model=profile["model"],
                    messages=[ChatMessage(**m) for m in case["messages"]],
                    temperature=float(dataset["benchmark_sequence"]["temperature"]),
                    max_tokens=int(dataset["benchmark_sequence"]["max_tokens"]),
                    namespace="s38a_mode_a",
                )
                await budgeted_provider.pace_before_upstream()
                t0 = time.perf_counter()
                try:
                    res = await budgeted_provider.generate(req)
                    elapsed = (time.perf_counter() - t0) * 1000.0
                    mode_a_latencies.append(round(elapsed, 2))
                    meta = res.metadata or {}
                    mode_a_usage.append({
                        "step_id": st["step_id"],
                        "status": "ok",
                        "usage_available": bool(meta.get("usage_available", True)),
                        "usage": res.usage.to_dict() if meta.get("usage_available", True) else None,
                        "finish_reason": res.finish_reason,
                        "retry_count": meta.get("retry_count", 0),
                    })
                except LiveEvalBudgetExceededError:
                    raise
                except ProviderError as p_err:
                    mode_a_usage.append({
                        "step_id": st["step_id"],
                        "status": "provider_error",
                        "error_type": type(p_err).__name__,
                        "retry_count": getattr(p_err, "retry_count", 0),
                        "message": redact_sensitive_text(str(p_err)),
                    })

            # Streaming Mode A TTFT check (2 prompts)
            for stream_prompt in (
                "Summarize Python context managers in two short sentences.",
                "List three HTTP status codes used for caching and explain each in one line.",
            ):
                s_req = GenerationRequest(
                    model=profile["model"],
                    messages=[ChatMessage(role="user", content=stream_prompt)],
                    temperature=0.0,
                    max_tokens=140,
                    stream=True,
                    namespace="s38a_mode_a_stream",
                )
                try:
                    await budgeted_provider.pace_before_upstream()
                    async for _ch in budgeted_provider.stream(s_req):
                        pass
                except LiveEvalBudgetExceededError:
                    raise
                except ProviderError:
                    pass
            mode_a_stream_obs = list(budgeted_provider.stream_observations)

        # 2. Mode B — Normal Gateway Exact Cache Enabled, Semantic Disabled
        if not skip_mode_comparison:
            env_b = build_stage38a_env(
                resolved_db_url, profile=profile, semantic_enabled=False, semantic_serving_enabled=False
            )
            mode_b_results = await _run_http_benchmark_mode(
                mode_name="mode_b_exact_only",
                env=env_b,
                bench_steps=bench_steps,
                cases_by_id=cases_by_id,
                budgeted_provider=budgeted_provider,
                model_name=profile["model"],
                temperature=float(dataset["benchmark_sequence"]["temperature"]),
                max_tokens=int(dataset["benchmark_sequence"]["max_tokens"]),
                include_stream_repeat=True,
            )

        # 3. Mode C — Isolated Gateway Exact + Semantic Cache Enabled
        await reset_pg_tables(resolved_db_url)
        env_c = build_stage38a_env(
            resolved_db_url, profile=profile, semantic_enabled=True, semantic_serving_enabled=True
        )
        if not skip_mode_comparison:
            mode_c_results = await _run_http_benchmark_mode(
                mode_name="mode_c_exact_plus_semantic",
                env=env_c,
                bench_steps=bench_steps,
                cases_by_id=cases_by_id,
                budgeted_provider=budgeted_provider,
                model_name=profile["model"],
                temperature=float(dataset["benchmark_sequence"]["temperature"]),
                max_tokens=int(dataset["benchmark_sequence"]["max_tokens"]),
                include_stream_repeat=False,
            )

        # 4. Full Stage 3.8A Workload & Privacy/Compatibility Cases in fresh Mode C namespace
        await reset_pg_tables(resolved_db_url)
        workload_outcomes, http_metrics_snap = await _run_full_workload_cases(
            env=env_c,
            dataset=dataset,
            budgeted_provider=budgeted_provider,
            model_name=profile["model"],
            selected_case_ids=case_ids,
        )

        # 5. Artifact Verifier Live Matrix
        artifact_report = await evaluate_artifact_verifier_live(
            groq_provider=budgeted_provider,
            budget=budget,
            model_name=profile["model"],
        )

    except LiveEvalBudgetExceededError as b_exc:
        logger.warning("Stopped live evaluation cleanly on budget limit: %s", b_exc)
    finally:
        await budgeted_provider.close()
        reset_semantic_serving_controller()
        # Clean up strictly evaluation-owned PG records cleanly
        await reset_pg_tables(resolved_db_url)

    # 6. Fault-injection recovery matrix (safe controlled mocks)
    fault_matrix = await run_fault_injection_matrix(resolved_db_url)

    report: Dict[str, Any] = {
        "stage": "3.8A",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "production_readiness": False,
        "provider": profile["provider"],
        "model": profile["model"],
        "environment": {
            "postgres_fastembed_384d": True,
            "streaming_semantic_cache_supported": False,
            "defaults_unchanged": True,
            "upstream_pacing_seconds": pacing_seconds,
        },
        "budget": budget.summary(),
        "real_provider_errors": budgeted_provider.real_provider_errors,
        "mode_a_upstream_only": {
            "source": "real_groq_api",
            "latency_summary": _mode_stats(mode_a_latencies),
            "usage_records": mode_a_usage,
            "streaming": mode_a_stream_obs,
        },
        "mode_b_exact_only": mode_b_results,
        "mode_c_exact_plus_semantic": mode_c_results,
        "workload_validation": workload_outcomes,
        "artifact_verification": artifact_report,
        "failure_recovery_matrix": fault_matrix,
        "gateway_metrics_snapshot": http_metrics_snap,
        "epistemic_labels": {
            "real_groq": ["mode_a_upstream_only", "mode_b_exact_only misses", "mode_c_exact_plus_semantic misses", "workload_validation", "artifact_verification live checks"],
            "local_pg_fastembed": ["exact_hits", "semantic_hits", "vector_search_latency_ms", "embedding_latency_ms"],
            "controlled_fault_injection": ["failure_recovery_matrix"],
        },
    }

    sanitized = sanitize_report_payload(report, extra_secrets=[profile["api_key"], resolved_db_url])
    verify_no_sensitive_leaks(sanitized, forbidden_values, "stage3_8a_report", check_forbidden_keys=False)

    out_dir = output_dir or Path(os.getenv("LIVE_PROVIDER_REPORT_DIR", "/tmp/stage3_8a_eval"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "stage3_8a_live_provider_evaluation.json"
    md_path = out_dir / "stage3_8a_live_provider_evaluation.md"
    json_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    md_path.write_text(render_stage38a_markdown(sanitized), encoding="utf-8")
    return sanitized


async def _run_http_benchmark_mode(
    *,
    mode_name: str,
    env: Dict[str, str],
    bench_steps: Sequence[Dict[str, Any]],
    cases_by_id: Dict[str, Any],
    budgeted_provider: BudgetedProviderProxy,
    model_name: str,
    temperature: float,
    max_tokens: int,
    include_stream_repeat: bool,
) -> Dict[str, Any]:
    cold_latencies: List[float] = []
    warm_exact_latencies: List[float] = []
    warm_semantic_latencies: List[float] = []
    step_logs: List[Dict[str, Any]] = []
    hdr_carol = {"X-Client-Token": "tok_s38a_carol"}
    hdr_dave = {"X-Client-Token": "tok_s38a_dave"}
    hdr_admin = {"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}

    collector = RuntimeMetricsCollector()
    set_metrics_collector(collector)
    try:
        with patch.dict(os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: budgeted_provider
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    for st in bench_steps:
                        case = cases_by_id[st["case_id"]]
                        hdr = hdr_carol if st["kind"] == "cold_seed" else hdr_dave
                        if st["kind"] in ("cold_seed", "semantic_paraphrase") and not (
                            mode_name == "mode_c_exact_plus_semantic" and st["kind"] == "semantic_paraphrase"
                        ):
                            await budgeted_provider.pace_before_upstream()
                        t0 = time.perf_counter()
                        resp = await client.post(
                            "/query",
                            headers=hdr,
                            json={
                                "model": model_name,
                                "messages": case["messages"],
                                "temperature": temperature,
                                "max_tokens": max_tokens,
                            },
                        )
                        elapsed = (time.perf_counter() - t0) * 1000.0
                        if budgeted_provider.budget.terminated_early_reason:
                            raise LiveEvalBudgetExceededError(budgeted_provider.budget.terminated_early_reason)
                        data = resp.json() if resp.status_code == 200 else {}
                        ctype = data.get("cache_type", "none") if resp.status_code == 200 else "error"
                        if ctype == "exact":
                            warm_exact_latencies.append(round(elapsed, 2))
                        elif ctype == "semantic":
                            warm_semantic_latencies.append(round(elapsed, 2))
                        else:
                            cold_latencies.append(round(elapsed, 2))
                        step_logs.append({
                            "step_id": st["step_id"],
                            "case_id": st["case_id"],
                            "kind": st["kind"],
                            "status_code": resp.status_code,
                            "cached": bool(data.get("cached", False)),
                            "cache_type": ctype,
                            "latency_ms": round(elapsed, 2),
                            "usage": data.get("usage"),
                        })

                    stream_summary = None
                    if include_stream_repeat:
                        sp_payload = {
                            "model": model_name,
                            "messages": [{"role": "user", "content": "Define idempotency in HTTP APIs in one short sentence."}],
                            "temperature": 0.0,
                            "max_tokens": 90,
                            "stream": True,
                        }
                        # Cold live stream
                        await budgeted_provider.pace_before_upstream()
                        t_s1 = time.perf_counter()
                        ttft_s1: Optional[float] = None
                        async with client.stream("POST", "/query", headers=hdr_carol, json=sp_payload) as r1:
                            async for line in r1.aiter_lines():
                                if line.startswith("data: {") and ttft_s1 is None and '"done": false' in line:
                                    ttft_s1 = (time.perf_counter() - t_s1) * 1000.0
                        tot_s1 = (time.perf_counter() - t_s1) * 1000.0
                        # Warm exact cached stream
                        t_s2 = time.perf_counter()
                        ttft_s2: Optional[float] = None
                        s2_cached = False
                        async with client.stream("POST", "/query", headers=hdr_dave, json=sp_payload) as r2:
                            async for line in r2.aiter_lines():
                                if line.startswith("data: {"):
                                    if '"done": false' in line and ttft_s2 is None:
                                        ttft_s2 = (time.perf_counter() - t_s2) * 1000.0
                                    if '"done": true' in line:
                                        obj2 = json.loads(line[6:])
                                        s2_cached = bool(obj2.get("cached"))
                        tot_s2 = (time.perf_counter() - t_s2) * 1000.0
                        stream_summary = {
                            "cold_stream_ttft_ms": round(ttft_s1 or tot_s1, 2),
                            "cold_stream_total_ms": round(tot_s1, 2),
                            "warm_exact_stream_ttft_ms": round(ttft_s2 or tot_s2, 2),
                            "warm_exact_stream_total_ms": round(tot_s2, 2),
                            "warm_exact_stream_cached": s2_cached,
                        }

                    admin_snap = (await client.get("/admin/metrics", headers=hdr_admin)).json()
    finally:
        set_metrics_collector(None)

    ns_beta = admin_snap.get("namespaces", {}).get("s38a_beta", {})
    return {
        "mode": mode_name,
        "steps": step_logs,
        "cold_miss_latency": _mode_stats(cold_latencies),
        "warm_exact_hit_latency": _mode_stats(warm_exact_latencies),
        "warm_semantic_hit_latency": _mode_stats(warm_semantic_latencies),
        "exact_stream_validation": stream_summary,
        "namespace_metrics": ns_beta,
    }


async def _run_full_workload_cases(
    *,
    env: Dict[str, str],
    dataset: Dict[str, Any],
    budgeted_provider: BudgetedProviderProxy,
    model_name: str,
    selected_case_ids: Optional[Sequence[str]],
) -> tuple[List[Dict[str, Any]], Dict[str, Any]]:
    outcomes: List[Dict[str, Any]] = []
    token_hdrs = {
        "alice": {"X-Client-Token": "tok_s38a_alice"},
        "bob": {"X-Client-Token": "tok_s38a_bob"},
        "carol": {"X-Client-Token": "tok_s38a_carol"},
        "dave": {"X-Client-Token": "tok_s38a_dave"},
        "frank": {"X-Client-Token": "tok_s38a_frank"},
    }
    hdr_admin = {"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}
    sel_set = set(selected_case_ids) if selected_case_ids else None

    collector = RuntimeMetricsCollector()
    set_metrics_collector(collector)
    admin_snap: Dict[str, Any] = {}
    try:
        with patch.dict(os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: budgeted_provider
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                    for case in dataset["cases"]:
                        cid = case["case_id"]
                        if sel_set is not None and cid not in sel_set:
                            continue
                        hdr = token_hdrs[case["principal"]]
                        if case.get("setup_messages"):
                            await client.post(
                                "/query",
                                headers=hdr,
                                json={
                                    "model": model_name,
                                    "messages": case["setup_messages"],
                                    "temperature": float(case["temperature"]),
                                    "max_tokens": int(case["max_tokens"]),
                                },
                            )
                        await budgeted_provider.pace_before_upstream()
                        r1 = await client.post(
                            "/query",
                            headers=hdr,
                            json={
                                "model": model_name,
                                "messages": case["messages"],
                                "temperature": float(case["temperature"]),
                                "max_tokens": int(case["max_tokens"]),
                            },
                        )
                        if budgeted_provider.budget.terminated_early_reason:
                            raise LiveEvalBudgetExceededError(budgeted_provider.budget.terminated_early_reason)
                        d1 = r1.json() if r1.status_code == 200 else {}
                        entry: Dict[str, Any] = {
                            "case_id": cid,
                            "category": case["category"],
                            "status_code": r1.status_code,
                            "cached": bool(d1.get("cached", False)),
                            "cache_type": d1.get("cache_type", "none"),
                            "latency_ms": d1.get("latency_ms"),
                            "expected_cache_behavior": case["expected_cache_behavior"],
                            "response_length": len(d1.get("response", "") or ""),
                        }
                        sem_svc = getattr(app.state, "semantic_cache", None)
                        adm = getattr(sem_svc, "last_admission_decision", None) if sem_svc is not None else None
                        if adm is not None and not entry["cached"]:
                            entry["admission_status"] = adm.status.value
                            entry["admission_reason_code"] = adm.reason_code.value

                        if case.get("followup_messages") and case.get("followup_principal"):
                            hdr_fu = token_hdrs[case["followup_principal"]]
                            await budgeted_provider.pace_before_upstream()
                            r2 = await client.post(
                                "/query",
                                headers=hdr_fu,
                                json={
                                    "model": model_name,
                                    "messages": case["followup_messages"],
                                    "temperature": float(case["temperature"]),
                                    "max_tokens": int(case["max_tokens"]),
                                },
                            )
                            if budgeted_provider.budget.terminated_early_reason:
                                raise LiveEvalBudgetExceededError(budgeted_provider.budget.terminated_early_reason)
                            d2 = r2.json() if r2.status_code == 200 else {}
                            entry["followup_status_code"] = r2.status_code
                            entry["followup_cached"] = bool(d2.get("cached", False))
                            entry["followup_cache_type"] = d2.get("cache_type", "none")

                        outcomes.append(entry)

                    # Cross-tenant isolation spot check
                    r_xt = await client.post(
                        "/query",
                        headers=token_hdrs["frank"],
                        json={
                            "model": model_name,
                            "messages": dataset["cases"][0]["messages"],
                            "temperature": 0.0,
                            "max_tokens": 120,
                        },
                    )
                    outcomes.append({
                        "case_id": "LPE-CROSS-TENANT",
                        "category": "cross_tenant_isolation",
                        "status_code": r_xt.status_code,
                        "cached": bool(r_xt.json().get("cached", False)) if r_xt.status_code == 200 else False,
                        "cache_type": r_xt.json().get("cache_type", "none") if r_xt.status_code == 200 else "error",
                        "expected_cache_behavior": "isolated_upstream",
                    })

                    # Spoofing + DB bypass verification (0 extra Groq requests)
                    spoof_resp = await client.post(
                        "/query",
                        headers=token_hdrs["carol"],
                        json={
                            "model": model_name,
                            "messages": [{"role": "user", "content": "Hello"}],
                            "context_fingerprint": "ctx-v1:spoofed",
                        },
                    )
                    vs = getattr(app.state, "vector_store", None)
                    bypass_clean = None
                    uncertain_clean = None
                    if vs is not None and getattr(vs, "pool", None) is not None:
                        from app.core.fingerprinting import generate_fingerprint
                        sens_case = next((c for c in dataset["cases"] if c["case_id"] == "LPE-019"), None)
                        sens_fp = ""
                        if sens_case is not None:
                            sens_fp = generate_fingerprint(
                                messages=[ChatMessage(**m) for m in sens_case["messages"]],
                                model=model_name,
                                temperature=float(sens_case["temperature"]),
                                max_tokens=int(sens_case["max_tokens"]),
                                provider="groq",
                            )
                        async with vs.pool.acquire() as conn:
                            sens_cnt = await conn.fetchval(
                                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace LIKE 's38a_%' AND (fingerprint = $1 OR response LIKE '%ExamplePlaceholderPass123%');",
                                sens_fp,
                            )
                            sens_vec = await conn.fetchval(
                                "SELECT COUNT(*) FROM semantic_variant_records WHERE cache_namespace LIKE 's38a_%' AND (fingerprint = $1 OR query_text LIKE '%ExamplePlaceholderPass123%');",
                                sens_fp,
                            )
                            unc_vec = await conn.fetchval(
                                "SELECT COUNT(*) FROM semantic_variant_records WHERE cache_namespace LIKE 's38a_%' AND (metadata->'context_contract'->>'context_class') = 'CONTEXT_UNCERTAIN';"
                            )
                            bypass_clean = (sens_cnt == 0 and sens_vec == 0)
                            uncertain_clean = (unc_vec == 0)
                    outcomes.append({
                        "case_id": "LPE-SECURITY-CHECKS",
                        "category": "spoofing_and_db_bypass_audit",
                        "spoof_context_status": spoof_resp.status_code,
                        "sensitive_bypass_db_clean": bypass_clean,
                        "uncertain_not_admitted_db_clean": uncertain_clean,
                    })
                    admin_snap = (await client.get("/admin/metrics", headers=hdr_admin)).json()
    finally:
        set_metrics_collector(None)

    return outcomes, admin_snap


def render_stage38a_markdown(report: Dict[str, Any]) -> str:
    b = report["budget"]
    ma = report["mode_a_upstream_only"]
    mb = report.get("mode_b_exact_only") or {}
    mc = report.get("mode_c_exact_plus_semantic") or {}
    lines = [
        "# Stage 3.8A — Controlled Live Upstream Provider Evaluation",
        "",
        f"- Generated at: `{report['generated_at']}`",
        f"- Provider / Model: `{report['provider']}` / `{report['model']}`",
        f"- Production Readiness Claim: `{report['production_readiness']}`",
        f"- Streaming Semantic Cache Supported: `{report['environment']['streaming_semantic_cache_supported']}` (exact streaming only)",
        f"- Actual Groq Requests Made: `{b['requests_made']}` / `{b['max_requests']}`",
        f"- Usage Reported Calls / Missing Calls: `{b['usage_reported_calls']}` / `{b['usage_missing_calls']}`",
        f"- Tokens Reported (Total): `{b['total_tokens_reported']}` | Cost Status: `{b['cost_status']}` (`{b['accumulated_cost_usd']}`)",
        "",
        "## Comparable Benchmark Summary (Cold vs Warm)",
        "",
        f"- **Mode A (Upstream Only):** `{ma['latency_summary']}`",
        f"- **Mode B (Exact Cache Only) Cold Miss:** `{mb.get('cold_miss_latency')}` | **Warm Exact Hit:** `{mb.get('warm_exact_hit_latency')}`",
        f"- **Mode C (Exact + Semantic) Cold Miss:** `{mc.get('cold_miss_latency')}` | **Warm Exact Hit:** `{mc.get('warm_exact_hit_latency')}` | **Warm Semantic Hit:** `{mc.get('warm_semantic_hit_latency')}`",
        "",
        "## Controlled Fault-Injection Matrix",
        "",
        "| Case | Scenario | Source | Passed |",
        "|---|---|---|---|",
    ]
    for item in report.get("failure_recovery_matrix", []):
        lines.append(f"| `{item['id']}` | {item['scenario']} | `{item['source']}` | `{item['passed']}` |")
    return "\n".join(lines) + "\n"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3.8A Controlled Live Upstream Provider Evaluation")
    p.add_argument("--confirm-live", action="store_true", help="Explicit confirmation to incur live Groq API requests")
    p.add_argument("--model", type=str, default=None, help="Explicit Groq model override (defaults to LIVE_PROVIDER_MODEL)")
    p.add_argument("--test-database-url", type=str, default=None, help="384-d PostgreSQL + pgvector connection string")
    p.add_argument("--output-dir", type=Path, default=Path("/tmp/stage3_8a_eval"))
    p.add_argument("--max-requests", type=int, default=None, help="Hard live API call ceiling")
    p.add_argument("--cases", nargs="*", default=None, help="Optional subset of dataset case IDs (e.g. LPE-001 LPE-002)")
    p.add_argument("--skip-mode-comparison", action="store_true", help="Skip Mode A/B/C comparison loop")
    p.add_argument("--pacing-seconds", type=float, default=5.5, help="Delay between consecutive real Groq calls to avoid OTPM throttling (0 on cache hits)")
    return p.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    args = _parse_args(argv)
    report = asyncio.run(
        run_live_provider_evaluation(
            confirm_live=args.confirm_live,
            db_url=args.test_database_url,
            output_dir=args.output_dir,
            model_override=args.model,
            max_requests_override=args.max_requests,
            case_ids=args.cases,
            skip_mode_comparison=args.skip_mode_comparison,
            pacing_seconds=args.pacing_seconds,
        )
    )
    print(
        json.dumps(
            {
                "status": "ok",
                "provider": report["provider"],
                "model": report["model"],
                "requests_made": report["budget"]["requests_made"],
                "faults_passed": all(f["passed"] for f in report["failure_recovery_matrix"]),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
