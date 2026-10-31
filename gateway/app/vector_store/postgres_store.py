"""
PostgreSQL vector storage implementation backed by asyncpg and pgvector.
Enforces namespace scoping, model isolation, and validation of vector dimensionality.
"""

from datetime import datetime, timezone
import json
import logging
import math
from pathlib import Path
import re
from typing import Any, Dict, List, Optional, Sequence

import asyncpg

from .contracts import (
    VectorRecord,
    VectorSearchResult,
    cosine_distance_to_similarity,
)
from .errors import (
    VectorDimensionMismatchError,
    VectorInvalidError,
    VectorStoreConnectionError,
    VectorStoreError,
)
from .interfaces import BaseVectorStore

logger = logging.getLogger("intellicache.gateway.vector_store")


def _format_vector(vector: Sequence[float]) -> str:
    """Formats a validated sequence of floats into pgvector string representation: '[x1,x2,...]'."""
    return f"[{','.join(str(float(x)) for x in vector)}]"


def _parse_vector(raw: Any) -> tuple[float, ...]:
    """Parses a raw vector from the database into an immutable tuple of floats."""
    if isinstance(raw, str):
        cleaned = raw.strip("[]() ")
        if not cleaned:
            return ()
        return tuple(float(x) for x in cleaned.split(","))
    if isinstance(raw, (list, tuple)):
        return tuple(float(x) for x in raw)
    raise VectorInvalidError(f"Unexpected vector format from database: {type(raw)}")


