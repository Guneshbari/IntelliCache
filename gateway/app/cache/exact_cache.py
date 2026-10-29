"""
In-memory LRU + TTL Exact Match Cache implementation for IntelliCache.
"""

import time
from typing import Dict, Optional
from .interfaces import BaseCache, CacheEntry, CacheStats


class ExactCache(BaseCache):
    """
    In-memory LRU + TTL Cache for LLM responses.
    Guarantees deterministic exact-match retrieval, expiration, and eviction.
    """

    def __init__(self, max_entries: int = 1000, ttl_seconds: int = 3600):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: Dict[str, CacheEntry] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._coalesced_requests: int = 0
        self._coalesced_successes: int = 0
        self._tokens_saved: int = 0
        self._latency_saved_ms: float = 0.0

    def get(self, key: str) -> Optional[CacheEntry]:
        """
        Retrieve an unexpired entry from cache and record hit metrics.
        Returns None if missing or expired.
        """
        entry = self._entries.get(key)
        if entry is None:
            return None

        if entry.is_expired(self.ttl_seconds):
            del self._entries[key]
            return None

        entry.hit_count += 1
        entry.last_accessed = time.time()
        self._hits += 1
        self._tokens_saved += entry.usage.total_tokens
        self._latency_saved_ms += entry.latency_ms
        return entry

    def set(self, key: str, entry: CacheEntry) -> None:
        """Add or update an entry with LRU eviction when capacity is reached."""
        if len(self._entries) >= self.max_entries and key not in self._entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k].last_accessed)
            del self._entries[oldest_key]

        self._entries[key] = entry

    def record_miss(self) -> None:
        """Record an independent request that required upstream generation."""
        self._misses += 1

    def record_coalesced_join(self) -> None:
        """Record that an incoming request joined an in-flight single-flight group."""
        self._coalesced_requests += 1

    def record_coalesced_success(self, entry: CacheEntry) -> None:
        """Record that a joined follower successfully received the leader's result."""
        self._coalesced_successes += 1
        entry.hit_count += 1
        entry.last_accessed = time.time()
        self._tokens_saved += entry.usage.total_tokens
        self._latency_saved_ms += entry.latency_ms

    def record_coalesced(self, entry: Optional[CacheEntry] = None) -> None:
        """Convenience method for tests and backwards compatibility."""
        self.record_coalesced_join()
        if entry is not None:
            self.record_coalesced_success(entry)

    def clear(self) -> None:
        """Clears all cached entries and resets statistics."""
        self._entries.clear()
        self._hits = 0
        self._misses = 0
        self._coalesced_requests = 0
        self._coalesced_successes = 0
        self._tokens_saved = 0
        self._latency_saved_ms = 0.0

    def stats(self, enabled: bool = True) -> CacheStats:
        """Returns statistical metrics on cache usage."""
        total_req = self._hits + self._misses + self._coalesced_requests
        reused_req = self._hits + self._coalesced_successes
        hit_rate = (reused_req / total_req) if total_req > 0 else 0.0
        return CacheStats(
            total_entries=len(self._entries),
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
        )


# Backward compatibility alias
QueryCache = ExactCache
