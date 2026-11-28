"""
Runtime metrics and observability package for IntelliCache.
"""

from .contracts import (
    AdminMetricsSnapshot,
    DerivedRates,
    DistributedSingleFlightMetrics,
    LatencyMetrics,
    LatencySummary,
    NamespaceMetricsSnapshot,
    ProviderAndSavingsMetrics,
    RequestAndCacheMetrics,
    SemanticMetrics,
)
from .collector import (
    RuntimeMetricsCollector,
    compute_latency_summary,
    get_metrics_collector,
    set_metrics_collector,
)

__all__ = [
    "AdminMetricsSnapshot",
    "DerivedRates",
    "DistributedSingleFlightMetrics",
    "LatencyMetrics",
    "LatencySummary",
    "NamespaceMetricsSnapshot",
    "ProviderAndSavingsMetrics",
    "RequestAndCacheMetrics",
    "RuntimeMetricsCollector",
    "SemanticMetrics",
    "compute_latency_summary",
    "get_metrics_collector",
    "set_metrics_collector",
]
