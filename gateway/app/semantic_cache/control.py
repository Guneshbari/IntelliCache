"""
Runtime Serving Control for Semantic Caching (Stage 3.5.4).

Provides an atomic, fail-closed, server-side runtime serving kill switch
(`SEMANTIC_CACHE_SERVING_ENABLED`) decoupled from startup infrastructure
availability (`SEMANTIC_CACHE_ENABLED`).

Effective semantic serving requires BOTH:
1. Startup wiring enabled (`semantic_cache_enabled=True` / initialized service)
2. Runtime controller state explicitly returning `True`
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
import threading
from typing import Any, Callable, Dict, Optional, Set

from ..metrics import RuntimeMetricsCollector, get_metrics_collector

logger = logging.getLogger("intellicache.gateway.semantic_cache.control")

_MAX_REASON_LEN = 256
_MAX_ACTOR_LEN = 64
_SAFE_NS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
_DEFAULT_CONTROL_QUERY_TIMEOUT_SECONDS = 2.0


def _sanitize_label(value: Optional[str], default: str, max_len: int) -> str:
    if not isinstance(value, str):
        return default
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if not cleaned:
        return default
    return cleaned[:max_len]


def _format_db_timestamp(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return str(value)


@dataclass(frozen=True)
class RuntimeServingControlState:
    """Immutable snapshot of runtime semantic serving control state."""

    runtime_enabled: bool
    version: int
    updated_at: Optional[str] = None
    updated_by: str = "startup"
    last_reason: Optional[str] = None
    namespace_overrides: Dict[str, bool] = field(default_factory=dict)
    coordination_backend: str = "process_local"

    def to_dict(
        self,
        *,
        startup_enabled: Optional[bool] = None,
        namespace: Optional[str] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "runtime_enabled": self.runtime_enabled,
            "version": self.version,
            "updated_at": self.updated_at,
            "updated_by": self.updated_by,
            "last_reason": self.last_reason,
            "namespace_overrides": dict(self.namespace_overrides),
            "coordination_backend": self.coordination_backend,
        }
        if namespace is not None:
            ns_clean = namespace.strip()
            effective_runtime = (
                bool(self.runtime_enabled)
                and self.namespace_overrides.get(ns_clean, True)
            )
            payload["namespace"] = ns_clean
            payload["namespace_runtime_enabled"] = effective_runtime
        else:
            effective_runtime = self.runtime_enabled

        if startup_enabled is not None:
            payload["startup_enabled"] = bool(startup_enabled)
            payload["effective_enabled"] = bool(startup_enabled) and bool(effective_runtime)

        return payload


class PostgresRuntimeControlStore:
    """
    PostgreSQL-backed shared runtime semantic serving control store (Stage 3.8B).

    Provides authoritative cross-worker reads and atomic updates backed by
    `semantic_runtime_control` and `semantic_runtime_namespace_overrides`.
    Every read validates strict boolean payloads and fails closed on missing,
    corrupted, or unreachable PostgreSQL state.
    """

    def __init__(
        self,
        pool: Any,
        *,
        query_timeout_seconds: float = _DEFAULT_CONTROL_QUERY_TIMEOUT_SECONDS,
    ) -> None:
        self.pool = pool
        self.query_timeout_seconds = max(0.1, float(query_timeout_seconds))
        self._pending_writes: Set[asyncio.Task[Any]] = set()
        self._last_scheduled_write: Optional[asyncio.Task[Any]] = None

    def schedule_write(self, coro: Any) -> Optional[asyncio.Task[Any]]:
        """Queues a FIFO-chained synchronous-bridge write task so reads can flush without deadlocks."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        prev = self._last_scheduled_write

        async def _chained() -> Any:
            if prev is not None and not prev.done():
                try:
                    await prev
                except Exception:
                    pass
            return await coro

        task = loop.create_task(_chained())
        self._last_scheduled_write = task
        self._pending_writes.add(task)
        task.add_done_callback(self._pending_writes.discard)
        return task

    async def _flush_pending_writes(self) -> None:
        if not self._pending_writes:
            return
        current = asyncio.current_task()
        pending = [t for t in list(self._pending_writes) if not t.done() and t is not current]
        if pending:
            await asyncio.gather(*pending, return_exceptions=True)

    async def _read_state_on_conn(self, conn: Any) -> RuntimeServingControlState:
        global_row = await conn.fetchrow(
            """
            SELECT runtime_enabled, version, updated_at, updated_by, last_reason
            FROM semantic_runtime_control
            WHERE control_key = 'global'
            """,
            timeout=self.query_timeout_seconds,
        )
        if global_row is None:
            raise RuntimeError("Global semantic runtime control row is missing.")

        raw_enabled = global_row["runtime_enabled"]
        if not isinstance(raw_enabled, bool):
            raise RuntimeError(
                f"Corrupted runtime_enabled type in PostgreSQL: {type(raw_enabled).__name__}."
            )

        override_rows = await conn.fetch(
            """
            SELECT namespace, runtime_enabled
            FROM semantic_runtime_namespace_overrides
            ORDER BY namespace ASC
            """,
            timeout=self.query_timeout_seconds,
        )
        overrides: Dict[str, bool] = {}
        for ov in override_rows:
            ns_name = ov["namespace"]
            ov_enabled = ov["runtime_enabled"]
            if not isinstance(ns_name, str) or not isinstance(ov_enabled, bool):
                raise RuntimeError("Corrupted namespace override row in PostgreSQL.")
            overrides[ns_name] = ov_enabled

        return RuntimeServingControlState(
            runtime_enabled=raw_enabled,
            version=int(global_row["version"]),
            updated_at=_format_db_timestamp(global_row["updated_at"]),
            updated_by=str(global_row["updated_by"] or "startup"),
            last_reason=global_row["last_reason"],
            namespace_overrides=overrides,
            coordination_backend="postgres_shared",
        )

    async def ensure_initialized(
        self,
        *,
        initial_enabled: bool = False,
        updated_by: str = "startup",
    ) -> RuntimeServingControlState:
        """
        Seeds the `'global'` row with `ON CONFLICT DO NOTHING` so worker startup
        preserves live shared state toggled by other workers or administrators.
        """
        if not isinstance(initial_enabled, bool):
            initial_enabled = False
        clean_actor = _sanitize_label(updated_by, "startup", _MAX_ACTOR_LEN)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    """
                    INSERT INTO semantic_runtime_control
                        (control_key, runtime_enabled, version, updated_at, updated_by, last_reason)
                    VALUES
                        ('global', $1, 1, NOW(), $2, 'initial_configuration')
                    ON CONFLICT (control_key) DO NOTHING
                    """,
                    initial_enabled,
                    clean_actor,
                    timeout=self.query_timeout_seconds,
                )
                return await self._read_state_on_conn(conn)

    async def reset_state(
        self,
        *,
        initial_enabled: bool = False,
        updated_by: str = "reset",
        reason: str = "reset",
    ) -> RuntimeServingControlState:
        """Explicitly resets shared PostgreSQL control state and clears all namespace overrides."""
        if not isinstance(initial_enabled, bool):
            initial_enabled = False
        clean_actor = _sanitize_label(updated_by, "reset", _MAX_ACTOR_LEN)
        clean_reason = _sanitize_label(reason, "reset", _MAX_REASON_LEN)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                await conn.execute(
                    "DELETE FROM semantic_runtime_namespace_overrides",
                    timeout=self.query_timeout_seconds,
                )
                await conn.execute(
                    """
                    INSERT INTO semantic_runtime_control
                        (control_key, runtime_enabled, version, updated_at, updated_by, last_reason)
                    VALUES
                        ('global', $1, 1, NOW(), $2, $3)
                    ON CONFLICT (control_key) DO UPDATE SET
                        runtime_enabled = EXCLUDED.runtime_enabled,
                        version = semantic_runtime_control.version + 1,
                        updated_at = NOW(),
                        updated_by = EXCLUDED.updated_by,
                        last_reason = EXCLUDED.last_reason
                    """,
                    initial_enabled,
                    clean_actor,
                    clean_reason,
                    timeout=self.query_timeout_seconds,
                )
                return await self._read_state_on_conn(conn)

    async def is_serving_enabled(self, namespace: Optional[str] = None) -> bool:
        """
        Authoritative PostgreSQL serving-state check for a global or namespace request.
        Raises on missing/corrupted/unreachable DB state so the controller fails closed.
        """
        await self._flush_pending_writes()
        ns_key = namespace.strip() if isinstance(namespace, str) and namespace.strip() else None
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(
                """
                SELECT g.runtime_enabled AS global_enabled,
                       n.runtime_enabled AS namespace_override
                FROM semantic_runtime_control g
                LEFT JOIN semantic_runtime_namespace_overrides n
                  ON n.namespace = $1
                WHERE g.control_key = 'global'
                """,
                ns_key,
                timeout=self.query_timeout_seconds,
            )
        if row is None:
            raise RuntimeError("Shared semantic_runtime_control row is missing.")
        global_enabled = row["global_enabled"]
        if not isinstance(global_enabled, bool):
            raise RuntimeError(
                f"Non-boolean global_enabled in PostgreSQL: {type(global_enabled).__name__}."
            )
        if not global_enabled:
            return False

        if ns_key is not None and row["namespace_override"] is not None:
            ns_override = row["namespace_override"]
            if not isinstance(ns_override, bool):
                raise RuntimeError(
                    f"Non-boolean namespace_override in PostgreSQL: {type(ns_override).__name__}."
                )
            return ns_override

        return True

    async def get_state(self) -> RuntimeServingControlState:
        await self._flush_pending_writes()
        async with self.pool.acquire() as conn:
            return await self._read_state_on_conn(conn)

    async def set_serving_enabled(
        self,
        enabled: bool,
        *,
        namespace: Optional[str] = None,
        clear_namespace_override: bool = False,
        updated_by: str = "admin",
        reason: Optional[str] = None,
        _flush_pending: bool = True,
    ) -> RuntimeServingControlState:
        if _flush_pending:
            await self._flush_pending_writes()
        clean_actor = _sanitize_label(updated_by, "admin", _MAX_ACTOR_LEN)
        clean_reason = _sanitize_label(reason, "", _MAX_REASON_LEN) or None

        async with self.pool.acquire() as conn:
            async with conn.transaction():
                if namespace is None:
                    if clear_namespace_override:
                        await conn.execute(
                            "DELETE FROM semantic_runtime_namespace_overrides",
                            timeout=self.query_timeout_seconds,
                        )
                    await conn.execute(
                        """
                        INSERT INTO semantic_runtime_control
                            (control_key, runtime_enabled, version, updated_at, updated_by, last_reason)
                        VALUES
                            ('global', $1, 1, NOW(), $2, $3)
                        ON CONFLICT (control_key) DO UPDATE SET
                            runtime_enabled = EXCLUDED.runtime_enabled,
                            version = semantic_runtime_control.version + 1,
                            updated_at = NOW(),
                            updated_by = EXCLUDED.updated_by,
                            last_reason = EXCLUDED.last_reason
                        """,
                        enabled,
                        clean_actor,
                        clean_reason,
                        timeout=self.query_timeout_seconds,
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO semantic_runtime_control
                            (control_key, runtime_enabled, version, updated_at, updated_by, last_reason)
                        VALUES
                            ('global', FALSE, 1, NOW(), 'startup', 'auto_seeded')
                        ON CONFLICT (control_key) DO NOTHING
                        """,
                        timeout=self.query_timeout_seconds,
                    )
                    if clear_namespace_override:
                        await conn.execute(
                            "DELETE FROM semantic_runtime_namespace_overrides WHERE namespace = $1",
                            namespace,
                            timeout=self.query_timeout_seconds,
                        )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO semantic_runtime_namespace_overrides
                                (namespace, runtime_enabled, updated_at, updated_by, reason)
                            VALUES
                                ($1, $2, NOW(), $3, $4)
                            ON CONFLICT (namespace) DO UPDATE SET
                                runtime_enabled = EXCLUDED.runtime_enabled,
                                updated_at = NOW(),
                                updated_by = EXCLUDED.updated_by,
                                reason = EXCLUDED.reason
                            """,
                            namespace,
                            enabled,
                            clean_actor,
                            clean_reason,
                            timeout=self.query_timeout_seconds,
                        )
                    await conn.execute(
                        """
                        UPDATE semantic_runtime_control
                        SET version = version + 1,
                            updated_at = NOW(),
                            updated_by = $1,
                            last_reason = $2
                        WHERE control_key = 'global'
                        """,
                        clean_actor,
                        clean_reason,
                        timeout=self.query_timeout_seconds,
                    )

                return await self._read_state_on_conn(conn)


