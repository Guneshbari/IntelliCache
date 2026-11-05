"""
Namespace-scoped runtime metrics collector for IntelliCache.
Provides thread-safe counter aggregation, bounded rolling latency distributions,
and separated tenant vs administrative snapshots.
"""

from collections import deque
import logging
import statistics
import threading
import time
from typing import Any, Deque, Dict, List, Optional, Sequence

from .contracts import (
    AdminMetricsSnapshot,
    DerivedRates,
    LatencyMetrics,
    LatencySummary,
    NamespaceMetricsSnapshot,
    ProviderAndSavingsMetrics,
    RequestAndCacheMetrics,
    SemanticMetrics,
)

logger = logging.getLogger("intellicache.gateway.metrics")


def compute_latency_summary(samples: Sequence[float]) -> LatencySummary:
    """
    Computes statistical percentiles (min, mean, p50, p95, p99) over a rolling sample window.
    Marks small_sample_warning=True when sample_count < 20.
    """
    n = len(samples)
    if n == 0:
        return LatencySummary(sample_count=0)

    sorted_s = sorted(samples)
    s_min = round(float(sorted_s[0]), 2)
    s_mean = round(float(sum(sorted_s) / n), 2)
    s_p50 = round(float(statistics.median(sorted_s)), 2)

    if n == 1:
        s_p95 = s_p50
        s_p99 = s_p50
    else:
        def _percentile(p: float) -> float:
            k = (n - 1) * (p / 100.0)
            f = int(k)
            c = min(f + 1, n - 1)
            d0 = sorted_s[f] * (c - k)
            d1 = sorted_s[c] * (k - f)
            return float(d0 + d1)

        s_p95 = round(_percentile(95), 2)
        s_p99 = round(_percentile(99), 2)

    return LatencySummary(
        sample_count=n,
        min=s_min,
        mean=s_mean,
        p50=s_p50,
        p95=s_p95,
        p99=s_p99,
        percentile_type="exact_rolling",
        small_sample_warning=(n < 20),
    )


