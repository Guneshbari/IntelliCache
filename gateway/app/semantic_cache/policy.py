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

from .compatibility import CompatibilityExtractor, compare_compatibility
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityReasonCode,
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
    # Frameworks & tools
    "react", "vue", "angular", "next.js", "nextjs", "fastapi", "flask", "django",
    "express", "node", "nodejs", "spring", "docker", "dockerfile", "kubernetes",
    "k8s", "git", "github", "postgres", "postgresql", "mysql", "sqlite", "redis",
    "linux", "ubuntu", "debian", "arch", "webpack", "vite", "vitest", "pytest",
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

    def is_candidate_compatible(
        self,
        candidate: VectorSearchResult,
        request: GenerationRequest,
        request_metadata: Optional[SemanticCompatibilityMetadata] = None,
    ) -> Tuple[bool, str]:
        """
        Verifies that a nearest-neighbour vector candidate matches all required
        compatibility criteria: namespace, embedding model, LLM provider, LLM model,
        temperature, max_tokens, validated metadata schema, and all domain dimensions.
        """
        if not candidate.cached_response or not candidate.cached_response.strip():
            return False, f"{CompatibilityReasonCode.NO_ASSOCIATED_RESPONSE}: Candidate has no associated active exact-cache response."

        if candidate.cache_namespace != request.namespace:
            return False, f"{CompatibilityReasonCode.NAMESPACE_MISMATCH}: Namespace mismatch: candidate '{candidate.cache_namespace}' != request '{request.namespace}'."

        if candidate.model != self.configured_model:
            return False, f"{CompatibilityReasonCode.EMBEDDING_MODEL_MISMATCH}: Embedding model mismatch: candidate '{candidate.model}' != configured '{self.configured_model}'."

        meta_dict = candidate.metadata or {}
        if not meta_dict:
            return False, f"{CompatibilityReasonCode.LEGACY_METADATA_MISSING}: Candidate is missing semantic compatibility metadata."

        if "schema_version" not in meta_dict:
            return False, f"{CompatibilityReasonCode.LEGACY_METADATA_MISSING}: Candidate lacks versioned semantic metadata (legacy record)."

        if meta_dict["schema_version"] not in SUPPORTED_SCHEMA_VERSIONS:
            return False, f"{CompatibilityReasonCode.UNSUPPORTED_SCHEMA_VERSION}: Unsupported metadata schema version {meta_dict['schema_version']}."

        try:
            cand_meta = SemanticCompatibilityMetadata.from_dict(meta_dict)
        except Exception as exc:
            return False, f"{CompatibilityReasonCode.INVALID_METADATA_FORMAT}: Candidate metadata malformed: {exc}."

        # Generation contract validation
        gen_cand = cand_meta.generation_contract
        if gen_cand.provider != request.provider:
            return False, f"{CompatibilityReasonCode.GENERATION_PROVIDER_MISMATCH}: Provider mismatch: candidate '{gen_cand.provider}' != request '{request.provider}'."

        if gen_cand.model != request.model:
            return False, f"{CompatibilityReasonCode.GENERATION_MODEL_MISMATCH}: Model mismatch: candidate '{gen_cand.model}' != request '{request.model}'."

        try:
            if abs(float(gen_cand.temperature) - float(request.temperature)) > 1e-4:
                return False, f"{CompatibilityReasonCode.GENERATION_TEMPERATURE_MISMATCH}: Temperature mismatch: candidate {gen_cand.temperature} != request {request.temperature}."
        except (ValueError, TypeError):
            return False, f"{CompatibilityReasonCode.GENERATION_TEMPERATURE_MISMATCH}: Candidate has invalid temperature configuration."

        if gen_cand.max_tokens != request.max_tokens:
            return False, f"{CompatibilityReasonCode.GENERATION_MAX_TOKENS_MISMATCH}: max_tokens mismatch: candidate {gen_cand.max_tokens} != request {request.max_tokens}."

        # Extract incoming request metadata once if not pre-extracted
        if request_metadata is not None:
            req_meta = request_metadata
        else:
            try:
                req_meta = self.extractor.extract(request, self.configured_model)
            except Exception as exc:
                return False, f"{CompatibilityReasonCode.INVALID_METADATA_FORMAT}: Failed to extract request metadata: {exc}."

        # Deterministic domain compatibility comparison
        is_compat, reason_code, message = compare_compatibility(req_meta, cand_meta)
        if not is_compat:
            return False, message

        return True, f"{CompatibilityReasonCode.COMPATIBLE}: Candidate satisfied all generation and domain requirements."

    def evaluate_candidates(
        self,
        candidates: List[VectorSearchResult],
        request: GenerationRequest,
        request_metadata: Optional[SemanticCompatibilityMetadata] = None,
    ) -> Tuple[Optional[VectorSearchResult], Optional[str]]:
        """
        Evaluates nearest-neighbour candidates in order of similarity.
        Applies mandatory compatibility checks before similarity thresholding, ensuring
        high dense similarity cannot override domain incompatibility.

        Returns:
            (winning_candidate, None) on successful match, or
            (None, rejection_reason) on rejection.
        """
        if not candidates:
            return None, "No vector candidates retrieved."

        # Extract request metadata once across all candidate comparisons if not provided
        req_meta = request_metadata
        if req_meta is None:
            try:
                req_meta = self.extractor.extract(request, self.configured_model)
            except Exception as exc:
                logger.debug("Failed to extract request metadata in evaluate_candidates: %s", exc)
                req_meta = None

        # 1. Filter candidates by deterministic compatibility
        compatible_candidates: List[VectorSearchResult] = []
        for cand in candidates:
            is_compat, reason = self.is_candidate_compatible(cand, request, request_metadata=req_meta)
            if is_compat:
                compatible_candidates.append(cand)
            else:
                logger.debug(
                    "Candidate fingerprint='%s...' rejected for compatibility: %s",
                    cand.fingerprint[:12],
                    reason,
                )

        if not compatible_candidates:
            return None, "No compatible candidates satisfied generation and domain requirements."

        # Ensure candidates are sorted descending by similarity / ascending by distance
        compatible_candidates.sort(key=lambda c: c.similarity, reverse=True)
        top_cand = compatible_candidates[0]

        # 2. Minimum similarity threshold check on top compatible candidate
        if top_cand.similarity < self.min_similarity:
            return (
                None,
                f"Similarity below threshold: {top_cand.similarity:.4f} < {self.min_similarity:.4f}.",
            )

        # 3. Ambiguity check: check top candidate against other compatible candidates
        top_response = (top_cand.cached_response or "").strip()
        for other in compatible_candidates[1:]:
            margin = top_cand.similarity - other.similarity
            if margin < self.min_margin:
                other_response = (other.cached_response or "").strip()
                if other_response != top_response:
                    return (
                        None,
                        f"Ambiguous candidate: nearest alternative candidate margin ({margin:.4f}) "
                        f"is below minimum margin ({self.min_margin:.4f}) with a different answer.",
                    )

        return top_cand, None