class SemanticServingController:
    """
    Thread-safe runtime serving kill-switch controller.

    Supports both PostgreSQL-backed multi-worker shared runtime state
    (`PostgresRuntimeControlStore`) and process-local fallback for in-memory mode.
    Guarantees fail-closed behavior whenever state is missing, malformed,
    or a state provider / backing store raises an exception.
    """

    def __init__(
        self,
        initial_enabled: bool = False,
        *,
        state_provider: Optional[Callable[[Optional[str]], Any]] = None,
        shared_store: Optional[PostgresRuntimeControlStore] = None,
        metrics_collector: Optional[RuntimeMetricsCollector] = None,
    ) -> None:
        if not isinstance(initial_enabled, bool):
            raise ValueError("initial_enabled must be strictly a boolean.")
        self._lock = threading.RLock()
        self._enabled: bool = initial_enabled
        self._namespace_overrides: Dict[str, bool] = {}
        self._version: int = 1
        self._updated_at: str = datetime.now(timezone.utc).isoformat()
        self._updated_by: str = "startup"
        self._last_reason: Optional[str] = "initial_configuration"
        self._state_provider = state_provider
        self._shared_store = shared_store
        self._metrics_collector = metrics_collector

    def _get_collector(self) -> RuntimeMetricsCollector:
        return self._metrics_collector or get_metrics_collector()

    def _record_control_change(self, namespace: Optional[str] = None) -> None:
        try:
            self._get_collector().record_runtime_control_change(namespace=namespace or "default")
        except Exception:
            pass

    def _record_control_failure(self, namespace: Optional[str] = None) -> None:
        try:
            self._get_collector().record_runtime_control_failure(namespace=namespace or "default")
        except Exception:
            pass

    def set_metrics_collector(self, collector: Optional[RuntimeMetricsCollector]) -> None:
        with self._lock:
            self._metrics_collector = collector

    def set_state_provider(
        self,
        provider: Optional[Callable[[Optional[str]], Any]],
    ) -> None:
        with self._lock:
            self._state_provider = provider

    def set_shared_store(self, store: Optional[PostgresRuntimeControlStore]) -> None:
        with self._lock:
            self._shared_store = store

    def get_shared_store(self) -> Optional[PostgresRuntimeControlStore]:
        with self._lock:
            return self._shared_store

    def _apply_snapshot(self, state: RuntimeServingControlState) -> None:
        with self._lock:
            self._enabled = bool(state.runtime_enabled)
            self._namespace_overrides = dict(state.namespace_overrides)
            self._version = int(state.version)
            self._updated_at = state.updated_at or datetime.now(timezone.utc).isoformat()
            self._updated_by = state.updated_by
            self._last_reason = state.last_reason

    @staticmethod
    def _validate_mutation_inputs(
        enabled: bool,
        *,
        namespace: Optional[str],
        updated_by: str,
        reason: Optional[str],
        on_error: Optional[Callable[[Optional[str]], None]] = None,
    ) -> tuple[Optional[str], str, Optional[str]]:
        if not isinstance(enabled, bool):
            if on_error is not None:
                on_error(namespace if isinstance(namespace, str) else None)
            raise ValueError("Runtime serving flag 'enabled' must be a strict boolean.")

        ns_key: Optional[str] = None
        if namespace is not None:
            if not isinstance(namespace, str) or not _SAFE_NS_PATTERN.match(namespace.strip()):
                if on_error is not None:
                    on_error(None)
                raise ValueError("Invalid namespace identifier for runtime control.")
            ns_key = namespace.strip()

        clean_actor = _sanitize_label(updated_by, "admin", _MAX_ACTOR_LEN)
        clean_reason = _sanitize_label(reason, "", _MAX_REASON_LEN) or None
        return ns_key, clean_actor, clean_reason

    def is_serving_enabled(self, namespace: Optional[str] = None) -> bool:
        """
        Synchronous serving check against `state_provider` and process-local state.
        Authoritative PostgreSQL workers use `await controller.acheck_serving_enabled()`.
        """
        ns_key = namespace.strip() if isinstance(namespace, str) and namespace.strip() else None

        with self._lock:
            provider = self._state_provider
            if provider is not None:
                try:
                    provided = provider(ns_key)
                except Exception as exc:
                    logger.warning("Runtime semantic control provider failed; failing closed: %s", type(exc).__name__)
                    self._record_control_failure(ns_key)
                    return False
                if not isinstance(provided, bool):
                    logger.warning(
                        "Runtime semantic control provider returned non-bool (%s); failing closed.",
                        type(provided).__name__,
                    )
                    self._record_control_failure(ns_key)
                    return False
                if not provided:
                    return False

            if not isinstance(self._enabled, bool):
                logger.warning("Corrupted runtime semantic serving state; failing closed.")
                self._record_control_failure(ns_key)
                return False

            if not self._enabled:
                return False

            if ns_key is not None and ns_key in self._namespace_overrides:
                override = self._namespace_overrides[ns_key]
                if not isinstance(override, bool):
                    logger.warning("Corrupted namespace serving override; failing closed.")
                    self._record_control_failure(ns_key)
                    return False
                return override

            return True

    async def acheck_serving_enabled(self, namespace: Optional[str] = None) -> bool:
        """
        Authoritative async check for Checkpoint 1 (pre-lookup) and Checkpoint 2 (pre-delivery).
        Queries PostgreSQL directly when `shared_store` is configured; fails closed on any error.
        """
        ns_key = namespace.strip() if isinstance(namespace, str) and namespace.strip() else None

        with self._lock:
            provider = self._state_provider
            store = self._shared_store

        if provider is not None:
            try:
                provided = provider(ns_key)
            except Exception as exc:
                logger.warning("Runtime semantic control provider failed; failing closed: %s", type(exc).__name__)
                self._record_control_failure(ns_key)
                return False
            if not isinstance(provided, bool):
                logger.warning(
                    "Runtime semantic control provider returned non-bool (%s); failing closed.",
                    type(provided).__name__,
                )
                self._record_control_failure(ns_key)
                return False
            if not provided:
                return False

        if store is not None:
            try:
                allowed = await store.is_serving_enabled(ns_key)
            except Exception as exc:
                logger.warning(
                    "PostgreSQL shared runtime serving check failed; failing closed: %s",
                    type(exc).__name__,
                )
                self._record_control_failure(ns_key)
                return False
            if not isinstance(allowed, bool):
                logger.warning(
                    "PostgreSQL shared runtime serving check returned non-bool (%s); failing closed.",
                    type(allowed).__name__,
                )
                self._record_control_failure(ns_key)
                return False
            return allowed

        return self.is_serving_enabled(ns_key)

    def set_serving_enabled(
        self,
        enabled: bool,
        *,
        namespace: Optional[str] = None,
        clear_namespace_override: bool = False,
        updated_by: str = "admin",
        reason: Optional[str] = None,
    ) -> RuntimeServingControlState:
        """
        Synchronously updates local serving state (and queues a PostgreSQL persistence task
        when `shared_store` is attached inside an active event loop).
        """
        ns_key, clean_actor, clean_reason = self._validate_mutation_inputs(
            enabled,
            namespace=namespace,
            updated_by=updated_by,
            reason=reason,
            on_error=self._record_control_failure,
        )

        with self._lock:
            store = self._shared_store
            if ns_key is not None:
                if clear_namespace_override:
                    self._namespace_overrides.pop(ns_key, None)
                else:
                    self._namespace_overrides[ns_key] = enabled
            else:
                self._enabled = enabled
                if clear_namespace_override:
                    self._namespace_overrides.clear()

            self._version += 1
            self._updated_at = datetime.now(timezone.utc).isoformat()
            self._updated_by = clean_actor
            self._last_reason = clean_reason

            state = RuntimeServingControlState(
                runtime_enabled=self._enabled,
                version=self._version,
                updated_at=self._updated_at,
                updated_by=self._updated_by,
                last_reason=self._last_reason,
                namespace_overrides=dict(self._namespace_overrides),
                coordination_backend="postgres_shared" if store is not None else "process_local",
            )

        if store is not None:
            store.schedule_write(
                store.set_serving_enabled(
                    enabled,
                    namespace=ns_key,
                    clear_namespace_override=clear_namespace_override,
                    updated_by=clean_actor,
                    reason=clean_reason,
                    _flush_pending=False,
                )
            )

        self._record_control_change(ns_key)
        logger.info(
            "Runtime semantic serving control updated: version=%d global_enabled=%s namespace=%s actor=%s reason=%s",
            state.version,
            state.runtime_enabled,
            ns_key or "*",
            clean_actor,
            clean_reason or "unspecified",
        )
        return state

    async def aset_serving_enabled(
        self,
        enabled: bool,
        *,
        namespace: Optional[str] = None,
        clear_namespace_override: bool = False,
        updated_by: str = "admin",
        reason: Optional[str] = None,
    ) -> RuntimeServingControlState:
        """
        Authoritatively updates shared PostgreSQL runtime control state (or local state
        when no shared store is attached) with strict input validation.
        """
        ns_key, clean_actor, clean_reason = self._validate_mutation_inputs(
            enabled,
            namespace=namespace,
            updated_by=updated_by,
            reason=reason,
            on_error=self._record_control_failure,
        )

        with self._lock:
            store = self._shared_store

        if store is not None:
            try:
                state = await store.set_serving_enabled(
                    enabled,
                    namespace=ns_key,
                    clear_namespace_override=clear_namespace_override,
                    updated_by=clean_actor,
                    reason=clean_reason,
                )
            except Exception:
                self._record_control_failure(ns_key)
                raise
            self._apply_snapshot(state)
            self._record_control_change(ns_key)
            logger.info(
                "Shared PostgreSQL runtime serving control updated: version=%d global_enabled=%s namespace=%s actor=%s reason=%s",
                state.version,
                state.runtime_enabled,
                ns_key or "*",
                clean_actor,
                clean_reason or "unspecified",
            )
            return state

        return self.set_serving_enabled(
            enabled,
            namespace=ns_key,
            clear_namespace_override=clear_namespace_override,
            updated_by=clean_actor,
            reason=clean_reason,
        )

    def reset(self, initial_enabled: bool = False, *, updated_by: str = "startup") -> RuntimeServingControlState:
        if not isinstance(initial_enabled, bool):
            self._record_control_failure()
            initial_enabled = False

        with self._lock:
            self._enabled = initial_enabled
            self._namespace_overrides.clear()
            self._state_provider = None
            self._shared_store = None
            self._version += 1
            self._updated_at = datetime.now(timezone.utc).isoformat()
            self._updated_by = _sanitize_label(updated_by, "startup", _MAX_ACTOR_LEN)
            self._last_reason = "reset"
            return RuntimeServingControlState(
                runtime_enabled=self._enabled,
                version=self._version,
                updated_at=self._updated_at,
                updated_by=self._updated_by,
                last_reason=self._last_reason,
                namespace_overrides={},
                coordination_backend="process_local",
            )

    def get_state(self) -> RuntimeServingControlState:
        with self._lock:
            runtime_flag = self._enabled if isinstance(self._enabled, bool) else False
            overrides = {
                k: v for k, v in self._namespace_overrides.items() if isinstance(v, bool)
            }
            return RuntimeServingControlState(
                runtime_enabled=runtime_flag,
                version=self._version,
                updated_at=self._updated_at,
                updated_by=self._updated_by,
                last_reason=self._last_reason,
                namespace_overrides=overrides,
                coordination_backend="postgres_shared" if self._shared_store is not None else "process_local",
            )

    async def aget_state(self) -> RuntimeServingControlState:
        with self._lock:
            store = self._shared_store
        if store is not None:
            try:
                state = await store.get_state()
            except Exception:
                self._record_control_failure()
                raise
            self._apply_snapshot(state)
            return state
        return self.get_state()


