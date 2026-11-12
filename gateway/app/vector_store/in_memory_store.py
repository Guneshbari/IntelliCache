"""
In-memory vector store implementation.
Provides an asynchronous, memory-backed vector storage option conforming to BaseVectorStore.
Supports nearest-neighbour retrieval by cosine distance, namespace isolation,
and integration with ExactCache for response resolution.
"""

import asyncio
from datetime import datetime, timezone
import logging
import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .contracts import VectorRecord, VectorSearchResult, cosine_distance_to_similarity
from .errors import (
    VectorDimensionMismatchError,
    VectorInvalidError,
)
from .interfaces import BaseVectorStore

logger = logging.getLogger("intellicache.gateway.vector_store.in_memory")


class InMemoryVectorStore(BaseVectorStore):
    """
    In-memory vector store for development, testing, and environments
    where PostgreSQL pgvector is unavailable.
    Thread-safe and coroutine-safe using asyncio.Lock.
    """

    def __init__(
        self,
        dimensions: int = 384,
        default_namespace: str = "default",
        default_model: str = "BAAI/bge-small-en-v1.5",
        exact_cache: Optional[Any] = None,
    ):
        if isinstance(dimensions, bool) or not isinstance(dimensions, int) or dimensions <= 0:
            raise VectorInvalidError(f"Vector dimensions must be a positive integer, got {dimensions}.")

        self.dimensions = dimensions
        self.default_namespace = (default_namespace or "default").strip()
        self.default_model = (default_model or "BAAI/bge-small-en-v1.5").strip()
        self.exact_cache = exact_cache
        self._lock = asyncio.Lock()
        # Key: (cache_namespace, fingerprint, model) -> VectorRecord
        self._records: Dict[Tuple[str, str, str], VectorRecord] = {}

    def _validate_vector_dimensions(self, vector: Sequence[float], context: str = "Vector") -> None:
        """Validates vector length and element types."""
        if not vector:
            raise VectorInvalidError(f"{context} cannot be empty.")
        if len(vector) != self.dimensions:
            raise VectorDimensionMismatchError(
                f"{context} length ({len(vector)}) does not match store dimension ({self.dimensions})."
            )
        for i, val in enumerate(vector):
            if isinstance(val, bool) or not isinstance(val, (int, float)) or math.isnan(val) or math.isinf(val):
                raise VectorInvalidError(
                    f"{context} contains non-finite or invalid number at index {i}: {val}"
                )

    async def upsert(self, record: VectorRecord) -> None:
        """
        Inserts or updates a vector record in memory.
        Validates dimensions against store configuration.
        """
        if not isinstance(record, VectorRecord):
            raise VectorInvalidError("Record must be an instance of VectorRecord.")

        self._validate_vector_dimensions(record.vector, context="Record vector")

        ns = record.cache_namespace.strip()
        if not ns:
            raise VectorInvalidError("Cache namespace cannot be empty.")

        key = (ns, record.fingerprint.strip(), record.model.strip())
        async with self._lock:
            self._records[key] = record

        logger.debug(
            "Upserted in-memory vector record: namespace='%s', fingerprint='%s', model='%s', dimensions=%d",
            ns,
            record.fingerprint[:12],
            record.model,
            self.dimensions,
        )

    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[VectorSearchResult]:
        """
        Searches for top-k nearest records by cosine distance.
        Enforces namespace and model isolation.
        """
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise VectorInvalidError(f"top_k must be a positive integer >= 1, got {top_k}.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Search namespace cannot be empty.")

        target_model = (model or self.default_model).strip()
        if not target_model:
            raise VectorInvalidError("Search model cannot be empty.")

        self._validate_vector_dimensions(query_vector, context="Query vector")

        async with self._lock:
            candidates = [
                rec for (r_ns, _, r_model), rec in self._records.items()
                if r_ns == ns and r_model == target_model
            ]

        if not candidates:
            return []

        # Compute query vector norm
        q_norm_sq = sum(x * x for x in query_vector)
        q_norm = math.sqrt(q_norm_sq)
        if q_norm == 0.0:
            raise VectorInvalidError("Query vector norm cannot be zero.")

        scored_candidates: List[VectorSearchResult] = []
        for rec in candidates:
            # Dot product and candidate norm
            dot = 0.0
            rec_norm_sq = 0.0
            for q_val, r_val in zip(query_vector, rec.vector):
                dot += q_val * r_val
                rec_norm_sq += r_val * r_val

            rec_norm = math.sqrt(rec_norm_sq)
            if rec_norm == 0.0:
                continue

            raw_sim = dot / (q_norm * rec_norm)
            cos_sim = max(-1.0, min(1.0, raw_sim))
            # pgvector distance operator <=> formula: distance = 1.0 - cosine_similarity
            distance = max(0.0, 1.0 - cos_sim)
            similarity = cosine_distance_to_similarity(distance)

            cached_resp: Optional[str] = None
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            original_latency_ms = 0.0

            if self.exact_cache is not None:
                entry = await self.exact_cache.get(rec.fingerprint, namespace=rec.cache_namespace)
                if entry is None:
                    # Inactive or expired exact cache entry -> exclude candidate
                    continue
                cached_resp = entry.response
                usage = entry.usage
                prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
                completion_tokens = getattr(usage, "completion_tokens", 0) or 0
                total_tokens = getattr(usage, "total_tokens", 0) or 0
                original_latency_ms = entry.latency_ms
            else:
                cached_resp = rec.metadata.get("cached_response")

            scored_candidates.append(
                VectorSearchResult(
                    cache_namespace=rec.cache_namespace,
                    fingerprint=rec.fingerprint,
                    query_text=rec.query_text,
                    model=rec.model,
                    distance=distance,
                    similarity=similarity,
                    cached_response=cached_resp,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    original_latency_ms=original_latency_ms,
                    metadata=dict(rec.metadata),
                    created_at=rec.created_at,
                )
            )

        # Order by cosine distance ascending (closest / highest similarity first)
        scored_candidates.sort(key=lambda c: c.distance)
        return scored_candidates[:top_k]

    async def delete(self, fingerprint: str, namespace: Optional[str] = None) -> bool:
        """Deletes a vector record by namespace and exact-cache fingerprint."""
        if not fingerprint or not fingerprint.strip():
            raise VectorInvalidError("Fingerprint cannot be empty.")

        ns = (namespace or self.default_namespace).strip()
        fp = fingerprint.strip()

        async with self._lock:
            keys_to_delete = [
                k for k in self._records
                if k[0] == ns and k[1] == fp
            ]
            for k in keys_to_delete:
                del self._records[k]
            return len(keys_to_delete) > 0

    async def clear(self, namespace: Optional[str] = None) -> int:
        """Deletes all vector records within the specified namespace."""
        ns = (namespace or self.default_namespace).strip()
        async with self._lock:
            keys_to_delete = [
                k for k in self._records
                if k[0] == ns
            ]
            for k in keys_to_delete:
                del self._records[k]
            return len(keys_to_delete)

    async def close(self) -> None:
        """Cleans up in-memory vector store resources."""
        async with self._lock:
            self._records.clear()
        logger.info("InMemoryVectorStore closed.")
