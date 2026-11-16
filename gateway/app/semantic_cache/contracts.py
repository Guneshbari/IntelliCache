"""
Contracts and data models for semantic caching.
Isolated from FastAPI, Groq SDK, Ollama SDK, and database implementations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
import math
from typing import Any, Dict, List, Optional


class MetadataStatus:
    """Explicit metadata dimension lifecycle states."""
    KNOWN = "KNOWN"
    UNKNOWN = "UNKNOWN"
    NOT_APPLICABLE = "NOT_APPLICABLE"


class TaskKind:
    """Supported deterministic task categories for technical queries."""
    CODE_GENERATION = "code_generation"
    CONCEPTUAL = "conceptual"
    DEBUGGING_EXPLANATION = "debugging_explanation"
    REFACTORING = "refactoring"
    CONFIGURATION = "configuration"

    ALL_VALUES = frozenset({
        CODE_GENERATION,
        CONCEPTUAL,
        DEBUGGING_EXPLANATION,
        REFACTORING,
        CONFIGURATION,
    })


class CompatibilityReasonCode:
    """Stable diagnostic reason codes for candidate compatibility verification."""
    COMPATIBLE = "COMPATIBLE"
    NO_ASSOCIATED_RESPONSE = "NO_ASSOCIATED_RESPONSE"
    NAMESPACE_MISMATCH = "NAMESPACE_MISMATCH"
    EMBEDDING_MODEL_MISMATCH = "EMBEDDING_MODEL_MISMATCH"
    LEGACY_METADATA_MISSING = "LEGACY_METADATA_MISSING"
    UNSUPPORTED_SCHEMA_VERSION = "UNSUPPORTED_SCHEMA_VERSION"
    INVALID_METADATA_FORMAT = "INVALID_METADATA_FORMAT"
    GENERATION_PROVIDER_MISMATCH = "GENERATION_PROVIDER_MISMATCH"
    GENERATION_MODEL_MISMATCH = "GENERATION_MODEL_MISMATCH"
    GENERATION_TEMPERATURE_MISMATCH = "GENERATION_TEMPERATURE_MISMATCH"
    GENERATION_MAX_TOKENS_MISMATCH = "GENERATION_MAX_TOKENS_MISMATCH"
    INCOMPATIBLE_TASK_KIND = "INCOMPATIBLE_TASK_KIND"
    LANGUAGE_MISMATCH = "LANGUAGE_MISMATCH"
    LANGUAGE_VERSION_MISMATCH = "LANGUAGE_VERSION_MISMATCH"
    FRAMEWORK_MISMATCH = "FRAMEWORK_MISMATCH"
    FRAMEWORK_VERSION_MISMATCH = "FRAMEWORK_VERSION_MISMATCH"
    RUNTIME_MISMATCH = "RUNTIME_MISMATCH"
    RUNTIME_VERSION_MISMATCH = "RUNTIME_VERSION_MISMATCH"
    EXECUTION_MODEL_MISMATCH = "EXECUTION_MODEL_MISMATCH"
    OUTPUT_FORMAT_MISMATCH = "OUTPUT_FORMAT_MISMATCH"
    CONSTRAINT_MISMATCH = "CONSTRAINT_MISMATCH"
    UNKNOWN_MATERIAL_DIMENSION = "UNKNOWN_MATERIAL_DIMENSION"
    VALIDATOR_EXCEPTION = "VALIDATOR_EXCEPTION"


CURRENT_SCHEMA_VERSION = 2
SUPPORTED_SCHEMA_VERSIONS = frozenset({2})

REQUIRED_DIMENSIONS = frozenset({
    "language",
    "language_version",
    "framework",
    "framework_version",
    "runtime",
    "runtime_version",
    "execution_model",
    "output_format",
    "constraints",
})


@dataclass(frozen=True)
class DimensionValue:
    """
    Represents an individual domain dimension with explicit lifecycle state.
    Field state rules:
      - KNOWN: requires normalized non-empty value.
      - UNKNOWN: requires value=None (dimension could be material but cannot be established).
      - NOT_APPLICABLE: requires value=None (dimension is genuinely irrelevant to task).
    """
    status: str
    value: Any = None

    def __post_init__(self) -> None:
        if self.status not in (MetadataStatus.KNOWN, MetadataStatus.UNKNOWN, MetadataStatus.NOT_APPLICABLE):
            raise ValueError(f"Invalid dimension status: '{self.status}'. Must be KNOWN, UNKNOWN, or NOT_APPLICABLE.")
        if self.status == MetadataStatus.KNOWN:
            if self.value is None or (isinstance(self.value, (str, list, dict)) and len(self.value) == 0):
                raise ValueError("KNOWN status requires a normalized non-empty value.")
        else:
            if self.value is not None:
                raise ValueError(f"{self.status} status requires value=None, got {self.value!r}")

    def to_dict(self) -> Dict[str, Any]:
        val = self.value
        if isinstance(val, list):
            val = [x.to_dict() if hasattr(x, "to_dict") else x for x in val]
        return {"status": self.status, "value": val}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DimensionValue":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for DimensionValue, got {type(data).__name__}")
        raw_status = data.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            raise ValueError(f"DimensionValue 'status' must be a non-empty string, got {raw_status} ({type(raw_status).__name__})")
        raw_val = data.get("value")
        # Handle list of constraint items if present
        if isinstance(raw_val, list):
            parsed_list = []
            for item in raw_val:
                if isinstance(item, dict):
                    parsed_list.append(ConstraintItem.from_dict(item))
                elif isinstance(item, ConstraintItem):
                    parsed_list.append(item)
                else:
                    raise ValueError(f"Expected dict or ConstraintItem in constraints list, got {type(item).__name__}")
            raw_val = parsed_list
        return cls(status=raw_status.strip(), value=raw_val)


@dataclass(frozen=True)
class ConstraintItem:
    """Normalized technical constraint specification (e.g. case_sensitivity=sensitive)."""
    SUPPORTED_OPERATORS = frozenset({"equals", "not_equals", "in", "not_in"})

    key: str
    operator: str = "equals"
    value: str = ""

    def __post_init__(self) -> None:
        if not self.key or not str(self.key).strip():
            raise ValueError("Constraint key cannot be empty.")
        if self.operator not in self.SUPPORTED_OPERATORS:
            raise ValueError(f"Unsupported constraint operator: '{self.operator}'. Must be one of {sorted(self.SUPPORTED_OPERATORS)}")
        if not self.value or not str(self.value).strip():
            raise ValueError("Constraint value cannot be empty.")

    def to_dict(self) -> Dict[str, Any]:
        return {"key": self.key, "operator": self.operator, "value": self.value}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ConstraintItem":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ConstraintItem, got {type(data).__name__}")
        raw_key = data.get("key")
        if not isinstance(raw_key, str) or not raw_key.strip():
            raise ValueError(f"ConstraintItem 'key' must be a non-empty string, got {raw_key} ({type(raw_key).__name__})")

        raw_op = data.get("operator", "equals")
        if not isinstance(raw_op, str) or not raw_op.strip():
            raise ValueError(f"ConstraintItem 'operator' must be a non-empty string, got {raw_op} ({type(raw_op).__name__})")

        raw_val = data.get("value")
        if not isinstance(raw_val, str) or not raw_val.strip():
            raise ValueError(f"ConstraintItem 'value' must be a non-empty string, got {raw_val} ({type(raw_val).__name__})")

        return cls(
            key=raw_key.strip(),
            operator=raw_op.strip(),
            value=raw_val.strip(),
        )


@dataclass(frozen=True)
class GenerationContract:
    """Immutable provider and generation parameters."""
    provider: str
    model: str
    temperature: float
    max_tokens: Optional[int] = None

    def __post_init__(self) -> None:
        if not self.provider or not str(self.provider).strip():
            raise ValueError("Generation provider cannot be empty.")
        if not self.model or not str(self.model).strip():
            raise ValueError("Generation model cannot be empty.")
        if not isinstance(self.temperature, (int, float)) or isinstance(self.temperature, bool):
            raise ValueError(f"Generation temperature must be a float, got {type(self.temperature).__name__}")
        if not math.isfinite(self.temperature) or math.isnan(self.temperature):
            raise ValueError(f"Generation temperature must be finite, got {self.temperature}")
        if self.temperature < 0.0:
            raise ValueError(f"Generation temperature cannot be negative, got {self.temperature}")
        if self.max_tokens is not None:
            if not isinstance(self.max_tokens, int) or isinstance(self.max_tokens, bool) or self.max_tokens <= 0:
                raise ValueError(f"Generation max_tokens must be a positive integer, got {self.max_tokens}")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "GenerationContract":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for GenerationContract, got {type(data).__name__}")

        raw_provider = data.get("provider")
        if not isinstance(raw_provider, str) or not raw_provider.strip():
            raise ValueError(f"GenerationContract 'provider' must be a non-empty string, got {raw_provider} ({type(raw_provider).__name__})")

        raw_model = data.get("model")
        if not isinstance(raw_model, str) or not raw_model.strip():
            raise ValueError(f"GenerationContract 'model' must be a non-empty string, got {raw_model} ({type(raw_model).__name__})")

        raw_temp = data.get("temperature")
        if raw_temp is None or not isinstance(raw_temp, (int, float)) or isinstance(raw_temp, bool):
            raise ValueError(f"GenerationContract 'temperature' must be a float, got {raw_temp} ({type(raw_temp).__name__})")

        raw_max = data.get("max_tokens")
        if raw_max is not None:
            if not isinstance(raw_max, int) or isinstance(raw_max, bool) or raw_max <= 0:
                raise ValueError(f"GenerationContract 'max_tokens' must be a positive integer, got {raw_max} ({type(raw_max).__name__})")
            max_tok = raw_max
        else:
            max_tok = None

        return cls(
            provider=raw_provider.strip(),
            model=raw_model.strip(),
            temperature=float(raw_temp),
            max_tokens=max_tok,
        )


@dataclass(frozen=True)
class EmbeddingContract:
    """Immutable embedding model identity."""
    model: str

    def __post_init__(self) -> None:
        if not self.model or not str(self.model).strip():
            raise ValueError("Embedding model identity cannot be empty.")

    def to_dict(self) -> Dict[str, Any]:
        return {"model": self.model}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "EmbeddingContract":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for EmbeddingContract, got {type(data).__name__}")
        raw_model = data.get("model")
        if not isinstance(raw_model, str) or not raw_model.strip():
            raise ValueError(f"EmbeddingContract 'model' must be a non-empty string, got {raw_model} ({type(raw_model).__name__})")
        return cls(model=raw_model.strip())


@dataclass(frozen=True)
class SemanticCompatibilityMetadata:
    """
    Validated, versioned semantic compatibility metadata contract (Stage 3.1.1).
    Ensures that domain compatibility (language, framework, runtime, version,
    constraints, output format) is verified before semantic response reuse.
    """
    schema_version: int
    extractor_version: str
    task_kind: DimensionValue
    dimensions: Dict[str, DimensionValue]
    generation_contract: GenerationContract
    embedding_contract: EmbeddingContract
    indexed_at: Optional[str] = None

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        """Enforces all schema integrity and field-state rules."""
        if self.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"Unsupported schema_version: {self.schema_version}. Expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}.")
        if not self.extractor_version or not str(self.extractor_version).strip():
            raise ValueError("extractor_version cannot be empty.")

        if not isinstance(self.task_kind, DimensionValue):
            raise ValueError(f"task_kind must be a DimensionValue, got {type(self.task_kind).__name__}")
        if self.task_kind.status == MetadataStatus.NOT_APPLICABLE:
            raise ValueError("task_kind cannot be NOT_APPLICABLE.")
        if self.task_kind.status == MetadataStatus.KNOWN:
            if self.task_kind.value not in TaskKind.ALL_VALUES:
                raise ValueError(f"Unsupported task_kind value: '{self.task_kind.value}'. Must be one of {sorted(TaskKind.ALL_VALUES)}")

        if not isinstance(self.dimensions, dict):
            raise ValueError(f"dimensions must be a dictionary, got {type(self.dimensions).__name__}")

        missing_dims = REQUIRED_DIMENSIONS - set(self.dimensions.keys())
        if missing_dims:
            raise ValueError(f"Missing required dimensions: {sorted(list(missing_dims))}")

        for dim_name, dim_val in self.dimensions.items():
            if not isinstance(dim_val, DimensionValue):
                raise ValueError(f"Dimension '{dim_name}' must be DimensionValue, got {type(dim_val).__name__}")
            if dim_val.status == MetadataStatus.KNOWN:
                if dim_name == "constraints":
                    if not isinstance(dim_val.value, list):
                        raise ValueError(f"Dimension '{dim_name}' value must be a list of ConstraintItem, got {type(dim_val.value).__name__}")
                    for c_item in dim_val.value:
                        if not isinstance(c_item, ConstraintItem):
                            raise ValueError(f"Constraints list item must be ConstraintItem, got {type(c_item).__name__}")
                else:
                    if not isinstance(dim_val.value, str):
                        raise ValueError(f"Dimension '{dim_name}' value must be a string, got {type(dim_val.value).__name__}")

        if not isinstance(self.generation_contract, GenerationContract):
            raise ValueError(f"generation_contract must be GenerationContract, got {type(self.generation_contract).__name__}")
        if not isinstance(self.embedding_contract, EmbeddingContract):
            raise ValueError(f"embedding_contract must be EmbeddingContract, got {type(self.embedding_contract).__name__}")

    def to_dict(self) -> Dict[str, Any]:
        """Serializes metadata to standard JSON-compatible dictionary."""
        return {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "task_kind": self.task_kind.to_dict(),
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "generation_contract": self.generation_contract.to_dict(),
            "embedding_contract": self.embedding_contract.to_dict(),
            "indexed_at": self.indexed_at or datetime.now(timezone.utc).isoformat(),
            # Supplementary keys for backwards-compatible diagnostics
            "llm_provider": self.generation_contract.provider,
            "llm_model": self.generation_contract.model,
            "temperature": self.generation_contract.temperature,
            "max_tokens": self.generation_contract.max_tokens,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SemanticCompatibilityMetadata":
        """Deserializes and validates metadata dictionary."""
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for SemanticCompatibilityMetadata, got {type(data).__name__}")

        raw_ver = data.get("schema_version")
        if raw_ver is None:
            raise ValueError("Missing 'schema_version' in metadata.")
        if not isinstance(raw_ver, int) or isinstance(raw_ver, bool):
            raise ValueError(f"Invalid 'schema_version': {raw_ver}. Must be an integer.")
        ver = raw_ver

        raw_ext = data.get("extractor_version")
        if not isinstance(raw_ext, str) or not raw_ext.strip():
            raise ValueError(f"extractor_version must be a non-empty string, got {raw_ext} ({type(raw_ext).__name__})")
        ext_ver = raw_ext.strip()

        raw_task = data.get("task_kind")
        if not isinstance(raw_task, dict):
            raise ValueError("Missing or invalid 'task_kind' in metadata.")
        task_kind = DimensionValue.from_dict(raw_task)

        raw_dims = data.get("dimensions")
        if not isinstance(raw_dims, dict):
            raise ValueError("Missing or invalid 'dimensions' in metadata.")

        dimensions: Dict[str, DimensionValue] = {}
        for dim_name in REQUIRED_DIMENSIONS:
            if dim_name not in raw_dims:
                raise ValueError(f"Missing required dimension '{dim_name}' in metadata.")
            dim_dict = raw_dims[dim_name]
            if not isinstance(dim_dict, dict):
                raise ValueError(f"Invalid dimension dict for '{dim_name}'")
            dimensions[dim_name] = DimensionValue.from_dict(dim_dict)

        raw_gen = data.get("generation_contract")
        if not isinstance(raw_gen, dict):
            # Fall back to top-level legacy keys if present
            raw_gen = {
                "provider": data.get("llm_provider", ""),
                "model": data.get("llm_model", ""),
                "temperature": data.get("temperature", 0.0),
                "max_tokens": data.get("max_tokens"),
            }
        generation_contract = GenerationContract.from_dict(raw_gen)

        raw_emb = data.get("embedding_contract")
        if not isinstance(raw_emb, dict):
            raw_emb = {"model": data.get("embedding_model", data.get("model", ""))}
        embedding_contract = EmbeddingContract.from_dict(raw_emb)

        return cls(
            schema_version=ver,
            extractor_version=ext_ver,
            task_kind=task_kind,
            dimensions=dimensions,
            generation_contract=generation_contract,
            embedding_contract=embedding_contract,
            indexed_at=data.get("indexed_at"),
        )


@dataclass(frozen=True)
class EligibilityResult:
    """Result of checking a request's eligibility for semantic caching."""
    eligible: bool
    reason: str
    metadata: Optional["SemanticCompatibilityMetadata"] = None


@dataclass(frozen=True)
class SemanticHit:
    """
    Represents a validated, un-ambiguous semantic cache hit.
    Includes original cached entry token counts and latency for savings accounting.
    """
    cached_response: str
    fingerprint: str
    distance: float
    similarity: float
    model: str
    query_text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    original_latency_ms: float = 0.0
    metadata: Dict[str, Any] = field(default_factory=dict)
    matched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SemanticCacheStats:
    """Diagnostic observability counters for semantic cache operations."""
    lookups: int = 0
    hits: int = 0
    misses: int = 0
    rejected_candidates: int = 0
    ambiguous_candidates: int = 0
    eligibility_rejections: int = 0
    embedding_failures: int = 0
    vector_store_failures: int = 0
    indexing_successes: int = 0
    indexing_failures: int = 0
    validator_failures: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "misses": self.misses,
            "rejected_candidates": self.rejected_candidates,
            "ambiguous_candidates": self.ambiguous_candidates,
            "eligibility_rejections": self.eligibility_rejections,
            "embedding_failures": self.embedding_failures,
            "vector_store_failures": self.vector_store_failures,
            "indexing_successes": self.indexing_successes,
            "indexing_failures": self.indexing_failures,
            "validator_failures": self.validator_failures,
        }
