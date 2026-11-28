"""
PostgreSQL-Backed Distributed SingleFlight Coordinator (Stage 3.9).

Coordinates equivalent eligible requests across independent gateway worker processes
using PostgreSQL atomic upserts, unguessable owner tokens, monotonically increasing
fencing tokens, bounded leases, background lease renewal, and bounded point-poll waiting.

Privacy & Security Invariants:
- Coordination keys are SHA-256 digests over canonical compatibility and privacy attributes;
  raw prompts, system instructions, plaintext principal IDs, and API keys are never stored.
- Completed exact responses are persisted into PostgreSQL `cache_entries` BEFORE publishing
  completion so remote followers re-enter the standard exact-cache read & validation path.
- Completed semantic hits publish only `outcome_type='semantic'` so remote followers execute
  independent semantic retrieval, compatibility validation, and Stage 3.8B.1 Checkpoint 2 checks.
- No Redis or shared process memory is used.
- Does NOT guarantee exactly-once external LLM provider execution under leader crash/timeout.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import logging
import secrets
import time
from typing import Any, Callable, Dict, Optional, Tuple

from .contracts import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    GenerationRequest,
    normalize_cache_visibility,
    normalize_principal_id,
)
from .fingerprinting import _normalize_exact_constraint_value
from ..metrics import RuntimeMetricsCollector, get_metrics_collector

logger = logging.getLogger("intellicache.gateway.core.distributed_singleflight")

_COMPATIBILITY_METADATA_KEYS = (
    "language",
    "framework",
    "runtime",
    "dependency",
    "dependencies",
    "sdk_version",
    "api_version",
    "task_kind",
)

_DEFAULT_QUERY_TIMEOUT_SECONDS = 2.0


@dataclass(frozen=True)
class DistributedSingleFlightConfig:
    """Configuration governing PostgreSQL distributed SingleFlight leases and waiting."""

    lease_seconds: float = 30.0
    renewal_interval_seconds: float = 10.0
    follower_wait_timeout_seconds: float = 35.0
    poll_interval_seconds: float = 0.05
    poll_max_interval_seconds: float = 0.25
    cleanup_batch_size: int = 100
    query_timeout_seconds: float = _DEFAULT_QUERY_TIMEOUT_SECONDS
    max_claim_attempts: int = 3
    completion_retention_seconds: float = 5.0

    def __post_init__(self) -> None:
        if self.lease_seconds < 0.05 or self.lease_seconds > 300.0:
            raise ValueError(f"lease_seconds must be in [0.05, 300.0], got {self.lease_seconds}.")
        if self.renewal_interval_seconds <= 0.0 or self.renewal_interval_seconds >= self.lease_seconds:
            raise ValueError(
                f"renewal_interval_seconds ({self.renewal_interval_seconds}) must be > 0 and < lease_seconds ({self.lease_seconds})."
            )
        if self.follower_wait_timeout_seconds <= 0.0 or self.follower_wait_timeout_seconds > 600.0:
            raise ValueError(
                f"follower_wait_timeout_seconds must be in (0.0, 600.0], got {self.follower_wait_timeout_seconds}."
            )
        if self.poll_interval_seconds <= 0.0 or self.poll_interval_seconds > 5.0:
            raise ValueError(f"poll_interval_seconds must be in (0.0, 5.0], got {self.poll_interval_seconds}.")
        if self.cleanup_batch_size < 1 or self.cleanup_batch_size > 10000:
            raise ValueError(f"cleanup_batch_size must be in [1, 10000], got {self.cleanup_batch_size}.")
        if self.query_timeout_seconds <= 0.0 or self.query_timeout_seconds > 30.0:
            raise ValueError(f"query_timeout_seconds must be in (0.0, 30.0], got {self.query_timeout_seconds}.")
        if self.max_claim_attempts < 1 or self.max_claim_attempts > 10:
            raise ValueError(f"max_claim_attempts must be in [1, 10], got {self.max_claim_attempts}.")


@dataclass(frozen=True)
class DistributedLease:
    """Authoritative lease token held by an elected distributed SingleFlight leader."""

    coordination_key: str
    cache_namespace: str
    visibility: str
    owner_token: str
    fencing_token: int
    lease_expires_at: datetime
    reclaimed_expired: bool = False


@dataclass(frozen=True)
class DistributedLeaseSnapshot:
    """Observed state of a coordination row during conflict inspection or follower polling."""

    coordination_key: str
    status: str
    owner_token: str
    fencing_token: int
    outcome_type: Optional[str]
    failure_code: Optional[str]
    lease_expires_at: datetime
    updated_at: datetime
    completed_at: Optional[datetime]


@dataclass(frozen=True)
class DistributedWaitResult:
    """Outcome returned when a distributed follower finishes bounded waiting."""

    status: str  # "completed" | "failed" | "expired" | "timeout" | "error"
    outcome_type: Optional[str] = None
    failure_code: Optional[str] = None
    snapshot: Optional[DistributedLeaseSnapshot] = None
    waited_ms: float = 0.0


def compute_distributed_coordination_key(
    *,
    namespace: str,
    visibility: str,
    owner_principal_id: Optional[str],
    allow_shared_reads: bool,
    fingerprint: str,
    request: GenerationRequest,
    context_class: str,
    context_fingerprint: Optional[str],
) -> str:
    """
    Computes an opaque 64-hex SHA-256 distributed coordination key over all fields
    that affect generation compatibility, context compatibility, and privacy scope.
    Never exposes raw prompts, system instructions, or plaintext principal IDs.
    """
    norm_ns = (namespace or "default").strip()
    norm_vis = normalize_cache_visibility(visibility)
    norm_owner = normalize_principal_id(owner_principal_id) if norm_vis == CACHE_VISIBILITY_PRIVATE else None
    owner_digest = (
        hashlib.sha256(f"owner:{norm_owner}".encode("utf-8")).hexdigest()
        if norm_owner
        else ""
    )

    extra_compat: Dict[str, Any] = {}
    req_meta = getattr(request, "metadata", None)
    if isinstance(req_meta, dict) and req_meta:
        for k in _COMPATIBILITY_METADATA_KEYS:
            v = req_meta.get(k)
            if v is not None:
                extra_compat[k] = _normalize_exact_constraint_value(k, v)

    conv_digest = ""
    if context_class != "CONTEXT_INDEPENDENT":
        conv_id = (getattr(request, "conversation_id", None) or "").strip()
        if conv_id:
            conv_digest = hashlib.sha256(f"conv:{conv_id}".encode("utf-8")).hexdigest()

    payload = {
        "v": "dsf-v1",
        "ns": norm_ns,
        "vis": norm_vis,
        "owner_digest": owner_digest,
        "allow_shared_reads": bool(allow_shared_reads),
        "fp": fingerprint,
        "stream": bool(getattr(request, "stream", False)),
        "ctx_class": str(context_class),
        "ctx_fp": context_fingerprint or "",
        "conv_digest": conv_digest,
        "compat": extra_compat,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _as_utc(dt: Any) -> datetime:
    if isinstance(dt, datetime):
        return dt.astimezone(timezone.utc) if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
    raise RuntimeError(f"Expected datetime from PostgreSQL, got {type(dt).__name__}.")


class PostgresDistributedSingleFlight:
    """
    PostgreSQL-backed distributed SingleFlight lease coordinator.

    Connections are acquired strictly for short (<1ms) point queries and NEVER
    held open across upstream LLM calls or follower wait sleeps.
    """

    def __init__(
        self,
        pool: Any,
        config: Optional[DistributedSingleFlightConfig] = None,
        *,
        metrics_collector: Optional[RuntimeMetricsCollector] = None,
        owner_token_factory: Optional[Callable[[], str]] = None,
    ) -> None:
        self.pool = pool
        self.config = config or DistributedSingleFlightConfig()
        self._metrics_collector = metrics_collector
        self._owner_token_factory = owner_token_factory or (lambda: secrets.token_hex(24))
        self._cleanup_counter = 0

    def _collector(self) -> RuntimeMetricsCollector:
        return self._metrics_collector if self._metrics_collector is not None else get_metrics_collector()

    def _record_db_latency(self, namespace: str, start_perf: float) -> None:
        elapsed_ms = (time.perf_counter() - start_perf) * 1000.0
        self._collector().record_latency(namespace, "distributed_singleflight_database_latency_ms", elapsed_ms)

    async def try_acquire_or_inspect(
        self,
        *,
        coordination_key: str,
        namespace: str,
        visibility: str,
        reclaim_completed_updated_at: Optional[datetime] = None,
    ) -> Tuple[Optional[DistributedLease], Optional[DistributedLeaseSnapshot]]:
        """
        Atomically attempts to claim leadership for `coordination_key`.

        Returns:
          - `(DistributedLease, None)` if this worker is elected leader.
          - `(None, DistributedLeaseSnapshot)` if another worker owns an active lease or
            just completed a handoff row (`status='completed'`).
        """
        norm_vis = normalize_cache_visibility(visibility)
        if norm_vis not in (CACHE_VISIBILITY_PRIVATE, CACHE_VISIBILITY_TENANT_SHARED):
            raise ValueError(f"Unsupported distributed SingleFlight visibility: '{visibility}'.")
        ns = (namespace or "default").strip()
        owner_token = self._owner_token_factory()
        timeout = self.config.query_timeout_seconds

        db_start = time.perf_counter()
        try:
            async with self.pool.acquire() as conn:
                async with conn.transaction():
                    # Atomic upsert: succeeds only when no row exists or the existing row is
                    # reclaimable (failed, expired, expired pending lease, expired completion grace,
                    # or explicit stale completed reclaim when exact cache read missed).
                    claimed = await conn.fetchrow(
                        """
                        WITH existing AS (
                            SELECT status, lease_expires_at
                            FROM distributed_singleflight_leases
                            WHERE coordination_key = $1
                        )
                        INSERT INTO distributed_singleflight_leases (
                            coordination_key, cache_namespace, visibility, owner_token,
                            fencing_token, status, outcome_type, failure_code,
                            lease_expires_at, created_at, updated_at, completed_at
                        ) VALUES (
                            $1, $2, $3, $4,
                            1, 'pending', NULL, NULL,
                            NOW() + make_interval(secs => $5), NOW(), NOW(), NULL
                        )
                        ON CONFLICT (coordination_key) DO UPDATE SET
                            cache_namespace = EXCLUDED.cache_namespace,
                            visibility = EXCLUDED.visibility,
                            owner_token = EXCLUDED.owner_token,
                            fencing_token = distributed_singleflight_leases.fencing_token + 1,
                            status = 'pending',
                            outcome_type = NULL,
                            failure_code = NULL,
                            lease_expires_at = NOW() + make_interval(secs => $5),
                            updated_at = NOW(),
                            completed_at = NULL
                        WHERE
                            distributed_singleflight_leases.status IN ('failed', 'expired')
                            OR (
                                distributed_singleflight_leases.status IN ('pending', 'completed')
                                AND distributed_singleflight_leases.lease_expires_at <= NOW()
                            )
                            OR (
                                $6::timestamptz IS NOT NULL
                                AND distributed_singleflight_leases.status = 'completed'
                                AND distributed_singleflight_leases.updated_at = $6::timestamptz
                            )
                        RETURNING
                            owner_token,
                            fencing_token,
                            lease_expires_at,
                            COALESCE(
                                (
                                    SELECT status = 'pending' AND lease_expires_at <= NOW()
                                    FROM existing
                                ),
                                FALSE
                            ) AS reclaimed_expired
                        """,
                        coordination_key,
                        ns,
                        norm_vis,
                        owner_token,
                        float(self.config.lease_seconds),
                        reclaim_completed_updated_at,
                        timeout=timeout,
                    )

                    if claimed is not None and claimed["owner_token"] == owner_token:
                        lease = DistributedLease(
                            coordination_key=coordination_key,
                            cache_namespace=ns,
                            visibility=norm_vis,
                            owner_token=owner_token,
                            fencing_token=int(claimed["fencing_token"]),
                            lease_expires_at=_as_utc(claimed["lease_expires_at"]),
                            reclaimed_expired=bool(claimed["reclaimed_expired"]),
                        )
                        collector = self._collector()
                        collector.record_distributed_singleflight_leader_claim(ns)
                        if lease.reclaimed_expired:
                            collector.record_distributed_singleflight_lease_expiration(ns)
                        return lease, None

                    row = await conn.fetchrow(
                        """
                        SELECT coordination_key, status, owner_token, fencing_token,
                               outcome_type, failure_code, lease_expires_at, updated_at, completed_at
                        FROM distributed_singleflight_leases
                        WHERE coordination_key = $1
                        """,
                        coordination_key,
                        timeout=timeout,
                    )
                    if row is None:
                        return None, None
                    snap = DistributedLeaseSnapshot(
                        coordination_key=row["coordination_key"],
                        status=str(row["status"]),
                        owner_token=str(row["owner_token"]),
                        fencing_token=int(row["fencing_token"]),
                        outcome_type=row["outcome_type"],
                        failure_code=row["failure_code"],
                        lease_expires_at=_as_utc(row["lease_expires_at"]),
                        updated_at=_as_utc(row["updated_at"]),
                        completed_at=_as_utc(row["completed_at"]) if row["completed_at"] else None,
                    )
                    return None, snap
        finally:
            self._record_db_latency(ns, db_start)

    async def renew_lease(self, lease: DistributedLease) -> Optional[DistributedLease]:
        """
        Extends `lease_expires_at` only if `owner_token`, `fencing_token`, and
        active unexpired `'pending'` status still hold.
        Returns updated `DistributedLease` if renewed, or `None` if lease ownership was lost/expired.
        """
        db_start = time.perf_counter()
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE distributed_singleflight_leases
                    SET lease_expires_at = NOW() + make_interval(secs => $4),
                        updated_at = NOW()
                    WHERE coordination_key = $1
                      AND owner_token = $2
                      AND fencing_token = $3
                      AND status = 'pending'
                      AND lease_expires_at > NOW()
                    RETURNING lease_expires_at
                    """,
                    lease.coordination_key,
                    lease.owner_token,
                    lease.fencing_token,
                    float(self.config.lease_seconds),
                    timeout=self.config.query_timeout_seconds,
                )
            if row is None:
                self._collector().record_distributed_singleflight_fencing_rejection(lease.cache_namespace)
                return None
            self._collector().record_distributed_singleflight_lease_renewal(lease.cache_namespace)
            return DistributedLease(
                coordination_key=lease.coordination_key,
                cache_namespace=lease.cache_namespace,
                visibility=lease.visibility,
                owner_token=lease.owner_token,
                fencing_token=lease.fencing_token,
                lease_expires_at=_as_utc(row["lease_expires_at"]),
                reclaimed_expired=lease.reclaimed_expired,
            )
        finally:
            self._record_db_latency(lease.cache_namespace, db_start)

    async def start_lease_renewal_task(
        self,
        lease: DistributedLease,
        on_lease_lost: Optional[Callable[[], None]] = None,
    ) -> Tuple[asyncio.Task[None], asyncio.Event]:
        """
        Launches a background renewal loop while upstream work is executing.
        Signals `lost_event` and invokes `on_lease_lost` if another worker acquires fencing.
        """
        lost_event = asyncio.Event()
        current_lease = lease

        async def _renewal_loop() -> None:
            nonlocal current_lease
            while True:
                await asyncio.sleep(self.config.renewal_interval_seconds)
                try:
                    updated = await self.renew_lease(current_lease)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._collector().record_distributed_singleflight_coordination_error(
                        current_lease.cache_namespace
                    )
                    logger.warning(
                        "Distributed SingleFlight lease renewal DB error for ns='%s': %s",
                        current_lease.cache_namespace,
                        type(exc).__name__,
                    )
                    continue

                if updated is None:
                    lost_event.set()
                    if on_lease_lost is not None:
                        try:
                            on_lease_lost()
                        except Exception:
                            pass
                    logger.warning(
                        "Distributed SingleFlight leader lost lease ownership/fencing (ns='%s', fencing=%d).",
                        current_lease.cache_namespace,
                        current_lease.fencing_token,
                    )
                    return
                current_lease = updated

        task = asyncio.create_task(_renewal_loop())
        return task, lost_event

    async def complete_success(
        self,
        lease: DistributedLease,
        *,
        outcome_type: str,
    ) -> bool:
        """
        Atomically marks the coordination row `'completed'` with `outcome_type` (`'exact'` or `'semantic'`)
        only if `owner_token` and `fencing_token` still own the active lease.
        Returns True if committed, False if rejected by fencing (stale leader).
        """
        if outcome_type not in ("exact", "semantic"):
            raise ValueError(f"Unsupported completion outcome_type: '{outcome_type}'.")
        retention = max(1.0, min(self.config.completion_retention_seconds, self.config.lease_seconds))
        db_start = time.perf_counter()
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE distributed_singleflight_leases
                    SET status = 'completed',
                        outcome_type = $4,
                        failure_code = NULL,
                        lease_expires_at = NOW() + make_interval(secs => $5),
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE coordination_key = $1
                      AND owner_token = $2
                      AND fencing_token = $3
                      AND status = 'pending'
                      AND lease_expires_at > NOW()
                    RETURNING fencing_token
                    """,
                    lease.coordination_key,
                    lease.owner_token,
                    lease.fencing_token,
                    outcome_type,
                    float(retention),
                    timeout=self.config.query_timeout_seconds,
                )
            if row is None:
                self._collector().record_distributed_singleflight_fencing_rejection(lease.cache_namespace)
                return False
            self._maybe_schedule_cleanup(lease.cache_namespace)
            return True
        finally:
            self._record_db_latency(lease.cache_namespace, db_start)

    async def complete_failure(
        self,
        lease: DistributedLease,
        *,
        failure_code: str = "LEADER_FAILED",
    ) -> bool:
        """
        Atomically marks the coordination row `'failed'` so waiting followers can stop polling
        and recover immediately. Enforces owner & fencing token match.
        """
        clean_code = (failure_code or "LEADER_FAILED").strip()[:64]
        db_start = time.perf_counter()
        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    UPDATE distributed_singleflight_leases
                    SET status = 'failed',
                        outcome_type = NULL,
                        failure_code = $4,
                        lease_expires_at = NOW() + INTERVAL '5 seconds',
                        completed_at = NOW(),
                        updated_at = NOW()
                    WHERE coordination_key = $1
                      AND owner_token = $2
                      AND fencing_token = $3
                      AND status = 'pending'
                    RETURNING fencing_token
                    """,
                    lease.coordination_key,
                    lease.owner_token,
                    lease.fencing_token,
                    clean_code,
                    timeout=self.config.query_timeout_seconds,
                )
            if row is None:
                return False
            self._collector().record_distributed_singleflight_leader_failure(lease.cache_namespace)
            return True
        finally:
            self._record_db_latency(lease.cache_namespace, db_start)

    async def wait_for_outcome(
        self,
        *,
        coordination_key: str,
        namespace: str,
        expected_fencing_token: Optional[int] = None,
    ) -> DistributedWaitResult:
        """
        Bounded follower wait using short non-blocking point reads with capped jittered backoff.
        Never holds a PostgreSQL connection while sleeping between polls.
        """
        ns = (namespace or "default").strip()
        start_perf = time.perf_counter()
        deadline = start_perf + self.config.follower_wait_timeout_seconds
        sleep_s = self.config.poll_interval_seconds
        max_sleep_s = max(sleep_s, self.config.poll_max_interval_seconds)
        consecutive_errors = 0

        try:
            while True:
                remaining = deadline - time.perf_counter()
                if remaining <= 0:
                    waited_ms = (time.perf_counter() - start_perf) * 1000.0
                    self._collector().record_distributed_singleflight_follower_timeout(ns)
                    self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                    return DistributedWaitResult(status="timeout", waited_ms=waited_ms)

                await asyncio.sleep(min(sleep_s, max(0.005, remaining)))
                sleep_s = min(max_sleep_s, sleep_s * 1.5)

                db_start = time.perf_counter()
                try:
                    async with self.pool.acquire() as conn:
                        row = await conn.fetchrow(
                            """
                            SELECT coordination_key, status, owner_token, fencing_token,
                                   outcome_type, failure_code, lease_expires_at, updated_at,
                                   completed_at, (lease_expires_at <= NOW()) AS is_expired
                            FROM distributed_singleflight_leases
                            WHERE coordination_key = $1
                            """,
                            coordination_key,
                            timeout=self.config.query_timeout_seconds,
                        )
                    consecutive_errors = 0
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    self._collector().record_distributed_singleflight_coordination_error(ns)
                    consecutive_errors += 1
                    logger.warning(
                        "Distributed SingleFlight follower poll error (%d) for ns='%s': %s",
                        consecutive_errors,
                        ns,
                        type(exc).__name__,
                    )
                    if consecutive_errors >= 3:
                        waited_ms = (time.perf_counter() - start_perf) * 1000.0
                        self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                        return DistributedWaitResult(status="error", waited_ms=waited_ms)
                    continue
                finally:
                    self._record_db_latency(ns, db_start)

                waited_ms = (time.perf_counter() - start_perf) * 1000.0
                if row is None:
                    self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                    return DistributedWaitResult(status="expired", waited_ms=waited_ms)

                snap = DistributedLeaseSnapshot(
                    coordination_key=row["coordination_key"],
                    status=str(row["status"]),
                    owner_token=str(row["owner_token"]),
                    fencing_token=int(row["fencing_token"]),
                    outcome_type=row["outcome_type"],
                    failure_code=row["failure_code"],
                    lease_expires_at=_as_utc(row["lease_expires_at"]),
                    updated_at=_as_utc(row["updated_at"]),
                    completed_at=_as_utc(row["completed_at"]) if row["completed_at"] else None,
                )

                # If a different leader already replaced our observed pending leader, continue waiting
                # cleanly up to the same bounded deadline rather than prematurely re-competing.
                if snap.status == "completed":
                    self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                    return DistributedWaitResult(
                        status="completed",
                        outcome_type=snap.outcome_type,
                        snapshot=snap,
                        waited_ms=waited_ms,
                    )

                if snap.status == "failed":
                    self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                    return DistributedWaitResult(
                        status="failed",
                        failure_code=snap.failure_code,
                        snapshot=snap,
                        waited_ms=waited_ms,
                    )

                if snap.status == "expired" or bool(row["is_expired"]):
                    self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
                    return DistributedWaitResult(
                        status="expired",
                        snapshot=snap,
                        waited_ms=waited_ms,
                    )
        except asyncio.CancelledError:
            waited_ms = (time.perf_counter() - start_perf) * 1000.0
            self._collector().record_latency(ns, "distributed_singleflight_wait_latency_ms", waited_ms)
            raise

    async def cleanup_expired_leases(
        self,
        *,
        batch_size: Optional[int] = None,
        pending_grace_seconds: float = 60.0,
    ) -> int:
        """
        Removes bounded batches of expired or finished non-active coordination rows.
        Active leases (`lease_expires_at > NOW()` with `status = 'pending'`) are NEVER deleted.
        """
        limit = int(batch_size or self.config.cleanup_batch_size)
        grace = max(5.0, float(pending_grace_seconds))
        async with self.pool.acquire() as conn:
            deleted = await conn.fetch(
                """
                DELETE FROM distributed_singleflight_leases
                WHERE coordination_key IN (
                    SELECT coordination_key
                    FROM distributed_singleflight_leases
                    WHERE
                        (status IN ('completed', 'failed', 'expired') AND lease_expires_at <= NOW())
                        OR (status = 'pending' AND lease_expires_at <= NOW() - make_interval(secs => $1))
                    ORDER BY lease_expires_at ASC
                    LIMIT $2
                )
                RETURNING coordination_key
                """,
                grace,
                limit,
                timeout=self.config.query_timeout_seconds,
            )
            return len(deleted)

    def _maybe_schedule_cleanup(self, namespace: str) -> None:
        self._cleanup_counter += 1
        if self._cleanup_counter % 50 != 0:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _run_cleanup() -> None:
            try:
                await self.cleanup_expired_leases()
            except Exception:
                self._collector().record_distributed_singleflight_coordination_error(namespace)

        loop.create_task(_run_cleanup())
