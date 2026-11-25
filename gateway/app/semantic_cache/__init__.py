"""
Semantic cache package for IntelliCache.
Provides policy evaluation, candidate verification, and semantic cache orchestration.
"""

from .compatibility import (
    CompatibilityExtractor,
    CompatibilityValidator,
    compare_compatibility,
    compare_compatibility_decision,
    extract_effective_user_query,
)
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityDecision,
    CompatibilityEvidence,
    CompatibilityReasonCode,
    CompatibilityStatus,
    ConstraintItem,
    DependencyItem,
    DimensionValue,
    EligibilityResult,
    EmbeddingContract,
    GenerationContract,
    LEGACY_SCHEMA_VERSIONS,
    MetadataStatus,
    REQUIRED_DIMENSIONS,
    SUPPORTED_SCHEMA_VERSIONS,
    SemanticCacheStats,
    SemanticCompatibilityMetadata,
    SemanticHit,
    TRUSTED_CURATED_EVIDENCE_ID,
    TRUSTED_REQUEST_EXTRACTOR_ID,
    TRUSTED_RESPONSE_VERIFIER_ID,
    TaskKind,
    V2_REQUIRED_DIMENSIONS,
)
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from .service import SemanticCacheService

__all__ = [
    "BaseSemanticCache",
    "CURRENT_SCHEMA_VERSION",
    "CompatibilityDecision",
    "CompatibilityEvidence",
    "CompatibilityExtractor",
    "CompatibilityReasonCode",
    "CompatibilityStatus",
    "CompatibilityValidator",
    "ConstraintItem",
    "DependencyItem",
    "DimensionValue",
    "EligibilityResult",
    "EmbeddingContract",
    "GenerationContract",
    "LEGACY_SCHEMA_VERSIONS",
    "MetadataStatus",
    "REQUIRED_DIMENSIONS",
    "SUPPORTED_SCHEMA_VERSIONS",
    "SemanticCachePolicy",
    "SemanticCacheService",
    "SemanticCacheStats",
    "SemanticCompatibilityMetadata",
    "SemanticHit",
    "TRUSTED_CURATED_EVIDENCE_ID",
    "TRUSTED_REQUEST_EXTRACTOR_ID",
    "TRUSTED_RESPONSE_VERIFIER_ID",
    "TaskKind",
    "V2_REQUIRED_DIMENSIONS",
    "compare_compatibility",
    "compare_compatibility_decision",
    "extract_effective_user_query",
]