class _NamespaceData:
    """Thread-safe state container for a single tenant namespace."""

    def __init__(self, namespace: str, max_samples: int = 1000):
        self.namespace = namespace
        self.max_samples = max_samples
        self.lock = threading.Lock()

        # Request & Cache Counters
        self.total_authenticated_requests = 0
        self.exact_cache_lookups = 0
        self.exact_cache_hits = 0
        self.exact_cache_misses = 0
        self.semantic_eligibility_checks = 0
        self.semantic_eligibility_rejections = 0
        self.semantic_lookups = 0
        self.semantic_hits = 0
        self.semantic_misses = 0
        self.provider_generation_attempts = 0
        self.provider_generation_successes = 0
        self.provider_generation_failures = 0
        self.cache_bypass_requests = 0
        self.streaming_requests = 0
        self.coalesced_joins = 0
        self.coalesced_successes = 0
        self.coalesced_failures = 0

        # Provider & Savings Counters
        self.provider_calls_attempted = 0
        self.provider_calls_succeeded = 0
        self.provider_calls_failed = 0
        self.provider_prompt_tokens_used = 0
        self.provider_completion_tokens_used = 0
        self.provider_total_tokens_used = 0
        self.prompt_tokens_saved = 0
        self.completion_tokens_saved = 0
        self.total_tokens_saved = 0
        self.provider_calls_avoided = 0

        # Semantic Diagnostics
        self.ambiguous_candidate_rejections = 0
        self.incompatible_candidate_rejections = 0
        self.embedding_failures = 0
        self.vector_store_failures = 0
        self.indexing_successes = 0
        self.indexing_failures = 0

        # Bounded Latency Samples
        self.latency_samples: Dict[str, Deque[float]] = {
            "total_request_latency_ms": deque(maxlen=max_samples),
            "exact_lookup_latency_ms": deque(maxlen=max_samples),
            "semantic_lookup_latency_ms": deque(maxlen=max_samples),
            "embedding_latency_ms": deque(maxlen=max_samples),
            "vector_search_latency_ms": deque(maxlen=max_samples),
            "candidate_policy_latency_ms": deque(maxlen=max_samples),
            "provider_generation_latency_ms": deque(maxlen=max_samples),
            "exact_cache_write_latency_ms": deque(maxlen=max_samples),
            "vector_indexing_latency_ms": deque(maxlen=max_samples),
            "estimated_latency_saved_ms": deque(maxlen=max_samples),
        }

    def record_request(self, stream: bool = False, bypass: bool = False) -> None:
        with self.lock:
            self.total_authenticated_requests += 1
            if stream:
                self.streaming_requests += 1
            if bypass:
                self.cache_bypass_requests += 1

    def record_exact_lookup(self, latency_ms: Optional[float] = None) -> None:
        with self.lock:
            self.exact_cache_lookups += 1
            if latency_ms is not None:
                self.latency_samples["exact_lookup_latency_ms"].append(max(0.0, float(latency_ms)))

    def record_exact_hit(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_saved_ms: float = 0.0,
    ) -> None:
        with self.lock:
            self.exact_cache_hits += 1
            self.provider_calls_avoided += 1
            self.prompt_tokens_saved += max(0, prompt_tokens)
            self.completion_tokens_saved += max(0, completion_tokens)
            self.total_tokens_saved += max(0, total_tokens)
            if latency_saved_ms > 0:
                self.latency_samples["estimated_latency_saved_ms"].append(max(0.0, float(latency_saved_ms)))

    def record_exact_miss(self) -> None:
        with self.lock:
            self.exact_cache_misses += 1

    def record_coalesced_join(self) -> None:
        with self.lock:
            self.coalesced_joins += 1

    def record_coalesced_success(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        with self.lock:
            self.coalesced_successes += 1
            self.provider_calls_avoided += 1
            self.prompt_tokens_saved += max(0, prompt_tokens)
            self.completion_tokens_saved += max(0, completion_tokens)
            self.total_tokens_saved += max(0, total_tokens)

    def record_coalesced_failure(self) -> None:
        with self.lock:
            self.coalesced_failures += 1

    def record_semantic_eligibility_check(self, eligible: bool) -> None:
        with self.lock:
            self.semantic_eligibility_checks += 1
            if not eligible:
                self.semantic_eligibility_rejections += 1

    def record_semantic_lookup(self, latency_ms: Optional[float] = None) -> None:
        with self.lock:
            self.semantic_lookups += 1
            if latency_ms is not None:
                self.latency_samples["semantic_lookup_latency_ms"].append(max(0.0, float(latency_ms)))

    def record_semantic_hit(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_saved_ms: float = 0.0,
    ) -> None:
        with self.lock:
            self.semantic_hits += 1
            self.provider_calls_avoided += 1
            self.prompt_tokens_saved += max(0, prompt_tokens)
            self.completion_tokens_saved += max(0, completion_tokens)
            self.total_tokens_saved += max(0, total_tokens)
            if latency_saved_ms > 0:
                self.latency_samples["estimated_latency_saved_ms"].append(max(0.0, float(latency_saved_ms)))

    def record_semantic_miss(self) -> None:
        with self.lock:
            self.semantic_misses += 1

    def record_ambiguous_candidate(self) -> None:
        with self.lock:
            self.ambiguous_candidate_rejections += 1

    def record_incompatible_candidate(self) -> None:
        with self.lock:
            self.incompatible_candidate_rejections += 1

    def record_embedding_failure(self) -> None:
        with self.lock:
            self.embedding_failures += 1

    def record_vector_store_failure(self) -> None:
        with self.lock:
            self.vector_store_failures += 1

    def record_indexing_success(self, latency_ms: Optional[float] = None) -> None:
        with self.lock:
            self.indexing_successes += 1
            if latency_ms is not None:
                self.latency_samples["vector_indexing_latency_ms"].append(max(0.0, float(latency_ms)))

    def record_indexing_failure(self) -> None:
        with self.lock:
            self.indexing_failures += 1

    def record_provider_attempt(self) -> None:
        with self.lock:
            self.provider_generation_attempts += 1
            self.provider_calls_attempted += 1

    def record_provider_success(
        self,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: Optional[float] = None,
    ) -> None:
        with self.lock:
            self.provider_generation_successes += 1
            self.provider_calls_succeeded += 1
            self.provider_prompt_tokens_used += max(0, prompt_tokens)
            self.provider_completion_tokens_used += max(0, completion_tokens)
            self.provider_total_tokens_used += max(0, total_tokens)
            if latency_ms is not None:
                self.latency_samples["provider_generation_latency_ms"].append(max(0.0, float(latency_ms)))

    def record_provider_failure(self) -> None:
        with self.lock:
            self.provider_generation_failures += 1
            self.provider_calls_failed += 1

    def record_latency(self, metric_name: str, latency_ms: float) -> None:
        with self.lock:
            target = self.latency_samples.get(metric_name)
            if target is not None:
                target.append(max(0.0, float(latency_ms)))

    def snapshot(self, uptime_seconds: float) -> NamespaceMetricsSnapshot:
        with self.lock:
            req_cache = RequestAndCacheMetrics(
                total_authenticated_requests=self.total_authenticated_requests,
                exact_cache_lookups=self.exact_cache_lookups,
                exact_cache_hits=self.exact_cache_hits,
                exact_cache_misses=self.exact_cache_misses,
                semantic_eligibility_checks=self.semantic_eligibility_checks,
                semantic_eligibility_rejections=self.semantic_eligibility_rejections,
                semantic_lookups=self.semantic_lookups,
                semantic_hits=self.semantic_hits,
                semantic_misses=self.semantic_misses,
                provider_generation_attempts=self.provider_generation_attempts,
                provider_generation_successes=self.provider_generation_successes,
                provider_generation_failures=self.provider_generation_failures,
                cache_bypass_requests=self.cache_bypass_requests,
                streaming_requests=self.streaming_requests,
                coalesced_joins=self.coalesced_joins,
                coalesced_successes=self.coalesced_successes,
                coalesced_failures=self.coalesced_failures,
            )

            prov_savings = ProviderAndSavingsMetrics(
                provider_calls_attempted=self.provider_calls_attempted,
                provider_calls_succeeded=self.provider_calls_succeeded,
                provider_calls_failed=self.provider_calls_failed,
                provider_prompt_tokens_used=self.provider_prompt_tokens_used,
                provider_completion_tokens_used=self.provider_completion_tokens_used,
                provider_total_tokens_used=self.provider_total_tokens_used,
                prompt_tokens_saved=self.prompt_tokens_saved,
                completion_tokens_saved=self.completion_tokens_saved,
                total_tokens_saved=self.total_tokens_saved,
                provider_calls_avoided=self.provider_calls_avoided,
            )

            semantic = SemanticMetrics(
                semantic_eligibility_rejections=self.semantic_eligibility_rejections,
                semantic_lookup_attempts=self.semantic_lookups,
                semantic_hits=self.semantic_hits,
                semantic_misses=self.semantic_misses,
                ambiguous_candidate_rejections=self.ambiguous_candidate_rejections,
                incompatible_candidate_rejections=self.incompatible_candidate_rejections,
                embedding_failures=self.embedding_failures,
                vector_store_failures=self.vector_store_failures,
                indexing_successes=self.indexing_successes,
                indexing_failures=self.indexing_failures,
            )

            # Rates
            exact_rate = (
                (self.exact_cache_hits / self.exact_cache_lookups)
                if self.exact_cache_lookups > 0
                else 0.0
            )
            sem_rate = (
                (self.semantic_hits / self.semantic_lookups)
                if self.semantic_lookups > 0
                else 0.0
            )
            overall_hits = self.exact_cache_hits + self.semantic_hits + self.coalesced_successes
            overall_rate = (
                (overall_hits / self.total_authenticated_requests)
                if self.total_authenticated_requests > 0
                else 0.0
            )
            coal_rate = (
                (self.coalesced_successes / self.coalesced_joins)
                if self.coalesced_joins > 0
                else 0.0
            )

            rates = DerivedRates(
                exact_hit_rate=round(exact_rate, 4),
                semantic_hit_rate=round(sem_rate, 4),
                overall_cache_hit_rate=round(overall_rate, 4),
                coalesced_reuse_rate=round(coal_rate, 4),
            )

            latencies = LatencyMetrics(
                total_request_latency_ms=compute_latency_summary(list(self.latency_samples["total_request_latency_ms"])),
                exact_lookup_latency_ms=compute_latency_summary(list(self.latency_samples["exact_lookup_latency_ms"])),
                semantic_lookup_latency_ms=compute_latency_summary(list(self.latency_samples["semantic_lookup_latency_ms"])),
                embedding_latency_ms=compute_latency_summary(list(self.latency_samples["embedding_latency_ms"])),
                vector_search_latency_ms=compute_latency_summary(list(self.latency_samples["vector_search_latency_ms"])),
                candidate_policy_latency_ms=compute_latency_summary(list(self.latency_samples["candidate_policy_latency_ms"])),
                provider_generation_latency_ms=compute_latency_summary(list(self.latency_samples["provider_generation_latency_ms"])),
                exact_cache_write_latency_ms=compute_latency_summary(list(self.latency_samples["exact_cache_write_latency_ms"])),
                vector_indexing_latency_ms=compute_latency_summary(list(self.latency_samples["vector_indexing_latency_ms"])),
                estimated_latency_saved_ms=compute_latency_summary(list(self.latency_samples["estimated_latency_saved_ms"])),
            )

            notes = {
                "percentile_type": "exact_rolling",
                "max_sample_window": str(self.max_samples),
                "latency_savings_note": "estimated_latency_saved_ms is calculated as max(0, original_generation_latency - lookup_latency) on direct cache hits. Excludes coalesced followers.",
            }

            return NamespaceMetricsSnapshot(
                scope="namespace",
                namespace=self.namespace,
                collection_uptime_seconds=round(uptime_seconds, 2),
                request_and_cache=req_cache,
                provider_and_savings=prov_savings,
                semantic=semantic,
                derived_rates=rates,
                latencies=latencies,
                notes=notes,
            )


class RuntimeMetricsCollector:
    """
    Central, thread-safe, namespace-isolated runtime metrics collector.
    Single authoritative metrics repository across exact caching, semantic caching,
    coalescing, and provider dispatch.
    """

    def __init__(self, max_samples: int = 1000):
        self.max_samples = max_samples
        self._start_time = time.time()
        self._namespaces: Dict[str, _NamespaceData] = {}
        self._lock = threading.Lock()

    def get_or_create_namespace(self, namespace: str) -> _NamespaceData:
        """Retrieves or creates isolated tenant metrics state."""
        ns = (namespace or "default").strip()
        with self._lock:
            if ns not in self._namespaces:
                self._namespaces[ns] = _NamespaceData(namespace=ns, max_samples=self.max_samples)
            return self._namespaces[ns]

    # -----------------------------------------------------------------------
    # Recording Facades (Fail-Safe: Never raise exceptions)
    # -----------------------------------------------------------------------

    def record_request(self, namespace: str, stream: bool = False, bypass: bool = False) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_request(stream=stream, bypass=bypass)
        except Exception as e:
            logger.debug("Metrics record_request failed: %s", e)

    def record_exact_lookup(self, namespace: str, latency_ms: Optional[float] = None) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_exact_lookup(latency_ms=latency_ms)
        except Exception as e:
            logger.debug("Metrics record_exact_lookup failed: %s", e)

    def record_exact_hit(
        self,
        namespace: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_saved_ms: float = 0.0,
    ) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_exact_hit(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_saved_ms=latency_saved_ms,
            )
        except Exception as e:
            logger.debug("Metrics record_exact_hit failed: %s", e)

    def record_exact_miss(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_exact_miss()
        except Exception as e:
            logger.debug("Metrics record_exact_miss failed: %s", e)

    def record_coalesced_join(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_coalesced_join()
        except Exception as e:
            logger.debug("Metrics record_coalesced_join failed: %s", e)

    def record_coalesced_success(
        self,
        namespace: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
    ) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_coalesced_success(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
            )
        except Exception as e:
            logger.debug("Metrics record_coalesced_success failed: %s", e)

    def record_coalesced_failure(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_coalesced_failure()
        except Exception as e:
            logger.debug("Metrics record_coalesced_failure failed: %s", e)

    def record_semantic_eligibility_check(self, namespace: str, eligible: bool) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_semantic_eligibility_check(eligible=eligible)
        except Exception as e:
            logger.debug("Metrics record_semantic_eligibility_check failed: %s", e)

    def record_semantic_lookup(self, namespace: str, latency_ms: Optional[float] = None) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_semantic_lookup(latency_ms=latency_ms)
        except Exception as e:
            logger.debug("Metrics record_semantic_lookup failed: %s", e)

    def record_semantic_hit(
        self,
        namespace: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_saved_ms: float = 0.0,
    ) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_semantic_hit(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_saved_ms=latency_saved_ms,
            )
        except Exception as e:
            logger.debug("Metrics record_semantic_hit failed: %s", e)

    def record_semantic_miss(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_semantic_miss()
        except Exception as e:
            logger.debug("Metrics record_semantic_miss failed: %s", e)

    def record_ambiguous_candidate(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_ambiguous_candidate()
        except Exception as e:
            logger.debug("Metrics record_ambiguous_candidate failed: %s", e)

    def record_incompatible_candidate(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_incompatible_candidate()
        except Exception as e:
            logger.debug("Metrics record_incompatible_candidate failed: %s", e)

    def record_embedding_failure(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_embedding_failure()
        except Exception as e:
            logger.debug("Metrics record_embedding_failure failed: %s", e)

    def record_vector_store_failure(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_vector_store_failure()
        except Exception as e:
            logger.debug("Metrics record_vector_store_failure failed: %s", e)

    def record_indexing_success(self, namespace: str, latency_ms: Optional[float] = None) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_indexing_success(latency_ms=latency_ms)
        except Exception as e:
            logger.debug("Metrics record_indexing_success failed: %s", e)

    def record_indexing_failure(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_indexing_failure()
        except Exception as e:
            logger.debug("Metrics record_indexing_failure failed: %s", e)

    def record_provider_attempt(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_provider_attempt()
        except Exception as e:
            logger.debug("Metrics record_provider_attempt failed: %s", e)

    def record_provider_success(
        self,
        namespace: str,
        prompt_tokens: int = 0,
        completion_tokens: int = 0,
        total_tokens: int = 0,
        latency_ms: Optional[float] = None,
    ) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_provider_success(
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                total_tokens=total_tokens,
                latency_ms=latency_ms,
            )
        except Exception as e:
            logger.debug("Metrics record_provider_success failed: %s", e)

    def record_provider_failure(self, namespace: str) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_provider_failure()
        except Exception as e:
            logger.debug("Metrics record_provider_failure failed: %s", e)

    def record_latency(self, namespace: str, metric_name: str, latency_ms: float) -> None:
        try:
            ns_data = self.get_or_create_namespace(namespace)
            ns_data.record_latency(metric_name=metric_name, latency_ms=latency_ms)
        except Exception as e:
            logger.debug("Metrics record_latency failed: %s", e)

    # -----------------------------------------------------------------------
    # Snapshots
    # -----------------------------------------------------------------------

    def get_namespace_snapshot(self, namespace: str) -> NamespaceMetricsSnapshot:
        """Returns snapshot for a single authenticated namespace."""
        uptime = time.time() - self._start_time
        ns_data = self.get_or_create_namespace(namespace)
        return ns_data.snapshot(uptime_seconds=uptime)

    def get_admin_snapshot(self) -> AdminMetricsSnapshot:
        """Returns global administrator snapshot with aggregated totals and per-namespace snapshots."""
        uptime = time.time() - self._start_time
        with self._lock:
            all_namespaces = list(self._namespaces.values())

        ns_snapshots: Dict[str, NamespaceMetricsSnapshot] = {}
        for ns_data in all_namespaces:
            ns_snapshots[ns_data.namespace] = ns_data.snapshot(uptime_seconds=uptime)

        # Aggregate across all namespaces
        agg_req = RequestAndCacheMetrics()
        agg_prov = ProviderAndSavingsMetrics()
        agg_sem = SemanticMetrics()
        merged_samples: Dict[str, List[float]] = {
            "total_request_latency_ms": [],
            "exact_lookup_latency_ms": [],
            "semantic_lookup_latency_ms": [],
            "embedding_latency_ms": [],
            "vector_search_latency_ms": [],
            "candidate_policy_latency_ms": [],
            "provider_generation_latency_ms": [],
            "exact_cache_write_latency_ms": [],
            "vector_indexing_latency_ms": [],
            "estimated_latency_saved_ms": [],
        }

        for snap in ns_snapshots.values():
            r = snap.request_and_cache
            agg_req.total_authenticated_requests += r.total_authenticated_requests
            agg_req.exact_cache_lookups += r.exact_cache_lookups
            agg_req.exact_cache_hits += r.exact_cache_hits
            agg_req.exact_cache_misses += r.exact_cache_misses
            agg_req.semantic_eligibility_checks += r.semantic_eligibility_checks
            agg_req.semantic_eligibility_rejections += r.semantic_eligibility_rejections
            agg_req.semantic_lookups += r.semantic_lookups
            agg_req.semantic_hits += r.semantic_hits
            agg_req.semantic_misses += r.semantic_misses
            agg_req.provider_generation_attempts += r.provider_generation_attempts
            agg_req.provider_generation_successes += r.provider_generation_successes
            agg_req.provider_generation_failures += r.provider_generation_failures
            agg_req.cache_bypass_requests += r.cache_bypass_requests
            agg_req.streaming_requests += r.streaming_requests
            agg_req.coalesced_joins += r.coalesced_joins
            agg_req.coalesced_successes += r.coalesced_successes
            agg_req.coalesced_failures += r.coalesced_failures

            p = snap.provider_and_savings
            agg_prov.provider_calls_attempted += p.provider_calls_attempted
            agg_prov.provider_calls_succeeded += p.provider_calls_succeeded
            agg_prov.provider_calls_failed += p.provider_calls_failed
            agg_prov.provider_prompt_tokens_used += p.provider_prompt_tokens_used
            agg_prov.provider_completion_tokens_used += p.provider_completion_tokens_used
            agg_prov.provider_total_tokens_used += p.provider_total_tokens_used
            agg_prov.prompt_tokens_saved += p.prompt_tokens_saved
            agg_prov.completion_tokens_saved += p.completion_tokens_saved
            agg_prov.total_tokens_saved += p.total_tokens_saved
            agg_prov.provider_calls_avoided += p.provider_calls_avoided

            s = snap.semantic
            agg_sem.semantic_eligibility_rejections += s.semantic_eligibility_rejections
            agg_sem.semantic_lookup_attempts += s.semantic_lookup_attempts
            agg_sem.semantic_hits += s.semantic_hits
            agg_sem.semantic_misses += s.semantic_misses
            agg_sem.ambiguous_candidate_rejections += s.ambiguous_candidate_rejections
            agg_sem.incompatible_candidate_rejections += s.incompatible_candidate_rejections
            agg_sem.embedding_failures += s.embedding_failures
            agg_sem.vector_store_failures += s.vector_store_failures
            agg_sem.indexing_successes += s.indexing_successes
            agg_sem.indexing_failures += s.indexing_failures

        # Merge raw samples from ns_data
        for ns_data in all_namespaces:
            with ns_data.lock:
                for k, samples in ns_data.latency_samples.items():
                    merged_samples[k].extend(samples)

        exact_rate = (
            (agg_req.exact_cache_hits / agg_req.exact_cache_lookups)
            if agg_req.exact_cache_lookups > 0
            else 0.0
        )
        sem_rate = (
            (agg_req.semantic_hits / agg_req.semantic_lookups)
            if agg_req.semantic_lookups > 0
            else 0.0
        )
        overall_hits = agg_req.exact_cache_hits + agg_sem.semantic_hits + agg_req.coalesced_successes
        overall_rate = (
            (overall_hits / agg_req.total_authenticated_requests)
            if agg_req.total_authenticated_requests > 0
            else 0.0
        )
        coal_rate = (
            (agg_req.coalesced_successes / agg_req.coalesced_joins)
            if agg_req.coalesced_joins > 0
            else 0.0
        )

        agg_rates = DerivedRates(
            exact_hit_rate=round(exact_rate, 4),
            semantic_hit_rate=round(sem_rate, 4),
            overall_cache_hit_rate=round(overall_rate, 4),
            coalesced_reuse_rate=round(coal_rate, 4),
        )

        agg_latencies = LatencyMetrics(
            total_request_latency_ms=compute_latency_summary(merged_samples["total_request_latency_ms"][-self.max_samples:]),
            exact_lookup_latency_ms=compute_latency_summary(merged_samples["exact_lookup_latency_ms"][-self.max_samples:]),
            semantic_lookup_latency_ms=compute_latency_summary(merged_samples["semantic_lookup_latency_ms"][-self.max_samples:]),
            embedding_latency_ms=compute_latency_summary(merged_samples["embedding_latency_ms"][-self.max_samples:]),
            vector_search_latency_ms=compute_latency_summary(merged_samples["vector_search_latency_ms"][-self.max_samples:]),
            candidate_policy_latency_ms=compute_latency_summary(merged_samples["candidate_policy_latency_ms"][-self.max_samples:]),
            provider_generation_latency_ms=compute_latency_summary(merged_samples["provider_generation_latency_ms"][-self.max_samples:]),
            exact_cache_write_latency_ms=compute_latency_summary(merged_samples["exact_cache_write_latency_ms"][-self.max_samples:]),
            vector_indexing_latency_ms=compute_latency_summary(merged_samples["vector_indexing_latency_ms"][-self.max_samples:]),
            estimated_latency_saved_ms=compute_latency_summary(merged_samples["estimated_latency_saved_ms"][-self.max_samples:]),
        )

        aggregate_snapshot = NamespaceMetricsSnapshot(
            scope="aggregate",
            namespace="all",
            collection_uptime_seconds=round(uptime, 2),
            request_and_cache=agg_req,
            provider_and_savings=agg_prov,
            semantic=agg_sem,
            derived_rates=agg_rates,
            latencies=agg_latencies,
            notes={
                "scope": "System-wide aggregate across all tenant namespaces.",
                "percentile_type": "exact_rolling",
            },
        )

        return AdminMetricsSnapshot(
            scope="admin",
            collection_uptime_seconds=round(uptime, 2),
            aggregate=aggregate_snapshot,
            namespaces=ns_snapshots,
        )

    def reset(self, namespace: Optional[str] = None) -> None:
        """Resets counters for a specific namespace or all namespaces (useful in tests)."""
        with self._lock:
            if namespace is not None:
                self._namespaces.pop(namespace, None)
            else:
                self._namespaces.clear()
                self._start_time = time.time()


# Singleton management
_metrics_collector_singleton: Optional[RuntimeMetricsCollector] = None


def get_metrics_collector() -> RuntimeMetricsCollector:
    """Dependency returning singleton RuntimeMetricsCollector."""
    global _metrics_collector_singleton
    if _metrics_collector_singleton is None:
        _metrics_collector_singleton = RuntimeMetricsCollector()
    return _metrics_collector_singleton


def set_metrics_collector(collector: Optional[RuntimeMetricsCollector]) -> None:
    """Configures global RuntimeMetricsCollector singleton."""
    global _metrics_collector_singleton
    _metrics_collector_singleton = collector
