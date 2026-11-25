"""
Deterministic response artifact verifier (Stage 3.5.3).
Inspects actual generated LLM outputs against request requirements (output format,
JSON/YAML/XML/CSV syntax, concrete JSON Schema subset, output constraints, and truncation)
with strict fail-closed behavior for failed or unverified artifacts.

Isolated from FastAPI, Starlette, Groq SDK, Ollama SDK, and asyncpg.
"""

from __future__ import annotations

import csv
import io
import json
import math
import re
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET

import yaml

from .contracts import (
    CompatibilityReasonCode,
    ConstraintItem,
    MetadataStatus,
    ResponseVerificationResult,
    SemanticCompatibilityMetadata,
    TRUSTED_RESPONSE_VERIFIER_ID,
    TruncationStatus,
    VerificationStatus,
)
from .evidence import compute_canonical_schema_digest

# Supported finish reasons from LLM providers
_COMPLETE_FINISH_REASONS = frozenset({"stop", "end_turn", "eos", "completed"})
_TRUNCATED_FINISH_REASONS = frozenset({
    "length",
    "max_tokens",
    "max_output_tokens",
    "content_filter",
    "incomplete",
    "error",
})

# Output constraint keys we can deterministically verify
_SUPPORTED_OUTPUT_CONSTRAINT_KEYS = frozenset({
    "strict_format",
    "schema_adherence",
    "markup",
    "code_only",
    "required_keys",
    "max_words",
    "max_chars",
})

# JSON Schema keywords outside our supported deterministic subset -> fail closed UNCERTAIN
_UNSUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "$ref",
    "$dynamicRef",
    "$defs",
    "definitions",
    "allOf",
    "anyOf",
    "oneOf",
    "not",
    "if",
    "then",
    "else",
    "patternProperties",
    "dependencies",
    "dependentSchemas",
    "dependentRequired",
    "contains",
    "propertyNames",
    "format",
    "contentMediaType",
    "contentEncoding",
})

_SUPPORTED_SCHEMA_KEYWORDS = frozenset({
    "$schema",
    "$id",
    "title",
    "description",
    "default",
    "examples",
    "type",
    "properties",
    "required",
    "additionalProperties",
    "items",
    "enum",
    "const",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "minItems",
    "maxItems",
    "uniqueItems",
})

_CONVERSATIONAL_PREAMBLE_RE = re.compile(
    r"^\s*(?:here\s+(?:is|are)\b|sure[,!.]|certainly[,!.]|below\s+is\b|you\s+can\s+use\b|to\s+do\s+this[,:])",
    re.IGNORECASE,
)


def _reject_nonfinite_json(token: str) -> Any:
    raise ValueError(f"Non-finite JSON constant '{token}' is not valid strict JSON.")


def extract_requested_json_schema(
    query_text: str,
    request_metadata: Optional[Dict[str, Any]] = None,
) -> Tuple[Optional[Dict[str, Any]], bool]:
    """
    Attempts to locate a concrete JSON Schema dict either from trusted request parameter
    (`json_schema` / `output_schema` / `response_schema`) or inline within the prompt text.
    Returns (schema_dict_or_none, malformed_schema_present).
    Never invents or fabricates schemas.
    """
    if isinstance(request_metadata, dict):
        for key in ("json_schema", "output_schema", "response_schema"):
            candidate = request_metadata.get(key)
            if isinstance(candidate, dict):
                if _looks_like_json_schema(candidate):
                    return candidate, False
                return None, True

    text = (query_text or "").strip()
    if not text:
        return None, False

    # Look for JSON blocks inside markdown fences first, then inline braces after schema hint
    candidates: List[str] = []
    for m in re.finditer(r"```(?:json|schema)?\s*(\{.*?\})\s*```", text, re.DOTALL | re.IGNORECASE):
        candidates.append(m.group(1))

    if not candidates and re.search(r"\bschema\b", text, re.IGNORECASE):
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start != -1 and brace_end > brace_start:
            candidates.append(text[brace_start:brace_end + 1])

    for snippet in candidates:
        try:
            parsed = json.loads(snippet, parse_constant=_reject_nonfinite_json)
        except Exception:
            continue
        if isinstance(parsed, dict) and _looks_like_json_schema(parsed):
            return parsed, False

    return None, False


