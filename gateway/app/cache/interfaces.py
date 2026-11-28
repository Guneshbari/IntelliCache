"""
Cache interfaces, entry models, and statistics contracts for IntelliCache.
Provides an asynchronous storage contract suitable for in-memory and PostgreSQL backends.
"""

from abc import ABC, abstractmethod
import time
from typing import Any, Dict, Optional
from pydantic import BaseModel

from ..privacy import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    CACHE_VISIBILITY_UNKNOWN,
    matches_cache_scope,
    normalize_cache_visibility,
    normalize_principal_id,
)


class CacheEntry:
    """Internal cache entry representation storing response and execution metadata."""
    __slots__ = (
        "response",
        "model",
        "provider",
        "usage",
        "created_at",
        "last_accessed",
        "hit_count",
        "latency_ms",
        "metadata",
        "visibility",
        "owner_principal_id",
    )

    def __init__(
        self,
        response: str,
        model: str,
        usage: Any,
        latency_ms: float,
        metadata: Optional[Dict[str, Any]] = None,
        provider: Optional[str] = None,
        visibility: str = CACHE_VISIBILITY_UNKNOWN,
        owner_principal_id: Optional[str] = None,
    ):
        self.response = response
        self.model = model
        self.provider = provider
        self.usage = usage
        self.created_at = time.time()
        self.last_accessed = self.created_at
        self.hit_count = 0
        self.latency_ms = latency_ms
        self.metadata = metadata or {}
        norm_vis = normalize_cache_visibility(visibility)
        norm_owner = normalize_principal_id(owner_principal_id)
        if norm_vis == CACHE_VISIBILITY_PRIVATE and norm_owner is None:
            raise ValueError("Private CacheEntry requires a non-empty owner_principal_id.")
        if norm_vis == CACHE_VISIBILITY_TENANT_SHARED and norm_owner is not None:
            raise ValueError("tenant_shared CacheEntry cannot define an owner_principal_id.")
        self.visibility = norm_vis
        self.owner_principal_id = norm_owner if norm_vis == CACHE_VISIBILITY_PRIVATE else None

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
    backend: str = "memory"
    semantic: Optional[Dict[str, int]] = None
    distributed_singleflight: Optional[Dict[str, Any]] = None


class BaseCache(ABC):
    """Abstract asynchronous interface for IntelliCache response caching implementations."""

    @abstractmethod
    async def get(
        self,
        key: str,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
        allow_shared_reads: bool = False,
    ) -> Optional[CacheEntry]:
        """Retrieve an unexpired entry from cache."""
        pass

    @abstractmethod
    async def peek(
        self,
        key: str,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
        allow_shared_reads: bool = False,
    ) -> Optional[CacheEntry]:
        """
        Inspect an unexpired entry from cache without mutating hit counters,
        access timestamps, tokens saved, or LRU eviction recency.
        """
        pass

    @abstractmethod
    async def set(self, key: str, entry: CacheEntry, namespace: Optional[str] = None) -> None:
        """Add or update an entry in cache with eviction if necessary."""
        pass

    async def delete(
        self,
        key: str,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> bool:
        """Optionally delete a single scoped cache entry."""
        return False

    @abstractmethod
    async def record_miss(self) -> None:
        """Record an independent request that required upstream generation."""
        pass

    @abstractmethod
    async def record_coalesced_join(self) -> None:
        """Record that an incoming request joined an in-flight generation as a follower."""
        pass

    @abstractmethod
    async def record_coalesced_success(self, entry: CacheEntry) -> None:
        """Record that a follower successfully received and reused the leader's generation."""
        pass

    @abstractmethod
    async def record_coalesced(self, entry: Optional[CacheEntry] = None) -> None:
        """Record a coalesced request (both join and, if entry provided, success)."""
        pass

    @abstractmethod
    async def record_semantic_hit(self, tokens_saved: int, latency_saved_ms: float) -> None:
        """Record tokens and latency saved by a semantic cache hit."""
        pass

    @abstractmethod
    async def clear(
        self,
        namespace: Optional[str] = None,
        visibility: Optional[str] = None,
        principal_id: Optional[str] = None,
    ) -> None:
        """Clear entries (optionally scoped) and reset metric counters."""
        pass

    @abstractmethod
    async def stats(self, enabled: bool = True, namespace: Optional[str] = None) -> CacheStats:
        """Return statistical metrics on cache usage."""
        pass

    async def close(self) -> None:
        """Clean up underlying connections or background resources."""
        pass
