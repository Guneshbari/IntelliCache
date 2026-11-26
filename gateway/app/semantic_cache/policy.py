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
    ContextClass,
    ContextCompatibilityContract,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
    EligibilityResult,
    MetadataStatus,
    SUPPORTED_SCHEMA_VERSIONS,
    SemanticCompatibilityMetadata,
    TaskKind,
)
from .context import (
    ContextAssessment,
    analyze_request_context,
    contains_sensitive_material,
    resolve_context_cache_scope,
)
from .evidence import EvidenceAuthority, get_default_evidence_authority
from ..privacy import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_UNKNOWN,
    matches_cache_scope,
    normalize_cache_visibility,
    normalize_principal_id,
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
        retrieval_budget: int = 25,
        evidence_authority: Optional[EvidenceAuthority] = None,
    ):
        self.min_similarity = float(min_similarity)
        self.min_margin = float(min_margin)
        self.top_k = int(top_k)
        self.retrieval_budget = max(self.top_k, min(50, int(retrieval_budget))) if int(retrieval_budget) <= 50 else max(self.top_k, int(retrieval_budget))
        self.configured_model = str(configured_model).strip()
        self.extractor = extractor or CompatibilityExtractor()
        self.evidence_authority = evidence_authority or get_default_evidence_authority()
        self._last_evaluation_diagnostics: Optional[Dict[str, Any]] = None

    def check_eligibility(self, request: GenerationRequest) -> EligibilityResult:
        """
        Determines if an incoming GenerationRequest is eligible for semantic caching.
        Enforces strict caller privacy scope plus single-turn user programming/technical rules.
        """
        req_vis = normalize_cache_visibility(getattr(request, "visibility", CACHE_VISIBILITY_PRIVATE))
        req_own = normalize_principal_id(getattr(request, "principal_id", None))
        if req_vis == CACHE_VISIBILITY_UNKNOWN:
            return EligibilityResult(
                False,
                f"{CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN.value}: Request cache visibility is missing or unknown.",
            )
        if req_vis == CACHE_VISIBILITY_PRIVATE and req_own is None:
            return EligibilityResult(
                False,
                f"{CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING.value}: Private cache scope requires a verified principal identity.",
            )

        if request.stream:
            return EligibilityResult(False, "Streaming requests are not eligible for semantic caching.")

        if request.bypass_cache:
            return EligibilityResult(False, "Cache bypass explicitly requested.")

        # Context-aware eligibility via Stage 3.6 ContextAssessment
        ctx_eval = analyze_request_context(request)

        if not request.messages:
            return EligibilityResult(
                False,
                "Only requests with at least one non-empty user message are eligible for semantic caching.",
                context_assessment=ctx_eval,
            )

        query_text = ctx_eval.effective_query
        if not query_text:
            return EligibilityResult(
                False,
                "Request must contain a non-empty user message for semantic caching.",
                context_assessment=ctx_eval,
            )

        if len(query_text) < 3:
            return EligibilityResult(
                False,
                "Query text is too short to establish semantic intent.",
                context_assessment=ctx_eval,
            )

        # Check all messages and system prompt metadata for credentials or secrets BEFORE caching/embedding
        all_msg_texts = [getattr(m, "content", "") or "" for m in request.messages]
        raw_sys = request.metadata.get("system_prompt") if isinstance(request.metadata, dict) else None
        if isinstance(raw_sys, str) and raw_sys.strip():
            all_msg_texts.append(raw_sys)
        for candidate_txt in all_msg_texts:
            if contains_sensitive_material(candidate_txt) or any(p.search(candidate_txt) for p in _SENSITIVE_PATTERNS):
                return EligibilityResult(
                    False,
                    "Query appears to contain sensitive credentials, keys, or secrets.",
                    context_assessment=ctx_eval,
                )

        # Fail closed when context is compacted, incomplete, or unverifiable (e.g. dangling follow-up)
        if ctx_eval.context_class == ContextClass.CONTEXT_UNCERTAIN:
            return EligibilityResult(
                False,
                f"{CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN.value}: "
                f"Conversation context is uncertain or incomplete ({ctx_eval.contract.classification_reason}).",
                context_assessment=ctx_eval,
            )

        # Technical/programming query classification:
        # For CONTEXT_INDEPENDENT, require effective query itself to be technical so irrelevant prior history
        # cannot make casual chatter eligible. For CONTEXT_DEPENDENT, include relevant prior context hints.
        if ctx_eval.context_class == ContextClass.CONTEXT_DEPENDENT:
            is_tech = self._is_technical_query(query_text) or (
                bool(ctx_eval.relevant_context_hint_text)
                and self._is_technical_query(ctx_eval.relevant_context_hint_text)
            )
        else:
            is_tech = self._is_technical_query(query_text)
        if not is_tech:
            return EligibilityResult(
                False,
                "Query does not match technical or programming criteria.",
                context_assessment=ctx_eval,
            )

        # Stage 3.1 + 3.6: Deterministic requirement and context-contract extraction
        try:
            req_meta = self.extractor.extract(
                request,
                self.configured_model,
                context_assessment=ctx_eval,
            )
        except Exception as exc:
            return EligibilityResult(
                False,
                f"Compatibility metadata extraction failed: {exc}",
                context_assessment=ctx_eval,
            )

        # 1. Task kind must be known
        if req_meta.task_kind.status == MetadataStatus.UNKNOWN:
            return EligibilityResult(
                False,
                "Query task kind could not be determined deterministically.",
                context_assessment=ctx_eval,
            )

        # 2. Material dimension requirements:
        # A code-generation request without an established programming language is ineligible
        if req_meta.task_kind.value == TaskKind.CODE_GENERATION:
            lang_dim = req_meta.dimensions.get("language")
            if not lang_dim or lang_dim.status != MetadataStatus.KNOWN:
                return EligibilityResult(
                    False,
                    "Code-generation request without an established programming language is ineligible for semantic caching.",
                    context_assessment=ctx_eval,
                )

        # 2b. Freshness and context boundaries
        ctx_dim = req_meta.dimensions.get("context_dependency")
        if ctx_eval.context_class == ContextClass.CONTEXT_DEPENDENT:
            if not req_meta.context_contract or not req_meta.context_contract.context_fingerprint:
                return EligibilityResult(
                    False,
                    "Context-dependent request is missing a deterministic context fingerprint.",
                    context_assessment=ctx_eval,
                )
        elif ctx_dim and ctx_dim.status != MetadataStatus.NOT_APPLICABLE:
            return EligibilityResult(
                False,
                "Single-turn request contains unresolved prior-context dependencies.",
                context_assessment=ctx_eval,
            )

        fresh_dim = req_meta.dimensions.get("freshness")
        if fresh_dim and fresh_dim.status != MetadataStatus.NOT_APPLICABLE:
            return EligibilityResult(
                False,
                "Time-sensitive or freshness-dependent requests are excluded from unverified semantic caching.",
                context_assessment=ctx_eval,
            )

        # 3. Check for contradictory or ambiguous dimensions
        for dim_name, dim_val in req_meta.dimensions.items():
            if dim_val.status == MetadataStatus.UNKNOWN:
                return EligibilityResult(
                    False,
                    f"Query contains ambiguous or contradictory technical requirements for '{dim_name}'.",
                    context_assessment=ctx_eval,
                )

        return EligibilityResult(
            True,
            "Eligible for semantic caching.",
            metadata=req_meta,
            context_assessment=ctx_eval,
        )

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
        Enforces privacy scope isolation first, then technical compatibility and HMAC evidence binding.
        Only CompatibilityStatus.COMPATIBLE authorizes downstream similarity/ambiguity checks.
        Both INCOMPATIBLE and UNCERTAIN fail closed.
        """
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

        req_ctx = analyze_request_context(request)
        scope = resolve_context_cache_scope(request, req_ctx, "")
        req_vis = scope.effective_visibility
        req_own = scope.effective_owner
        allow_shared = scope.allow_shared_reads
        if req_vis == CACHE_VISIBILITY_UNKNOWN:
            code = CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Request cache visibility is missing or unknown (fail-closed).",
                dimension="privacy_scope",
            )
        if req_vis == CACHE_VISIBILITY_PRIVATE and req_own is None:
            code = CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Private request requires verified principal identity (fail-closed).",
                dimension="privacy_scope",
            )

        cand_vis = normalize_cache_visibility(getattr(candidate, "visibility", CACHE_VISIBILITY_UNKNOWN))
        cand_own = normalize_principal_id(getattr(candidate, "owner_principal_id", None))
        if cand_vis == CACHE_VISIBILITY_UNKNOWN:
            code = CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN
            return CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=code,
                message=f"{code}: Candidate visibility is missing or unknown (fail-closed).",
                dimension="privacy_scope",
            )

        if not matches_cache_scope(
            cand_vis,
            cand_own,
            req_vis,
            req_own,
            allow_shared_reads=allow_shared,
        ):
            if cand_vis == CACHE_VISIBILITY_PRIVATE and req_vis == CACHE_VISIBILITY_PRIVATE:
                code = CompatibilityReasonCode.PRINCIPAL_ISOLATION_MISMATCH
            else:
                code = CompatibilityReasonCode.VISIBILITY_MISMATCH
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Candidate privacy scope is isolated from caller request.",
                dimension="privacy_scope",
            )

        if not candidate.cached_response or not candidate.cached_response.strip():
            code = CompatibilityReasonCode.NO_ASSOCIATED_RESPONSE
            return CompatibilityDecision(
                status=CompatibilityStatus.INCOMPATIBLE,
                reason_code=code,
                message=f"{code}: Candidate has no associated active exact-cache response.",
                dimension="response",
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

        if "context_contract" in meta_dict and meta_dict["context_contract"] is not None:
            try:
                ContextCompatibilityContract.from_dict(meta_dict["context_contract"])
            except Exception as exc:
                code = CompatibilityReasonCode.CONTEXT_METADATA_INVALID
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=code,
                    message=f"{code}: Candidate context_contract malformed: {exc}.",
                    dimension="context_dependency",
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

        decision = compare_compatibility_decision(req_meta, cand_meta, raw_candidate_metadata=meta_dict)
        if decision.is_incompatible:
            return decision

        # Cryptographic provenance and binding verification for response_verified candidates
        cand_ev = cand_meta.compatibility_evidence
        if cand_ev is not None and cand_ev.evidence_tier == EVIDENCE_TIER_RESPONSE_VERIFIED:
            bound_ok, bound_code, bound_msg = self.evidence_authority.verify_evidence_binding(
                cand_ev,
                cache_namespace=candidate.cache_namespace,
                fingerprint=candidate.fingerprint,
                response_text=candidate.cached_response or "",
                metadata=cand_meta,
                expected_visibility=cand_vis,
                expected_principal_id=cand_own,
                allow_shared_reads=False,
            )
            if not bound_ok:
                return CompatibilityDecision(
                    status=CompatibilityStatus.UNCERTAIN,
                    reason_code=bound_code,
                    message=bound_msg,
                    dimension="compatibility_evidence",
                    evidence={
                        "source": cand_ev.source,
                        "evidence_tier": cand_ev.evidence_tier,
                        "has_bound_signature": cand_ev.has_bound_signature(),
                    },
                )

        return decision

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
        import time as _time
        compat_start = _time.perf_counter()

        def _base_diag_fields(ctx_ms: float, compat_ms: float) -> Dict[str, Any]:
            return {
                "context_validation_latency_ms": round(ctx_ms, 4),
                "compatibility_validation_latency_ms": round(compat_ms, 4),
                "context_compatibility_matches": 0,
                "context_compatibility_rejections": 0,
                "context_metadata_missing": 0,
                "context_fingerprint_mismatches": 0,
                "private_scope_rejections": 0,
                "tenant_shared_authorization_rejections": 0,
            }

        if not candidates:
            compat_ms = (_time.perf_counter() - compat_start) * 1000.0
            self._last_evaluation_diagnostics = {
                "outcome": "empty_candidates",
                "decision_status": CompatibilityStatus.UNCERTAIN.value,
                "reason_code": CompatibilityReasonCode.ALL_CANDIDATES_REJECTED.value,
                "dimension": None,
                "decision_evidence": {},
                "candidate_decisions": [],
                "candidates_retrieved": 0,
                "candidates_compatible": 0,
                "candidates_rejected": 0,
                "candidates_incompatible": 0,
                "candidates_uncertain": 0,
                "candidates_legacy": 0,
                "all_candidates_rejected": False,
                "retrieval_budget_exhausted": False,
                "retrieval_budget": self.retrieval_budget,
                **_base_diag_fields(0.0, compat_ms),
            }
            return None, "No vector candidates retrieved."

        ctx_start = _time.perf_counter()
        req_meta = request_metadata
        if req_meta is None:
            try:
                req_meta = self.extractor.extract(request, self.configured_model)
            except Exception as exc:
                logger.debug("Failed to extract request metadata in evaluate_candidates: %s", exc)
                req_meta = None
        ctx_elapsed_ms = (_time.perf_counter() - ctx_start) * 1000.0

        compatible_candidates: List[VectorSearchResult] = []
        rejected_decisions: List[Tuple[VectorSearchResult, CompatibilityDecision]] = []
        decision_records: List[Dict[str, Any]] = []
        incompatible_count = 0
        uncertain_count = 0
        legacy_count = 0
        context_matches = 0
        context_rejections = 0
        context_missing = 0
        context_fp_mismatch = 0
        private_scope_rejections = 0
        tenant_shared_auth_rejections = 0

        _ctx_rejection_codes = {
            CompatibilityReasonCode.CONTEXT_CLASS_MISMATCH.value,
            CompatibilityReasonCode.CONTEXT_FINGERPRINT_MISMATCH.value,
            CompatibilityReasonCode.CONTEXT_METADATA_MISSING.value,
            CompatibilityReasonCode.CONTEXT_METADATA_INVALID.value,
            CompatibilityReasonCode.CONTEXT_DEPENDENCY_UNRESOLVED.value,
        }

        for cand in candidates:
            raw_sv = (cand.metadata or {}).get("schema_version")
            if raw_sv is None or raw_sv == 2:
                legacy_count += 1
            cand_ctx_t0 = _time.perf_counter()
            decision = self.validate_candidate(cand, request, request_metadata=req_meta)
            cand_val_ms = (_time.perf_counter() - cand_ctx_t0) * 1000.0
            if decision.dimension == "context_dependency" or decision.is_compatible:
                ctx_elapsed_ms += cand_val_ms
            cand_src_digest = (cand.metadata or {}).get("source_conversation_digest")
            decision_records.append({
                "fingerprint_prefix": (cand.fingerprint or "")[:12],
                "similarity": cand.similarity,
                "source_conversation_digest": cand_src_digest,
                "decision": decision.to_dict(),
            })
            if decision.is_compatible:
                compatible_candidates.append(cand)
                context_matches += 1
            else:
                rejected_decisions.append((cand, decision))
                if decision.is_incompatible:
                    incompatible_count += 1
                elif decision.is_uncertain:
                    uncertain_count += 1
                r_code = decision.reason_code.value
                if r_code in _ctx_rejection_codes:
                    context_rejections += 1
                    if r_code == CompatibilityReasonCode.CONTEXT_METADATA_MISSING.value:
                        context_missing += 1
                    elif r_code == CompatibilityReasonCode.CONTEXT_FINGERPRINT_MISMATCH.value:
                        context_fp_mismatch += 1
                if r_code in (
                    CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING.value,
                    CompatibilityReasonCode.PRINCIPAL_ISOLATION_MISMATCH.value,
                ):
                    private_scope_rejections += 1
                elif r_code in (
                    CompatibilityReasonCode.VISIBILITY_MISMATCH.value,
                    CompatibilityReasonCode.TENANT_SHARED_AUTH_REJECTED.value,
                ):
                    tenant_shared_auth_rejections += 1
                logger.debug(
                    "Candidate fingerprint='%s...' rejected (%s / %s): %s",
                    (cand.fingerprint or "")[:12],
                    decision.status.value,
                    decision.reason_code.value,
                    decision.message,
                )

        def _stage36_diag(compat_ms: float) -> Dict[str, Any]:
            return {
                "context_validation_latency_ms": round(ctx_elapsed_ms, 4),
                "compatibility_validation_latency_ms": round(compat_ms, 4),
                "context_compatibility_matches": context_matches,
                "context_compatibility_rejections": context_rejections,
                "context_metadata_missing": context_missing,
                "context_fingerprint_mismatches": context_fp_mismatch,
                "private_scope_rejections": private_scope_rejections,
                "tenant_shared_authorization_rejections": tenant_shared_auth_rejections,
            }

        if not compatible_candidates:
            compat_ms = (_time.perf_counter() - compat_start) * 1000.0
            primary_decision = rejected_decisions[0][1] if rejected_decisions else CompatibilityDecision(
                status=CompatibilityStatus.UNCERTAIN,
                reason_code=CompatibilityReasonCode.ALL_CANDIDATES_REJECTED,
                message=f"{CompatibilityReasonCode.ALL_CANDIDATES_REJECTED}: No candidates evaluated.",
            )
            budget_exhausted = len(candidates) >= self.retrieval_budget
            self._last_evaluation_diagnostics = {
                "outcome": "compatibility_rejected",
                "decision_status": primary_decision.status.value,
                "reason_code": primary_decision.reason_code.value,
                "dimension": primary_decision.dimension,
                "decision_evidence": dict(primary_decision.evidence),
                "candidate_decisions": decision_records,
                "candidates_retrieved": len(candidates),
                "candidates_compatible": 0,
                "candidates_rejected": len(rejected_decisions),
                "candidates_incompatible": incompatible_count,
                "candidates_uncertain": uncertain_count,
                "candidates_legacy": legacy_count,
                "all_candidates_rejected": True,
                "retrieval_budget_exhausted": budget_exhausted,
                "retrieval_budget": self.retrieval_budget,
                **_stage36_diag(compat_ms),
            }
            return (
                None,
                f"{CompatibilityReasonCode.ALL_CANDIDATES_REJECTED.value}: No compatible candidates satisfied generation and domain requirements. "
                f"Top rejection ({primary_decision.status.value}): {primary_decision.message}",
            )

        compatible_candidates.sort(key=lambda c: c.similarity, reverse=True)
        # Apply final semantic pool limit after compatibility filtering so starvable legacy/incompatible
        # entries do not crowd out compatible candidates within retrieval_budget.
        compatible_pool = compatible_candidates[: max(1, self.top_k)]
        top_cand = compatible_pool[0]

        if top_cand.similarity < self.min_similarity:
            compat_ms = (_time.perf_counter() - compat_start) * 1000.0
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
                "candidates_retrieved": len(candidates),
                "candidates_compatible": len(compatible_candidates),
                "candidates_rejected": len(rejected_decisions),
                "candidates_incompatible": incompatible_count,
                "candidates_uncertain": uncertain_count,
                "candidates_legacy": legacy_count,
                "all_candidates_rejected": False,
                "retrieval_budget_exhausted": False,
                "retrieval_budget": self.retrieval_budget,
                **_stage36_diag(compat_ms),
            }
            return (
                None,
                f"Similarity below threshold: {top_cand.similarity:.4f} < {self.min_similarity:.4f}.",
            )

        top_response = (top_cand.cached_response or "").strip()
        for other in compatible_pool[1:]:
            margin = top_cand.similarity - other.similarity
            if margin < self.min_margin:
                other_response = (other.cached_response or "").strip()
                if other_response != top_response:
                    compat_ms = (_time.perf_counter() - compat_start) * 1000.0
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
                        "candidates_retrieved": len(candidates),
                        "candidates_compatible": len(compatible_candidates),
                        "candidates_rejected": len(rejected_decisions),
                        "candidates_incompatible": incompatible_count,
                        "candidates_uncertain": uncertain_count,
                        "candidates_legacy": legacy_count,
                        "all_candidates_rejected": False,
                        "retrieval_budget_exhausted": False,
                        "retrieval_budget": self.retrieval_budget,
                        **_stage36_diag(compat_ms),
                    }
                    return (
                        None,
                        f"Ambiguous candidate: nearest alternative candidate margin ({margin:.4f}) "
                        f"is below minimum margin ({self.min_margin:.4f}) with a different answer.",
                    )

        compat_ms = (_time.perf_counter() - compat_start) * 1000.0
        self._last_evaluation_diagnostics = {
            "outcome": "compatible",
            "decision_status": CompatibilityStatus.COMPATIBLE.value,
            "reason_code": CompatibilityReasonCode.COMPATIBLE.value,
            "dimension": None,
            "decision_evidence": {},
            "candidate_decisions": decision_records,
            "candidates_retrieved": len(candidates),
            "candidates_compatible": len(compatible_candidates),
            "candidates_rejected": len(rejected_decisions),
            "candidates_incompatible": incompatible_count,
            "candidates_uncertain": uncertain_count,
            "candidates_legacy": legacy_count,
            "all_candidates_rejected": False,
            "retrieval_budget_exhausted": False,
            "retrieval_budget": self.retrieval_budget,
            **_stage36_diag(compat_ms),
        }
        return top_cand, None
