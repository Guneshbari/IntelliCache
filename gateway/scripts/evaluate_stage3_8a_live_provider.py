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
import contextlib
from datetime import datetime, timezone
import hashlib
import json
import logging
import math
import os
from pathlib import Path
import subprocess
import sys
import time
from typing import Any, AsyncGenerator, Dict, List, Optional, Sequence, Tuple
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


class PhysicalRequestReservation:
    """Represents an atomically reserved physical provider request attempt and its reserved cost."""

    def __init__(
        self,
        guard: "LiveEvalBudgetGuard",
        reserved_cost: float,
        is_streaming: bool,
        attempt: int,
        ledger_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.guard = guard
        self.reserved_cost = float(reserved_cost)
        self.is_streaming = is_streaming
        self.attempt = attempt
        self.ledger_entry = ledger_entry
        self._settled = False

    async def record_success(self, usage: Optional[UsageMetrics], *, usage_available: bool) -> None:
        if self._settled:
            return
        self._settled = True
        await self.guard._reconcile_success(
            self.reserved_cost,
            usage,
            usage_available=usage_available,
            ledger_entry=self.ledger_entry,
        )

    async def record_failure(
        self,
        err: BaseException,
        *,
        dispatched: bool = True,
        partial_output: bool = False,
        usage: Optional[UsageMetrics] = None,
        usage_available: bool = False,
    ) -> None:
        if self._settled:
            return
        self._settled = True
        await self.guard._reconcile_failure(
            self.reserved_cost,
            err,
            dispatched=dispatched,
            partial_output=partial_output,
            usage=usage,
            usage_available=usage_available,
            ledger_entry=self.ledger_entry,
        )


class LiveEvalBudgetGuard:
    """
    Enforces hard physical provider request ceilings, token ceilings, and heuristic
    pre-dispatch estimated-cost reservation limits.

    Note: Because Groq serves diverse models without a single bundled model-exact tokenizer,
    pre-dispatch token/cost reservations use `heuristic_char_ratio_v1` and cannot alone
    guarantee an exact provider-billing ceiling; `strict` mode conservatively retains
    uncertain spend on ambiguous failures and tracks overruns against actual usage.
    """

    def __init__(
        self,
        *,
        max_requests: int,
        max_cost: Optional[float] = None,
        input_cost_per_million: Optional[float] = None,
        output_cost_per_million: Optional[float] = None,
        currency: str = "USD",
        pricing_source: Optional[str] = None,
        pricing_effective_date: Optional[str] = None,
        cost_mode: str = "strict",
        max_input_tokens: Optional[int] = None,
        max_output_tokens: Optional[int] = None,
    ) -> None:
        if max_requests < 1:
            raise ValueError(f"max_requests must be >= 1, got {max_requests}.")

        if cost_mode not in ("strict", "report_only"):
            raise ValueError(f"cost_mode must be 'strict' or 'report_only', got '{cost_mode}'.")

        if max_input_tokens is not None and (isinstance(max_input_tokens, bool) or int(max_input_tokens) < 1):
            raise ValueError(f"max_input_tokens must be >= 1, got {max_input_tokens}.")
        if max_output_tokens is not None and (isinstance(max_output_tokens, bool) or int(max_output_tokens) < 1):
            raise ValueError(f"max_output_tokens must be >= 1, got {max_output_tokens}.")

        for name, val in (
            ("input_cost_per_million", input_cost_per_million),
            ("output_cost_per_million", output_cost_per_million),
        ):
            if val is not None and (math.isnan(val) or math.isinf(val) or val < 0.0):
                raise ValueError(f"{name} must be a finite non-negative number, got {val}.")

        if max_cost is not None:
            if math.isnan(max_cost) or math.isinf(max_cost) or max_cost <= 0.0:
                raise ValueError(f"max_cost must be a finite strictly positive number, got {max_cost}.")
            if input_cost_per_million is None or output_cost_per_million is None:
                raise ValueError("input_cost_per_million and output_cost_per_million are required when max_cost is set.")
            if not pricing_source:
                raise ValueError("pricing_source is required when max_cost is set.")
            if not pricing_effective_date:
                raise ValueError("pricing_effective_date is required when max_cost is set.")

        self.max_requests = int(max_requests)
        self.max_cost = float(max_cost) if max_cost is not None else None
        self.input_cost_per_million = float(input_cost_per_million) if input_cost_per_million is not None else None
        self.output_cost_per_million = float(output_cost_per_million) if output_cost_per_million is not None else None
        self.currency = currency
        self.pricing_source = pricing_source
        self.pricing_effective_date = pricing_effective_date
        self.cost_mode = cost_mode
        self.max_input_tokens = int(max_input_tokens) if max_input_tokens is not None else None
        self.max_output_tokens = int(max_output_tokens) if max_output_tokens is not None else None

        # Physical and logical attempt counters
        self.physical_requests_dispatched = 0
        self.requests_made = 0  # alias for backward-compatibility
        self.logical_operations = 0
        self.retry_attempts = 0
        self.successful_responses = 0
        self.failed_attempts = 0
        self.unprocessed_failures_count = 0
        self.uncertain_failures_count = 0
        self.partial_stream_failures_count = 0
        self.budget_rejections = 0

        # Cost and token tracking (kept cleanly separated: actual vs uncertain vs released vs reserved)
        self.active_reserved_cost: float = 0.0
        self.accumulated_reserved_cost: float = 0.0
        self.reservation_overrun_cost: float = 0.0
        self.reservation_overrun_count: int = 0
        self.actual_reported_cost: float = 0.0
        self.uncertain_estimated_cost: float = 0.0
        self.released_unprocessed_cost: float = 0.0
        self.cost_ceiling_exceeded_in_report_only: bool = False

        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.total_tokens = 0
        self.usage_reported_calls = 0
        self.usage_missing_calls = 0

        self.terminated_early_reason: Optional[str] = None
        self._dispatch_phase: str = "unspecified"
        self._dispatch_case_id: str = "unspecified"
        self.dispatch_ledger: List[Dict[str, Any]] = []
        self._lock = asyncio.Lock()

    def set_dispatch_context(self, phase: str, case_id: str) -> None:
        """Annotates subsequent physical provider dispatches with sanitized provenance labels."""
        self._dispatch_phase = str(phase or "unspecified")
        self._dispatch_case_id = str(case_id or "unspecified")

    @property
    def accumulated_cost(self) -> float:
        """Total accounted spend (reported actual cost plus conservatively retained uncertain estimates)."""
        return self.actual_reported_cost + self.uncertain_estimated_cost

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

    def estimate_tokens(self, request: GenerationRequest) -> Tuple[int, int]:
        """
        Heuristic pre-dispatch token estimation (`heuristic_char_ratio_v1`).
        Prompt: ceil(chars / 3.0 * 1.5) + 32 (heuristic estimate; tokenization varies by content/model).
        Completion: request.max_tokens if positive, else max_output_tokens ceiling or 2048 fallback.
        """
        total_chars = sum(len(m.role or "") + len(m.content or "") for m in request.messages)
        prompt_tokens_est = int(math.ceil((total_chars / 3.0) * 1.5)) + 32
        if request.max_tokens is not None and request.max_tokens > 0:
            completion_tokens_est = int(request.max_tokens)
        else:
            completion_tokens_est = self.max_output_tokens if self.max_output_tokens is not None else 2048
        return prompt_tokens_est, completion_tokens_est

    async def reserve_attempt(
        self,
        request: GenerationRequest,
        *,
        attempt: int,
        is_streaming: bool,
    ) -> PhysicalRequestReservation:
        """
        Atomically reserves 1 physical provider request attempt and its heuristic estimated cost.
        Blocks and raises LiveEvalBudgetExceededError before outbound HTTP dispatch if configured limits fail.
        """
        prompt_est, comp_est = self.estimate_tokens(request)
        reserved_cost = self.calculate_cost(prompt_est, comp_est) or 0.0

        async with self._lock:
            # 1. Enforce physical request limit (always active in both strict and report_only modes)
            if self.physical_requests_dispatched >= self.max_requests:
                self.budget_rejections += 1
                self.terminated_early_reason = f"max_requests ({self.max_requests}) reached"
                raise LiveEvalBudgetExceededError(
                    f"Live evaluation budget exhausted: {self.terminated_early_reason}."
                )

            # 2. Enforce configured input-token heuristic ceiling BEFORE dispatch
            if self.max_input_tokens is not None and prompt_est > self.max_input_tokens:
                self.budget_rejections += 1
                self.terminated_early_reason = (
                    f"max_input_tokens ({self.max_input_tokens}) exceeded by heuristic input estimate ({prompt_est})"
                )
                raise LiveEvalBudgetExceededError(
                    f"Live evaluation input ceiling exceeded: {self.terminated_early_reason}."
                )

            # 3. Enforce configured / required output-token ceiling BEFORE dispatch
            if self.max_output_tokens is not None:
                if request.max_tokens is None or isinstance(request.max_tokens, bool) or request.max_tokens <= 0:
                    self.budget_rejections += 1
                    self.terminated_early_reason = (
                        f"max_output_tokens ({self.max_output_tokens}) requires explicit positive request.max_tokens"
                    )
                    raise LiveEvalBudgetExceededError(
                        f"Live evaluation output ceiling requires explicit max_tokens: {self.terminated_early_reason}."
                    )
                if request.max_tokens > self.max_output_tokens:
                    self.budget_rejections += 1
                    self.terminated_early_reason = (
                        f"max_output_tokens ({self.max_output_tokens}) exceeded by request.max_tokens ({request.max_tokens})"
                    )
                    raise LiveEvalBudgetExceededError(
                        f"Live evaluation output ceiling exceeded: {self.terminated_early_reason}."
                    )
            elif self.cost_mode == "strict" and self.max_cost is not None:
                if request.max_tokens is None or isinstance(request.max_tokens, bool) or request.max_tokens <= 0:
                    self.budget_rejections += 1
                    self.terminated_early_reason = (
                        "strict monetary cost mode requires explicit positive request.max_tokens to bound output tokens"
                    )
                    raise LiveEvalBudgetExceededError(
                        f"Live evaluation strict cost ceiling requires explicit output bound: {self.terminated_early_reason}."
                    )

            # 4. Enforce or report monetary reservation ceiling depending on cost_mode
            if self.max_cost is not None:
                if self.cost_mode == "strict":
                    if not self.pricing_configured:
                        self.budget_rejections += 1
                        self.terminated_early_reason = "pricing_not_configured"
                        raise LiveEvalBudgetExceededError(
                            "Live evaluation cost ceiling configured but pricing is not configured."
                        )
                    potential_total = (
                        self.actual_reported_cost
                        + self.uncertain_estimated_cost
                        + self.active_reserved_cost
                        + reserved_cost
                    )
                    if potential_total > (self.max_cost + 1e-9):
                        self.budget_rejections += 1
                        remaining = max(
                            0.0,
                            self.max_cost - (self.actual_reported_cost + self.uncertain_estimated_cost + self.active_reserved_cost),
                        )
                        self.terminated_early_reason = (
                            f"max_cost ({self.max_cost:.6f} {self.currency}) reached: "
                            f"estimated reservation ${reserved_cost:.6f} exceeds remaining budget (${remaining:.6f})"
                        )
                        raise LiveEvalBudgetExceededError(
                            f"Live evaluation cost ceiling reached: {self.terminated_early_reason}."
                        )
                else:
                    # report_only: never block on monetary ceiling; only record whether ceiling would be exceeded
                    potential_total = (
                        self.actual_reported_cost
                        + self.uncertain_estimated_cost
                        + self.active_reserved_cost
                        + reserved_cost
                    )
                    if self.pricing_configured and potential_total > (self.max_cost + 1e-9):
                        self.cost_ceiling_exceeded_in_report_only = True

            # 5. Apply physical dispatch & heuristic reservation atomically
            self.physical_requests_dispatched += 1
            self.requests_made = self.physical_requests_dispatched
            if attempt > 0:
                self.retry_attempts += 1
            self.active_reserved_cost += reserved_cost
            self.accumulated_reserved_cost += reserved_cost

            ledger_entry: Dict[str, Any] = {
                "dispatch_seq": self.physical_requests_dispatched,
                "phase": self._dispatch_phase,
                "case_id": self._dispatch_case_id,
                "operation": "stream" if is_streaming else "generate",
                "attempt": attempt,
                "status": "dispatched_pending",
                "usage_available": False,
                "prompt_tokens": None,
                "completion_tokens": None,
                "reserved_cost_usd": round(reserved_cost, 6) if self.pricing_configured else None,
                "actual_cost_usd": None,
            }
            self.dispatch_ledger.append(ledger_entry)

            return PhysicalRequestReservation(
                guard=self,
                reserved_cost=reserved_cost,
                is_streaming=is_streaming,
                attempt=attempt,
                ledger_entry=ledger_entry,
            )

    @staticmethod
    def _classify_failure(err: BaseException, *, dispatched: bool, partial_output: bool) -> str:
        """
        Distinguishes definitely unprocessed rejections from ambiguous/partial failures.
        - 'unprocessed': never reached model inference (not dispatched or 400/401/403/404/422/429 without partial stream).
        - 'partial_stream': streaming failed/cancelled after delivering tokens.
        - 'ambiguous': timeout, connection drop, 5xx, or cancellation after dispatch (may be billed upstream).
        """
        if not dispatched:
            return "unprocessed"
        if partial_output:
            return "partial_stream"

        non_billable_types: Tuple[type, ...] = (
            groq.AuthenticationError,
            groq.PermissionDeniedError,
            groq.BadRequestError,
            groq.NotFoundError,
            groq.UnprocessableEntityError,
            groq.RateLimitError,
            ProviderAuthenticationError,
            ProviderRateLimitError,
            ProviderConfigurationError,
        )
        if isinstance(err, non_billable_types):
            return "unprocessed"

        if isinstance(err, (groq.APIStatusError, ProviderUpstreamError)):
            code = getattr(err, "status_code", None)
            if code in (400, 401, 403, 404, 422, 429):
                return "unprocessed"

        return "ambiguous"

    def _record_reported_usage_locked(
        self,
        reserved_cost: float,
        usage: UsageMetrics,
        *,
        ledger_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.usage_reported_calls += 1
        pt = max(0, int(usage.prompt_tokens))
        ct = max(0, int(usage.completion_tokens))
        tt = max(0, int(usage.total_tokens))
        self.prompt_tokens += pt
        self.completion_tokens += ct
        self.total_tokens += tt

        actual_cost = self.calculate_cost(pt, ct)
        if ledger_entry is not None:
            ledger_entry["usage_available"] = True
            ledger_entry["prompt_tokens"] = pt
            ledger_entry["completion_tokens"] = ct
            if actual_cost is not None:
                ledger_entry["actual_cost_usd"] = round(actual_cost, 6)
        if actual_cost is not None:
            self.actual_reported_cost += actual_cost
            if actual_cost > reserved_cost:
                overrun = actual_cost - reserved_cost
                self.reservation_overrun_cost += overrun
                self.reservation_overrun_count += 1
            if (
                self.cost_mode == "report_only"
                and self.max_cost is not None
                and (self.actual_reported_cost + self.uncertain_estimated_cost) > (self.max_cost + 1e-9)
            ):
                self.cost_ceiling_exceeded_in_report_only = True

    async def _reconcile_success(
        self,
        reserved_cost: float,
        usage: Optional[UsageMetrics],
        *,
        usage_available: bool,
        ledger_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            self.active_reserved_cost = max(0.0, self.active_reserved_cost - reserved_cost)
            self.successful_responses += 1
            if ledger_entry is not None:
                ledger_entry["status"] = "succeeded"
            if not usage_available or usage is None:
                self.usage_missing_calls += 1
                if self.pricing_configured and reserved_cost > 0.0:
                    self.uncertain_estimated_cost += reserved_cost
                return

            self._record_reported_usage_locked(reserved_cost, usage, ledger_entry=ledger_entry)

    async def _reconcile_failure(
        self,
        reserved_cost: float,
        err: BaseException,
        *,
        dispatched: bool = True,
        partial_output: bool = False,
        usage: Optional[UsageMetrics] = None,
        usage_available: bool = False,
        ledger_entry: Optional[Dict[str, Any]] = None,
    ) -> None:
        async with self._lock:
            self.active_reserved_cost = max(0.0, self.active_reserved_cost - reserved_cost)
            self.failed_attempts += 1
            classification = self._classify_failure(err, dispatched=dispatched, partial_output=partial_output)
            if ledger_entry is not None:
                ledger_entry["status"] = f"failed_{classification}"

            if classification == "unprocessed":
                self.unprocessed_failures_count += 1
                if self.pricing_configured and reserved_cost > 0.0:
                    self.released_unprocessed_cost += reserved_cost
                return

            self.uncertain_failures_count += 1
            if classification == "partial_stream":
                self.partial_stream_failures_count += 1

            if usage_available and usage is not None:
                self._record_reported_usage_locked(reserved_cost, usage, ledger_entry=ledger_entry)
            elif self.pricing_configured and reserved_cost > 0.0:
                self.uncertain_estimated_cost += reserved_cost

    async def reserve_request(self) -> None:
        """Backward-compatible reservation for callers not using attempt_guard hooks."""
        req = GenerationRequest(model="default", messages=[ChatMessage(role="user", content="hello")], max_tokens=10)
        await self.reserve_attempt(req, attempt=0, is_streaming=False)

    async def record_usage(self, usage: Optional[UsageMetrics], *, usage_available: bool = True) -> None:
        """Backward-compatible usage recorder for callers not using reservation objects."""
        await self._reconcile_success(0.0, usage, usage_available=usage_available)

    def summary(self) -> Dict[str, Any]:
        if not self.pricing_configured:
            cost_status = "unavailable"
        elif self.usage_missing_calls > 0 or self.uncertain_failures_count > 0:
            if self.usage_reported_calls > 0:
                cost_status = "usage_partially_missing"
            elif self.usage_missing_calls > 0:
                cost_status = "no_usage_reported"
            else:
                cost_status = "estimated_uncertain_only"
        elif self.usage_reported_calls > 0:
            cost_status = "calculated_from_usage"
        else:
            cost_status = "no_requests"

        has_accounted_spend = (
            self.usage_reported_calls > 0
            or self.usage_missing_calls > 0
            or self.uncertain_failures_count > 0
        )
        return {
            "max_requests": self.max_requests,
            "physical_requests_dispatched": self.physical_requests_dispatched,
            "requests_made": self.physical_requests_dispatched,
            "logical_operations": self.logical_operations,
            "retry_attempts": self.retry_attempts,
            "successful_responses": self.successful_responses,
            "failed_attempts": self.failed_attempts,
            "unprocessed_failures_count": self.unprocessed_failures_count,
            "uncertain_failures_count": self.uncertain_failures_count,
            "partial_stream_failures_count": self.partial_stream_failures_count,
            "budget_rejections": self.budget_rejections,
            "max_cost": self.max_cost,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "currency": self.currency,
            "cost_mode": self.cost_mode,
            "cost_estimation_method": "heuristic_char_ratio_v1",
            "cost_guarantee": "heuristic_reservation_only_not_billing_guarantee",
            "cost_ceiling_exceeded_in_report_only": self.cost_ceiling_exceeded_in_report_only,
            "pricing_configured": self.pricing_configured,
            "pricing_source": self.pricing_source,
            "pricing_effective_date": self.pricing_effective_date,
            "prompt_tokens_reported": self.prompt_tokens if self.usage_reported_calls > 0 else None,
            "completion_tokens_reported": self.completion_tokens if self.usage_reported_calls > 0 else None,
            "total_tokens_reported": self.total_tokens if self.usage_reported_calls > 0 else None,
            "usage_reported_calls": self.usage_reported_calls,
            "usage_missing_calls": self.usage_missing_calls,
            "accumulated_reserved_cost": round(self.accumulated_reserved_cost, 6),
            "active_reserved_cost": round(self.active_reserved_cost, 6),
            "actual_reported_cost_usd": round(self.actual_reported_cost, 6) if self.pricing_configured else None,
            "uncertain_estimated_cost_usd": round(self.uncertain_estimated_cost, 6) if self.pricing_configured else None,
            "released_unprocessed_cost_usd": round(self.released_unprocessed_cost, 6) if self.pricing_configured else None,
            "estimated_total_accounted_cost_usd": (
                round(self.actual_reported_cost + self.uncertain_estimated_cost, 6)
                if self.pricing_configured
                else None
            ),
            "reservation_overrun_cost": round(self.reservation_overrun_cost, 6),
            "reservation_overrun_count": self.reservation_overrun_count,
            "accumulated_cost_usd": (
                round(
                    (self.actual_reported_cost + self.uncertain_estimated_cost)
                    if self.cost_mode == "strict"
                    else self.actual_reported_cost,
                    6,
                )
                if (self.pricing_configured and has_accounted_spend)
                else None
            ),
            "cost_status": cost_status,
            "terminated_early_reason": self.terminated_early_reason,
        }


class BudgetedProviderProxy(LLMProvider):
    """Wraps an actual GroqProvider to enforce hard request/cost budget, pacing & record TTFT/usage."""

    def __init__(self, inner: GroqProvider, budget: LiveEvalBudgetGuard, *, min_interval_seconds: float = 0.0) -> None:
        self.inner = inner
        self.budget = budget
        if hasattr(self.inner, "set_attempt_guard"):
            self.inner.set_attempt_guard(self.budget)
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

    def set_dispatch_context(self, phase: str, case_id: str) -> None:
        self.budget.set_dispatch_context(phase, case_id)

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.budget.logical_operations += 1
        if getattr(self.inner, "attempt_guard", None) is None and not hasattr(self.inner, "set_attempt_guard"):
            # Fallback for custom mocks that do not support attempt_guard hooks
            await self.budget.reserve_attempt(request, attempt=0, is_streaming=False)
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
        return res

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        self.budget.logical_operations += 1
        if getattr(self.inner, "attempt_guard", None) is None and not hasattr(self.inner, "set_attempt_guard"):
            await self.budget.reserve_attempt(request, attempt=0, is_streaming=True)
        t0 = time.perf_counter()
        ttft_ms: Optional[float] = None
        chunk_count = 0
        final_chunk: Optional[StreamChunk] = None
        text_parts: List[str] = []
        try:
            async with contextlib.aclosing(self.inner.stream(request)) as inner_stream:
                async for chunk in inner_stream:
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
    currency: Optional[str] = None,
    pricing_source: Optional[str] = None,
    pricing_effective_date: Optional[str] = None,
    cost_mode: Optional[str] = None,
    max_input_tokens: Optional[int] = None,
    max_output_tokens: Optional[int] = None,
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

    env_max_in_tok = os.getenv("LIVE_PROVIDER_MAX_INPUT_TOKENS", "").strip()
    eff_max_in_tok = int(max_input_tokens) if max_input_tokens is not None else (int(env_max_in_tok) if env_max_in_tok else None)
    env_max_out_tok = os.getenv("LIVE_PROVIDER_MAX_OUTPUT_TOKENS", "").strip()
    eff_max_out_tok = int(max_output_tokens) if max_output_tokens is not None else (int(env_max_out_tok) if env_max_out_tok else None)

    if eff_timeout <= 0 or eff_timeout > 300.0:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_TIMEOUT_SECONDS out of bounds: {eff_timeout}")
    if eff_retries < 0 or eff_retries > 5:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_RETRIES out of bounds: {eff_retries}")
    if eff_max_req < 1 or eff_max_req > 500:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_REQUESTS out of bounds: {eff_max_req}")
    if eff_max_cost is not None and (math.isnan(eff_max_cost) or math.isinf(eff_max_cost) or eff_max_cost <= 0):
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_COST must be a finite number > 0, got {eff_max_cost}")
    if eff_max_in_tok is not None and eff_max_in_tok < 1:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_INPUT_TOKENS must be >= 1, got {eff_max_in_tok}")
    if eff_max_out_tok is not None and eff_max_out_tok < 1:
        raise ProviderConfigurationError(f"LIVE_PROVIDER_MAX_OUTPUT_TOKENS must be >= 1, got {eff_max_out_tok}")

    env_in_cost = os.getenv("LIVE_PROVIDER_INPUT_COST_PER_MILLION", "").strip()
    env_out_cost = os.getenv("LIVE_PROVIDER_OUTPUT_COST_PER_MILLION", "").strip()
    in_cpm = float(input_cost_per_million) if input_cost_per_million is not None else (float(env_in_cost) if env_in_cost else None)
    out_cpm = float(output_cost_per_million) if output_cost_per_million is not None else (float(env_out_cost) if env_out_cost else None)

    for cost_name, cost_val in (
        ("LIVE_PROVIDER_INPUT_COST_PER_MILLION", in_cpm),
        ("LIVE_PROVIDER_OUTPUT_COST_PER_MILLION", out_cpm),
    ):
        if cost_val is not None and (math.isnan(cost_val) or math.isinf(cost_val) or cost_val < 0.0):
            raise ProviderConfigurationError(f"{cost_name} must be finite and >= 0.0, got {cost_val}")

    src = pricing_source or (os.getenv("LIVE_PROVIDER_PRICING_SOURCE", "").strip() or None)
    eff_date = pricing_effective_date or (os.getenv("LIVE_PROVIDER_PRICING_EFFECTIVE_DATE", "").strip() or None)
    curr = (currency or os.getenv("LIVE_PROVIDER_CURRENCY", "USD")).strip().upper()
    mode = (cost_mode or os.getenv("LIVE_PROVIDER_COST_MODE", "strict")).strip().lower()

    if eff_max_cost is not None:
        if in_cpm is None or out_cpm is None:
            raise ProviderConfigurationError(
                "When LIVE_PROVIDER_MAX_COST is configured, both LIVE_PROVIDER_INPUT_COST_PER_MILLION and LIVE_PROVIDER_OUTPUT_COST_PER_MILLION are required."
            )
        if not src:
            raise ProviderConfigurationError(
                "When LIVE_PROVIDER_MAX_COST is configured, LIVE_PROVIDER_PRICING_SOURCE is required."
            )
        if not eff_date:
            raise ProviderConfigurationError(
                "When LIVE_PROVIDER_MAX_COST is configured, LIVE_PROVIDER_PRICING_EFFECTIVE_DATE is required."
            )

    if len(curr) != 3 or not curr.isalpha():
        raise ProviderConfigurationError(f"LIVE_PROVIDER_CURRENCY must be a valid 3-letter currency code, got '{curr}'.")

    if mode not in ("strict", "report_only"):
        raise ProviderConfigurationError(f"LIVE_PROVIDER_COST_MODE must be 'strict' or 'report_only', got '{mode}'.")

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
        "pricing_effective_date": eff_date,
        "currency": curr,
        "cost_mode": mode,
        "max_input_tokens": eff_max_in_tok,
        "max_output_tokens": eff_max_out_tok,
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
    env: Dict[str, str] = {
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
        "LIVE_PROVIDER_COST_MODE": str(profile.get("cost_mode", "strict")),
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
    if profile.get("max_input_tokens") is not None:
        env["LIVE_PROVIDER_MAX_INPUT_TOKENS"] = str(profile["max_input_tokens"])
    if profile.get("max_output_tokens") is not None:
        env["LIVE_PROVIDER_MAX_OUTPUT_TOKENS"] = str(profile["max_output_tokens"])
    return env


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

    async def _call(prompt: str, max_tokens: int = 160, system: Optional[str] = None, *, check_id: str = "ART-LIVE") -> GenerationResult:
        if hasattr(groq_provider, "set_dispatch_context"):
            groq_provider.set_dispatch_context("direct_artifact_verifier", check_id)
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
    r_json = await _call(schema_prompt, max_tokens=120, check_id="ART-LIVE-JSON-SCHEMA")
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
    r_md = await _call(
        "Explain Python tuples in two short sentences with one tiny fenced python snippet.",
        max_tokens=450,
        check_id="ART-LIVE-MARKDOWN-WHEN-JSON-REQUIRED",
    )
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
    r_trunc = await _call(
        "Write a detailed four-paragraph essay explaining TCP congestion control.",
        max_tokens=8,
        check_id="ART-LIVE-TRUNCATED-MAX-TOKENS",
    )
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
    r_code = await _call(code_prompt, max_tokens=200, check_id="ART-LIVE-CODE-ONLY")
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
    max_cost_override: Optional[float] = None,
    input_cost_override: Optional[float] = None,
    output_cost_override: Optional[float] = None,
    currency_override: Optional[str] = None,
    pricing_source_override: Optional[str] = None,
    pricing_effective_date_override: Optional[str] = None,
    cost_mode_override: Optional[str] = None,
    max_input_tokens_override: Optional[int] = None,
    max_output_tokens_override: Optional[int] = None,
    case_ids: Optional[Sequence[str]] = None,
    skip_mode_comparison: bool = False,
    pacing_seconds: float = 5.5,
) -> Dict[str, Any]:
    """Executes the full Stage 3.8A controlled live-provider validation and persists JSON/MD reports."""
    profile = validate_live_eval_profile(
        confirm_live=confirm_live,
        model_name=model_override,
        max_requests=max_requests_override,
        max_cost=max_cost_override,
        input_cost_per_million=input_cost_override,
        output_cost_per_million=output_cost_override,
        currency=currency_override,
        pricing_source=pricing_source_override,
        pricing_effective_date=pricing_effective_date_override,
        cost_mode=cost_mode_override,
        max_input_tokens=max_input_tokens_override,
        max_output_tokens=max_output_tokens_override,
    )
    resolved_db_url = db_url or await resolve_fastembed_database_url()
    await reset_pg_tables(resolved_db_url, include_runtime_control=True)

    dataset_bytes = DATASET_PATH.read_bytes()
    dataset_sha256 = hashlib.sha256(dataset_bytes).hexdigest()
    dataset = json.loads(dataset_bytes.decode("utf-8"))

    commit_sha = os.getenv("GITHUB_SHA")
    if not commit_sha:
        try:
            commit_sha = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
        except Exception:
            commit_sha = "unknown"

    budget = LiveEvalBudgetGuard(
        max_requests=profile["max_requests"],
        max_cost=profile["max_cost"],
        input_cost_per_million=profile["input_cost_per_million"],
        output_cost_per_million=profile["output_cost_per_million"],
        currency=profile["currency"],
        pricing_source=profile["pricing_source"],
        pricing_effective_date=profile["pricing_effective_date"],
        cost_mode=profile["cost_mode"],
        max_input_tokens=profile["max_input_tokens"],
        max_output_tokens=profile["max_output_tokens"],
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
                budgeted_provider.set_dispatch_context("mode_a_upstream_only", st["case_id"])
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
        await reset_pg_tables(resolved_db_url, include_runtime_control=True)
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
        await reset_pg_tables(resolved_db_url, include_runtime_control=True)
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
        await reset_pg_tables(resolved_db_url, include_runtime_control=True)

    # 6. Fault-injection recovery matrix (safe controlled mocks)
    fault_matrix = await run_fault_injection_matrix(resolved_db_url)

    phase_counts: Dict[str, int] = {}
    for item in budget.dispatch_ledger:
        ph = str(item.get("phase") or "unspecified")
        phase_counts[ph] = phase_counts.get(ph, 0) + 1
    gw_attempted = (
        (http_metrics_snap or {})
        .get("aggregate", {})
        .get("provider_and_savings", {})
        .get("provider_calls_attempted", 0)
    )
    gw_dispatches = phase_counts.get("gateway_workload", 0)

    report: Dict[str, Any] = {
        "stage": "3.8A.2",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "commit_sha": commit_sha,
        "dataset": {
            "name": dataset.get("metadata", {}).get("dataset_name", "stage3_8a_live_provider_dataset"),
            "version": dataset.get("metadata", {}).get("version", "1.0.0"),
            "sha256": dataset_sha256,
        },
        "production_readiness": False,
        "benchmark_status": "not_run_mode_comparison_skipped" if skip_mode_comparison else "completed",
        "provider": profile["provider"],
        "model": profile["model"],
        "environment": {
            "postgres_fastembed_384d": True,
            "streaming_semantic_cache_supported": False,
            "defaults_unchanged": True,
            "upstream_pacing_seconds": pacing_seconds,
        },
        "budget": budget.summary(),
        "dispatch_reconciliation": {
            "physical_dispatches_total": budget.physical_requests_dispatched,
            "by_phase": phase_counts,
            "gateway_metrics_provider_calls_attempted": gw_attempted,
            "gateway_ledger_matches_metrics": gw_dispatches == gw_attempted,
            "ledger_total_matches_physical_dispatches": sum(phase_counts.values()) == budget.physical_requests_dispatched,
        },
        "dispatch_ledger": budget.dispatch_ledger,
        "latency_diagnostic_note": (
            "Gateway latency percentiles calculated on small sample counts (<20) are diagnostic "
            "smoke observations only and do not establish tail-latency guarantees."
        ),
        "real_provider_errors": budgeted_provider.real_provider_errors,
        "mode_a_upstream_only": {
            "source": "real_groq_api",
            "status": "not_run_skipped" if skip_mode_comparison else "completed",
            "latency_summary": _mode_stats(mode_a_latencies),
            "usage_records": mode_a_usage,
            "streaming": mode_a_stream_obs,
        },
        "mode_b_exact_only": mode_b_results if mode_b_results else {"status": "not_run_skipped" if skip_mode_comparison else "empty"},
        "mode_c_exact_plus_semantic": mode_c_results if mode_c_results else {"status": "not_run_skipped" if skip_mode_comparison else "empty"},
        "workload_validation": workload_outcomes,
        "artifact_verification": artifact_report,
        "failure_recovery_matrix": fault_matrix,
        "gateway_metrics_snapshot": http_metrics_snap,
        "limitations": [
            "Evaluated against Groq tier-1/free rate limits (requires upstream pacing).",
            "Streaming responses only support exact match caching, not semantic cache admission or lookup.",
            "Pre-dispatch token estimation uses heuristic_char_ratio_v1; strict mode enforces an estimated-cost reservation ceiling with explicit output bounds, not a guaranteed billing ceiling.",
            "Ambiguous failures (timeouts, connection drops, cancellations after dispatch) and partial stream failures conservatively retain reserved estimates as uncertain spend.",
            "Small-sample latency percentiles (<20 samples) are diagnostic observations only; comparative Mode A/B/C benchmarks remain empty when --skip-mode-comparison is used.",
            "Live evaluation opt-in only; safe production defaults remain disabled.",
        ],
        "epistemic_labels": {
            "real_groq": [
                "mode_a_upstream_only",
                "mode_b_exact_only misses",
                "mode_c_exact_plus_semantic misses",
                "workload_validation misses",
                "artifact_verification live checks",
                "dispatch_ledger",
            ],
            "local_pg_fastembed": [
                "exact_hits",
                "semantic_hits",
                "vector_search_latency_ms",
                "embedding_latency_ms",
                "LPE-SECURITY-CHECKS db queries",
            ],
            "controlled_fault_injection": ["failure_recovery_matrix"],
        },
    }

    sanitized = sanitize_report_payload(report, extra_secrets=[profile["api_key"], resolved_db_url])
    verify_no_sensitive_leaks(sanitized, forbidden_values, "stage3_8a_report_json", check_forbidden_keys=False)

    md_content = render_stage38a_markdown(sanitized)
    verify_no_sensitive_leaks(md_content, forbidden_values, "stage3_8a_report_markdown", check_forbidden_keys=False)

    out_dir = output_dir or Path(os.getenv("LIVE_PROVIDER_REPORT_DIR", "/tmp/stage3_8a_eval"))
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "stage3_8a_live_provider_report.json"
    md_path = out_dir / "stage3_8a_live_provider_report.md"
    json_path.write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    md_path.write_text(md_content, encoding="utf-8")

    # Backward-compatible aliases
    (out_dir / "stage3_8a_live_provider_evaluation.json").write_text(json.dumps(sanitized, indent=2), encoding="utf-8")
    (out_dir / "stage3_8a_live_provider_evaluation.md").write_text(md_content, encoding="utf-8")
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
                        budgeted_provider.set_dispatch_context(mode_name, st["case_id"])
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
                        budgeted_provider.set_dispatch_context("gateway_workload", cid)
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
                        if not entry["cached"]:
                            entry["admission_status"] = d1.get("admission_status") or "BYPASS"
                            entry["admission_reason_code"] = (
                                d1.get("admission_reason_code") or "CACHE_BYPASSED_SENSITIVE_OR_INELIGIBLE"
                            )

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
                    budgeted_provider.set_dispatch_context("gateway_workload", "LPE-CROSS-TENANT")
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
    b = report.get("budget", {})
    ma = report.get("mode_a_upstream_only") or {}
    mb = report.get("mode_b_exact_only") or {}
    mc = report.get("mode_c_exact_plus_semantic") or {}
    ds = report.get("dataset") or {}
    env = report.get("environment") or {}
    rec = report.get("dispatch_reconciliation") or {}
    ledger = report.get("dispatch_ledger") or []
    curr = b.get("currency", "USD")

    lines = [
        "# Stage 3.8A — Controlled Live Upstream Provider Evaluation",
        "",
        f"- Generated at: `{report.get('generated_at')}`",
        f"- Commit SHA: `{report.get('commit_sha', 'unknown')}`",
        f"- Dataset: `{ds.get('name', 'stage3_8a')}` v`{ds.get('version', '1.0')}` (SHA-256: `{ds.get('sha256', 'unknown')}`)",
        f"- Provider / Model: `{report.get('provider')}` / `{report.get('model')}`",
        f"- Benchmark Status: `{report.get('benchmark_status', 'unknown')}` | Production Readiness Claim: `{report.get('production_readiness')}`",
        f"- Streaming Semantic Cache Supported: `{env.get('streaming_semantic_cache_supported')}` (exact streaming only)",
        f"- Physical Groq Dispatches: `{b.get('physical_requests_dispatched', b.get('requests_made', 0))}` / `{b.get('max_requests')}`",
        f"- Dispatch Reconciliation: `{rec.get('by_phase')}` | Gateway `/admin/metrics` provider calls: `{rec.get('gateway_metrics_provider_calls_attempted')}` | Reconciled: `{rec.get('gateway_ledger_matches_metrics')}`",
        f"- Logical Operations: `{b.get('logical_operations', 0)}` | Retries: `{b.get('retry_attempts', 0)}`",
        f"- Successful Responses: `{b.get('successful_responses', 0)}` | Failed Attempts: `{b.get('failed_attempts', 0)}` (Unprocessed: `{b.get('unprocessed_failures_count', 0)}`, Uncertain/Ambiguous: `{b.get('uncertain_failures_count', 0)}`, Partial Stream: `{b.get('partial_stream_failures_count', 0)}`) | Budget Rejections: `{b.get('budget_rejections', 0)}`",
        f"- Token Ceilings (Input Heuristic / Output Cap): `{b.get('max_input_tokens')}` / `{b.get('max_output_tokens')}`",
        f"- Usage Reported Calls / Missing Calls: `{b.get('usage_reported_calls', 0)}` / `{b.get('usage_missing_calls', 0)}`",
        f"- Tokens Reported (Total): `{b.get('total_tokens_reported')}` (Prompt: `{b.get('prompt_tokens_reported')}`, Completion: `{b.get('completion_tokens_reported')}`)",
        f"- Cost Ceiling ({b.get('cost_mode', 'strict')}): `{b.get('max_cost')}` {curr} | Accounted Total Spend: `{b.get('accumulated_cost_usd')}` {curr}",
        f"- Spend Breakdown — Actual Reported: `{b.get('actual_reported_cost_usd')}` {curr} | Uncertain Retained: `{b.get('uncertain_estimated_cost_usd')}` {curr} | Released Unprocessed: `{b.get('released_unprocessed_cost_usd')}` {curr} | Pre-Dispatch Reserved Total: `{b.get('accumulated_reserved_cost')}` {curr}",
        f"- Cost Estimation Method / Guarantee: `{b.get('cost_estimation_method', 'heuristic_char_ratio_v1')}` (`{b.get('cost_guarantee', 'heuristic_reservation_only_not_billing_guarantee')}`)",
        f"- Cost Status: `{b.get('cost_status')}` | Pricing Source: `{b.get('pricing_source')}` ({b.get('pricing_effective_date')})",
        f"- Reservation Overruns: count=`{b.get('reservation_overrun_count', 0)}`, cost=`{b.get('reservation_overrun_cost', 0.0)}`",
        "",
        "## Comparable Benchmark Summary (Cold vs Warm)",
        "",
        f"- **Benchmark Status:** `{report.get('benchmark_status', 'unknown')}`",
        f"- **Mode A (Upstream Only):** `{ma.get('latency_summary')}`",
        f"- **Mode B (Exact Cache Only) Cold Miss:** `{mb.get('cold_miss_latency')}` | **Warm Exact Hit:** `{mb.get('warm_exact_hit_latency')}`",
        f"- **Mode C (Exact + Semantic) Cold Miss:** `{mc.get('cold_miss_latency')}` | **Warm Exact Hit:** `{mc.get('warm_exact_hit_latency')}` | **Warm Semantic Hit:** `{mc.get('warm_semantic_hit_latency')}`",
    ]
    if ledger:
        lines.extend([
            "",
            "## Physical Dispatch Ledger (Sanitized)",
            "",
            "| Seq | Phase | Case / Check ID | Op | Attempt | Status | Tokens (In/Out) | Actual Cost USD |",
            "|---|---|---|---|---|---|---|---|",
        ])
        for row in ledger:
            lines.append(
                f"| `{row.get('dispatch_seq')}` | `{row.get('phase')}` | `{row.get('case_id')}` | "
                f"`{row.get('operation')}` | `{row.get('attempt')}` | `{row.get('status')}` | "
                f"`{row.get('prompt_tokens')}/{row.get('completion_tokens')}` | `{row.get('actual_cost_usd')}` |"
            )

    lines.extend([
        "",
        "## Controlled Fault-Injection Matrix",
        "",
        "| Case | Scenario | Source | Passed |",
        "|---|---|---|---|",
    ])
    for item in report.get("failure_recovery_matrix", []):
        lines.append(f"| `{item['id']}` | {item['scenario']} | `{item['source']}` | `{item['passed']}` |")

    lims = report.get("limitations", [])
    if lims:
        lines.extend(["", "## Limitations and Operational Scope", ""])
        for lim in lims:
            lines.append(f"- {lim}")

    return "\n".join(lines) + "\n"


def _parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Stage 3.8A Controlled Live Upstream Provider Evaluation")
    p.add_argument("--confirm-live", action="store_true", help="Explicit confirmation to incur live Groq API requests")
    p.add_argument("--model", type=str, default=None, help="Explicit Groq model override (defaults to LIVE_PROVIDER_MODEL)")
    p.add_argument("--test-database-url", type=str, default=None, help="384-d PostgreSQL + pgvector connection string")
    p.add_argument("--output-dir", type=Path, default=Path("/tmp/stage3_8a_eval"))
    p.add_argument("--max-requests", type=int, default=None, help="Hard live API physical call ceiling")
    p.add_argument("--max-cost", type=float, default=None, help="Hard live API spend ceiling in USD")
    p.add_argument("--input-cost", type=float, default=None, help="Input tokens price per million")
    p.add_argument("--output-cost", type=float, default=None, help="Output tokens price per million")
    p.add_argument("--currency", type=str, default=None, help="Currency code (default USD)")
    p.add_argument("--pricing-source", type=str, default=None, help="Documentation/pricing source reference")
    p.add_argument("--pricing-effective-date", type=str, default=None, help="Effective date of pricing rates (e.g. 2026-11-27)")
    p.add_argument("--cost-mode", type=str, default=None, choices=["strict", "report_only"], help="Cost enforcement mode")
    p.add_argument("--max-input-tokens", type=int, default=None, help="Heuristic max input token ceiling per attempt")
    p.add_argument("--max-output-tokens", type=int, default=None, help="Hard max output token ceiling per attempt")
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
            max_cost_override=args.max_cost,
            input_cost_override=args.input_cost,
            output_cost_override=args.output_cost,
            currency_override=args.currency,
            pricing_source_override=args.pricing_source,
            pricing_effective_date_override=args.pricing_effective_date,
            cost_mode_override=args.cost_mode,
            max_input_tokens_override=args.max_input_tokens,
            max_output_tokens_override=args.max_output_tokens,
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
