"""
Contracts and data models for semantic caching.
Isolated from FastAPI, Groq SDK, Ollama SDK, and database implementations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class EligibilityResult:
    """Result of checking a request's eligibility for semantic caching."""
    eligible: bool
    reason: str


@dataclass(frozen=True)
class SemanticHit:
    """
    Represents a validated, un-ambiguous semantic cache hit.
    Includes original cached entry token counts and latency for savings accounting.
    """
    cached_response: str
    fingerprint: str
    distance: float
    similarity: float
    model: str
    query_text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    original_latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    matched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SemanticCacheStats:
    """Diagnostic observability counters for semantic cache operations."""
    lookups: int = 0
    hits: int = 0
    misses: int = 0
    rejected_candidates: int = 0
    ambiguous_candidates: int = 0
    eligibility_rejections: int = 0
    embedding_failures: int = 0
    vector_store_failures: int = 0
    indexing_successes: int = 0
    indexing_failures: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "rejected_candidates": self.rejected_candidates,
            "ambiguous_candidates": self.ambiguous_candidates,
            "eligibility_rejections": self.eligibility_rejections,
            "embedding_failures": self.embedding_failures,
            "vector_store_failures": self.vector_store_failures,
            "indexing_successes": self.indexing_successes,
            "indexing_failures": self.indexing_failures,
        }
