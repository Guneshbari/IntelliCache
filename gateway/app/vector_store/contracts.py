"""
Contracts and data models for provider-independent vector storage.
Modules in this package MUST NOT import FastAPI, Groq SDK, or Ollama SDK details.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Dict, Optional, Sequence, Tuple

from .errors import VectorInvalidError


def cosine_distance_to_similarity(distance: float) -> float:
    """
    Computes cosine similarity from PostgreSQL pgvector cosine distance.

    Mathematical formulation:
      In pgvector, the cosine distance operator '<=>' is defined as:
        distance = 1.0 - cos_similarity(u, v) = 1.0 - (u . v) / (||u|| * ||v||)

      Therefore, cosine similarity is recovered as:
        similarity = 1.0 - distance

    Values:
      - Identical vectors: distance = 0.0 -> similarity = 1.0
      - Orthogonal vectors: distance = 1.0 -> similarity = 0.0
      - Opposite vectors: distance = 2.0 -> similarity = -1.0

    To guard against floating-point precision inaccuracies, the result is clamped to [-1.0, 1.0].
    """
    if isinstance(distance, bool) or not isinstance(distance, (int, float)) or math.isnan(distance) or math.isinf(distance):
        raise VectorInvalidError(f"Cosine distance must be a finite number, got {distance}.")
    sim = 1.0 - float(distance)
    return max(-1.0, min(1.0, sim))


@dataclass(frozen=True)
class VectorRecord:
    """
    Persistent vector record containing embedding vector and metadata.
    Guarantees immutability of the vector (stored as a tuple of floats) and
    rigorous validation of dimensionality and numeric validity.
    """
    cache_namespace: str
    fingerprint: str
    query_text: str
    vector: Sequence[float]
    model: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    def __post_init__(self) -> None:
        if not self.cache_namespace or not self.cache_namespace.strip():
            raise VectorInvalidError("Cache namespace cannot be empty.")

        if not self.fingerprint or not self.fingerprint.strip():
            raise VectorInvalidError("Fingerprint cannot be empty.")

        if not self.query_text or not self.query_text.strip():
            raise VectorInvalidError("Query text cannot be empty.")

        if not self.model or not self.model.strip():
            raise VectorInvalidError("Embedding model identity cannot be empty.")

        if not self.vector or len(self.vector) == 0:
            raise VectorInvalidError("Embedding vector cannot be empty.")

        # Validate finite numeric values (reject booleans, NaN, +Inf, -Inf, non-numeric)
        # and normalize into an immutable tuple of floats
        validated: list[float] = []
        for i, val in enumerate(self.vector):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                raise VectorInvalidError(
                    f"Vector contains non-finite or invalid number at index {i}: {val}"
                )
            validated.append(float(val))

        object.__setattr__(self, "vector", tuple(validated))
        object.__setattr__(self, "cache_namespace", self.cache_namespace.strip())
        object.__setattr__(self, "fingerprint", self.fingerprint.strip())
        object.__setattr__(self, "model", self.model.strip())
        if self.created_at is None:
            object.__setattr__(self, "created_at", datetime.now(timezone.utc))

    @property
    def namespace(self) -> str:
        """Alias for cache_namespace for caller convenience."""
        return self.cache_namespace

    @property
    def dimensions(self) -> int:
        """Dimensionality of the embedding vector."""
        return len(self.vector)

    def to_list(self) -> list[float]:
        """Returns the embedding vector as a standard Python list of floats."""
        return list(self.vector)

    def __repr__(self) -> str:
        # Redact raw query text and vector elements to prevent sensitive leakage in logs
        preview = self.query_text[:16] + "..." if len(self.query_text) > 16 else self.query_text
        return (
            f"VectorRecord(namespace='{self.cache_namespace}', fingerprint='{self.fingerprint[:12]}...', "
            f"model='{self.model}', dimensions={self.dimensions}, query='[REDACTED:{len(self.query_text)} chars]')"
        )


@dataclass(frozen=True)
class VectorSearchResult:
    """
    Result of a nearest-neighbour vector search query.
    Contains distance, similarity score, and associated exact-cache response if available.
    """
    cache_namespace: str
    fingerprint: str
    query_text: str
    model: str
    distance: float
    similarity: float
    cached_response: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    created_at: Optional[datetime] = None

    @property
    def namespace(self) -> str:
        """Alias for cache_namespace for caller convenience."""
        return self.cache_namespace

    def __repr__(self) -> str:
        # Redact response and query text from log representations
        return (
            f"VectorSearchResult(namespace='{self.cache_namespace}', fingerprint='{self.fingerprint[:12]}...', "
            f"model='{self.model}', distance={self.distance:.4f}, similarity={self.similarity:.4f}, "
            f"has_response={self.cached_response is not None})"
        )