def _looks_like_json_schema(obj: Dict[str, Any]) -> bool:
    schema_markers = {"$schema", "type", "properties", "required", "items", "enum", "const"}
    return any(k in obj for k in schema_markers)


def _validate_schema_definition_supported(schema: Any) -> Optional[str]:
    """
    Checks recursively whether all keywords in the JSON Schema are inside our deterministic subset.
    Returns None if fully supported, or an error description naming any unsupported construct.
    """
    if isinstance(schema, bool):
        return None
    if not isinstance(schema, dict):
        return f"Schema node must be a dict or bool, got {type(schema).__name__}."

    for key in sorted(schema.keys()):
        if key in _UNSUPPORTED_SCHEMA_KEYWORDS or key not in _SUPPORTED_SCHEMA_KEYWORDS:
            return f"Unsupported JSON Schema keyword '{key}'."

    props = schema.get("properties")
    if props is not None:
        if not isinstance(props, dict):
            return "'properties' must be a dict."
        for prop_name, prop_schema in props.items():
            err = _validate_schema_definition_supported(prop_schema)
            if err:
                return f"properties.{prop_name}: {err}"

    add_props = schema.get("additionalProperties")
    if isinstance(add_props, dict):
        err = _validate_schema_definition_supported(add_props)
        if err:
            return f"additionalProperties: {err}"
    elif add_props is not None and not isinstance(add_props, bool):
        return "'additionalProperties' must be a boolean or schema object."

    items = schema.get("items")
    if items is not None:
        err = _validate_schema_definition_supported(items)
        if err:
            return f"items: {err}"

    return None


def _matches_json_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return (
            isinstance(value, (int, float))
            and not isinstance(value, bool)
            and math.isfinite(value)
        )
    if expected_type == "null":
        return value is None
    return False


def _validate_value_against_schema(value: Any, schema: Any, path: str = "$") -> Optional[str]:
    """Deterministically validates a parsed JSON value against a supported JSON Schema subset."""
    if isinstance(schema, bool):
        return None if schema else f"{path}: disallowed by boolean schema false."

    if "const" in schema and value != schema["const"]:
        return f"{path}: expected constant {schema['const']!r}."

    if "enum" in schema:
        enum_vals = schema["enum"]
        if isinstance(enum_vals, list) and not any(
            value == e and type(value) is type(e)
            if isinstance(value, bool) or isinstance(e, bool)
            else value == e
            for e in enum_vals
        ):
            return f"{path}: value {value!r} not in enum {enum_vals!r}."

    expected_type = schema.get("type")
    if expected_type is not None:
        if isinstance(expected_type, list):
            if not any(_matches_json_type(value, t) for t in expected_type):
                return f"{path}: value type {type(value).__name__} does not match allowed types {expected_type}."
        elif isinstance(expected_type, str):
            if not _matches_json_type(value, expected_type):
                return f"{path}: expected JSON type '{expected_type}', got {type(value).__name__}."

    if isinstance(value, dict):
        required = schema.get("required") or []
        for req_key in required:
            if req_key not in value:
                return f"{path}: missing required key '{req_key}'."

        properties = schema.get("properties") or {}
        add_props = schema.get("additionalProperties", True)
        for k, v in value.items():
            if k in properties:
                err = _validate_value_against_schema(v, properties[k], path=f"{path}.{k}")
                if err:
                    return err
            else:
                if add_props is False:
                    return f"{path}: additional property '{k}' is forbidden."
                if isinstance(add_props, dict):
                    err = _validate_value_against_schema(v, add_props, path=f"{path}.{k}")
                    if err:
                        return err

    elif isinstance(value, list):
        min_items = schema.get("minItems")
        if min_items is not None and len(value) < min_items:
            return f"{path}: array length {len(value)} < minItems {min_items}."
        max_items = schema.get("maxItems")
        if max_items is not None and len(value) > max_items:
            return f"{path}: array length {len(value)} > maxItems {max_items}."
        if schema.get("uniqueItems"):
            seen = set()
            for item in value:
                canon = json.dumps(item, sort_keys=True, separators=(",", ":"))
                if canon in seen:
                    return f"{path}: duplicate array items violate uniqueItems."
                seen.add(canon)
        items_schema = schema.get("items")
        if items_schema is not None:
            for idx, item in enumerate(value):
                err = _validate_value_against_schema(item, items_schema, path=f"{path}[{idx}]")
                if err:
                    return err

    elif isinstance(value, str):
        min_len = schema.get("minLength")
        if min_len is not None and len(value) < min_len:
            return f"{path}: string length {len(value)} < minLength {min_len}."
        max_len = schema.get("maxLength")
        if max_len is not None and len(value) > max_len:
            return f"{path}: string length {len(value)} > maxLength {max_len}."
        pattern = schema.get("pattern")
        if pattern is not None:
            if len(str(pattern)) > 256 or not re.search(pattern, value):
                return f"{path}: string does not match required pattern."

    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        if "minimum" in schema and value < schema["minimum"]:
            return f"{path}: value {value} < minimum {schema['minimum']}."
        if "maximum" in schema and value > schema["maximum"]:
            return f"{path}: value {value} > maximum {schema['maximum']}."
        if "exclusiveMinimum" in schema and value <= schema["exclusiveMinimum"]:
            return f"{path}: value {value} <= exclusiveMinimum {schema['exclusiveMinimum']}."
        if "exclusiveMaximum" in schema and value >= schema["exclusiveMaximum"]:
            return f"{path}: value {value} >= exclusiveMaximum {schema['exclusiveMaximum']}."

    return None