_controller_lock = threading.Lock()
_controller_singleton: Optional[SemanticServingController] = None


def get_semantic_serving_controller() -> SemanticServingController:
    """Return process-wide runtime serving controller (defaults to False / fail-closed)."""
    global _controller_singleton
    if _controller_singleton is None:
        with _controller_lock:
            if _controller_singleton is None:
                _controller_singleton = SemanticServingController(initial_enabled=False)
    return _controller_singleton


def reset_semantic_serving_controller(initial_enabled: bool = False) -> SemanticServingController:
    """Reset the singleton controller (for startup sync or isolated tests)."""
    controller = get_semantic_serving_controller()
    controller.reset(initial_enabled=initial_enabled)
    return controller


def sync_semantic_serving_controller_from_settings(settings: Any) -> SemanticServingController:
    """Synchronize process-wide runtime serving controller from server configuration."""
    controller = get_semantic_serving_controller()
    raw_flag = getattr(settings, "semantic_cache_serving_enabled", False)
    if not isinstance(raw_flag, bool):
        logger.warning(
            "Non-boolean semantic_cache_serving_enabled (%s); failing closed.",
            type(raw_flag).__name__,
        )
        controller._record_control_failure()
        controller.reset(initial_enabled=False, updated_by="settings_fail_closed")
        return controller
    controller.reset(initial_enabled=raw_flag, updated_by="settings_sync")
    return controller


