"""
Deterministic semantic cache policy and candidate evaluation engine (Stage 3.1).
Enforces query eligibility, technical/programming query classification,
credential detection, validated compatibility metadata, deterministic domain guardrails,
candidate similarity thresholds, and ambiguity rejection.
No LLM calls are made for eligibility, classification, or compatibility.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

from .compatibility import (
    CompatibilityExtractor,
    compare_compatibility_decision,
    extract_effective_user_query,
)
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityDecision,
    CompatibilityReasonCode,
    CompatibilityStatus,
    EligibilityResult,
    MetadataStatus,
    SUPPORTED_SCHEMA_VERSIONS,
    SemanticCompatibilityMetadata,
    TaskKind,
)
from ..vector_store.contracts import VectorSearchResult

if TYPE_CHECKING:
    from ..core.contracts import GenerationRequest

logger = logging.getLogger("intellicache.gateway.semantic_cache.policy")

# Regex patterns for detecting credentials, keys, tokens, and private material
_SENSITIVE_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"gsk_[a-zA-Z0-9_\-]{20,}", re.IGNORECASE),
    re.compile(r"gh[pousr]_[a-zA-Z0-9]{20,}", re.IGNORECASE),
    re.compile(r"xox[baprs]-[a-zA-Z0-9_\-]{10,}", re.IGNORECASE),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"bearer\s+[a-zA-Z0-9_\-\.]{25,}", re.IGNORECASE),
    re.compile(r"(?:password|passwd|pwd)\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"api[_\-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"secret[_\-]?key\s*[:=]\s*\S+", re.IGNORECASE),
    re.compile(r"(?:postgres|mysql|mongodb|redis):\/\/[^:\s]+:[^@\s]+@", re.IGNORECASE),
]

# Technical & programming keywords, languages, frameworks, and syntax indicators
_TECHNICAL_KEYWORDS = {
    # Languages & runtimes
    "python", "javascript", "typescript", "rust", "golang", "go", "java", "c++",
    "c#", "c", "sql", "html", "css", "bash", "shell", "sh", "zsh", "powershell",
    "php", "ruby", "swift", "kotlin", "scala", "r", "perl", "lua", "elixir",
    "dart", "haskell", "clojure", "erlang", "zig", "ocaml", "solidity", "groovy",
    # Frameworks, libraries & tools
    "react", "vue", "angular", "next.js", "nextjs", "fastapi", "flask", "django",
    "express", "node", "nodejs", "spring", "docker", "dockerfile", "kubernetes",
    "k8s", "git", "github", "postgres", "postgresql", "mysql", "sqlite", "redis",
    "linux", "ubuntu", "debian", "arch", "webpack", "vite", "vitest", "pytest",
    "pydantic", "sqlalchemy", "pandas", "numpy", "pytorch", "tensorflow", "prisma",
    "nestjs", "svelte", "laravel", "rails", "dom", "vdom",
    # Concepts & queries
    "algorithm", "function", "class", "method", "variable", "interface", "struct",
    "array", "list", "dict", "hashmap", "queue", "stack", "tree", "graph",
    "pointer", "reference", "recursion", "async", "await", "coroutine", "thread",
    "mutex", "lock", "concurrency", "deadlock", "race condition", "regex",
    "regular expression", "endpoint", "api", "rest", "graphql", "json", "yaml",
    "xml", "http", "https", "status code", "header", "payload", "jwt",
    # Error & debugging terms
    "error", "exception", "traceback", "stacktrace", "bug", "syntax error",
    "type error", "typeerror", "referenceerror", "nullpointer", "segfault",
    "segmentation fault", "undefined", "nan", "debug", "refactor", "optimize",
    "implementation", "implement", "unit test", "mock", "patch",
}

_CODE_SYNTAX_MARKERS = [
    "```",
    "`",
    "def ",
    "class ",
    "import ",
    "from ",
    "async def ",
    "const ",
    "let ",
    "var ",
    "function ",
    "SELECT ",
    "FROM ",
    "WHERE ",
    "INSERT INTO ",
    "UPDATE ",
    "DELETE FROM ",
    "CREATE TABLE ",
    "public static void ",
    "fn ",
    "pub fn ",
    "struct ",
    "impl ",
    "console.log",
    "print(",
    "printf(",
    "System.out.println",
    "return ",
]


class SemanticCachePolicy:
    """
    Evaluates request eligibility, deterministic metadata extraction, and candidate compatibility.
    Deterministic, zero external network calls.
    """

    def __init__(
        self,
        min_similarity: float = 0.92,
        min_margin: float = 0.03,
        top_k: int = 5,
        configured_model: str = "nomic-embed-text",
        extractor: Optional[CompatibilityExtractor] = None,
    ):
        self.min_similarity = float(min_similarity)
        self.min_margin = float(min_margin)
        self.top_k = int(top_k)
        self.configured_model = str(configured_model).strip()
        self.extractor = extractor or CompatibilityExtractor()
        self._last_evaluation_diagnostics: Optional[Dict[str, Any]] = None

    def check_eligibility(self, request: GenerationRequest) -> EligibilityResult:
        """
        Determines if an incoming GenerationRequest is eligible for semantic caching.
        Permits only single-turn user programming/technical queries without secrets or context dependencies,
        with established material domain requirements.
        """
        if request.stream:
            return EligibilityResult(False, "Streaming requests are not eligible for semantic caching.")

        if request.bypass_cache:
            return EligibilityResult(False, "Cache bypass explicitly requested.")

        if request.conversation_id and request.conversation_id.strip():
            return EligibilityResult(False, "Multi-turn conversation context or conversation ID present.")

        if not request.messages or len(request.messages) != 1:
            msg_count = len(request.messages) if request.messages else 0
            return EligibilityResult(
                False,
                f"Only single-turn user requests are eligible for semantic caching (received {msg_count} messages).",
            )

        single_msg = request.messages[0]
        if single_msg.role != "user":
            return EligibilityResult(
                False,
                f"Single turn must have role 'user' (received '{single_msg.role}').",
            )

        query_text = (single_msg.content or "").strip()
        if len(query_text) < 3:
            return EligibilityResult(False, "Query text is too short to establish semantic intent.")

        # Check for system prompt in metadata or payload
        if request.metadata.get("system_prompt") or any(m.role == "system" for m in request.messages):
            return EligibilityResult(False, "System prompts are not eligible for semantic caching.")

        # Check for credentials or secrets in query text
        for pattern in _SENSITIVE_PATTERNS:
            if pattern.search(query_text):
                return EligibilityResult(
                    False,
                    "Query appears to contain sensitive credentials, keys, or secrets.",
                )

        # Technical/programming query classification
        if not self._is_technical_query(query_text):
            return EligibilityResult(
                False,
                "Query does not match technical or programming criteria.",
            )

        # Stage 3.1: Deterministic requirement extraction and materiality validation
        try:
            req_meta = self.extractor.extract(request, self.configured_model)
        except Exception as exc:
            return EligibilityResult(
                False,
                f"Compatibility metadata extraction failed: {exc}",
            )

        # 1. Task kind must be known
        if req_meta.task_kind.status == MetadataStatus.UNKNOWN:
            return EligibilityResult(
                False,
                "Query task kind could not be determined deterministically.",
            )

        # 2. Material dimension requirements:
        # A code-generation request without an established programming language is ineligible
        if req_meta.task_kind.value == TaskKind.CODE_GENERATION:
            lang_dim = req_meta.dimensions.get("language")
            if not lang_dim or lang_dim.status != MetadataStatus.KNOWN:
                return EligibilityResult(
                    False,
                    "Code-generation request without an established programming language is ineligible for semantic caching.",
                )

        # 2b. Freshness and single-turn context boundaries
        ctx_dim = req_meta.dimensions.get("context_dependency")
        if ctx_dim and ctx_dim.status != MetadataStatus.NOT_APPLICABLE:
            return EligibilityResult(
                False,
                "Single-turn request contains unresolved prior-context dependencies.",
            )

        fresh_dim = req_meta.dimensions.get("freshness")
        if fresh_dim and fresh_dim.status != MetadataStatus.NOT_APPLICABLE:
            return EligibilityResult(
                False,
                "Time-sensitive or freshness-dependent requests are excluded from unverified semantic caching.",
            )

        # 3. Check for contradictory or ambiguous dimensions
        for dim_name, dim_val in req_meta.dimensions.items():
            if dim_val.status == MetadataStatus.UNKNOWN:
                return EligibilityResult(
                    False,
                    f"Query contains ambiguous or contradictory technical requirements for '{dim_name}'.",
                )

        return EligibilityResult(True, "Eligible for semantic caching.", metadata=req_meta)

    def _is_technical_query(self, text: str) -> bool:
        """Determines if query contains technical markers, keywords, or code structures."""
        # Check code markers
        for marker in _CODE_SYNTAX_MARKERS:
            if marker in text:
                return True

        # Tokenize words for keyword matching
        words = set(re.findall(r"[a-zA-Z0-9_#\+\.\-]+", text.lower()))
        if words.intersection(_TECHNICAL_KEYWORDS):
            return True

        # Common programming question structure ("how to ...", "how do i ...", "write a function ...")
        lower_text = text.lower()
        if any(lower_text.startswith(prefix) for prefix in ("how to ", "how do i ", "write a ", "implement ", "explain ")):
            if any(term in lower_text for term in ("code", "script", "program", "query", "loop", "error", "file", "test", "data")):
                return True

        return False

    def validate_candidate(
        self,
        candidate: VectorSearchResult,
        request: GenerationRequest,
        request_metadata: Optional[SemanticCompatibilityMetadata] = None,
    ) -> CompatibilityDecision:
        """
        Evaluates candidate compatibility and returns an explicit tri-state CompatibilityDecision.
        Only CompatibilityStatus.COMPATIBLE authorizes downstream similarity/ambiguity checks.
        Both INCOMPATIBLE and UNCERTAIN fail closed.
        """
        if not candidate.cached_response or not candidate.cached_response.strip():
            code = CompatibilityReasonCode.NO_ASSOCIATED_RESPONSE
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Candidate has no associated active exact-cache response.",
                dimension="response",
            )

        if candidate.cache_namespace != request.namespace:
            code = CompatibilityReasonCode.NAMESPACE_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Namespace mismatch: candidate '{candidate.cache_namespace}' != request '{request.namespace}'.",
                dimension="namespace",
            )

        if candidate.model != self.configured_model:
            code = CompatibilityReasonCode.EMBEDDING_MODEL_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Embedding model mismatch: candidate '{candidate.model}' != configured '{self.configured_model}'.",
                dimension="embedding_contract",
            )

        meta_dict = candidate.metadata or {}
        if not meta_dict or "schema_version" not in meta_dict:
            code = CompatibilityReasonCode.LEGACY_METADATA_MISSING
            msg = (
                f"{code}: Candidate is missing semantic compatibility metadata."
                if not meta_dict
                else f"{code}: Candidate lacks versioned semantic metadata (legacy record)."
            )
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=msg,
                dimension="schema_version",
            )

        if meta_dict["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            code = CompatibilityReasonCode.UNSUPPORTED_SCHEMA_VERSION
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Unsupported metadata schema version {meta_dict['schema_version']}.",
                dimension="schema_version",
                evidence={"schema_version": meta_dict.get("schema_version")},
            )

        try:
            cand_meta = SemanticCompatibilityMetadata.from_dict(meta_dict)
        except Exception as exc:
            code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Candidate metadata malformed: {exc}.",
                dimension="metadata",
            )

        gen_cand = cand_meta.generation_contract
        if gen_cand.provider != request.provider:
            code = CompatibilityReasonCode.GENERATION_PROVIDER_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Provider mismatch: candidate '{gen_cand.provider}' != request '{request.provider}'.",
                dimension="generation_provider",
                evidence={"request": request.provider, "candidate": gen_cand.provider},
            )

        if gen_cand.model != request.model:
            code = CompatibilityReasonCode.GENERATION_MODEL_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Model mismatch: candidate '{gen_cand.model}' != request '{request.model}'.",
                dimension="generation_model",
                evidence={"request": request.model, "candidate": gen_cand.model},
            )

        try:
            if abs(float(gen_cand.temperature) - float(request.temperature)) > 1e-4:
                code = CompatibilityReasonCode.GENERATION_TEMPERATURE_MISMATCH
                return CompatibilityDecision(
                    status=CompatibilityStatus.INCOMPATIBLE,
                    reason_code=code,
                    message=f"{code}: Temperature mismatch: candidate {gen_cand.temperature} != request {request.temperature}.",
                    dimension="generation_temperature",
                    evidence={"request": request.temperature, "candidate": gen_cand.temperature},
                )
        except (ValueError, TypeError):
            code = CompatibilityReasonCode.GENERATION_TEMPERATURE_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Candidate has invalid temperature configuration.",
                dimension="generation_temperature",
            )

        if gen_cand.max_tokens != request.max_tokens:
            code = CompatibilityReasonCode.GENERATION_MAX_TOKENS_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: max_tokens mismatch: candidate {gen_cand.max_tokens} != request {request.max_tokens}.",
                dimension="generation_max_tokens",
                evidence={"request": request.max_tokens, "candidate": gen_cand.max_tokens},
            )

        if request_metadata is not None:
            req_meta = request_metadata
        else:
            try:
                req_meta = self.extractor.extract(request, self.configured_model)
            except Exception as exc:
                code = CompatibilityReasonCode.INVALID_METADATA_FORMAT
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=code,
                    message=f"{code}: Failed to extract request metadata: {exc}.",
                    dimension="request_metadata",
                )

        return compare_compatibility_decision(req_meta, cand_meta, raw_candidate_metadata=meta_dict)

    def is_candidate_compatible(
        self,
        candidate: VectorSearchResult,
        request: GenerationRequest,
        request_metadata: Optional[SemanticCompatibilityMetadata] = None,
    ) -> Tuple[bool, str]:
        """
        Backwards-compatible boolean adapter over validate_candidate().
        Fail-closed: only CompatibilityStatus.COMPATIBLE returns True; both
        INCOMPATIBLE and UNCERTAIN return False with diagnostic message.
        """
        decision = self.validate_candidate(candidate, request, request_metadata=request_metadata)
        return decision.is_compatible, decision.message

    def evaluate_candidates(
        self,
        candidates: List[VectorSearchResult],
        request: GenerationRequest,
        request_metadata: Optional[SemanticCompatibilityMetadata] = None,
    ) -> Tuple[Optional[VectorSearchResult], Optional[str]]:
        """
        Evaluates nearest-neighbour candidates in order of similarity.
        Rejects both INCOMPATIBLE and UNCERTAIN candidates while continuing evaluation across
        lower-ranked candidates, before applying similarity threshold and ambiguity margin rules.
        """
        if not candidates:
            self._last_evaluation_diagnostics = {
                "outcome": "empty_candidates",
                "decision_status": CompatibilityStatus.UNCERTAIN.value,
                "reason_code": CompatibilityReasonCode.ALL_CANDIDATES_REJECTED.value,
                "dimension": None,
                "decision_evidence": {},
                "candidate_decisions": [],
            }
            return None, "No vector candidates retrieved."

        req_meta = request_metadata
        if req_meta is None:
            try:
                req_meta = self.extractor.extract(request, self.configured_model)
            except Exception as exc:
                logger.debug("Failed to extract request metadata in evaluate_candidates: %s", exc)
                req_meta = None

        compatible_candidates: List[VectorSearchResult] = []
        rejected_decisions: List[Tuple[VectorSearchResult, CompatibilityDecision]] = []
        decision_records: List[Dict[str, Any]] = []

        for cand in candidates:
            decision = self.validate_candidate(cand, request, request_metadata=req_meta)
            decision_records.append({
                "fingerprint_prefix": (cand.fingerprint or "")[:12],
                "similarity": cand.similarity,
                "decision": decision.to_dict(),
            })
            if decision.is_compatible:
                compatible_candidates.append(cand)
            else:
                rejected_decisions.append((cand, decision))
                logger.debug(
                    "Candidate fingerprint='%s...' rejected (%s / %s): %s",
                    (cand.fingerprint or "")[:12],
                    decision.status.value,
                    decision.reason_code.value,
                    decision.message,
                )

        if not compatible_candidates:
            primary_decision = rejected_decisions[0][1] if rejected_decisions else CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=CompatibilityReasonCode.ALL_CANDIDATES_REJECTED,
                message=f"{CompatibilityReasonCode.ALL_CANDIDATES_REJECTED}: No candidates evaluated.",
            )
            self._last_evaluation_diagnostics = {
                "outcome": "compatibility_rejected",
                "decision_status": primary_decision.status.value,
                "reason_code": primary_decision.reason_code.value,
                "dimension": primary_decision.dimension,
                "decision_evidence": dict(primary_decision.evidence),
                "candidate_decisions": decision_records,
            }
            return (
                None,
                f"{CompatibilityReasonCode.ALL_CANDIDATES_REJECTED.value}: No compatible candidates satisfied generation and domain requirements. "
                f"Top rejection ({primary_decision.status.value}): {primary_decision.message}",
            )

        compatible_candidates.sort(key=lambda c: c.similarity, reverse=True)
        top_cand = compatible_candidates[0]

        if top_cand.similarity < self.min_similarity:
            self._last_evaluation_diagnostics = {
                "outcome": "below_threshold",
                "decision_status": CompatibilityStatus.COMPATIBLE.value,
                "reason_code": CompatibilityReasonCode.COMPATIBLE.value,
                "dimension": "similarity_threshold",
                "decision_evidence": {
                    "top_similarity": round(float(top_cand.similarity), 6),
                    "min_similarity": round(float(self.min_similarity), 6),
                },
                "candidate_decisions": decision_records,
            }
            return (
                None,
                f"Similarity below threshold: {top_cand.similarity:.4f} < {self.min_similarity:.4f}.",
            )

        top_response = (top_cand.cached_response or "").strip()
        for other in compatible_candidates[1:]:
            margin = top_cand.similarity - other.similarity
            if margin < self.min_margin:
                other_response = (other.cached_response or "").strip()
                if other_response != top_response:
                    self._last_evaluation_diagnostics = {
                        "outcome": "ambiguous",
                        "decision_status": CompatibilityStatus.UNCERTAIN.value,
                        "reason_code": CompatibilityReasonCode.AMBIGUOUS_CANDIDATES.value,
                        "dimension": "ambiguity_margin",
                        "decision_evidence": {
                            "margin": round(float(margin), 6),
                            "min_margin": round(float(self.min_margin), 6),
                        },
                        "candidate_decisions": decision_records,
                    }
                    return (
                        None,
                        f"Ambiguous candidate: nearest alternative candidate margin ({margin:.4f}) "
                        f"is below minimum margin ({self.min_margin:.4f}) with a different answer.",
                    )

        self._last_evaluation_diagnostics = {
            "outcome": "compatible",
            "decision_status": CompatibilityStatus.COMPATIBLE.value,
            "reason_code": CompatibilityReasonCode.COMPATIBLE.value,
            "dimension": None,
            "decision_evidence": {},
            "candidate_decisions": decision_records,
        }
        return top_cand, None
