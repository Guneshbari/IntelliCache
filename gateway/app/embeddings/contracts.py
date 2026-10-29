"""
Contracts and data models for provider-independent text embeddings.
Core modules and embedding contracts MUST NOT import FastAPI, Groq SDK, or database modules.
"""

from dataclasses import dataclass
from enum import Enum
import math
from typing import List, Sequence

from .errors import EmbeddingResponseError


class EmbeddingTask(str, Enum):
    """Explicit embedding task classification for asymmetric retrieval models."""
    QUERY = "query"
    DOCUMENT = "document"


@dataclass(frozen=True)
class EmbeddingResult:
    """
    Normalized embedding vector result from an embedding provider.
    Guarantees validation of dimensionality, finite values, and non-emptiness.
    """
    vector: Sequence[float]
    model: str
    dimensions: int

    def __post_init__(self) -> None:
        if not self.model or not self.model.strip():
            raise EmbeddingResponseError("Embedding model identity cannot be empty.")

        if not self.vector or len(self.vector) == 0:
            raise EmbeddingResponseError("Embedding vector cannot be empty.")

        if self.dimensions <= 0:
            raise EmbeddingResponseError(f"Embedding dimensions must be positive, got {self.dimensions}.")

        if len(self.vector) != self.dimensions:
            raise EmbeddingResponseError(
                f"Embedding vector length ({len(self.vector)}) does not match declared dimensions ({self.dimensions})."
            )

        # Validate finite numeric values (reject NaN, +Inf, -Inf, non-numeric)
        for i, val in enumerate(self.vector):
            if not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                raise EmbeddingResponseError(
                    f"Embedding vector contains non-finite or invalid number at index {i}: {val}"
                )

    def to_list(self) -> List[float]:
        """Returns the embedding vector as a standard Python list of floats."""
        return [float(x) for x in self.vector]