class ResponseArtifactVerifier:
    """
    Verifies generated LLM responses against extracted request metadata and constraints.
    Returns explicit VERIFIED / FAILED / UNCERTAIN decisions and never invents claims.
    """

    VERIFIER_VERSION = TRUSTED_RESPONSE_VERIFIER_ID

    def verify(
        self,
        *,
        response_text: str,
        request_metadata: SemanticCompatibilityMetadata,
        query_text: str = "",
        finish_reason: Optional[str] = None,
        completion_tokens: int = 0,
        request_max_tokens: Optional[int] = None,
        request_extra_metadata: Optional[Dict[str, Any]] = None,
    ) -> ResponseVerificationResult:
        raw_text = response_text if isinstance(response_text, str) else ""
        stripped = raw_text.strip()

        if not stripped:
            code = CompatibilityReasonCode.VERIFICATION_FAILED_EMPTY
            return ResponseVerificationResult(
                status=VerificationStatus.FAILED,
                truncation_status=TruncationStatus.UNKNOWN,
                reason_code=code,
                message=f"{code}: Response artifact is empty or whitespace-only.",
                verifier_version=self.VERIFIER_VERSION,
            )

        out_fmt_dim = request_metadata.dimensions.get("output_format")
        req_fmt = (
            str(out_fmt_dim.value).strip().lower()
            if out_fmt_dim and out_fmt_dim.status == MetadataStatus.KNOWN
            else None
        )

        # 1. Truncation and structural completeness checks
        trunc_status, trunc_reason, trunc_msg = self._check_truncation(
            stripped=stripped,
            expected_format=req_fmt,
            finish_reason=finish_reason,
            completion_tokens=completion_tokens,
            request_max_tokens=request_max_tokens,
        )
        if trunc_status == TruncationStatus.TRUNCATED:
            return ResponseVerificationResult(
                status=VerificationStatus.FAILED,
                truncation_status=TruncationStatus.TRUNCATED,
                reason_code=trunc_reason,
                message=trunc_msg,
                verifier_version=self.VERIFIER_VERSION,
                evidence_details={"finish_reason": finish_reason, "completion_tokens": completion_tokens},
            )
        if trunc_reason == CompatibilityReasonCode.VERIFICATION_UNCERTAIN_TRUNCATION:
            return ResponseVerificationResult(
                status=VerificationStatus.UNCERTAIN,
                truncation_status=TruncationStatus.UNKNOWN,
                reason_code=trunc_reason,
                message=trunc_msg,
                verifier_version=self.VERIFIER_VERSION,
                evidence_details={"finish_reason": finish_reason, "completion_tokens": completion_tokens},
            )

        verified_fmt: Optional[str] = None
        verified_json_valid = False
        parsed_structured: Any = None
        verified_constraints: List[str] = []

        # 2. Output format syntactic verification (when requested)
        if req_fmt is not None:
            fmt_ok, fmt_code, fmt_msg, parsed_obj = self._verify_output_format(stripped, req_fmt)
            if not fmt_ok:
                return ResponseVerificationResult(
                    status=VerificationStatus.FAILED,
                    truncation_status=trunc_status,
                    reason_code=fmt_code,
                    message=fmt_msg,
                    verifier_version=self.VERIFIER_VERSION,
                    evidence_details={"requested_output_format": req_fmt},
                )
            verified_fmt = req_fmt
            parsed_structured = parsed_obj
            if req_fmt == "json":
                verified_json_valid = True

        # 3. Output constraints verification
        out_c_dim = request_metadata.dimensions.get("output_constraints")
        verified_schema_version: Optional[int] = None
        verified_schema_digest: Optional[str] = None

        if out_c_dim and out_c_dim.status == MetadataStatus.KNOWN and isinstance(out_c_dim.value, list):
            for raw_item in out_c_dim.value:
                c_item = raw_item if isinstance(raw_item, ConstraintItem) else ConstraintItem.from_dict(raw_item)
                if c_item.key not in _SUPPORTED_OUTPUT_CONSTRAINT_KEYS:
                    code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                    return ResponseVerificationResult(
                        status=VerificationStatus.UNCERTAIN,
                        truncation_status=trunc_status,
                        reason_code=code,
                        message=f"{code}: Output constraint '{c_item.key}' lacks a deterministic verifier.",
                        verifier_version=self.VERIFIER_VERSION,
                        verified_output_format=verified_fmt,
                        verified_json_valid=verified_json_valid,
                        verified_constraints=tuple(verified_constraints),
                        unverified_constraints=(c_item.key,),
                    )

                if c_item.key == "strict_format" and c_item.value == "json_only":
                    if "```" in stripped:
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: strict_format=json_only rejects markdown code fences.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    if not verified_json_valid:
                        fmt_ok, fmt_code, fmt_msg, parsed_obj = self._verify_output_format(stripped, "json")
                        if not fmt_ok:
                            return ResponseVerificationResult(
                                status=VerificationStatus.FAILED,
                                truncation_status=trunc_status,
                                reason_code=fmt_code,
                                message=fmt_msg,
                                verifier_version=self.VERIFIER_VERSION,
                            )
                        verified_fmt = "json"
                        verified_json_valid = True
                        parsed_structured = parsed_obj
                    verified_constraints.append("strict_format=json_only")

                elif c_item.key == "markup" and c_item.value == "no_markdown":
                    if "```" in stripped or re.search(r"(?m)^#{1,6}\s+", stripped):
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Response contains markdown formatting despite no_markdown constraint.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_constraints.append("markup=no_markdown")

                elif c_item.key == "code_only":
                    if not self._looks_like_code_only(stripped):
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Response includes conversational prose outside code_only constraint.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_constraints.append("code_only=true")

                elif c_item.key == "max_words":
                    limit = self._parse_positive_int(c_item.value)
                    if limit is None:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Unparseable max_words limit '{c_item.value}'.",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("max_words",),
                        )
                    word_count = len(stripped.split())
                    if word_count > limit:
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Word count {word_count} exceeds max_words={limit}.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_constraints.append(f"max_words={limit}")

                elif c_item.key == "max_chars":
                    limit = self._parse_positive_int(c_item.value)
                    if limit is None:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Unparseable max_chars limit '{c_item.value}'.",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("max_chars",),
                        )
                    if len(stripped) > limit:
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Char count {len(stripped)} exceeds max_chars={limit}.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_constraints.append(f"max_chars={limit}")

                elif c_item.key == "required_keys":
                    expected_keys = [k.strip() for k in str(c_item.value).split(",") if k.strip()]
                    if not expected_keys:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Empty required_keys specification.",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("required_keys",),
                        )
                    if parsed_structured is None and req_fmt in (None, "json"):
                        fmt_ok, fmt_code, fmt_msg, parsed_obj = self._verify_output_format(stripped, "json")
                        if not fmt_ok:
                            return ResponseVerificationResult(
                                status=VerificationStatus.FAILED,
                                truncation_status=trunc_status,
                                reason_code=fmt_code,
                                message=fmt_msg,
                                verifier_version=self.VERIFIER_VERSION,
                            )
                        verified_fmt = "json"
                        verified_json_valid = True
                        parsed_structured = parsed_obj
                    if not isinstance(parsed_structured, dict):
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: required_keys constraint requires a mapping object response.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    missing = [k for k in expected_keys if k not in parsed_structured]
                    if missing:
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Response missing required keys: {missing}.",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_constraints.append(f"required_keys={','.join(sorted(expected_keys))}")

                elif c_item.key == "schema_adherence":
                    schema_obj, malformed = extract_requested_json_schema(query_text, request_extra_metadata)
                    if malformed:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Supplied schema payload is invalid or unsupported.",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("schema_adherence",),
                        )
                    if schema_obj is None:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Schema adherence requested without a concrete schema definition.",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("schema_adherence",),
                        )
                    unsupported_err = _validate_schema_definition_supported(schema_obj)
                    if unsupported_err is not None:
                        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
                        return ResponseVerificationResult(
                            status=VerificationStatus.UNCERTAIN,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: {unsupported_err}",
                            verifier_version=self.VERIFIER_VERSION,
                            unverified_constraints=("schema_adherence",),
                        )
                    if not verified_json_valid:
                        fmt_ok, fmt_code, fmt_msg, parsed_obj = self._verify_output_format(stripped, "json")
                        if not fmt_ok:
                            return ResponseVerificationResult(
                                status=VerificationStatus.FAILED,
                                truncation_status=trunc_status,
                                reason_code=fmt_code,
                                message=fmt_msg,
                                verifier_version=self.VERIFIER_VERSION,
                            )
                        verified_fmt = "json"
                        verified_json_valid = True
                        parsed_structured = parsed_obj
                    schema_err = _validate_value_against_schema(parsed_structured, schema_obj)
                    if schema_err is not None:
                        code = CompatibilityReasonCode.VERIFICATION_FAILED_SCHEMA
                        return ResponseVerificationResult(
                            status=VerificationStatus.FAILED,
                            truncation_status=trunc_status,
                            reason_code=code,
                            message=f"{code}: Response failed JSON Schema validation: {schema_err}",
                            verifier_version=self.VERIFIER_VERSION,
                        )
                    verified_schema_version = 1
                    verified_schema_digest = compute_canonical_schema_digest(schema_obj)
                    verified_constraints.append(f"schema_adherence={verified_schema_digest[:16]}")

        return ResponseVerificationResult(
            status=VerificationStatus.VERIFIED,
            truncation_status=trunc_status,
            reason_code=CompatibilityReasonCode.VERIFICATION_PASSED,
            message="Response artifact passed all deterministic verification checks.",
            verifier_version=self.VERIFIER_VERSION,
            verified_output_format=verified_fmt,
            verified_json_valid=verified_json_valid,
            verified_schema_version=verified_schema_version,
            verified_schema_digest=verified_schema_digest,
            verified_constraints=tuple(sorted(set(verified_constraints))),
            unverified_constraints=(),
            evidence_details={
                "truncation_status": trunc_status.value,
                "verified_output_format": verified_fmt,
                "verified_json_valid": verified_json_valid,
            },
        )

    def _check_truncation(
        self,
        *,
        stripped: str,
        expected_format: Optional[str],
        finish_reason: Optional[str],
        completion_tokens: int,
        request_max_tokens: Optional[int],
    ) -> Tuple[TruncationStatus, CompatibilityReasonCode, str]:
        norm_reason = finish_reason.strip().lower() if isinstance(finish_reason, str) and finish_reason.strip() else None

        if norm_reason in _TRUNCATED_FINISH_REASONS:
            code = CompatibilityReasonCode.VERIFICATION_FAILED_TRUNCATED
            return (
                TruncationStatus.TRUNCATED,
                code,
                f"{code}: Upstream finished with truncated reason '{norm_reason}'.",
            )

        # Unbalanced markdown fences strongly indicate mid-block truncation
        if stripped.count("```") % 2 != 0:
            code = CompatibilityReasonCode.VERIFICATION_FAILED_TRUNCATED
            return (
                TruncationStatus.TRUNCATED,
                code,
                f"{code}: Response contains unclosed markdown code fence.",
            )

        # Heuristic cut-off detection when response hit or matched request max_tokens without clean stop
        if (
            request_max_tokens is not None
            and request_max_tokens > 0
            and completion_tokens >= request_max_tokens
            and norm_reason not in _COMPLETE_FINISH_REASONS
        ):
            code = CompatibilityReasonCode.VERIFICATION_FAILED_TRUNCATED
            return (
                TruncationStatus.TRUNCATED,
                code,
                f"{code}: completion_tokens ({completion_tokens}) reached max_tokens ({request_max_tokens}) without stop finish_reason.",
            )

        # Expected JSON with incomplete brace/bracket closure
        if expected_format == "json" and (stripped.startswith("{") or stripped.startswith("[")):
            open_braces = stripped.count("{") - stripped.count("}")
            open_brackets = stripped.count("[") - stripped.count("]")
            if open_braces > 0 or open_brackets > 0:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_TRUNCATED
                return (
                    TruncationStatus.TRUNCATED,
                    code,
                    f"{code}: JSON response appears syntactically truncated (unclosed delimiters).",
                )

        if norm_reason in _COMPLETE_FINISH_REASONS:
            return TruncationStatus.COMPLETE, CompatibilityReasonCode.VERIFICATION_PASSED, "Complete response."

        if norm_reason is not None:
            code = CompatibilityReasonCode.VERIFICATION_UNCERTAIN_TRUNCATION
            return (
                TruncationStatus.UNKNOWN,
                code,
                f"{code}: Unrecognized finish_reason '{norm_reason}' cannot confirm completeness.",
            )

        return TruncationStatus.UNKNOWN, CompatibilityReasonCode.VERIFICATION_PASSED, "No truncation indicators observed."

    def _verify_output_format(
        self,
        stripped: str,
        fmt: str,
    ) -> Tuple[bool, CompatibilityReasonCode, str, Any]:
        if fmt == "json":
            if "```" in stripped or not (stripped.startswith("{") or stripped.startswith("[")):
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Expected raw JSON artifact without prose or markdown wrappers.", None
            try:
                parsed = json.loads(stripped, parse_constant=_reject_nonfinite_json)
            except Exception as exc:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_JSON
                return False, code, f"{code}: Invalid JSON syntax: {exc}", None
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid JSON.", parsed

        if fmt == "yaml":
            if "```" in stripped:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Expected raw YAML artifact without markdown fences.", None
            try:
                parsed = yaml.safe_load(stripped)
            except Exception as exc:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Invalid YAML syntax: {exc}", None
            if not isinstance(parsed, (dict, list)):
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: YAML output did not parse into a structured mapping or sequence.", None
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid YAML.", parsed

        if fmt == "xml":
            if "```" in stripped:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Expected raw XML artifact without markdown fences.", None
            upper = stripped.upper()
            if "<!DOCTYPE" in upper or "<!ENTITY" in upper:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: XML DTD/ENTITY declarations are rejected.", None
            try:
                root = ET.fromstring(stripped)
            except Exception as exc:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Invalid XML syntax: {exc}", None
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid XML.", root

        if fmt == "csv":
            if "```" in stripped:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Expected raw CSV artifact without markdown fences.", None
            try:
                rows = [r for r in csv.reader(io.StringIO(stripped)) if r]
            except Exception as exc:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Invalid CSV syntax: {exc}", None
            if not rows or len(rows[0]) < 2:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: CSV output must contain tabular delimited fields.", None
            widths = {len(r) for r in rows}
            if len(widths) != 1:
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: CSV rows have inconsistent column counts {sorted(widths)}.", None
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid CSV.", rows

        if fmt == "plain_text":
            if "```" in stripped or re.search(r"(?m)^#{1,6}\s+", stripped):
                code = CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT
                return False, code, f"{code}: Plain-text format rejects markdown headings or code fences.", None
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid plain text.", stripped

        if fmt == "markdown":
            return True, CompatibilityReasonCode.VERIFICATION_PASSED, "Valid markdown.", stripped

        code = CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT
        return False, code, f"{code}: Unsupported output format '{fmt}'.", None

    @staticmethod
    def _looks_like_code_only(stripped: str) -> bool:
        if _CONVERSATIONAL_PREAMBLE_RE.search(stripped):
            return False
        if "```" in stripped:
            # Ensure fences are closed and no non-empty prose exists outside fences
            parts = stripped.split("```")
            if len(parts) % 2 == 0:
                return False
            outside = [parts[i].strip() for i in range(0, len(parts), 2)]
            if any(chunk for chunk in outside):
                return False
        return True

    @staticmethod
    def _parse_positive_int(raw: str) -> Optional[int]:
        try:
            val = int(str(raw).strip())
            return val if val > 0 else None
        except (ValueError, TypeError):
            return None
