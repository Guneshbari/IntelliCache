"""
In-memory LRU + TTL Exact Match Cache implementation for IntelliCache.
Provides asynchronous access and namespace support matching BaseCache.
"""

import time
from typing import Dict, Optional, Tuple
from .interfaces import BaseCache, CacheEntry, CacheStats


class ExactCache(BaseCache):
    """
    In-memory LRU + TTL Cache for LLM responses.
    Guarantees deterministic exact-match retrieval, expiration, eviction,
    and cache namespace isolation.
    """

    def __init__(
        self,
        max_entries: int = 1000,
        ttl_seconds: int = 3600,
        default_namespace: str = "default",
    ):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self.default_namespace = default_namespace
        # Keyed by (cache_namespace, fingerprint)
        self._entries: Dict[Tuple[str, str], CacheEntry] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._coalesced_requests: int = 0
        self._coalesced_successes: int = 0
        self._tokens_saved: int = 0
        self._latency_saved_ms: float = 0.0

    async def get(self, key: str, namespace: Optional[str] = None) -> Optional[CacheEntry]:
        """
        Retrieve an unexpired entry from cache and record hit metrics.
        Returns None if missing or expired.
        """
        ns = (namespace or self.default_namespace).strip()
        cache_key = (ns, key)
        entry = self._entries.get(cache_key)
        if entry is None:
            return None

        if entry.is_expired(self.ttl_seconds):
            del self._entries[cache_key]
            return None

        entry.hit_count += 1
        entry.last_accessed = time.time()
        self._hits += 1
        self._tokens_saved += getattr(entry.usage, "total_tokens", 0) or 0
        self._latency_saved_ms += entry.latency_ms
        return entry

    async def set(self, key: str, entry: CacheEntry, namespace: Optional[str] = None) -> None:
        """Add or update an entry with LRU eviction when capacity is reached."""
        ns = (namespace or self.default_namespace).strip()
        cache_key = (ns, key)

        # Evict expired entries first if nearing capacity
        if len(self._entries) >= self.max_entries and cache_key not in self._entries:
            expired_keys = [k for k, v in self._entries.items() if v.is_expired(self.ttl_seconds)]
            for k in expired_keys:
                del self._entries[k]

        # Evict oldest entry if still at or over capacity
        if len(self._entries) >= self.max_entries and cache_key not in self._entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k].last_accessed)
            del self._entries[oldest_key]

        self._entries[cache_key] = entry

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
        entry.last_accessed = time.time()
        self._tokens_saved += getattr(entry.usage, "total_tokens", 0) or 0
        self._latency_saved_ms += entry.latency_ms

    async def record_coalesced(self, entry: Optional[CacheEntry] = None) -> None:
        """Convenience method for tests and backwards compatibility."""
        await self.record_coalesced_join()
        if entry is not None:
            await self.record_coalesced_success(entry)

    async def clear(self, namespace: Optional[str] = None) -> None:
        """Clears cached entries and resets statistics."""
        if namespace is not None:
            self._entries = {k: v for k, v in self._entries.items() if k[0] != namespace}
        else:
            self._entries.clear()
            self._hits = 0
            self._misses = 0
            self._coalesced_requests = 0
            self._coalesced_successes = 0
            self._tokens_saved = 0
            self._latency_saved_ms = 0.0

    async def stats(self, enabled: bool = True, namespace: Optional[str] = None) -> CacheStats:
        """Returns statistical metrics on cache usage."""
        total_req = self._hits + self._misses + self._coalesced_requests
        reused_req = self._hits + self._coalesced_successes
        hit_rate = (reused_req / total_req) if total_req > 0 else 0.0

        if namespace is not None:
            entries_count = sum(1 for k in self._entries if k[0] == namespace)
        else:
            entries_count = len(self._entries)

        return CacheStats(
            total_entries=entries_count,
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
            backend="memory",
        )

    async def close(self) -> None:
        """In-memory cache requires no connection cleanup."""
        pass


# Backward compatibility alias
QueryCache = ExactCache