class PostgresVectorStore(BaseVectorStore):
    """
    Persistent vector store using PostgreSQL with the pgvector extension.
    Provides nearest-neighbour retrieval with cosine distance and similarity scoring.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        default_namespace: str = "default",
        default_model: str = "nomic-embed-text",
        dimensions: int = 768,
        owns_pool: bool = False,
    ):
        if dimensions <= 0:
            raise VectorInvalidError(f"Vector dimensions must be positive, got {dimensions}.")
        if not default_namespace or not default_namespace.strip():
            raise VectorInvalidError("Default namespace cannot be empty.")
        if not default_model or not default_model.strip():
            raise VectorInvalidError("Default model cannot be empty.")

        self.pool = pool
        self.default_namespace = default_namespace.strip()
        self.default_model = default_model.strip()
        self.dimensions = dimensions
        self._owns_pool = owns_pool

    def _validate_vector_dimensions(self, vector: Sequence[float], context: str = "vector") -> None:
        """Validates that a vector's dimension matches the configured store dimension."""
        if not vector or len(vector) == 0:
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
        Inserts or updates a vector record in the database.
        Never logs raw query text or embedding vectors.
        """
        if not isinstance(record, VectorRecord):
            raise VectorInvalidError("Record must be an instance of VectorRecord.")

        # Validate dimensions against store configuration
        self._validate_vector_dimensions(record.vector, context="Record vector")

        ns = record.cache_namespace.strip()
        if not ns:
            raise VectorInvalidError("Cache namespace cannot be empty.")

        vec_str = _format_vector(record.vector)
        meta_json = json.dumps(record.metadata or {})
        created_at = record.created_at or datetime.now(timezone.utc)

        query = """
            INSERT INTO vector_records (
                cache_namespace, fingerprint, query_text, model, vector, metadata, created_at, updated_at
            )
            VALUES (
                $1, $2, $3, $4, $5::vector, $6::jsonb, $7, NOW()
            )
            ON CONFLICT (cache_namespace, fingerprint, model)
            DO UPDATE SET
                query_text = EXCLUDED.query_text,
                vector = EXCLUDED.vector,
                metadata = EXCLUDED.metadata,
                updated_at = NOW();
        """

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(
                    query,
                    ns,
                    record.fingerprint,
                    record.query_text,
                    record.model,
                    vec_str,
                    meta_json,
                    created_at,
                )
            logger.debug(
                "Upserted vector record: namespace='%s', fingerprint='%s', model='%s', dimensions=%d",
                ns,
                record.fingerprint[:12],
                record.model,
                self.dimensions,
            )
        except asyncpg.ForeignKeyViolationError as e:
            raise VectorStoreError(
                f"Cannot upsert vector record: associated exact cache entry not found for "
                f"namespace='{ns}', fingerprint='{record.fingerprint}'."
            ) from e
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector upsert: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector upsert: {e}") from e

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

        vec_str = _format_vector(query_vector)

        query = """
            SELECT
                v.id,
                v.cache_namespace,
                v.fingerprint,
                v.query_text,
                v.model,
                v.metadata,
                v.created_at,
                (v.vector <=> $1::vector) AS distance,
                c.response AS cached_response,
                c.prompt_tokens,
                c.completion_tokens,
                c.total_tokens,
                c.latency_ms AS original_latency_ms
            FROM vector_records v
            JOIN cache_entries c
                ON v.cache_namespace = c.cache_namespace
               AND v.fingerprint = c.fingerprint
               AND (c.expires_at IS NULL OR c.expires_at > NOW())
            WHERE v.cache_namespace = $2
              AND v.model = $3
            ORDER BY distance ASC
            LIMIT $4;
        """

        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, vec_str, ns, target_model, top_k)

            results: List[VectorSearchResult] = []
            for row in rows:
                dist = float(row["distance"])
                sim = cosine_distance_to_similarity(dist)
                raw_meta = row["metadata"]
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})

                prompt_toks = int(row["prompt_tokens"]) if row["prompt_tokens"] is not None else 0
                comp_toks = int(row["completion_tokens"]) if row["completion_tokens"] is not None else 0
                tot_toks = int(row["total_tokens"]) if row["total_tokens"] is not None else 0
                orig_latency = float(row["original_latency_ms"]) if row["original_latency_ms"] is not None else 0.0

                results.append(
                    VectorSearchResult(
                        cache_namespace=row["cache_namespace"],
                        fingerprint=row["fingerprint"],
                        query_text=row["query_text"],
                        model=row["model"],
                        distance=dist,
                        similarity=sim,
                        cached_response=row["cached_response"],
                        prompt_tokens=prompt_toks,
                        completion_tokens=comp_toks,
                        total_tokens=tot_toks,
                        original_latency_ms=orig_latency,
                        metadata=meta,
                        created_at=row["created_at"],
                    )
                )

            logger.debug(
                "Vector search completed: namespace='%s', model='%s', top_k=%d, found=%d",
                ns,
                target_model,
                top_k,
                len(results),
            )
            return results
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector search: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector search: {e}") from e

    async def get(
        self,
        fingerprint: str,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> Optional[VectorRecord]:
        """Retrieves a specific vector record by namespace, fingerprint, and model."""
        fp = (fingerprint or "").strip()
        if not fp:
            raise VectorInvalidError("Fingerprint cannot be empty.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        target_model = (model or self.default_model).strip()
        if not target_model:
            raise VectorInvalidError("Model cannot be empty.")

        query = """
            SELECT cache_namespace, fingerprint, query_text, model, vector, metadata, created_at
            FROM vector_records
            WHERE cache_namespace = $1 AND fingerprint = $2 AND model = $3
        """

        try:
            async with self.pool.acquire() as conn:
                row = await conn.fetchrow(query, ns, fp, target_model)
                if not row:
                    return None

                parsed_vec = _parse_vector(row["vector"])
                raw_meta = row["metadata"]
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})

                return VectorRecord(
                    cache_namespace=row["cache_namespace"],
                    fingerprint=row["fingerprint"],
                    query_text=row["query_text"],
                    model=row["model"],
                    vector=parsed_vec,
                    metadata=meta,
                    created_at=row["created_at"],
                )
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector retrieval: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector retrieval: {e}") from e

    async def delete(self, fingerprint: str, namespace: Optional[str] = None) -> bool:
        """Deletes vector records matching fingerprint and namespace."""
        fp = (fingerprint or "").strip()
        if not fp:
            raise VectorInvalidError("Fingerprint cannot be empty.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        query = "DELETE FROM vector_records WHERE cache_namespace = $1 AND fingerprint = $2"
        try:
            async with self.pool.acquire() as conn:
                result_tag = await conn.execute(query, ns, fp)
            # Result tag is e.g. "DELETE 1" or "DELETE 0"
            count = int(result_tag.split()[-1]) if result_tag else 0
            return count > 0
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector deletion: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector deletion: {e}") from e

    async def clear(self, namespace: Optional[str] = None) -> int:
        """Deletes all vector records within the specified namespace."""
        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        query = "DELETE FROM vector_records WHERE cache_namespace = $1"
        try:
            async with self.pool.acquire() as conn:
                result_tag = await conn.execute(query, ns)
            count = int(result_tag.split()[-1]) if result_tag else 0
            logger.info("Cleared %d vector records in namespace '%s'", count, ns)
            return count
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during namespace clear: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during namespace clear: {e}") from e

    async def get_index_info(self) -> Dict[str, Any]:
        """
        Inspects whether vector indexes (e.g. HNSW) are present on the vector_records table.
        """
        query = """
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = 'vector_records';
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query)
            indexes = {r["indexname"]: r["indexdef"] for r in rows}
            hnsw_present = any("hnsw" in (idef or "").lower() for idef in indexes.values())
            return {
                "vector_index_type": "hnsw" if hnsw_present else "none",
                "vector_index_available": hnsw_present,
                "indexes": list(indexes.keys()),
            }
        except Exception as e:
            logger.warning("Failed to query index information: %s", e)
            return {
                "vector_index_type": "unknown",
                "vector_index_available": False,
                "error": str(e),
            }

    async def explain_search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
        analyze: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes EXPLAIN (or EXPLAIN ANALYZE) on the nearest-neighbour search query.
        Returns whether the HNSW index was actually selected by the query planner,
        the retrieval method classification, and sanitized execution plan lines.
        """
        self._validate_vector_dimensions(query_vector, context="Query vector")
        ns = (namespace or self.default_namespace).strip()
        target_model = (model or self.default_model).strip()
        vec_str = _format_vector(query_vector)

        explain_cmd = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN (BUFFERS)"
        query = f"""
            {explain_cmd}
            SELECT
                v.id,
                v.cache_namespace,
                v.fingerprint,
                v.query_text,
                v.model,
                v.metadata,
                v.created_at,
                (v.vector <=> $1::vector) AS distance,
                c.response AS cached_response,
                c.prompt_tokens,
                c.completion_tokens,
                c.total_tokens,
                c.latency_ms AS original_latency_ms
            FROM vector_records v
            JOIN cache_entries c
                ON v.cache_namespace = c.cache_namespace
               AND v.fingerprint = c.fingerprint
               AND (c.expires_at IS NULL OR c.expires_at > NOW())
            WHERE v.cache_namespace = $2
              AND v.model = $3
            ORDER BY distance ASC
            LIMIT $4;
        """
        try:
            async with self.pool.acquire() as conn:
                rows = await conn.fetch(query, vec_str, ns, target_model, top_k)
            plan_lines = [r[0] for r in rows]
            plan_text = " ".join(plan_lines).lower()

            hnsw_used = "idx_vector_records_embedding_hnsw" in plan_text
            retrieval_method = (
                "HNSW approximate nearest-neighbour search"
                if hnsw_used
                else "Exact filtered vector search (B-tree index / sequential scan with exact distance sort)"
            )
            sanitized_lines = []
            for line in plan_lines:
                sanitized = re.sub(r"\'?\[[0-9\.\,\s\-eE]+\]\'?::vector", "[vector(...)]", line)
                sanitized_lines.append(sanitized)

            return {
                "vector_index_used": hnsw_used,
                "retrieval_method": retrieval_method,
                "execution_plan_summary": sanitized_lines[:40],
            }
        except Exception as e:
            logger.warning("Failed to run explain_search: %s", e)
            return {
                "vector_index_used": False,
                "retrieval_method": "Unknown",
                "error": str(e),
                "execution_plan_summary": [],
            }

    async def close(self) -> None:
        """Closes the underlying pool if owned by this instance."""
        if self._owns_pool and self.pool:
            await self.pool.close()
            logger.info("PostgresVectorStore connection pool closed.")
