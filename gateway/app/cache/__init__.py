"""
IntelliCache Cache Module exports.
"""

from .interfaces import BaseCache, CacheEntry, CacheStats
from .exact_cache import ExactCache, QueryCache

__all__ = ["BaseCache", "CacheEntry", "CacheStats", "ExactCache", "QueryCache"]