async def sync_semantic_serving_controller_with_postgres(
    settings: Any,
    pool: Any,
) -> SemanticServingController:
    """
    Attach a shared PostgreSQL runtime control store to the singleton controller
    and seed `'global'` with `ON CONFLICT DO NOTHING` so worker startups never
    clobber live control state set by administrators or peer workers.
    """
    controller = get_semantic_serving_controller()
    raw_flag = getattr(settings, "semantic_cache_serving_enabled", False)
    if not isinstance(raw_flag, bool):
        logger.warning(
            "Non-boolean semantic_cache_serving_enabled (%s); failing closed.",
            type(raw_flag).__name__,
        )
        controller._record_control_failure()
        raw_flag = False

    store = PostgresRuntimeControlStore(pool=pool)
    state = await store.ensure_initialized(initial_enabled=raw_flag, updated_by="settings_sync")
    controller.reset(initial_enabled=state.runtime_enabled, updated_by=state.updated_by)
    controller.set_shared_store(store)
    controller._apply_snapshot(state)
    return controller


def detach_postgres_runtime_control() -> None:
    """
    Detaches the PostgreSQL runtime control store and resets process-local controller
    mirrors on worker shutdown WITHOUT modifying shared PostgreSQL control tables.
    """
    controller = get_semantic_serving_controller()
    controller.reset(initial_enabled=False, updated_by="worker_shutdown")
