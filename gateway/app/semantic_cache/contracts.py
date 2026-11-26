"""
Contracts and data models for semantic caching.
Isolated from FastAPI, Groq SDK, Ollama SDK, and database implementations.
"""

from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import hashlib
import json
import math
from typing import Any, Dict, List, Optional, Tuple


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


class CompatibilityStatus(str, Enum):
    """Explicit tri-state outcome for semantic compatibility evaluation."""
    COMPATIBLE = "COMPATIBLE"
    INCOMPATIBLE = "INCOMPATIBLE"
    UNCERTAIN = "UNCERTAIN"

    def __str__(self) -> str:
        return self.value


class CompatibilityReasonCode(str, Enum):
    """Stable machine-readable diagnostic reason codes for compatibility verification."""
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
    DEPENDENCY_MISMATCH = "DEPENDENCY_MISMATCH"
    DEPENDENCY_VERSION_MISMATCH = "DEPENDENCY_VERSION_MISMATCH"
    EXECUTION_MODEL_MISMATCH = "EXECUTION_MODEL_MISMATCH"
    OUTPUT_FORMAT_MISMATCH = "OUTPUT_FORMAT_MISMATCH"
    OUTPUT_CONSTRAINT_MISMATCH = "OUTPUT_CONSTRAINT_MISMATCH"
    CONSTRAINT_MISMATCH = "CONSTRAINT_MISMATCH"
    UNKNOWN_MATERIAL_DIMENSION = "UNKNOWN_MATERIAL_DIMENSION"
    UNKNOWN_LANGUAGE = "UNKNOWN_LANGUAGE"
    UNKNOWN_FRAMEWORK = "UNKNOWN_FRAMEWORK"
    UNKNOWN_DEPENDENCY = "UNKNOWN_DEPENDENCY"
    UNKNOWN_VERSION = "UNKNOWN_VERSION"
    UNKNOWN_RUNTIME = "UNKNOWN_RUNTIME"
    UNKNOWN_EXECUTION_MODEL = "UNKNOWN_EXECUTION_MODEL"
    UNKNOWN_OUTPUT_FORMAT = "UNKNOWN_OUTPUT_FORMAT"
    MISSING_REQUIRED_METADATA = "MISSING_REQUIRED_METADATA"
    UNVERIFIED_COMPATIBILITY_EVIDENCE = "UNVERIFIED_COMPATIBILITY_EVIDENCE"
    UNVERIFIED_OUTPUT_ARTIFACT = "UNVERIFIED_OUTPUT_ARTIFACT"
    FRESHNESS_NOT_ESTABLISHED = "FRESHNESS_NOT_ESTABLISHED"
    CONTEXT_DEPENDENCY_UNRESOLVED = "CONTEXT_DEPENDENCY_UNRESOLVED"
    AMBIGUOUS_COMPATIBILITY_EVIDENCE = "AMBIGUOUS_COMPATIBILITY_EVIDENCE"
    AMBIGUOUS_CANDIDATES = "AMBIGUOUS_CANDIDATES"
    ALL_CANDIDATES_REJECTED = "ALL_CANDIDATES_REJECTED"
    VALIDATOR_EXCEPTION = "VALIDATOR_EXCEPTION"
    # Stage 3.5.3: Response verification, evidence binding, and admission codes
    VERIFICATION_PASSED = "VERIFICATION_PASSED"
    VERIFICATION_FAILED_EMPTY = "VERIFICATION_FAILED_EMPTY"
    VERIFICATION_FAILED_FORMAT = "VERIFICATION_FAILED_FORMAT"
    VERIFICATION_FAILED_JSON = "VERIFICATION_FAILED_JSON"
    VERIFICATION_FAILED_SCHEMA = "VERIFICATION_FAILED_SCHEMA"
    VERIFICATION_FAILED_CONSTRAINT = "VERIFICATION_FAILED_CONSTRAINT"
    VERIFICATION_FAILED_TRUNCATED = "VERIFICATION_FAILED_TRUNCATED"
    VERIFICATION_UNSUPPORTED_CONSTRAINT = "VERIFICATION_UNSUPPORTED_CONSTRAINT"
    VERIFICATION_UNCERTAIN_TRUNCATION = "VERIFICATION_UNCERTAIN_TRUNCATION"
    ADMISSION_ADMITTED = "ADMISSION_ADMITTED"
    ADMISSION_REJECTED_INELIGIBLE = "ADMISSION_REJECTED_INELIGIBLE"
    ADMISSION_REJECTED_VERIFICATION = "ADMISSION_REJECTED_VERIFICATION"
    ADMISSION_REJECTED_VARIANT_COLLISION = "ADMISSION_REJECTED_VARIANT_COLLISION"
    ADMISSION_UNCERTAIN_VERIFICATION = "ADMISSION_UNCERTAIN_VERIFICATION"
    EVIDENCE_BINDING_MISMATCH = "EVIDENCE_BINDING_MISMATCH"
    EVIDENCE_SIGNATURE_INVALID = "EVIDENCE_SIGNATURE_INVALID"
    # Stage 3.5.4: Runtime serving control & privacy isolation codes
    RUNTIME_SERVING_DISABLED = "RUNTIME_SERVING_DISABLED"
    VISIBILITY_MISSING_OR_UNKNOWN = "VISIBILITY_MISSING_OR_UNKNOWN"
    VISIBILITY_MISMATCH = "VISIBILITY_MISMATCH"
    PRINCIPAL_IDENTITY_MISSING = "PRINCIPAL_IDENTITY_MISSING"
    PRINCIPAL_ISOLATION_MISMATCH = "PRINCIPAL_ISOLATION_MISMATCH"
    PRIVACY_POLICY_VIOLATION = "PRIVACY_POLICY_VIOLATION"
    EVIDENCE_SCOPE_MISMATCH = "EVIDENCE_SCOPE_MISMATCH"
    # Stage 3.6: Context-aware semantic caching reason codes
    CONTEXT_CLASS_MISMATCH = "CONTEXT_CLASS_MISMATCH"
    CONTEXT_FINGERPRINT_MISMATCH = "CONTEXT_FINGERPRINT_MISMATCH"
    CONTEXT_METADATA_MISSING = "CONTEXT_METADATA_MISSING"
    CONTEXT_METADATA_INVALID = "CONTEXT_METADATA_INVALID"
    ADMISSION_REJECTED_CONTEXT_UNCERTAIN = "ADMISSION_REJECTED_CONTEXT_UNCERTAIN"
    ADMISSION_REJECTED_CONTEXT_PRIVACY = "ADMISSION_REJECTED_CONTEXT_PRIVACY"
    TENANT_SHARED_AUTH_REJECTED = "TENANT_SHARED_AUTH_REJECTED"

    def __str__(self) -> str:
        return self.value


