"""
Semantic cache package for IntelliCache.
Provides policy evaluation, candidate verification, and semantic cache orchestration.
"""

from .compatibility import CompatibilityExtractor, compare_compatibility
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityReasonCode,
    ConstraintItem,
    DimensionValue,
    EligibilityResult,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    REQUIRED_DIMENSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
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
    "CURRENT_SCHEMA_VERSION",
    "CompatibilityExtractor",
    "CompatibilityReasonCode",
    "ConstraintItem",
    "DimensionValue",
    "EligibilityResult",
    "EmbeddingContract",
    "GenerationContract",
    "MetadataStatus",
    "REQUIRED_DIMENSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SemanticCachePolicy",
    "SemanticCacheService",
    "SemanticCacheStats",
    "SemanticCompatibilityMetadata",
    "SemanticHit",
    "TaskKind",
    "compare_compatibility",
]
