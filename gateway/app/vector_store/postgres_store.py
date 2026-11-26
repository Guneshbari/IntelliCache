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

from ..privacy import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    CACHE_VISIBILITY_UNKNOWN,
    normalize_cache_visibility,
    normalize_principal_id,
)
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
        table_name: str = "vector_records",
        readonly_transaction: bool = False,
        exclude_unmigrated_legacy: bool = False,
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
        self.table_name = table_name.strip()
        self.readonly_transaction = bool(readonly_transaction)
        self.exclude_unmigrated_legacy = bool(exclude_unmigrated_legacy)

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
        variant_key = (record.variant_key or "").strip()
        vis = normalize_cache_visibility(record.visibility)
        owner = normalize_principal_id(record.owner_principal_id) if vis == CACHE_VISIBILITY_PRIVATE else None
        if vis == CACHE_VISIBILITY_PRIVATE and owner is None:
            raise VectorInvalidError("Cannot upsert private VectorRecord without owner_principal_id.")

        if self.table_name == "vector_records" and variant_key and record.response_text is not None:
            query = """
                INSERT INTO semantic_variant_records (
                    cache_namespace, fingerprint, variant_key, query_text, model, vector,
                    metadata, cached_response, created_at, updated_at, visibility, owner_principal_id
                )
                VALUES (
                    $1, $2, $3, $4, $5, $6::vector, $7::jsonb, $8, $9, NOW(), $10, $11
                )
                ON CONFLICT (cache_namespace, fingerprint, model, variant_key)
                DO UPDATE SET
                    query_text = EXCLUDED.query_text,
                    vector = EXCLUDED.vector,
                    metadata = EXCLUDED.metadata,
                    cached_response = EXCLUDED.cached_response,
                    visibility = EXCLUDED.visibility,
                    owner_principal_id = EXCLUDED.owner_principal_id,
                    updated_at = NOW();
            """
            params = (
                ns,
                record.fingerprint,
                variant_key,
                record.query_text,
                record.model,
                vec_str,
                meta_json,
                record.response_text,
                created_at,
                vis,
                owner,
            )
        else:
            query = f"""
                INSERT INTO {self.table_name} (
                    cache_namespace, fingerprint, query_text, model, vector, metadata, created_at, updated_at,
                    visibility, owner_principal_id
                )
                VALUES (
                    $1, $2, $3, $4, $5::vector, $6::jsonb, $7, NOW(), $8, $9
                )
                ON CONFLICT (cache_namespace, fingerprint, model)
                DO UPDATE SET
                    query_text = EXCLUDED.query_text,
                    vector = EXCLUDED.vector,
                    metadata = EXCLUDED.metadata,
                    visibility = EXCLUDED.visibility,
                    owner_principal_id = EXCLUDED.owner_principal_id,
                    updated_at = NOW();
            """
            params = (
                ns,
                record.fingerprint,
                record.query_text,
                record.model,
                vec_str,
                meta_json,
                created_at,
                vis,
                owner,
            )

        try:
            async with self.pool.acquire() as conn:
                await conn.execute(query, *params)
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

    @staticmethod
    def _build_scope_filter(
        alias: str,
        exact_alias: str,
        visibility: Optional[str],
        principal_id: Optional[str],
        allow_shared_reads: bool,
        use_left_join: bool = False,
        start_idx: int = 5,
    ) -> tuple[str, list[Any], bool]:
        if visibility is None:
            if use_left_join:
                return (
                    f"AND {alias}.visibility <> 'private' "
                    f"AND ({exact_alias}.id IS NULL OR {exact_alias}.visibility <> 'private')",
                    [],
                    False,
                )
            return f"AND {alias}.visibility <> 'private' AND {exact_alias}.visibility <> 'private'", [], False

        req_vis = normalize_cache_visibility(visibility)
        if req_vis == CACHE_VISIBILITY_UNKNOWN:
            return "", [], True

        if req_vis == CACHE_VISIBILITY_TENANT_SHARED:
            exact_cond = (
                f"({exact_alias}.id IS NULL OR ({exact_alias}.visibility = 'tenant_shared' AND {exact_alias}.owner_principal_id IS NULL))"
                if use_left_join
                else f"{exact_alias}.visibility = 'tenant_shared' AND {exact_alias}.owner_principal_id IS NULL"
            )
            return (
                f"AND {alias}.visibility = 'tenant_shared' AND {alias}.owner_principal_id IS NULL AND {exact_cond}",
                [],
                False,
            )

        if req_vis == CACHE_VISIBILITY_PRIVATE:
            req_own = normalize_principal_id(principal_id)
            if req_own is None:
                return "", [], True
            if not allow_shared_reads:
                exact_cond = (
                    f"({exact_alias}.id IS NULL OR ({exact_alias}.visibility = 'private' AND {exact_alias}.owner_principal_id = ${start_idx}))"
                    if use_left_join
                    else f"{exact_alias}.visibility = 'private' AND {exact_alias}.owner_principal_id = ${start_idx}"
                )
                return (
                    f"AND {alias}.visibility = 'private' AND {alias}.owner_principal_id = ${start_idx} AND {exact_cond}",
                    [req_own],
                    False,
                )
            if use_left_join:
                return (
                    f"AND (({alias}.visibility = 'private' AND {alias}.owner_principal_id = ${start_idx} "
                    f"AND ({exact_alias}.id IS NULL OR ({exact_alias}.visibility = 'private' AND {exact_alias}.owner_principal_id = ${start_idx}))) "
                    f"OR ({alias}.visibility = 'tenant_shared' AND {alias}.owner_principal_id IS NULL "
                    f"AND ({exact_alias}.id IS NULL OR ({exact_alias}.visibility = 'tenant_shared' AND {exact_alias}.owner_principal_id IS NULL))))",
                    [req_own],
                    False,
                )
            return (
                f"AND (({alias}.visibility = 'private' AND {alias}.owner_principal_id = ${start_idx} "
                f"AND {exact_alias}.visibility = 'private' AND {exact_alias}.owner_principal_id = ${start_idx}) "
                f"OR ({alias}.visibility = 'tenant_shared' AND {alias}.owner_principal_id IS NULL "
                f"AND {exact_alias}.visibility = 'tenant_shared' AND {exact_alias}.owner_principal_id IS NULL))",
                [req_own],
                False,
            )

        return "", [], True

    def _build_search_queries(
        self,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
        allow_shared_reads: bool = False,
        context_class: Optional[str] = None,
        context_fingerprint: Optional[str] = None,
    ) -> tuple[str, Optional[str], list[Any], bool]:
        """Builds parameterized single-table fallback SQL and combined UNION ALL production SQL with scope filters."""
        join_type = "JOIN" if self.table_name == "vector_records" else "LEFT JOIN"
        use_left_join = join_type == "LEFT JOIN"
        scope_v, scope_params, fail_closed = self._build_scope_filter(
            "v",
            "c",
            visibility,
            principal_id,
            allow_shared_reads,
            use_left_join=use_left_join,
            start_idx=5,
        )
        if fail_closed:
            return "", None, [], True

        ctx_clauses_v: list[str] = []
        ctx_clauses_sv: list[str] = []
        ctx_params: list[Any] = []
        next_param_idx = 5 + len(scope_params)
        if context_class is not None:
            norm_cls = str(context_class).strip().upper()
            ctx_clauses_v.append(f"AND (v.metadata->'context_contract'->>'context_class') = ${next_param_idx}")
            ctx_clauses_sv.append(f"AND (sv.metadata->'context_contract'->>'context_class') = ${next_param_idx}")
            ctx_params.append(norm_cls)
            next_param_idx += 1
        if context_fingerprint is not None:
            norm_fp = str(context_fingerprint).strip()
            ctx_clauses_v.append(f"AND (v.metadata->'context_contract'->>'context_fingerprint') = ${next_param_idx}")
            ctx_clauses_sv.append(f"AND (sv.metadata->'context_contract'->>'context_fingerprint') = ${next_param_idx}")
            ctx_params.append(norm_fp)
            next_param_idx += 1
        ctx_sql_v = "\n                  ".join(ctx_clauses_v)
        ctx_sql_sv = "\n                  ".join(ctx_clauses_sv)
        all_filter_params = [*scope_params, *ctx_params]

        legacy_filter = "AND COALESCE((v.metadata->>'legacy_excluded')::boolean, FALSE) = FALSE"
        if self.exclude_unmigrated_legacy:
            legacy_filter += (
                "\n              AND (v.metadata->>'schema_version') ~ '^[0-9]+$'"
                "\n              AND (v.metadata->>'schema_version')::integer >= 3"
            )

        single_query = f"""
            SELECT
                v.id,
                v.cache_namespace,
                v.fingerprint,
                v.query_text,
                v.model,
                v.metadata,
                v.created_at,
                v.visibility,
                v.owner_principal_id,
                (v.vector <=> $1::vector) AS distance,
                c.response AS cached_response,
                c.prompt_tokens,
                c.completion_tokens,
                c.total_tokens,
                c.latency_ms AS original_latency_ms
            FROM {self.table_name} v
            {join_type} cache_entries c
                ON v.cache_namespace = c.cache_namespace
               AND v.fingerprint = c.fingerprint
               AND (c.expires_at IS NULL OR c.expires_at > NOW())
            WHERE v.cache_namespace = $2
              AND v.model = $3
              {legacy_filter}
              {scope_v}
              {ctx_sql_v}
            ORDER BY distance ASC
            LIMIT $4;
        """

        if self.table_name != "vector_records":
            return single_query, None, all_filter_params, False

        sv_legacy_filter = legacy_filter.replace("v.metadata", "sv.metadata")
        scope_sv, _, _ = self._build_scope_filter(
            "sv",
            "c",
            visibility,
            principal_id,
            allow_shared_reads,
            use_left_join=False,
            start_idx=5,
        )
        combined_query = f"""
            WITH combined AS (
                (SELECT
                    v.id,
                    v.cache_namespace,
                    v.fingerprint,
                    v.query_text,
                    v.model,
                    v.metadata,
                    v.created_at,
                    v.visibility,
                    v.owner_principal_id,
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
                  {legacy_filter}
                  {scope_v}
                  {ctx_sql_v}
                ORDER BY distance ASC
                LIMIT $4)
                UNION ALL
                (SELECT
                    sv.id,
                    sv.cache_namespace,
                    sv.fingerprint,
                    sv.query_text,
                    sv.model,
                    sv.metadata,
                    sv.created_at,
                    sv.visibility,
                    sv.owner_principal_id,
                    (sv.vector <=> $1::vector) AS distance,
                    sv.cached_response AS cached_response,
                    c.prompt_tokens,
                    c.completion_tokens,
                    c.total_tokens,
                    c.latency_ms AS original_latency_ms
                FROM semantic_variant_records sv
                JOIN cache_entries c
                    ON sv.cache_namespace = c.cache_namespace
                   AND sv.fingerprint = c.fingerprint
                   AND (c.expires_at IS NULL OR c.expires_at > NOW())
                WHERE sv.cache_namespace = $2
                  AND sv.model = $3
                  {sv_legacy_filter}
                  {scope_sv}
                  {ctx_sql_sv}
                ORDER BY distance ASC
                LIMIT $4)
            )
            SELECT * FROM combined ORDER BY distance ASC LIMIT $4;
        """
        return single_query, combined_query, all_filter_params, False

    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
        allow_shared_reads: bool = False,
        context_class: Optional[str] = None,
        context_fingerprint: Optional[str] = None,
    ) -> List[VectorSearchResult]:
        """Searches nearest neighbors within namespace, model, and privacy scope, filtering out expired entries."""
        if isinstance(top_k, bool) or not isinstance(top_k, int) or top_k <= 0:
            raise VectorInvalidError(f"top_k must be a positive integer >= 1, got {top_k}.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Search namespace cannot be empty.")

        target_model = (model or self.default_model).strip()
        if not target_model:
            raise VectorInvalidError("Search model cannot be empty.")

        self._validate_vector_dimensions(query_vector, context="Query vector")

        query, combined_query, scope_params, fail_closed = self._build_search_queries(
            visibility=visibility,
            principal_id=principal_id,
            allow_shared_reads=allow_shared_reads,
            context_class=context_class,
            context_fingerprint=context_fingerprint,
        )
        if fail_closed:
            return []

        vec_str = _format_vector(query_vector)
        params: list[Any] = [vec_str, ns, target_model, top_k, *scope_params]

        try:
            async with self.pool.acquire() as conn:
                if combined_query is not None:
                    try:
                        if self.readonly_transaction:
                            async with conn.transaction(readonly=True):
                                rows = await conn.fetch(combined_query, *params)
                        else:
                            rows = await conn.fetch(combined_query, *params)
                    except asyncpg.UndefinedTableError:
                        if self.readonly_transaction:
                            async with conn.transaction(readonly=True):
                                rows = await conn.fetch(query, *params)
                        else:
                            rows = await conn.fetch(query, *params)
                else:
                    if self.readonly_transaction:
                        async with conn.transaction(readonly=True):
                            rows = await conn.fetch(query, *params)
                    else:
                        rows = await conn.fetch(query, *params)

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
                resp_text = row["cached_response"] or meta.get("cached_response")

                results.append(
                    VectorSearchResult(
                        cache_namespace=row["cache_namespace"],
                        fingerprint=row["fingerprint"],
                        query_text=row["query_text"],
                        model=row["model"],
                        distance=dist,
                        similarity=sim,
                        cached_response=resp_text,
                        prompt_tokens=prompt_toks,
                        completion_tokens=comp_toks,
                        total_tokens=tot_toks,
                        original_latency_ms=orig_latency,
                        metadata=meta,
                        created_at=row["created_at"],
                        visibility=row["visibility"],
                        owner_principal_id=row["owner_principal_id"],
                    )
                )

            results.sort(key=lambda r: (r.distance, r.fingerprint, str((r.metadata or {}).get("variant_key") or "")))
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
        variant_key: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> Optional[VectorRecord]:
        """Retrieves a specific vector record by namespace, fingerprint, model[, variant_key[, scope]]."""
        fp = (fingerprint or "").strip()
        if not fp:
            raise VectorInvalidError("Fingerprint cannot be empty.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        target_model = (model or self.default_model).strip()
        if not target_model:
            raise VectorInvalidError("Model cannot be empty.")

        scope_sql = ""
        scope_params: list[Any] = []
        if visibility is not None:
            norm_vis = normalize_cache_visibility(visibility)
            if norm_vis == CACHE_VISIBILITY_UNKNOWN:
                return None
            if norm_vis == CACHE_VISIBILITY_PRIVATE:
                norm_p = normalize_principal_id(principal_id)
                if norm_p is None:
                    return None
                scope_sql = " AND visibility = 'private' AND owner_principal_id = $4"
                scope_params = [norm_p]
            else:
                scope_sql = " AND visibility = 'tenant_shared' AND owner_principal_id IS NULL"
        else:
            scope_sql = " AND visibility <> 'private'"

        base_params: list[Any] = [ns, fp, target_model, *scope_params]
        query = f"""
            SELECT cache_namespace, fingerprint, query_text, model, vector, metadata, created_at,
                   visibility, owner_principal_id
            FROM {self.table_name}
            WHERE cache_namespace = $1 AND fingerprint = $2 AND model = $3{scope_sql}
        """

        try:
            async with self.pool.acquire() as conn:
                if self.table_name == "vector_records":
                    try:
                        if variant_key is not None:
                            vk_idx = 4 + len(scope_params)
                            sv_row = await conn.fetchrow(
                                f"""
                                SELECT cache_namespace, fingerprint, variant_key, query_text, model, vector, metadata,
                                       cached_response, created_at, visibility, owner_principal_id
                                FROM semantic_variant_records
                                WHERE cache_namespace = $1 AND fingerprint = $2 AND model = $3{scope_sql}
                                  AND variant_key = ${vk_idx}
                                LIMIT 1
                                """,
                                *base_params,
                                variant_key.strip(),
                            )
                        else:
                            sv_row = await conn.fetchrow(
                                f"""
                                SELECT cache_namespace, fingerprint, variant_key, query_text, model, vector, metadata,
                                       cached_response, created_at, visibility, owner_principal_id
                                FROM semantic_variant_records
                                WHERE cache_namespace = $1 AND fingerprint = $2 AND model = $3{scope_sql}
                                ORDER BY updated_at DESC
                                LIMIT 1
                                """,
                                *base_params,
                            )
                    except asyncpg.UndefinedTableError:
                        sv_row = None

                    if sv_row is not None:
                        raw_meta = sv_row["metadata"]
                        meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
                        return VectorRecord(
                            cache_namespace=sv_row["cache_namespace"],
                            fingerprint=sv_row["fingerprint"],
                            query_text=sv_row["query_text"],
                            model=sv_row["model"],
                            vector=_parse_vector(sv_row["vector"]),
                            metadata=meta,
                            created_at=sv_row["created_at"],
                            variant_key=sv_row["variant_key"],
                            response_text=sv_row["cached_response"],
                            visibility=sv_row["visibility"],
                            owner_principal_id=sv_row["owner_principal_id"],
                        )
                    if variant_key is not None:
                        vk_idx = 4 + len(scope_params)
                        row = await conn.fetchrow(
                            f"""
                            SELECT cache_namespace, fingerprint, query_text, model, vector, metadata, created_at,
                                   visibility, owner_principal_id
                            FROM {self.table_name}
                            WHERE cache_namespace = $1 AND fingerprint = $2 AND model = $3{scope_sql}
                              AND metadata->>'variant_key' = ${vk_idx}
                            """,
                            *base_params,
                            variant_key.strip(),
                        )
                    else:
                        row = await conn.fetchrow(query, *base_params)
                else:
                    row = await conn.fetchrow(query, *base_params)

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
                    visibility=row["visibility"],
                    owner_principal_id=row["owner_principal_id"],
                )
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector retrieval: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector retrieval: {e}") from e

    async def get_by_fingerprint(
        self,
        fingerprint: str,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
        variant_key: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> Optional[VectorRecord]:
        """Retrieves an existing vector record for (namespace, fingerprint, model[, variant_key[, scope]]), if any."""
        ns = (namespace or self.default_namespace).strip()
        target_model = (model or self.default_model).strip()
        fp = (fingerprint or "").strip()
        if not ns or not fp or not target_model:
            return None
        return await self.get(
            fp,
            namespace=ns,
            model=target_model,
            variant_key=variant_key,
            visibility=visibility,
            principal_id=principal_id,
        )

    async def delete(
        self,
        fingerprint: str,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> bool:
        """Deletes vector records matching fingerprint, namespace, and optional privacy scope."""
        fp = (fingerprint or "").strip()
        if not fp:
            raise VectorInvalidError("Fingerprint cannot be empty.")

        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        clauses = ["cache_namespace = $1", "fingerprint = $2"]
        params: list[Any] = [ns, fp]
        if visibility is not None:
            norm_vis = normalize_cache_visibility(visibility)
            if norm_vis == CACHE_VISIBILITY_UNKNOWN:
                return False
            params.append(norm_vis)
            clauses.append(f"visibility = ${len(params)}")
        if principal_id is not None:
            norm_p = normalize_principal_id(principal_id)
            if norm_p is None:
                return False
            params.append(norm_p)
            clauses.append(f"owner_principal_id = ${len(params)}")

        where_sql = " AND ".join(clauses)
        query = f"DELETE FROM {self.table_name} WHERE {where_sql}"
        try:
            total_deleted = 0
            async with self.pool.acquire() as conn:
                result_tag = await conn.execute(query, *params)
                total_deleted += int(result_tag.split()[-1]) if result_tag else 0
                if self.table_name == "vector_records":
                    try:
                        sv_tag = await conn.execute(
                            f"DELETE FROM semantic_variant_records WHERE {where_sql}",
                            *params,
                        )
                        total_deleted += int(sv_tag.split()[-1]) if sv_tag else 0
                    except asyncpg.UndefinedTableError:
                        pass
            return total_deleted > 0
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during vector deletion: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during vector deletion: {e}") from e

    async def clear(
        self,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> int:
        """Deletes vector records within the specified namespace (optionally scoped by visibility/principal)."""
        ns = (namespace or self.default_namespace).strip()
        if not ns:
            raise VectorInvalidError("Namespace cannot be empty.")

        clauses = ["cache_namespace = $1"]
        params: list[Any] = [ns]
        if visibility is not None:
            norm_vis = normalize_cache_visibility(visibility)
            if norm_vis == CACHE_VISIBILITY_UNKNOWN:
                return 0
            params.append(norm_vis)
            clauses.append(f"visibility = ${len(params)}")
        if principal_id is not None:
            norm_p = normalize_principal_id(principal_id)
            if norm_p is None:
                return 0
            params.append(norm_p)
            clauses.append(f"owner_principal_id = ${len(params)}")

        where_sql = " AND ".join(clauses)
        query = f"DELETE FROM {self.table_name} WHERE {where_sql}"
        try:
            count = 0
            async with self.pool.acquire() as conn:
                result_tag = await conn.execute(query, *params)
                count += int(result_tag.split()[-1]) if result_tag else 0
                if self.table_name == "vector_records":
                    try:
                        sv_tag = await conn.execute(
                            f"DELETE FROM semantic_variant_records WHERE {where_sql}",
                            *params,
                        )
                        count += int(sv_tag.split()[-1]) if sv_tag else 0
                    except asyncpg.UndefinedTableError:
                        pass
            logger.info("Cleared %d vector records in namespace '%s'", count, ns)
            return count
        except asyncpg.PostgresError as e:
            raise VectorStoreError(f"Database error during namespace clear: {e}") from e
        except Exception as e:
            raise VectorStoreConnectionError(f"Connection failure during namespace clear: {e}") from e

    async def verify_schema(self) -> bool:
        """
        Verifies that the vector_records table in PostgreSQL matches the configured store dimension.

        Raises:
            VectorDimensionMismatchError: If table exists with conflicting dimensions.
        """
        from .migration import verify_vector_schema
        return await verify_vector_schema(self.pool, self.dimensions)

    async def get_index_info(self) -> Dict[str, Any]:
        """
        Inspects whether vector indexes (e.g. HNSW) are present on the vector_records table.
        """
        query = f"""
            SELECT indexname, indexdef
            FROM pg_indexes
            WHERE tablename = '{self.table_name}';
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
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
        allow_shared_reads: bool = False,
    ) -> Dict[str, Any]:
        """
        Executes EXPLAIN (or EXPLAIN ANALYZE) on the exact production nearest-neighbour
        search query (combined UNION ALL across vector_records and semantic_variant_records
        when table_name == 'vector_records', with single-table fallback).
        Returns whether HNSW indexes were selected by the query planner,
        the retrieval method classification, and sanitized execution plan lines.
        """
        self._validate_vector_dimensions(query_vector, context="Query vector")
        ns = (namespace or self.default_namespace).strip()
        target_model = (model or self.default_model).strip()
        vec_str = _format_vector(query_vector)

        explain_cmd = "EXPLAIN (ANALYZE, BUFFERS)" if analyze else "EXPLAIN (BUFFERS)"
        single_query, combined_query, scope_params, fail_closed = self._build_search_queries(
            visibility=visibility,
            principal_id=principal_id,
            allow_shared_reads=allow_shared_reads,
        )
        if fail_closed:
            return {
                "vector_index_used": False,
                "vector_records_hnsw_used": False,
                "semantic_variant_records_hnsw_used": False,
                "retrieval_method": "Fail-closed empty scope",
                "execution_plan_summary": [],
            }
        params: list[Any] = [vec_str, ns, target_model, top_k, *scope_params]
        try:
            async with self.pool.acquire() as conn:
                if combined_query is not None:
                    try:
                        rows = await conn.fetch(
                            f"{explain_cmd}\n{combined_query}",
                            *params,
                        )
                    except asyncpg.UndefinedTableError:
                        rows = await conn.fetch(
                            f"{explain_cmd}\n{single_query}",
                            *params,
                        )
                else:
                    rows = await conn.fetch(
                        f"{explain_cmd}\n{single_query}",
                        *params,
                    )
            plan_lines = [r[0] for r in rows]
            plan_text = " ".join(plan_lines).lower()

            vr_hnsw_used = "idx_vector_records_embedding_hnsw" in plan_text
            sv_hnsw_used = "idx_semantic_variant_records_hnsw" in plan_text
            hnsw_used = vr_hnsw_used or sv_hnsw_used
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
                "vector_records_hnsw_used": vr_hnsw_used,
                "semantic_variant_records_hnsw_used": sv_hnsw_used,
                "retrieval_method": retrieval_method,
                "execution_plan_summary": sanitized_lines[:80],
            }
        except Exception as e:
            logger.warning("Failed to run explain_search: %s", e)
            return {
                "vector_index_used": False,
                "vector_records_hnsw_used": False,
                "semantic_variant_records_hnsw_used": False,
                "retrieval_method": "Unknown",
                "error": str(e),
                "execution_plan_summary": [],
            }

    async def close(self) -> None:
        """Closes the underlying pool if owned by this instance."""
        if self._owns_pool and self.pool:
            await self.pool.close()
            logger.info("PostgresVectorStore connection pool closed.")
