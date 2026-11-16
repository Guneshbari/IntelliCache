"""
Semantic cache package for IntelliCache.
Provides policy evaluation, candidate verification, and semantic cache orchestration.
"""

from .compatibility import CompatibilityExtractor, compare_compatibility
from .contracts import (
    CompatibilityReasonCode,
    ConstraintItem,
    DimensionValue,
    EligibilityResult,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    REQUIRED_DIMENSIONS,
    SemanticCacheStats,
    SemanticCompatibilityMetadata,
    SemanticHit,
    TaskKind,
)
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from .service import SemanticCacheService

__all__ = [
    "BaseSemanticCache",
    "CompatibilityExtractor",
    "CompatibilityReasonCode",
    "ConstraintItem",
    "DimensionValue",
    "EligibilityResult",
    "EmbeddingContract",
    "GenerationContract",
    "MetadataStatus",
    "REQUIRED_DIMENSIONS",
    "SemanticCachePolicy",
    "SemanticCacheService",
    "SemanticCacheStats",
    "SemanticCompatibilityMetadata",
    "SemanticHit",
    "TaskKind",
    "compare_compatibility",
]
