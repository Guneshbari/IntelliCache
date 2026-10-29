"""
Semantic cache package for IntelliCache.
Provides policy evaluation, candidate verification, and semantic cache orchestration.
"""

from .contracts import EligibilityResult, SemanticCacheStats, SemanticHit
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from .service import SemanticCacheService

__all__ = [
    "BaseSemanticCache",
    "EligibilityResult",
    "SemanticCachePolicy",
    "SemanticCacheService",
    "SemanticCacheStats",
    "SemanticHit",
]
