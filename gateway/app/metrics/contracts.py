"""
Pydantic contracts and data models for IntelliCache runtime metrics.
Includes namespace-scoped request, cache, provider, savings, semantic, and latency contracts.
"""

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class RequestAndCacheMetrics(BaseModel):
    """Counts tracking request ingress, cache lookups, outcomes, and coalescing."""
    total_authenticated_requests: int = 0
    exact_cache_lookups: int = 0
    exact_cache_hits: int = 0
    exact_cache_misses: int = 0
    cache_write_failures: int = 0
    semantic_eligibility_checks: int = 0
    semantic_eligibility_rejections: int = 0
    semantic_lookups: int = 0
    semantic_hits: int = 0
    semantic_misses: int = 0
    provider_generation_attempts: int = 0
    provider_generation_successes: int = 0
    provider_generation_failures: int = 0
    cache_bypass_requests: int = 0
    streaming_requests: int = 0
    coalesced_joins: int = 0
    coalesced_successes: int = 0
    coalesced_failures: int = 0


class ProviderAndSavingsMetrics(BaseModel):
    """Metrics tracking provider calls, token consumption, and savings."""
    provider_calls_attempted: int = 0
    provider_calls_succeeded: int = 0
    provider_calls_failed: int = 0
    provider_prompt_tokens_used: int = 0
    provider_completion_tokens_used: int = 0
    provider_total_tokens_used: int = 0
    prompt_tokens_saved: int = 0
    completion_tokens_saved: int = 0
    total_tokens_saved: int = 0
    provider_calls_avoided: int = 0


class SemanticMetrics(BaseModel):
    """Detailed diagnostics for semantic cache eligibility, retrieval, and indexing."""
    semantic_eligibility_rejections: int = 0
    semantic_lookup_attempts: int = 0
    semantic_hits: int = 0
    semantic_misses: int = 0
    ambiguous_candidate_rejections: int = 0
    incompatible_candidate_rejections: int = 0
    embedding_failures: int = 0
    vector_store_failures: int = 0
    indexing_successes: int = 0
    indexing_failures: int = 0


class LatencySummary(BaseModel):
    """Statistical summary of operation latency distributions in milliseconds."""
    sample_count: int = 0
    min: Optional[float] = None
    mean: Optional[float] = None
    p50: Optional[float] = None
    p95: Optional[float] = None
    p99: Optional[float] = None
    percentile_type: str = "exact_rolling"
    small_sample_warning: bool = False


class LatencyMetrics(BaseModel):
    """Collection of operation latency summaries across the request lifecycle."""
    total_request_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    exact_lookup_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    semantic_lookup_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    embedding_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    vector_search_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    candidate_policy_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    provider_generation_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    exact_cache_write_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    vector_indexing_latency_ms: LatencySummary = Field(default_factory=LatencySummary)
    estimated_latency_saved_ms: LatencySummary = Field(default_factory=LatencySummary)


class DerivedRates(BaseModel):
    """Calculated rates and ratios derived from request and cache counters."""
    exact_hit_rate: float = 0.0
    semantic_hit_rate: float = 0.0
    overall_cache_hit_rate: float = 0.0
    coalesced_reuse_rate: float = 0.0


class NamespaceMetricsSnapshot(BaseModel):
    """Complete observability snapshot for a specific cache namespace."""
    scope: str = "namespace"
    namespace: str
    collection_uptime_seconds: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    request_and_cache: RequestAndCacheMetrics = Field(default_factory=RequestAndCacheMetrics)
    provider_and_savings: ProviderAndSavingsMetrics = Field(default_factory=ProviderAndSavingsMetrics)
    semantic: SemanticMetrics = Field(default_factory=SemanticMetrics)
    derived_rates: DerivedRates = Field(default_factory=DerivedRates)
    latencies: LatencyMetrics = Field(default_factory=LatencyMetrics)
    notes: Optional[Dict[str, str]] = None


class AdminMetricsSnapshot(BaseModel):
    """Global administrative snapshot with system aggregate and per-namespace breakdown."""
    scope: str = "admin"
    collection_uptime_seconds: float
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    aggregate: NamespaceMetricsSnapshot
    namespaces: Dict[str, NamespaceMetricsSnapshot]
