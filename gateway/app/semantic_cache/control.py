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

from dataclasses import dataclass, field
from datetime import datetime, timezone
import logging
import re
import threading
from typing import Any, Callable, Dict, Optional

from ..metrics import RuntimeMetricsCollector, get_metrics_collector

logger = logging.getLogger("intellicache.gateway.semantic_cache.control")

_MAX_REASON_LEN = 256
_MAX_ACTOR_LEN = 64
_SAFE_NS_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")


def _sanitize_label(value: Optional[str], default: str, max_len: int) -> str:
    if not isinstance(value, str):
        return default
    cleaned = re.sub(r"[\x00-\x1f\x7f]+", " ", value).strip()
    if not cleaned:
        return default
    return cleaned[:max_len]


@dataclass(frozen=True)
class RuntimeServingControlState:
    """Immutable snapshot of runtime semantic serving control state."""

    runtime_enabled: bool
    version: int
    updated_at: Optional[str] = None
    updated_by: str = "startup"
    last_reason: Optional[str] = None
    namespace_overrides: Dict[str, bool] = field(default_factory=dict)

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


class SemanticServingController:
    """
    Thread-safe, process-local runtime serving kill-switch controller.

    Guarantees fail-closed behavior whenever state is missing, malformed,
    or a state provider raises an exception. Note: State is maintained per
    worker process unless an external shared `state_provider` is configured;
    restarts restore the configured startup `SEMANTIC_CACHE_SERVING_ENABLED`.
    """

    def __init__(
        self,
        initial_enabled: bool = False,
        *,
        state_provider: Optional[Callable[[Optional[str]], Any]] = None,
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

    def is_serving_enabled(self, namespace: Optional[str] = None) -> bool:
        """
        Checks whether runtime semantic serving is currently permitted.
        Fails closed (returns False) on any invalid, missing, or failing control state.
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

    def set_serving_enabled(
        self,
        enabled: bool,
        *,
        namespace: Optional[str] = None,
        clear_namespace_override: bool = False,
        updated_by: str = "admin",
        reason: Optional[str] = None,
    ) -> RuntimeServingControlState:
        """Atomically update runtime semantic serving state with strict input validation."""
        if not isinstance(enabled, bool):
            self._record_control_failure(namespace if isinstance(namespace, str) else None)
            raise ValueError("Runtime serving flag 'enabled' must be a strict boolean.")

        ns_key: Optional[str] = None
        if namespace is not None:
            if not isinstance(namespace, str) or not _SAFE_NS_PATTERN.match(namespace.strip()):
                self._record_control_failure()
                raise ValueError("Invalid namespace identifier for runtime control.")
            ns_key = namespace.strip()

        clean_actor = _sanitize_label(updated_by, "admin", _MAX_ACTOR_LEN)
        clean_reason = _sanitize_label(reason, "", _MAX_REASON_LEN) or None

        with self._lock:
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

    def reset(self, initial_enabled: bool = False, *, updated_by: str = "startup") -> RuntimeServingControlState:
        if not isinstance(initial_enabled, bool):
            self._record_control_failure()
            initial_enabled = False

        with self._lock:
            self._enabled = initial_enabled
            self._namespace_overrides.clear()
            self._state_provider = None
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
            )


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
