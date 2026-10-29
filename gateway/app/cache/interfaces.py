"""
Cache interfaces, entry models, and statistics contracts for IntelliCache.
"""

from abc import ABC, abstractmethod
import time
from typing import Any, Dict, Optional
from pydantic import BaseModel


class CacheEntry:
    """Internal cache entry representation storing response and execution metadata."""
    __slots__ = (
        "response",
        "model",
        "usage",
        "created_at",
        "last_accessed",
        "hit_count",
        "latency_ms",
        "metadata",
    )

    def __init__(
        self,
        response: str,
        model: str,
        usage: Any,
        latency_ms: float,
        metadata: Optional[Dict[str, Any]] = None,
    ):
        self.response = response
        self.model = model
        self.usage = usage
        self.created_at = time.time()
        self.last_accessed = self.created_at
        self.hit_count = 0
        self.latency_ms = latency_ms
        self.metadata = metadata or {}

    def is_expired(self, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            return False
        return (time.time() - self.created_at) > ttl_seconds


class CacheStats(BaseModel):
    """Statistical summary of cache capacity, throughput, and savings."""
    total_entries: int = 0
    max_entries: int = 1000
    hits: int = 0
    misses: int = 0
    coalesced_requests: int = 0
    total_requests: int = 0
    hit_rate: float = 0.0
    total_tokens_saved: int = 0
    total_latency_saved_ms: float = 0.0
    ttl_seconds: int = 3600
    enabled: bool = True


class BaseCache(ABC):
    """Abstract interface for IntelliCache response caching implementations."""

    @abstractmethod
    def get(self, key: str) -> Optional[CacheEntry]:
        """Retrieve an unexpired entry from cache."""
        pass

    @abstractmethod
    def set(self, key: str, entry: CacheEntry) -> None:
        """Add or update an entry in cache with eviction if necessary."""
        pass

    @abstractmethod
    def record_miss(self) -> None:
        """Record an independent request that required upstream generation."""
        pass

    @abstractmethod
    def record_coalesced_join(self) -> None:
        """Record that an incoming request joined an in-flight generation as a follower."""
        pass

    @abstractmethod
    def record_coalesced_success(self, entry: CacheEntry) -> None:
        """Record that a follower successfully received and reused the leader's generation."""
        pass

    @abstractmethod
    def record_coalesced(self, entry: Optional[CacheEntry] = None) -> None:
        """Record a coalesced request (both join and, if entry provided, success)."""
        pass

    @abstractmethod
    def clear(self) -> None:
        """Clear all entries and reset metric counters."""
        pass

    @abstractmethod
    def stats(self, enabled: bool = True) -> CacheStats:
        """Return statistical metrics on cache usage."""
        pass