CONTEXT_CONTRACT_SCHEMA_VERSION = 1
SUPPORTED_CONTEXT_CONTRACT_VERSIONS = frozenset({CONTEXT_CONTRACT_SCHEMA_VERSION})
CONTEXT_INDEPENDENT_SENTINEL = "ctx-v1:independent"
ALLOWED_CONTEXT_DOMAINS = frozenset({
    "system",
    "schema",
    "code",
    "config",
    "framework",
    "constraints",
    "decisions",
    "assistant_artifact",
})


class ContextClass(str, Enum):
    """Explicit classification of a request's dependency on conversation context."""
    CONTEXT_INDEPENDENT = "CONTEXT_INDEPENDENT"
    CONTEXT_DEPENDENT = "CONTEXT_DEPENDENT"
    CONTEXT_UNCERTAIN = "CONTEXT_UNCERTAIN"

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True)
class ContextCompatibilityContract:
    """
    Versioned structural representation of conversation context dependencies without
    persisting raw private conversation transcripts inside shared vector records.
    """
    context_version: int = CONTEXT_CONTRACT_SCHEMA_VERSION
    context_class: ContextClass = ContextClass.CONTEXT_INDEPENDENT
    context_fingerprint: Optional[str] = CONTEXT_INDEPENDENT_SENTINEL
    material_domains: Tuple[str, ...] = ()
    has_system_instructions: bool = False
    history_turn_count: int = 0
    source_conversation_present: bool = False
    eligible_for_shared_reuse: bool = False
    classification_reason: str = "STANDALONE_TECHNICAL"

    def __post_init__(self) -> None:
        self.validate()

    def validate(self) -> None:
        if (
            not isinstance(self.context_version, int)
            or isinstance(self.context_version, bool)
            or self.context_version not in SUPPORTED_CONTEXT_CONTRACT_VERSIONS
        ):
            raise ValueError(
                f"Unsupported context_version: {self.context_version}. Expected {sorted(SUPPORTED_CONTEXT_CONTRACT_VERSIONS)}."
            )
        if not isinstance(self.context_class, ContextClass):
            object.__setattr__(self, "context_class", ContextClass(self.context_class))

        if not isinstance(self.has_system_instructions, bool):
            raise ValueError("has_system_instructions must be bool.")
        if not isinstance(self.source_conversation_present, bool):
            raise ValueError("source_conversation_present must be bool.")
        if not isinstance(self.eligible_for_shared_reuse, bool):
            raise ValueError("eligible_for_shared_reuse must be bool.")
        if (
            not isinstance(self.history_turn_count, int)
            or isinstance(self.history_turn_count, bool)
            or self.history_turn_count < 0
        ):
            raise ValueError("history_turn_count must be a non-negative integer.")
        if not isinstance(self.classification_reason, str) or not self.classification_reason.strip():
            raise ValueError("classification_reason must be a non-empty string.")
        object.__setattr__(self, "classification_reason", self.classification_reason.strip())

        norm_domains = tuple(
            sorted({str(d).strip().lower() for d in (self.material_domains or ()) if str(d).strip()})
        )
        for d in norm_domains:
            if d not in ALLOWED_CONTEXT_DOMAINS:
                raise ValueError(f"Unsupported context material domain '{d}'.")
        object.__setattr__(self, "material_domains", norm_domains)

        fp = self.context_fingerprint
        if fp is not None:
            if not isinstance(fp, str) or not fp.strip():
                raise ValueError("context_fingerprint must be None or a non-empty string.")
            fp = fp.strip().lower()
            object.__setattr__(self, "context_fingerprint", fp)

        if self.context_class == ContextClass.CONTEXT_INDEPENDENT:
            if fp != CONTEXT_INDEPENDENT_SENTINEL:
                raise ValueError(
                    f"CONTEXT_INDEPENDENT requires context_fingerprint == '{CONTEXT_INDEPENDENT_SENTINEL}'."
                )
        elif self.context_class == ContextClass.CONTEXT_DEPENDENT:
            if not fp or not fp.startswith("ctx-v1:") or len(fp) != len("ctx-v1:") + 64:
                raise ValueError(
                    "CONTEXT_DEPENDENT requires a canonical 64-hex 'ctx-v1:<sha256>' context_fingerprint."
                )
            hex_part = fp.split(":", 1)[1]
            if any(ch not in "0123456789abcdef" for ch in hex_part):
                raise ValueError("CONTEXT_DEPENDENT context_fingerprint contains non-hex characters.")
        elif self.context_class == ContextClass.CONTEXT_UNCERTAIN:
            if self.eligible_for_shared_reuse:
                raise ValueError("CONTEXT_UNCERTAIN cannot set eligible_for_shared_reuse=True.")

    def to_binding_dict(self) -> Dict[str, Any]:
        """Minimal security-critical payload hashed into variant keys and HMAC evidence."""
        return {
            "context_version": self.context_version,
            "context_class": self.context_class.value,
            "context_fingerprint": self.context_fingerprint,
            "material_domains": list(self.material_domains),
            "has_system_instructions": self.has_system_instructions,
            "eligible_for_shared_reuse": self.eligible_for_shared_reuse,
        }

    def to_dict(self) -> Dict[str, Any]:
        return {
            **self.to_binding_dict(),
            "history_turn_count": self.history_turn_count,
            "source_conversation_present": self.source_conversation_present,
            "classification_reason": self.classification_reason,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "ContextCompatibilityContract":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for ContextCompatibilityContract, got {type(data).__name__}")
        raw_ver = data.get("context_version")
        raw_cls = data.get("context_class")
        if raw_ver is None or raw_cls is None:
            raise ValueError("ContextCompatibilityContract missing 'context_version' or 'context_class'.")
        raw_domains = data.get("material_domains", ())
        if not isinstance(raw_domains, (list, tuple)):
            raise ValueError("ContextCompatibilityContract 'material_domains' must be a list or tuple.")
        return cls(
            context_version=raw_ver,
            context_class=ContextClass(raw_cls),
            context_fingerprint=data.get("context_fingerprint"),
            material_domains=tuple(raw_domains),
            has_system_instructions=bool(data.get("has_system_instructions", False)),
            history_turn_count=int(data.get("history_turn_count", 0)),
            source_conversation_present=bool(data.get("source_conversation_present", False)),
            eligible_for_shared_reuse=bool(data.get("eligible_for_shared_reuse", False)),
            classification_reason=str(data.get("classification_reason") or "CONTEXT_CLASSIFIED"),
        )


class VerificationStatus(str, Enum):
    """Explicit tri-state outcome for actual response artifact verification."""
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"
    UNCERTAIN = "UNCERTAIN"

    def __str__(self) -> str:
        return self.value


class TruncationStatus(str, Enum):
    """Completeness classification of a generated LLM response."""
    COMPLETE = "COMPLETE"
    TRUNCATED = "TRUNCATED"
    UNKNOWN = "UNKNOWN"

    def __str__(self) -> str:
        return self.value


class AdmissionStatus(str, Enum):
    """Explicit admission controller decision before indexing into vector store."""
    ADMIT = "ADMIT"
    REJECT = "REJECT"
    UNCERTAIN = "UNCERTAIN"

    def __str__(self) -> str:
        return self.value


CURRENT_SCHEMA_VERSION = 3
LEGACY_SCHEMA_VERSIONS = frozenset({2})
SUPPORTED_SCHEMA_VERSIONS = frozenset({2, 3})

EVIDENCE_TIER_REQUEST_DERIVED = "request_derived"
EVIDENCE_TIER_RESPONSE_VERIFIED = "response_verified"
VALID_EVIDENCE_TIERS = frozenset({
    EVIDENCE_TIER_REQUEST_DERIVED,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
})

TRUSTED_REQUEST_EXTRACTOR_ID = "compat-extractor-v3"
TRUSTED_RESPONSE_VERIFIER_ID = "response-artifact-verifier-v1"
TRUSTED_CURATED_EVIDENCE_ID = "compat-evidence-curator-v1"

TRUSTED_REQUEST_EVIDENCE_PRODUCERS = frozenset({
    TRUSTED_REQUEST_EXTRACTOR_ID,
})
TRUSTED_RESPONSE_EVIDENCE_PRODUCERS = frozenset({
    TRUSTED_RESPONSE_VERIFIER_ID,
    TRUSTED_CURATED_EVIDENCE_ID,
})

# Keys forbidden in diagnostic evidence payloads to prevent prompt/response/secret leakage.
_SENSITIVE_DIAGNOSTIC_KEYS = frozenset({
    "prompt",
    "query",
    "query_text",
    "raw_text",
    "content",
    "messages",
    "cached_response",
    "response",
    "response_text",
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
    "cookie",
    "principal_id",
    "owner_principal_id",
    "evidence_signature",
    "binding_digest",
    "client_token",
})

V2_REQUIRED_DIMENSIONS = frozenset({
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

REQUIRED_DIMENSIONS = frozenset({
    "language",
    "language_version",
    "framework",
    "framework_version",
    "runtime",
    "runtime_version",
    "dependencies",
    "execution_model",
    "output_format",
    "output_constraints",
    "constraints",
    "freshness",
    "context_dependency",
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
    def from_dict(
        cls,
        data: Dict[str, Any],
        dimension_name: Optional[str] = None,
    ) -> "DimensionValue":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for DimensionValue, got {type(data).__name__}")
        raw_status = data.get("status")
        if not isinstance(raw_status, str) or not raw_status.strip():
            raise ValueError(f"DimensionValue 'status' must be a non-empty string, got {raw_status} ({type(raw_status).__name__})")
        raw_val = data.get("value")
        # Handle list of structured constraint or dependency items if present
        if isinstance(raw_val, list):
            parsed_list = []
            for item in raw_val:
                if dimension_name == "dependencies":
                    if isinstance(item, dict):
                        parsed_list.append(DependencyItem.from_dict(item))
                    elif isinstance(item, DependencyItem):
                        parsed_list.append(item)
                    else:
                        raise ValueError(f"Expected dict or DependencyItem in dependencies list, got {type(item).__name__}")
                elif dimension_name in ("constraints", "output_constraints"):
                    if isinstance(item, dict):
                        parsed_list.append(ConstraintItem.from_dict(item))
                    elif isinstance(item, ConstraintItem):
                        parsed_list.append(item)
                    else:
                        raise ValueError(f"Expected dict or ConstraintItem in {dimension_name} list, got {type(item).__name__}")
                elif isinstance(item, dict):
                    has_name = "name" in item
                    has_key = "key" in item
                    if has_name and not has_key:
                        parsed_list.append(DependencyItem.from_dict(item))
                    elif has_key and not has_name:
                        parsed_list.append(ConstraintItem.from_dict(item))
                    else:
                        raise ValueError("Ambiguous or invalid structured list item in DimensionValue.")
                elif isinstance(item, (ConstraintItem, DependencyItem)):
                    parsed_list.append(item)
                else:
                    raise ValueError(f"Expected dict, ConstraintItem, or DependencyItem in list, got {type(item).__name__}")
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
class DependencyItem:
    """Normalized library or dependency requirement with optional version pin."""
    name: str
    version: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Dependency name must be a non-empty string.")
        norm_name = self.name.strip().lower()
        object.__setattr__(self, "name", norm_name)

        if self.version is not None:
            if not isinstance(self.version, str) or not self.version.strip():
                raise ValueError("Dependency version must be None or a non-empty string.")
            norm_ver = self.version.strip().lower()
            object.__setattr__(self, "version", norm_ver)

    def to_dict(self) -> Dict[str, Any]:
        return {"name": self.name, "version": self.version}

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "DependencyItem":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for DependencyItem, got {type(data).__name__}")
        raw_name = data.get("name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            raise ValueError(
                f"DependencyItem 'name' must be a non-empty string, got {raw_name!r} ({type(raw_name).__name__})"
            )
        raw_ver = data.get("version")
        if raw_ver is not None and (not isinstance(raw_ver, str) or not raw_ver.strip()):
            raise ValueError(
                f"DependencyItem 'version' must be None or a non-empty string, got {raw_ver!r} ({type(raw_ver).__name__})"
            )
        return cls(
            name=raw_name.strip().lower(),
            version=raw_ver.strip().lower() if raw_ver is not None else None,
        )


def _normalize_string_tuple(raw: Any, field_name: str) -> Tuple[str, ...]:
    """Validates and normalizes a collection of non-empty strings into a sorted unique tuple."""
    if raw is None:
        return ()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        raise ValueError(f"CompatibilityEvidence '{field_name}' must be a sequence of strings, got {type(raw).__name__}")
    items: List[str] = []
    for entry in raw:
        if not isinstance(entry, str) or not entry.strip():
            raise ValueError(f"CompatibilityEvidence '{field_name}' entries must be non-empty strings, got {entry!r}")
        items.append(entry.strip().lower())
    return tuple(sorted(set(items)))


@dataclass(frozen=True)
class CompatibilityEvidence:
    """
    Explicit provenance-bound evidence supporting conditional compatibility decisions.
    Separates request-derived extracted attributes from trusted response-verified artifacts.
    """
    source: str
    evidence_tier: str = EVIDENCE_TIER_REQUEST_DERIVED
    version_independent: bool = False
    compatible_language_versions: Tuple[str, ...] = ()
    compatible_framework_versions: Tuple[str, ...] = ()
    compatible_dependency_versions: Tuple[str, ...] = ()
    language_independent: bool = False
    context_independent: bool = False
    verified_output_format: Optional[str] = None
    verified_json_valid: bool = False
    freshness_verified: bool = False
    artifact_schema_version: Optional[int] = None
    verifier_version: Optional[str] = None
    response_digest: Optional[str] = None
    binding_digest: Optional[str] = None
    evidence_signature: Optional[str] = None
    verified_constraints: Tuple[str, ...] = ()
    verified_schema_digest: Optional[str] = None
    signing_key_id: Optional[str] = None
    visibility: Optional[str] = None
    owner_principal_id: Optional[str] = None
    _authority_minted: bool = field(default=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.source, str) or not self.source.strip():
            raise ValueError("CompatibilityEvidence 'source' must be a non-empty string.")
        object.__setattr__(self, "source", self.source.strip())

        if not isinstance(self.evidence_tier, str) or not self.evidence_tier.strip():
            raise ValueError("CompatibilityEvidence 'evidence_tier' must be a non-empty string.")
        object.__setattr__(self, "evidence_tier", self.evidence_tier.strip())

        for bool_field in (
            "version_independent",
            "language_independent",
            "context_independent",
            "verified_json_valid",
            "freshness_verified",
            "_authority_minted",
        ):
            val = getattr(self, bool_field)
            if not isinstance(val, bool):
                raise ValueError(f"CompatibilityEvidence '{bool_field}' must be a boolean, got {type(val).__name__}")

        object.__setattr__(
            self,
            "compatible_language_versions",
            _normalize_string_tuple(self.compatible_language_versions, "compatible_language_versions"),
        )
        object.__setattr__(
            self,
            "compatible_framework_versions",
            _normalize_string_tuple(self.compatible_framework_versions, "compatible_framework_versions"),
        )
        object.__setattr__(
            self,
            "compatible_dependency_versions",
            _normalize_string_tuple(self.compatible_dependency_versions, "compatible_dependency_versions"),
        )
        object.__setattr__(
            self,
            "verified_constraints",
            _normalize_string_tuple(self.verified_constraints, "verified_constraints"),
        )

        if self.verified_output_format is not None:
            if not isinstance(self.verified_output_format, str) or not self.verified_output_format.strip():
                raise ValueError("CompatibilityEvidence 'verified_output_format' must be None or a non-empty string.")
            object.__setattr__(self, "verified_output_format", self.verified_output_format.strip().lower())

        for opt_field in (
            "verifier_version",
            "response_digest",
            "binding_digest",
            "evidence_signature",
            "verified_schema_digest",
            "signing_key_id",
            "visibility",
            "owner_principal_id",
        ):
            val = getattr(self, opt_field)
            if val is not None:
                if not isinstance(val, str) or not val.strip():
                    raise ValueError(f"CompatibilityEvidence '{opt_field}' must be None or a non-empty string.")
                norm_val = val.strip()
                if opt_field not in ("verifier_version", "owner_principal_id"):
                    norm_val = norm_val.lower()
                object.__setattr__(self, opt_field, norm_val)

        if self.artifact_schema_version is not None:
            if (
                not isinstance(self.artifact_schema_version, int)
                or isinstance(self.artifact_schema_version, bool)
                or self.artifact_schema_version <= 0
            ):
                raise ValueError(
                    f"CompatibilityEvidence 'artifact_schema_version' must be a positive int, got {self.artifact_schema_version!r}"
                )

    @classmethod
    def default_request_derived(cls, source: str = TRUSTED_REQUEST_EXTRACTOR_ID) -> "CompatibilityEvidence":
        return cls(source=source, evidence_tier=EVIDENCE_TIER_REQUEST_DERIVED)

    def has_bound_signature(self) -> bool:
        """True only when response/binding digests and HMAC signature are present."""
        return bool(
            self.verifier_version
            and self.response_digest
            and self.binding_digest
            and self.evidence_signature
        )

    def has_independence_or_verification_claims(self) -> bool:
        """True if any independence relaxation or response-level artifact claim is asserted."""
        return bool(
            self.version_independent
            or self.compatible_language_versions
            or self.compatible_framework_versions
            or self.compatible_dependency_versions
            or self.language_independent
            or self.context_independent
            or self.verified_output_format is not None
            or self.verified_json_valid
            or self.freshness_verified
            or self.artifact_schema_version is not None
            or self.verifier_version is not None
            or self.response_digest is not None
            or self.binding_digest is not None
            or self.evidence_signature is not None
            or self.verified_constraints
            or self.verified_schema_digest is not None
            or self.signing_key_id is not None
        )

    def is_trusted_response_verified(self) -> bool:
        """True only when issued at response_verified tier by an explicitly trusted producer."""
        return (
            self.evidence_tier == EVIDENCE_TIER_RESPONSE_VERIFIED
            and self.source in TRUSTED_RESPONSE_EVIDENCE_PRODUCERS
        )

    def is_trusted_request_derived(self) -> bool:
        """True when emitted by the deterministic request extractor without response claims."""
        return (
            self.evidence_tier == EVIDENCE_TIER_REQUEST_DERIVED
            and self.source in TRUSTED_REQUEST_EVIDENCE_PRODUCERS
            and not self.has_independence_or_verification_claims()
        )

    def has_conflicting_claims(self) -> bool:
        """Detects internally inconsistent or ambiguous claims within CompatibilityEvidence."""
        if self.evidence_tier not in VALID_EVIDENCE_TIERS:
            return True
        if self.evidence_tier == EVIDENCE_TIER_REQUEST_DERIVED and self.has_independence_or_verification_claims():
            return True
        if self.verified_json_valid and self.verified_output_format is not None and self.verified_output_format != "json":
            return True
        if self.version_independent and (
            self.compatible_language_versions
            or self.compatible_framework_versions
            or self.compatible_dependency_versions
        ):
            return True
        return False

    def to_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source": self.source,
            "evidence_tier": self.evidence_tier,
            "version_independent": self.version_independent,
            "compatible_language_versions": list(self.compatible_language_versions),
            "compatible_framework_versions": list(self.compatible_framework_versions),
            "compatible_dependency_versions": list(self.compatible_dependency_versions),
            "language_independent": self.language_independent,
            "context_independent": self.context_independent,
            "verified_output_format": self.verified_output_format,
            "verified_json_valid": self.verified_json_valid,
            "freshness_verified": self.freshness_verified,
            "artifact_schema_version": self.artifact_schema_version,
        }
        if self.verifier_version is not None:
            payload["verifier_version"] = self.verifier_version
        if self.response_digest is not None:
            payload["response_digest"] = self.response_digest
        if self.binding_digest is not None:
            payload["binding_digest"] = self.binding_digest
        if self.evidence_signature is not None:
            payload["evidence_signature"] = self.evidence_signature
        if self.verified_constraints:
            payload["verified_constraints"] = list(self.verified_constraints)
        if self.verified_schema_digest is not None:
            payload["verified_schema_digest"] = self.verified_schema_digest
        if self.signing_key_id is not None:
            payload["signing_key_id"] = self.signing_key_id
        if self.visibility is not None:
            payload["visibility"] = self.visibility
        if self.owner_principal_id is not None:
            payload["owner_principal_id"] = self.owner_principal_id
        return payload

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "CompatibilityEvidence":
        if not isinstance(data, dict):
            raise ValueError(f"Expected dict for CompatibilityEvidence, got {type(data).__name__}")
        raw_source = data.get("source")
        if not isinstance(raw_source, str) or not raw_source.strip():
            raise ValueError(f"CompatibilityEvidence 'source' must be a non-empty string, got {raw_source!r}")
        raw_tier = data.get("evidence_tier")
        if not isinstance(raw_tier, str) or not raw_tier.strip():
            raise ValueError(f"CompatibilityEvidence 'evidence_tier' must be a non-empty string, got {raw_tier!r}")
        return cls(
            source=raw_source.strip(),
            evidence_tier=raw_tier.strip(),
            version_independent=data.get("version_independent", False),
            compatible_language_versions=tuple(data.get("compatible_language_versions") or ()),
            compatible_framework_versions=tuple(data.get("compatible_framework_versions") or ()),
            compatible_dependency_versions=tuple(data.get("compatible_dependency_versions") or ()),
            language_independent=data.get("language_independent", False),
            context_independent=data.get("context_independent", False),
            verified_output_format=data.get("verified_output_format"),
            verified_json_valid=data.get("verified_json_valid", False),
            freshness_verified=data.get("freshness_verified", False),
            artifact_schema_version=data.get("artifact_schema_version"),
            verifier_version=data.get("verifier_version"),
            response_digest=data.get("response_digest"),
            binding_digest=data.get("binding_digest"),
            evidence_signature=data.get("evidence_signature"),
            verified_constraints=tuple(data.get("verified_constraints") or ()),
            verified_schema_digest=data.get("verified_schema_digest"),
            signing_key_id=data.get("signing_key_id"),
            visibility=data.get("visibility"),
            owner_principal_id=data.get("owner_principal_id"),
            _authority_minted=False,
        )


def _sanitize_diagnostic_value(val: Any) -> Any:
    if isinstance(val, Enum):
        return val.value
    if isinstance(val, dict):
        return sanitize_diagnostic_evidence(val)
    if isinstance(val, (list, tuple)):
        return [_sanitize_diagnostic_value(v) for v in val[:25]]
    if isinstance(val, str):
        return val[:200]
    if isinstance(val, (int, float, bool)) or val is None:
        return val
    return str(val)[:200]


def sanitize_diagnostic_evidence(raw: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Removes potentially sensitive payload keys before attaching diagnostic evidence."""
    if not raw or not isinstance(raw, dict):
        return {}
    cleaned: Dict[str, Any] = {}
    for k, v in raw.items():
        key_str = str(k).strip()
        if key_str.lower() in _SENSITIVE_DIAGNOSTIC_KEYS:
            continue
        cleaned[key_str] = _sanitize_diagnostic_value(v)
    return cleaned


@dataclass(frozen=True)
class CompatibilityDecision:
    """
    Explicit structured tri-state compatibility outcome.
    Replaces ambiguous boolean-only results across validation and candidate filtering.
    """
    status: CompatibilityStatus
    reason_code: CompatibilityReasonCode
    message: str
    dimension: Optional[str] = None
    evidence: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, CompatibilityStatus):
            object.__setattr__(self, "status", CompatibilityStatus(self.status))
        if not isinstance(self.reason_code, CompatibilityReasonCode):
            object.__setattr__(self, "reason_code", CompatibilityReasonCode(self.reason_code))
        if not isinstance(self.message, str) or not self.message.strip():
            raise ValueError("CompatibilityDecision 'message' must be a non-empty string.")
        if self.dimension is not None and (not isinstance(self.dimension, str) or not self.dimension.strip()):
            raise ValueError("CompatibilityDecision 'dimension' must be None or a non-empty string.")
        object.__setattr__(self, "evidence", sanitize_diagnostic_evidence(self.evidence))

    @property
    def is_compatible(self) -> bool:
        return self.status == CompatibilityStatus.COMPATIBLE

    @property
    def is_incompatible(self) -> bool:
        return self.status == CompatibilityStatus.INCOMPATIBLE

    @property
    def is_uncertain(self) -> bool:
        return self.status == CompatibilityStatus.UNCERTAIN

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "dimension": self.dimension,
            "evidence": dict(self.evidence),
        }


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
    compatibility_evidence: Optional[CompatibilityEvidence] = None
    context_contract: Optional[ContextCompatibilityContract] = None

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

        required_dims = V2_REQUIRED_DIMENSIONS if self.schema_version == 2 else REQUIRED_DIMENSIONS
        missing_dims = required_dims - set(self.dimensions.keys())
        if missing_dims:
            raise ValueError(f"Missing required dimensions: {sorted(list(missing_dims))}")

        for dim_name, dim_val in self.dimensions.items():
            if not isinstance(dim_val, DimensionValue):
                raise ValueError(f"Dimension '{dim_name}' must be DimensionValue, got {type(dim_val).__name__}")
            if dim_val.status == MetadataStatus.KNOWN:
                if dim_name in ("constraints", "output_constraints"):
                    if not isinstance(dim_val.value, list):
                        raise ValueError(f"Dimension '{dim_name}' value must be a list of ConstraintItem, got {type(dim_val.value).__name__}")
                    for c_item in dim_val.value:
                        if not isinstance(c_item, ConstraintItem):
                            raise ValueError(f"{dim_name} list item must be ConstraintItem, got {type(c_item).__name__}")
                elif dim_name == "dependencies":
                    if not isinstance(dim_val.value, list):
                        raise ValueError(f"Dimension '{dim_name}' value must be a list of DependencyItem, got {type(dim_val.value).__name__}")
                    for d_item in dim_val.value:
                        if not isinstance(d_item, DependencyItem):
                            raise ValueError(f"Dependencies list item must be DependencyItem, got {type(d_item).__name__}")
                else:
                    if not isinstance(dim_val.value, str):
                        raise ValueError(f"Dimension '{dim_name}' value must be a string, got {type(dim_val.value).__name__}")

        if not isinstance(self.generation_contract, GenerationContract):
            raise ValueError(f"generation_contract must be GenerationContract, got {type(self.generation_contract).__name__}")
        if not isinstance(self.embedding_contract, EmbeddingContract):
            raise ValueError(f"embedding_contract must be EmbeddingContract, got {type(self.embedding_contract).__name__}")
        if self.compatibility_evidence is not None and not isinstance(self.compatibility_evidence, CompatibilityEvidence):
            raise ValueError(
                f"compatibility_evidence must be CompatibilityEvidence or None, got {type(self.compatibility_evidence).__name__}"
            )
        if self.context_contract is not None:
            if not isinstance(self.context_contract, ContextCompatibilityContract):
                raise ValueError(
                    f"context_contract must be ContextCompatibilityContract or None, got {type(self.context_contract).__name__}"
                )
            self.context_contract.validate()

    def to_dict(self) -> Dict[str, Any]:
        """Serializes metadata to standard JSON-compatible dictionary."""
        payload = {
            "schema_version": self.schema_version,
            "extractor_version": self.extractor_version,
            "task_kind": self.task_kind.to_dict(),
            "dimensions": {k: v.to_dict() for k, v in self.dimensions.items()},
            "generation_contract": self.generation_contract.to_dict(),
            "embedding_contract": self.embedding_contract.to_dict(),
            "indexed_at": self.indexed_at or datetime.now(timezone.utc).isoformat(),
            "compatibility_evidence": (
                self.compatibility_evidence.to_dict() if self.compatibility_evidence is not None else None
            ),
            # Supplementary keys for backwards-compatible diagnostics
            "llm_provider": self.generation_contract.provider,
            "llm_model": self.generation_contract.model,
            "temperature": self.generation_contract.temperature,
            "max_tokens": self.generation_contract.max_tokens,
        }
        if self.context_contract is not None:
            payload["context_contract"] = self.context_contract.to_dict()
        return payload

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
        if ver not in SUPPORTED_SCHEMA_VERSIONS:
            raise ValueError(f"Unsupported schema_version: {ver}. Expected one of {sorted(SUPPORTED_SCHEMA_VERSIONS)}.")

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

        required_dims = V2_REQUIRED_DIMENSIONS if ver == 2 else REQUIRED_DIMENSIONS
        dimensions: Dict[str, DimensionValue] = {}
        for dim_name in required_dims:
            if dim_name not in raw_dims:
                raise ValueError(f"Missing required dimension '{dim_name}' in metadata.")
            dim_dict = raw_dims[dim_name]
            if not isinstance(dim_dict, dict):
                raise ValueError(f"Invalid dimension dict for '{dim_name}'")
            dimensions[dim_name] = DimensionValue.from_dict(dim_dict, dimension_name=dim_name)

        raw_gen = data.get("generation_contract")
        if not isinstance(raw_gen, dict):
            raise ValueError("Missing or invalid 'generation_contract' in metadata dictionary.")
        generation_contract = GenerationContract.from_dict(raw_gen)

        raw_emb = data.get("embedding_contract")
        if not isinstance(raw_emb, dict):
            raise ValueError("Missing or invalid 'embedding_contract' in metadata dictionary.")
        embedding_contract = EmbeddingContract.from_dict(raw_emb)

        raw_ev = data.get("compatibility_evidence")
        compatibility_evidence: Optional[CompatibilityEvidence] = None
        if raw_ev is not None:
            compatibility_evidence = CompatibilityEvidence.from_dict(raw_ev)

        context_contract: Optional[ContextCompatibilityContract] = None
        if "context_contract" in data and data.get("context_contract") is not None:
            context_contract = ContextCompatibilityContract.from_dict(data["context_contract"])

        return cls(
            schema_version=ver,
            extractor_version=ext_ver,
            task_kind=task_kind,
            dimensions=dimensions,
            generation_contract=generation_contract,
            embedding_contract=embedding_contract,
            indexed_at=data.get("indexed_at"),
            compatibility_evidence=compatibility_evidence,
            context_contract=context_contract,
        )


@dataclass(frozen=True)
class ResponseVerificationResult:
    """Structured outcome from ResponseArtifactVerifier."""
    status: VerificationStatus
    truncation_status: TruncationStatus
    reason_code: CompatibilityReasonCode
    message: str
    verifier_version: str = TRUSTED_RESPONSE_VERIFIER_ID
    verified_output_format: Optional[str] = None
    verified_json_valid: bool = False
    verified_schema_version: Optional[int] = None
    verified_schema_digest: Optional[str] = None
    verified_constraints: Tuple[str, ...] = ()
    unverified_constraints: Tuple[str, ...] = ()
    evidence_details: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, VerificationStatus):
            object.__setattr__(self, "status", VerificationStatus(self.status))
        if not isinstance(self.truncation_status, TruncationStatus):
            object.__setattr__(self, "truncation_status", TruncationStatus(self.truncation_status))
        if not isinstance(self.reason_code, CompatibilityReasonCode):
            object.__setattr__(self, "reason_code", CompatibilityReasonCode(self.reason_code))
        object.__setattr__(self, "evidence_details", sanitize_diagnostic_evidence(self.evidence_details))

    @property
    def is_verified(self) -> bool:
        return self.status == VerificationStatus.VERIFIED

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "truncation_status": self.truncation_status.value,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "verifier_version": self.verifier_version,
            "verified_output_format": self.verified_output_format,
            "verified_json_valid": self.verified_json_valid,
            "verified_schema_version": self.verified_schema_version,
            "verified_schema_digest": self.verified_schema_digest,
            "verified_constraints": list(self.verified_constraints),
            "unverified_constraints": list(self.unverified_constraints),
            "evidence_details": dict(self.evidence_details),
        }


@dataclass(frozen=True)
class AdmissionDecision:
    """Decision record produced before semantic cache embedding/upsert."""
    status: AdmissionStatus
    reason_code: CompatibilityReasonCode
    message: str
    variant_key: Optional[str] = None
    verification: Optional[ResponseVerificationResult] = None
    diagnostics: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.status, AdmissionStatus):
            object.__setattr__(self, "status", AdmissionStatus(self.status))
        if not isinstance(self.reason_code, CompatibilityReasonCode):
            object.__setattr__(self, "reason_code", CompatibilityReasonCode(self.reason_code))
        object.__setattr__(self, "diagnostics", sanitize_diagnostic_evidence(self.diagnostics))

    @property
    def should_admit(self) -> bool:
        return self.status == AdmissionStatus.ADMIT

    def to_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status.value,
            "reason_code": self.reason_code.value,
            "message": self.message,
            "variant_key": self.variant_key,
            "verification": self.verification.to_dict() if self.verification else None,
            "diagnostics": dict(self.diagnostics),
        }


def compute_variant_key(
    metadata: SemanticCompatibilityMetadata,
    schema_digest: Optional[str] = None,
    visibility: Optional[str] = None,
    owner_principal_id: Optional[str] = None,
) -> str:
    """
    Computes a deterministic normalized compatibility-variant identity across all technical
    dimensions, generation contract, embedding contract, schema version, and optional schema digest.
    Variants that differ in language, framework, runtime, dependency, constraints, output format,
    or generation parameters yield strictly distinct variant keys so multiple variants can coexist.
    """
    norm_dims: Dict[str, Any] = {}
    for dim_name in sorted(metadata.dimensions.keys()):
        dim = metadata.dimensions[dim_name]
        if dim.status != MetadataStatus.KNOWN:
            norm_dims[dim_name] = {"status": dim.status, "value": None}
            continue
        if dim_name == "dependencies" and isinstance(dim.value, list):
            items = [
                (d if isinstance(d, DependencyItem) else DependencyItem.from_dict(d)).to_dict()
                for d in dim.value
            ]
            items.sort(key=lambda d: (d["name"], d.get("version") or ""))
            norm_dims[dim_name] = {"status": dim.status, "value": items}
        elif dim_name in ("constraints", "output_constraints") and isinstance(dim.value, list):
            items = [
                (c if isinstance(c, ConstraintItem) else ConstraintItem.from_dict(c)).to_dict()
                for c in dim.value
            ]
            items.sort(key=lambda c: (c["key"].lower(), c["operator"].lower(), str(c["value"]).lower()))
            norm_dims[dim_name] = {"status": dim.status, "value": items}
        else:
            norm_dims[dim_name] = {
                "status": dim.status,
                "value": str(dim.value).strip().lower(),
            }

    ev_schema_digest = schema_digest
    if ev_schema_digest is None and metadata.compatibility_evidence is not None:
        ev_schema_digest = metadata.compatibility_evidence.verified_schema_digest

    ev_visibility = visibility
    if ev_visibility is None and metadata.compatibility_evidence is not None:
        ev_visibility = metadata.compatibility_evidence.visibility
    ev_owner = owner_principal_id
    if ev_owner is None and metadata.compatibility_evidence is not None:
        ev_owner = metadata.compatibility_evidence.owner_principal_id

    canonical_obj = {
        "schema_version": metadata.schema_version,
        "task_kind": {
            "status": metadata.task_kind.status,
            "value": str(metadata.task_kind.value).strip().lower()
            if metadata.task_kind.value is not None
            else None,
        },
        "dimensions": norm_dims,
        "generation_contract": {
            "provider": metadata.generation_contract.provider.strip(),
            "model": metadata.generation_contract.model.strip(),
            "temperature": round(float(metadata.generation_contract.temperature), 8),
            "max_tokens": metadata.generation_contract.max_tokens,
        },
        "embedding_contract": {
            "model": metadata.embedding_contract.model.strip(),
        },
        "schema_digest": ev_schema_digest.strip().lower() if isinstance(ev_schema_digest, str) and ev_schema_digest.strip() else None,
    }
    if isinstance(ev_visibility, str) and ev_visibility.strip():
        canonical_obj["visibility"] = ev_visibility.strip().lower()
    if isinstance(ev_owner, str) and ev_owner.strip():
        canonical_obj["owner_principal_id"] = ev_owner.strip()
    if metadata.context_contract is not None:
        canonical_obj["context_contract"] = metadata.context_contract.to_binding_dict()
    encoded = json.dumps(canonical_obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    digest = hashlib.sha256(encoded).hexdigest()
    return f"v{metadata.schema_version}:{digest}"


@dataclass(frozen=True)
class EligibilityResult:
    """Result of checking a request's eligibility for semantic caching."""
    eligible: bool
    reason: str
    metadata: Optional["SemanticCompatibilityMetadata"] = None
    context_assessment: Optional[Any] = None


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
    is_cross_conversation: bool = False
    matched_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


@dataclass
class SemanticCacheStats:
    """Diagnostic observability counters for semantic cache operations."""
    lookups: int = 0
    hits: int = 0
    coalesced_semantic_hits: int = 0
    misses: int = 0
    rejected_candidates: int = 0
    uncertain_candidates: int = 0
    ambiguous_candidates: int = 0
    no_candidate_misses: int = 0
    threshold_rejections: int = 0
    all_candidates_rejected_misses: int = 0
    retrieval_budget_exhaustions: int = 0
    eligibility_rejections: int = 0
    embedding_failures: int = 0
    vector_store_failures: int = 0
    indexing_successes: int = 0
    indexing_failures: int = 0
    validator_failures: int = 0
    admission_verified: int = 0
    admission_rejected: int = 0
    admission_uncertain: int = 0
    runtime_disabled_skips: int = 0
    candidates_retrieved: int = 0
    visibility_rejections: int = 0
    principal_isolation_rejections: int = 0
    evidence_validation_rejections: int = 0
    upstream_fallbacks_after_miss: int = 0
    context_independent_requests: int = 0
    context_dependent_requests: int = 0
    context_uncertain_requests: int = 0
    context_compatibility_matches: int = 0
    context_compatibility_rejections: int = 0
    context_metadata_missing: int = 0
    context_fingerprint_mismatches: int = 0
    cross_conversation_semantic_hits: int = 0
    cross_conversation_semantic_misses: int = 0
    private_scope_rejections: int = 0
    tenant_shared_authorization_rejections: int = 0
    semantic_admission_successes: int = 0
    semantic_admission_rejections: int = 0
    semantic_fallbacks: int = 0

    def to_dict(self) -> Dict[str, int]:
        return {
            "lookups": self.lookups,
            "hits": self.hits,
            "coalesced_semantic_hits": self.coalesced_semantic_hits,
            "misses": self.misses,
            "rejected_candidates": self.rejected_candidates,
            "uncertain_candidates": self.uncertain_candidates,
            "ambiguous_candidates": self.ambiguous_candidates,
            "no_candidate_misses": self.no_candidate_misses,
            "threshold_rejections": self.threshold_rejections,
            "all_candidates_rejected_misses": self.all_candidates_rejected_misses,
            "retrieval_budget_exhaustions": self.retrieval_budget_exhaustions,
            "eligibility_rejections": self.eligibility_rejections,
            "embedding_failures": self.embedding_failures,
            "vector_store_failures": self.vector_store_failures,
            "indexing_successes": self.indexing_successes,
            "indexing_failures": self.indexing_failures,
            "validator_failures": self.validator_failures,
            "admission_verified": self.admission_verified,
            "admission_rejected": self.admission_rejected,
            "admission_uncertain": self.admission_uncertain,
            "runtime_disabled_skips": self.runtime_disabled_skips,
            "candidates_retrieved": self.candidates_retrieved,
            "visibility_rejections": self.visibility_rejections,
            "principal_isolation_rejections": self.principal_isolation_rejections,
            "evidence_validation_rejections": self.evidence_validation_rejections,
            "upstream_fallbacks_after_miss": self.upstream_fallbacks_after_miss,
            "context_independent_requests": self.context_independent_requests,
            "context_dependent_requests": self.context_dependent_requests,
            "context_uncertain_requests": self.context_uncertain_requests,
            "context_compatibility_matches": self.context_compatibility_matches,
            "context_compatibility_rejections": self.context_compatibility_rejections,
            "context_metadata_missing": self.context_metadata_missing,
            "context_fingerprint_mismatches": self.context_fingerprint_mismatches,
            "cross_conversation_semantic_hits": self.cross_conversation_semantic_hits,
            "cross_conversation_semantic_misses": self.cross_conversation_semantic_misses,
            "private_scope_rejections": self.private_scope_rejections,
            "tenant_shared_authorization_rejections": self.tenant_shared_authorization_rejections,
            "semantic_admission_successes": self.semantic_admission_successes,
            "semantic_admission_rejections": self.semantic_admission_rejections,
            "semantic_fallbacks": self.semantic_fallbacks,
        }
