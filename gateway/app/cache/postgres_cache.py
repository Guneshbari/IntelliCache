"""
PostgreSQL-backed centralized exact cache implementation using asyncpg.
Supports multi-client sharing, namespace isolation, bounded capacity, and LRU eviction.
"""

from datetime import datetime, timedelta, timezone
import hashlib
import json
import logging
from typing import Any, Dict, Optional

import asyncpg

from .interfaces import BaseCache, CacheEntry, CacheStats
from ..core.contracts import UsageMetrics

logger = logging.getLogger("intellicache.gateway.postgres_cache")


def _namespace_to_lock_id(namespace: str) -> int:
    """Computes a deterministic 64-bit signed integer for PostgreSQL advisory locking."""
    digest = hashlib.sha256(f"intellicache:ns:{namespace}".encode("utf-8")).digest()
    return int.from_bytes(digest[:8], byteorder="big", signed=True)


class PostgresCache(BaseCache):
    """
    Centralized persistent exact-match cache backed by PostgreSQL.
    Multiple client PCs query and populate the same authoritative store
    over non-blocking asyncpg connections.
    """

    def __init__(
        self,
        pool: asyncpg.Pool,
        max_entries: int = 1000,
        ttl_seconds: int = 3600,
        default_namespace: str = "default",
    ):
        self.pool = pool
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.default_namespace = default_namespace
        self._hits: int = 0
        self._misses: int = 0
        self._coalesced_requests: int = 0
        self._coalesced_successes: int = 0
        self._tokens_saved: int = 0
        self._latency_saved_ms: float = 0.0

    async def get(self, key: str, namespace: Optional[str] = None) -> Optional[CacheEntry]:
        """
        Retrieves an unexpired cache entry for the specified namespace and fingerprint.
        Updates last_accessed timestamp and increments hit count atomically.
        """
        ns = (namespace or self.default_namespace).strip()
        query = """
            SELECT id, response, model, provider, prompt_tokens, completion_tokens, total_tokens,
                   latency_ms, hit_count, metadata, created_at, expires_at
            FROM cache_entries
            WHERE cache_namespace = $1 AND fingerprint = $2
        """
        async with self.pool.acquire() as conn:
            row = await conn.fetchrow(query, ns, key)
            if not row:
                return None

            now = datetime.now(timezone.utc)
            expires_at = row["expires_at"]
            if expires_at is not None:
                if expires_at.tzinfo is None:
                    expires_at = expires_at.replace(tzinfo=timezone.utc)
                if expires_at <= now:
                    # Entry is expired: clean up lazily and treat as cache miss
                    await conn.execute("DELETE FROM cache_entries WHERE id = $1", row["id"])
                    return None

            # Mark hit and update access timestamp
            await conn.execute(
                """
                UPDATE cache_entries
                SET last_accessed = NOW(), hit_count = hit_count + 1
                WHERE id = $1
                """,
                row["id"],
            )

        self._hits += 1
        total_toks = row["total_tokens"] or 0
        latency = float(row["latency_ms"] or 0.0)
        self._tokens_saved += total_toks
        self._latency_saved_ms += latency

        usage = UsageMetrics(
            prompt_tokens=row["prompt_tokens"] or 0,
            completion_tokens=row["completion_tokens"] or 0,
            total_tokens=total_toks,
        )

        raw_meta = row["metadata"]
        if isinstance(raw_meta, str):
            try:
                metadata = json.loads(raw_meta)
            except Exception:
                metadata = {}
        elif isinstance(raw_meta, dict):
            metadata = raw_meta
        else:
            metadata = {}

        entry = CacheEntry(
            response=row["response"],
            model=row["model"],
            usage=usage,
            latency_ms=latency,
            metadata=metadata,
            provider=row["provider"],
        )
        entry.hit_count = row["hit_count"] + 1
        created = row["created_at"]
        if created:
            entry.created_at = created.timestamp()
        return entry

    async def set(self, key: str, entry: CacheEntry, namespace: Optional[str] = None) -> None:
        """
        Inserts or updates an exact-match entry with conflict safety and bounded LRU capacity.
        """
        ns = (namespace or self.default_namespace).strip()
        now = datetime.now(timezone.utc)
        expires_at: Optional[datetime] = None
        if self.ttl_seconds > 0:
            expires_at = now + timedelta(seconds=self.ttl_seconds)

        created_dt = datetime.fromtimestamp(entry.created_at, tz=timezone.utc)
        meta_json = json.dumps(entry.metadata) if entry.metadata else "{}"

        prompt_toks = getattr(entry.usage, "prompt_tokens", 0) or 0
        comp_toks = getattr(entry.usage, "completion_tokens", 0) or 0
        tot_toks = getattr(entry.usage, "total_tokens", 0) or 0
        provider_name = (entry.provider or "groq").strip()

        upsert_query = """
            INSERT INTO cache_entries (
                cache_namespace, fingerprint, response, model, provider,
                prompt_tokens, completion_tokens, total_tokens, latency_ms,
                hit_count, metadata, created_at, last_accessed, expires_at
            ) VALUES (
                $1, $2, $3, $4, $5,
                $6, $7, $8, $9,
                $10, $11::jsonb, $12, $13, $14
            )
            ON CONFLICT (cache_namespace, fingerprint) DO UPDATE SET
                response = EXCLUDED.response,
                model = EXCLUDED.model,
                provider = EXCLUDED.provider,
                prompt_tokens = EXCLUDED.prompt_tokens,
                completion_tokens = EXCLUDED.completion_tokens,
                total_tokens = EXCLUDED.total_tokens,
                latency_ms = EXCLUDED.latency_ms,
                metadata = EXCLUDED.metadata,
                last_accessed = EXCLUDED.last_accessed,
                expires_at = EXCLUDED.expires_at;
        """

        lock_id = _namespace_to_lock_id(ns)
        async with self.pool.acquire() as conn:
            async with conn.transaction():
                # Serialize upsert and capacity enforcement per namespace
                await conn.execute("SELECT pg_advisory_xact_lock($1)", lock_id)

                # 1. Conflict-safe upsert
                await conn.execute(
                    upsert_query,
                    ns,
                    key,
                    entry.response,
                    entry.model,
                    provider_name,
                    prompt_toks,
                    comp_toks,
                    tot_toks,
                    entry.latency_ms,
                    entry.hit_count,
                    meta_json,
                    created_dt,
                    now,
                    expires_at,
                )

                # 2. Evict expired entries in this namespace
                await conn.execute(
                    "DELETE FROM cache_entries WHERE cache_namespace = $1 AND expires_at IS NOT NULL AND expires_at <= NOW()",
                    ns,
                )

                # 3. Bounded capacity check and LRU eviction for this namespace
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                    ns,
                )
                if count and count > self.max_entries:
                    excess = count - self.max_entries
                    evict_query = """
                        DELETE FROM cache_entries
                        WHERE id IN (
                            SELECT id FROM cache_entries
                            WHERE cache_namespace = $1
                            ORDER BY last_accessed ASC
                            LIMIT $2
                        )
                    """
                    await conn.execute(evict_query, ns, excess)

    async def record_miss(self) -> None:
        """Record an independent request that required upstream generation."""
        self._misses += 1

    async def record_coalesced_join(self) -> None:
        """Record that an incoming request joined an in-flight single-flight group."""
        self._coalesced_requests += 1

    async def record_coalesced_success(self, entry: CacheEntry) -> None:
        """Record that a joined follower successfully received the leader's result."""
        self._coalesced_successes += 1
        entry.hit_count += 1
        self._tokens_saved += getattr(entry.usage, "total_tokens", 0) or 0
        self._latency_saved_ms += entry.latency_ms

    async def record_coalesced(self, entry: Optional[CacheEntry] = None) -> None:
        """Convenience method for tests and backwards compatibility."""
        await self.record_coalesced_join()
        if entry is not None:
            await self.record_coalesced_success(entry)

    async def record_semantic_hit(self, tokens_saved: int, latency_saved_ms: float) -> None:
        """Record tokens and latency saved by a semantic cache hit."""
        self._hits += 1
        self._tokens_saved += max(0, tokens_saved)
        self._latency_saved_ms += max(0.0, latency_saved_ms)

    async def clear(self, namespace: Optional[str] = None) -> None:
        """Clears entries in PostgreSQL. If namespace is provided, clears only that namespace."""
        async with self.pool.acquire() as conn:
            if namespace is not None:
                await conn.execute(
                    "DELETE FROM cache_entries WHERE cache_namespace = $1",
                    namespace.strip(),
                )
            else:
                await conn.execute("DELETE FROM cache_entries")

        self._hits = 0
        self._misses = 0
        self._coalesced_requests = 0
        self._coalesced_successes = 0
        self._tokens_saved = 0
        self._latency_saved_ms = 0.0

    async def stats(self, enabled: bool = True, namespace: Optional[str] = None) -> CacheStats:
        """Returns statistical metrics on PostgreSQL cache capacity and session throughput."""
        ns = namespace.strip() if namespace else None
        async with self.pool.acquire() as conn:
            if ns:
                count = await conn.fetchval(
                    "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1",
                    ns,
                )
            else:
                count = await conn.fetchval("SELECT COUNT(*) FROM cache_entries")

        total_req = self._hits + self._misses + self._coalesced_requests
        reused_req = self._hits + self._coalesced_successes
        hit_rate = (reused_req / total_req) if total_req > 0 else 0.0

        return CacheStats(
            total_entries=count or 0,
            max_entries=self.max_entries,
            hits=self._hits,
            misses=self._misses,
            coalesced_requests=self._coalesced_requests,
            total_requests=total_req,
            hit_rate=round(hit_rate, 4),
            total_tokens_saved=self._tokens_saved,
            total_latency_saved_ms=round(self._latency_saved_ms, 2),
            ttl_seconds=self.ttl_seconds,
            enabled=enabled,
            backend="postgres",
        )

    async def close(self) -> None:
        """Closes the underlying asyncpg connection pool cleanly."""
        if self.pool is not None:
            await self.pool.close()
