"""
Semantic cache service implementation.
Coordinates query eligibility, embedding generation, vector retrieval, candidate evaluation,
safe failure fallbacks, and vector indexing.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import logging
import time
from typing import Any, Callable, Dict, Optional

from .compatibility import extract_effective_user_query
from .context import (
    ContextAssessment,
    analyze_request_context,
    compute_source_conversation_digest,
    resolve_context_cache_scope,
)
from .contracts import (
    AdmissionDecision,
    AdmissionStatus,
    CompatibilityReasonCode,
    CompatibilityStatus,
    ContextClass,
    EligibilityResult,
    SemanticCacheStats,
    SemanticCompatibilityMetadata,
    SemanticHit,
    VerificationStatus,
    compute_variant_key,
)
from .evidence import EvidenceAuthority, get_default_evidence_authority
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from .verification import ResponseArtifactVerifier
from ..core.contracts import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_UNKNOWN,
    GenerationRequest,
    GenerationResult,
    derive_scoped_exact_key,
    normalize_cache_visibility,
    normalize_principal_id,
)
from ..core.errors import sanitize_error_message
from ..core.fingerprinting import generate_fingerprint
from ..embeddings.contracts import EmbeddingTask
from ..embeddings.interfaces import EmbeddingProvider
from ..metrics import RuntimeMetricsCollector, get_metrics_collector
from ..vector_store.contracts import VectorRecord
from ..vector_store.interfaces import BaseVectorStore

logger = logging.getLogger("intellicache.gateway.semantic_cache")


class SemanticCacheService(BaseSemanticCache):
    """
    Service coordinating semantic cache lookup and indexing.
    Fails open to provider generation if any infrastructure component encounters an error.
    """

    def __init__(
        self,
        embedding_provider: EmbeddingProvider,
        vector_store: BaseVectorStore,
        policy: SemanticCachePolicy,
        default_namespace: str = "default",
        metrics_collector: Optional[RuntimeMetricsCollector] = None,
        diagnostics_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
        verifier: Optional[ResponseArtifactVerifier] = None,
        evidence_authority: Optional[EvidenceAuthority] = None,
        sql_context_prefilter: bool = False,
    ):
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.policy = policy
        self.default_namespace = default_namespace
        self.stats = SemanticCacheStats()
        self.metrics_collector = metrics_collector
        self.diagnostics_hook = diagnostics_hook
        self.verifier = verifier or ResponseArtifactVerifier()
        self.evidence_authority = (
            evidence_authority
            or getattr(policy, "evidence_authority", None)
            or get_default_evidence_authority()
        )
        self.sql_context_prefilter = bool(sql_context_prefilter)
        self.last_admission_decision: Optional[AdmissionDecision] = None

    def _get_collector(self) -> RuntimeMetricsCollector:
        return self.metrics_collector or get_metrics_collector()

    def _emit_diagnostics(
        self,
        data: Dict[str, Any],
        hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> None:
        effective_hook = hook or self.diagnostics_hook
        if effective_hook:
            try:
                effective_hook(data)
            except Exception as hook_exc:
                logger.warning("Diagnostics hook failed: %s", hook_exc)

    def is_eligible(self, request: GenerationRequest) -> EligibilityResult:
        """Evaluates whether the request is eligible for semantic caching."""
        res = self.policy.check_eligibility(request)
        ns = request.namespace or self.default_namespace
        self._get_collector().record_semantic_eligibility_check(ns, eligible=res.eligible)
        return res

    async def lookup(
        self,
        request: GenerationRequest,
        diagnostics_hook: Optional[Callable[[Dict[str, Any]], None]] = None,
    ) -> Optional[SemanticHit]:
        """
        Attempts semantic lookup for an eligible request.
        Fails open on embedding or vector storage errors, returning None.
        Never logs raw query text or embedding vectors.
        """
        collector = self._get_collector()
        ns = request.namespace or self.default_namespace
        lookup_start = time.perf_counter()
        query_text = extract_effective_user_query(request)
        query_hash = hashlib.sha256(query_text.encode("utf-8")).hexdigest()
        req_vis = normalize_cache_visibility(getattr(request, "visibility", CACHE_VISIBILITY_PRIVATE))
        req_own = normalize_principal_id(getattr(request, "principal_id", None))
        allow_shared = bool(getattr(request, "allow_shared_reads", False))

        ctx_cls_start = time.perf_counter()
        eligibility = self.is_eligible(request)
        elig_ctx_ms = (time.perf_counter() - ctx_cls_start) * 1000.0
        ctx_eval: ContextAssessment = getattr(eligibility, "context_assessment", None) or analyze_request_context(request)
        ctx_cls_val = ctx_eval.context_class.value
        if ctx_cls_val == ContextClass.CONTEXT_INDEPENDENT.value:
            self.stats.context_independent_requests += 1
        elif ctx_cls_val == ContextClass.CONTEXT_DEPENDENT.value:
            self.stats.context_dependent_requests += 1
        else:
            self.stats.context_uncertain_requests += 1
        collector.record_context_classification(ns, ctx_cls_val)

        if not eligibility.eligible:
            collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms)
            self.stats.eligibility_rejections += 1
            reason_msg = eligibility.reason or ""
            if reason_msg.startswith(CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN.value):
                self.stats.visibility_rejections += 1
                collector.record_visibility_rejection(ns)
                collector.record_semantic_rejection_reason(ns, CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN.value)
            elif reason_msg.startswith(CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING.value):
                self.stats.principal_isolation_rejections += 1
                self.stats.private_scope_rejections += 1
                collector.record_principal_isolation_rejection(ns)
                collector.record_private_scope_rejection(ns)
                collector.record_semantic_rejection_reason(ns, CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING.value)
            elif reason_msg.startswith(CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN.value):
                collector.record_semantic_rejection_reason(ns, CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN.value)
            elif ctx_eval.contains_sensitive_material and req_vis == "tenant_shared":
                self.stats.tenant_shared_authorization_rejections += 1
                collector.record_tenant_shared_authorization_rejection(ns)
            logger.debug(
                "Request ineligible for semantic caching: %s",
                eligibility.reason,
            )
            self._emit_diagnostics(
                {
                    "query_hash": query_hash,
                    "eligible": False,
                    "reason": eligibility.reason,
                    "metadata": None,
                    "context_class": ctx_cls_val,
                    "context_fingerprint": ctx_eval.context_fingerprint,
                    "embed_latency_ms": 0.0,
                    "search_latency_ms": 0.0,
                    "eval_latency_ms": 0.0,
                    "lookup_latency_ms": (time.perf_counter() - lookup_start) * 1000.0,
                    "candidates": [],
                    "winning_candidate": None,
                    "rejection_reason": eligibility.reason,
                },
                hook=diagnostics_hook,
            )
            return None

        self.stats.lookups += 1
        collector.record_semantic_lookup(ns)

        scope = resolve_context_cache_scope(request, ctx_eval, "")
        search_vis = scope.effective_visibility
        search_own = scope.effective_owner
        search_allow_shared = scope.allow_shared_reads
        use_sql_ctx_filter = bool(getattr(self, "sql_context_prefilter", False))

        # 1. Generate query embedding (effective user query only; never embed unrelated chatter or raw secrets)
        embed_start = time.perf_counter()
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.QUERY)
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
        except Exception as exc:
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
            collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms)
            self.stats.embedding_failures += 1
            collector.record_embedding_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            self._emit_diagnostics(
                {
                    "query_hash": query_hash,
                    "eligible": True,
                    "reason": eligibility.reason,
                    "metadata": eligibility.metadata,
                    "embed_latency_ms": embed_lat_ms,
                    "search_latency_ms": 0.0,
                    "eval_latency_ms": 0.0,
                    "lookup_latency_ms": sem_lat_ms,
                    "candidates": [],
                    "winning_candidate": None,
                    "rejection_reason": f"Embedding failure: {exc}",
                },
                hook=diagnostics_hook,
            )
            return None

        # Validate reported model identity against configured model (fail open)
        if embed_res.model != self.policy.configured_model:
            collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms)
            self.stats.embedding_failures += 1
            collector.record_embedding_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup model mismatch: expected '%s', got '%s'",
                self.policy.configured_model,
                embed_res.model,
            )
            self._emit_diagnostics(
                {
                    "query_hash": query_hash,
                    "eligible": True,
                    "reason": eligibility.reason,
                    "metadata": eligibility.metadata,
                    "embed_latency_ms": embed_lat_ms,
                    "search_latency_ms": 0.0,
                    "eval_latency_ms": 0.0,
                    "lookup_latency_ms": sem_lat_ms,
                    "candidates": [],
                    "winning_candidate": None,
                    "rejection_reason": f"Model mismatch: expected {self.policy.configured_model}, got {embed_res.model}",
                },
                hook=diagnostics_hook,
            )
            return None

        # 2. Search nearest neighbours in vector store with bounded retrieval budget
        search_start = time.perf_counter()
        retrieval_limit = max(
            int(getattr(self.policy, "top_k", 5)),
            int(getattr(self.policy, "retrieval_budget", getattr(self.policy, "top_k", 5))),
        )
        try:
            search_kwargs: Dict[str, Any] = {}
            if use_sql_ctx_filter:
                search_kwargs["context_class"] = ctx_cls_val
                if ctx_eval.context_fingerprint:
                    search_kwargs["context_fingerprint"] = ctx_eval.context_fingerprint
            candidates = await self.vector_store.search(
                query_vector=embed_res.vector,
                top_k=retrieval_limit,
                namespace=ns,
                model=self.policy.configured_model,
                visibility=search_vis,
                principal_id=search_own,
                allow_shared_reads=search_allow_shared,
                **search_kwargs,
            )
            self.stats.candidates_retrieved += len(candidates)
            collector.record_semantic_candidates_retrieved(ns, len(candidates))
            search_lat_ms = (time.perf_counter() - search_start) * 1000.0
            collector.record_latency(ns, "vector_search_latency_ms", search_lat_ms)
        except Exception as exc:
            search_lat_ms = (time.perf_counter() - search_start) * 1000.0
            collector.record_latency(ns, "vector_search_latency_ms", search_lat_ms)
            collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms)
            self.stats.vector_store_failures += 1
            collector.record_vector_store_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup failed during vector search (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            self._emit_diagnostics(
                {
                    "query_hash": query_hash,
                    "eligible": True,
                    "reason": eligibility.reason,
                    "metadata": eligibility.metadata,
                    "embed_latency_ms": embed_lat_ms,
                    "search_latency_ms": search_lat_ms,
                    "eval_latency_ms": 0.0,
                    "lookup_latency_ms": sem_lat_ms,
                    "candidates": [],
                    "winning_candidate": None,
                    "rejection_reason": f"Vector search failure: {exc}",
                },
                hook=diagnostics_hook,
            )
            return None

        # 3. Evaluate candidates against policy
        eval_start = time.perf_counter()
        if hasattr(self.policy, "_last_evaluation_diagnostics"):
            self.policy._last_evaluation_diagnostics = None
        try:
            winning_candidate, rejection_reason = self.policy.evaluate_candidates(
                candidates,
                request,
                request_metadata=eligibility.metadata,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            eval_lat_ms = (time.perf_counter() - eval_start) * 1000.0
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "candidate_policy_latency_ms", eval_lat_ms)
            collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms)
            collector.record_latency(ns, "compatibility_validation_latency_ms", eval_lat_ms)
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            self.stats.validator_failures += 1
            collector.record_validator_failure(ns)
            logger.warning(
                "Semantic candidate validation failed (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            self._emit_diagnostics(
                {
                    "query_hash": query_hash,
                    "eligible": True,
                    "reason": eligibility.reason,
                    "metadata": eligibility.metadata,
                    "embed_latency_ms": embed_lat_ms,
                    "search_latency_ms": search_lat_ms,
                    "eval_latency_ms": eval_lat_ms,
                    "lookup_latency_ms": sem_lat_ms,
                    "candidates": candidates,
                    "winning_candidate": None,
                    "rejection_reason": f"Policy evaluation failure: {exc}",
                    "reason_code": CompatibilityReasonCode.VALIDATOR_EXCEPTION.value,
                    "decision_status": "UNCERTAIN",
                    "dimension": None,
                },
                hook=diagnostics_hook,
            )
            return None

        eval_lat_ms = (time.perf_counter() - eval_start) * 1000.0
        collector.record_latency(ns, "candidate_policy_latency_ms", eval_lat_ms)
        eval_diag = getattr(self.policy, "_last_evaluation_diagnostics", None)
        cand_ctx_ms = float(eval_diag.get("context_validation_latency_ms") or 0.0) if isinstance(eval_diag, dict) else 0.0
        compat_val_ms = float(eval_diag.get("compatibility_validation_latency_ms") or eval_lat_ms) if isinstance(eval_diag, dict) else eval_lat_ms
        collector.record_latency(ns, "context_validation_latency_ms", elig_ctx_ms + cand_ctx_ms)
        collector.record_latency(ns, "compatibility_validation_latency_ms", compat_val_ms)

        sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
        collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)

        if isinstance(eval_diag, dict):
            ctx_matches = int(eval_diag.get("context_compatibility_matches") or 0)
            ctx_rejs = int(eval_diag.get("context_compatibility_rejections") or 0)
            ctx_miss_meta = int(eval_diag.get("context_metadata_missing") or 0)
            ctx_fp_mis = int(eval_diag.get("context_fingerprint_mismatches") or 0)
            priv_rejs = int(eval_diag.get("private_scope_rejections") or 0)
            ts_auth_rejs = int(eval_diag.get("tenant_shared_authorization_rejections") or 0)
            for _ in range(ctx_matches):
                self.stats.context_compatibility_matches += 1
                collector.record_context_compatibility_match(ns)
            for _ in range(ctx_rejs):
                self.stats.context_compatibility_rejections += 1
                collector.record_context_compatibility_rejection(ns)
            for _ in range(ctx_miss_meta):
                self.stats.context_metadata_missing += 1
                collector.record_context_metadata_missing(ns)
            for _ in range(ctx_fp_mis):
                self.stats.context_fingerprint_mismatches += 1
                collector.record_context_fingerprint_mismatch(ns)
            for _ in range(priv_rejs):
                self.stats.private_scope_rejections += 1
                collector.record_private_scope_rejection(ns)
            for _ in range(ts_auth_rejs):
                self.stats.tenant_shared_authorization_rejections += 1
                collector.record_tenant_shared_authorization_rejection(ns)

        req_conv_digest = compute_source_conversation_digest(request)

        if winning_candidate is None:
            self.stats.misses += 1
            collector.record_semantic_miss(ns)
            if any(
                (c.metadata or {}).get("source_conversation_digest") not in (None, req_conv_digest)
                for c in candidates
            ):
                self.stats.cross_conversation_semantic_misses += 1
                collector.record_cross_conversation_miss(ns)
            eval_outcome = eval_diag.get("outcome") if isinstance(eval_diag, dict) else None
            decision_status = eval_diag.get("decision_status") if isinstance(eval_diag, dict) else None
            if eval_outcome == "empty_candidates":
                self.stats.no_candidate_misses += 1
                collector.record_no_candidate_miss(ns)
            elif eval_outcome == "below_threshold":
                self.stats.threshold_rejections += 1
                collector.record_threshold_rejection(ns)
            elif eval_outcome == "ambiguous":
                self.stats.ambiguous_candidates += 1
                collector.record_ambiguous_candidate(ns)
            elif eval_outcome == "compatibility_rejected":
                self.stats.rejected_candidates += 1
                self.stats.all_candidates_rejected_misses += 1
                collector.record_all_candidates_rejected_miss(ns)
                if isinstance(eval_diag, dict) and eval_diag.get("retrieval_budget_exhausted"):
                    self.stats.retrieval_budget_exhaustions += 1
                    collector.record_retrieval_budget_exhaustion(ns)
                if decision_status == CompatibilityStatus.UNCERTAIN.value:
                    self.stats.uncertain_candidates += 1
                    collector.record_uncertain_candidate(ns)
                else:
                    collector.record_incompatible_candidate(ns)

            if eval_outcome in ("compatibility_rejected", "below_threshold", "ambiguous"):
                cand_decisions = eval_diag.get("candidate_decisions") if isinstance(eval_diag, dict) else None
                recorded_codes: list[str] = []
                if isinstance(cand_decisions, list):
                    for item in cand_decisions:
                        dec = item.get("decision") if isinstance(item, dict) else None
                        if isinstance(dec, dict) and dec.get("reason_code"):
                            if eval_outcome == "compatibility_rejected" or dec.get("status") in (
                                CompatibilityStatus.INCOMPATIBLE.value,
                                CompatibilityStatus.UNCERTAIN.value,
                            ):
                                recorded_codes.append(str(dec["reason_code"]))
                elif eval_outcome == "compatibility_rejected" and isinstance(eval_diag, dict) and eval_diag.get("reason_code"):
                    recorded_codes.append(str(eval_diag["reason_code"]))
                for r_code in recorded_codes:
                    collector.record_semantic_rejection_reason(ns, r_code)
                    if r_code in (
                        CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN.value,
                        CompatibilityReasonCode.VISIBILITY_MISMATCH.value,
                    ):
                        self.stats.visibility_rejections += 1
                        collector.record_visibility_rejection(ns)
                    elif r_code in (
                        CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING.value,
                        CompatibilityReasonCode.PRINCIPAL_ISOLATION_MISMATCH.value,
                    ):
                        self.stats.principal_isolation_rejections += 1
                        collector.record_principal_isolation_rejection(ns)
                    elif r_code in (
                        CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH.value,
                        CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID.value,
                        CompatibilityReasonCode.EVIDENCE_SCOPE_MISMATCH.value,
                        CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE.value,
                        CompatibilityReasonCode.AMBIGUOUS_COMPATIBILITY_EVIDENCE.value,
                    ):
                        self.stats.evidence_validation_rejections += 1
                        collector.record_evidence_validation_rejection(ns)

            logger.debug("Semantic lookup miss: %s", rejection_reason)
            diag_payload: Dict[str, Any] = {
                "query_hash": query_hash,
                "eligible": True,
                "reason": eligibility.reason,
                "metadata": eligibility.metadata,
                "context_class": ctx_cls_val,
                "context_fingerprint": ctx_eval.context_fingerprint,
                "embed_latency_ms": embed_lat_ms,
                "search_latency_ms": search_lat_ms,
                "eval_latency_ms": eval_lat_ms,
                "lookup_latency_ms": sem_lat_ms,
                "candidates": candidates,
                "winning_candidate": None,
                "rejection_reason": rejection_reason,
            }
            if isinstance(eval_diag, dict):
                diag_payload["decision_status"] = eval_diag.get("decision_status")
                diag_payload["reason_code"] = eval_diag.get("reason_code")
                diag_payload["dimension"] = eval_diag.get("dimension")
                diag_payload["decision_evidence"] = eval_diag.get("decision_evidence")
                diag_payload["candidate_decisions"] = eval_diag.get("candidate_decisions")
                diag_payload["candidates_retrieved"] = eval_diag.get("candidates_retrieved")
                diag_payload["candidates_compatible"] = eval_diag.get("candidates_compatible")
                diag_payload["candidates_rejected"] = eval_diag.get("candidates_rejected")
                diag_payload["candidates_incompatible"] = eval_diag.get("candidates_incompatible")
                diag_payload["candidates_uncertain"] = eval_diag.get("candidates_uncertain")
                diag_payload["candidates_legacy"] = eval_diag.get("candidates_legacy")
                diag_payload["all_candidates_rejected"] = eval_diag.get("all_candidates_rejected")
                diag_payload["retrieval_budget_exhausted"] = eval_diag.get("retrieval_budget_exhausted")
                diag_payload["retrieval_budget"] = eval_diag.get("retrieval_budget")
                diag_payload["context_compatibility_matches"] = eval_diag.get("context_compatibility_matches")
                diag_payload["context_compatibility_rejections"] = eval_diag.get("context_compatibility_rejections")
                diag_payload["context_metadata_missing"] = eval_diag.get("context_metadata_missing")
                diag_payload["context_fingerprint_mismatches"] = eval_diag.get("context_fingerprint_mismatches")
            self._emit_diagnostics(diag_payload, hook=diagnostics_hook)
            return None

        # 4. Candidate validated and selected — caller (IntelliCacheCore) confirms pre-delivery runtime gate
        cand_conv_digest = (winning_candidate.metadata or {}).get("source_conversation_digest")
        is_cross_conv_hit = bool(cand_conv_digest and cand_conv_digest != req_conv_digest)
        logger.debug(
            "Semantic candidate validated for delivery: namespace='%s', fingerprint='%s...', distance=%.4f, similarity=%.4f",
            winning_candidate.cache_namespace,
            winning_candidate.fingerprint[:12],
            winning_candidate.distance,
            winning_candidate.similarity,
        )

        hit_diag: Dict[str, Any] = {
            "query_hash": query_hash,
            "eligible": True,
            "reason": eligibility.reason,
            "metadata": eligibility.metadata,
            "context_class": ctx_cls_val,
            "context_fingerprint": ctx_eval.context_fingerprint,
            "is_cross_conversation": is_cross_conv_hit,
            "embed_latency_ms": embed_lat_ms,
            "search_latency_ms": search_lat_ms,
            "eval_latency_ms": eval_lat_ms,
            "lookup_latency_ms": sem_lat_ms,
            "candidates": candidates,
            "winning_candidate": winning_candidate,
            "rejection_reason": None,
        }
        if isinstance(eval_diag, dict):
            hit_diag["candidates_retrieved"] = eval_diag.get("candidates_retrieved")
            hit_diag["candidates_compatible"] = eval_diag.get("candidates_compatible")
            hit_diag["candidates_rejected"] = eval_diag.get("candidates_rejected")
            hit_diag["candidates_incompatible"] = eval_diag.get("candidates_incompatible")
            hit_diag["candidates_uncertain"] = eval_diag.get("candidates_uncertain")
            hit_diag["candidates_legacy"] = eval_diag.get("candidates_legacy")
        self._emit_diagnostics(hit_diag, hook=diagnostics_hook)

        return SemanticHit(
            cached_response=winning_candidate.cached_response or "",
            fingerprint=winning_candidate.fingerprint,
            distance=winning_candidate.distance,
            similarity=winning_candidate.similarity,
            model=winning_candidate.model,
            query_text=winning_candidate.query_text,
            prompt_tokens=winning_candidate.prompt_tokens,
            completion_tokens=winning_candidate.completion_tokens,
            total_tokens=winning_candidate.total_tokens,
            original_latency_ms=winning_candidate.original_latency_ms,
            metadata=winning_candidate.metadata,
            is_cross_conversation=is_cross_conv_hit,
        )

    def record_served_hit(self, namespace: Optional[str] = None, cross_conversation: bool = False) -> None:
        """Records that a validated semantic hit passed final pre-delivery control check and was served."""
        self.stats.hits += 1
        if cross_conversation:
            self.stats.cross_conversation_semantic_hits += 1
            ns = namespace or self.default_namespace
            self._get_collector().record_cross_conversation_hit(ns)

    async def index(self, request: GenerationRequest, result: GenerationResult) -> bool:
        """
        Verifies generated response artifacts, mints cryptographically bound compatibility evidence,
        enforces variant coexistence rules, and indexes verified candidates into the vector store.
        Enforces strict visibility & owner_principal_id isolation before admitting.
        Does not admit FAILED, UNCERTAIN, truncated, unknown visibility, missing private principal, or colliding variants.
        Fails safely without raising exceptions or invalidating the generation result.
        """
        # Rule 8: Do not index responses from semantic hits or exact hits
        if result.cached or result.cache_type in ("exact", "semantic"):
            return False

        self.last_admission_decision = None
        ns = request.namespace or self.default_namespace
        collector = self._get_collector()

        ctx_eval = analyze_request_context(request)
        raw_vis = normalize_cache_visibility(getattr(request, "visibility", CACHE_VISIBILITY_PRIVATE))
        effective_fp = (getattr(result, "fingerprint", None) or "").strip()
        if not effective_fp:
            effective_fp = generate_fingerprint(
                messages=request.messages,
                model=request.model,
                temperature=request.temperature,
                max_tokens=request.max_tokens,
                provider=request.provider,
            )

        scope = resolve_context_cache_scope(request, ctx_eval, effective_fp)
        if scope.privacy_rejection_reason == "VISIBILITY_MISSING_OR_UNKNOWN":
            self.stats.visibility_rejections += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_visibility_rejection(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.REJECT,
                reason_code=CompatibilityReasonCode.VISIBILITY_MISSING_OR_UNKNOWN,
                message="Admission rejected: missing or unknown cache visibility.",
            )
            return False
        if scope.privacy_rejection_reason == "PRINCIPAL_IDENTITY_MISSING":
            self.stats.principal_isolation_rejections += 1
            self.stats.private_scope_rejections += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_principal_isolation_rejection(ns)
            collector.record_private_scope_rejection(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.REJECT,
                reason_code=CompatibilityReasonCode.PRINCIPAL_IDENTITY_MISSING,
                message="Admission rejected: private cache scope requires verified principal_id.",
            )
            return False
        if not scope.can_admit_semantic:
            if scope.privacy_rejection_reason == "ADMISSION_REJECTED_CONTEXT_PRIVACY":
                self.stats.admission_rejected += 1
                self.stats.semantic_admission_rejections += 1
                collector.record_admission_outcome(ns, AdmissionStatus.REJECT.value)
                collector.record_semantic_admission_outcome(ns, accepted=False)
                if raw_vis == "tenant_shared":
                    self.stats.tenant_shared_authorization_rejections += 1
                    collector.record_tenant_shared_authorization_rejection(ns)
                else:
                    self.stats.private_scope_rejections += 1
                    collector.record_private_scope_rejection(ns)
                self.last_admission_decision = AdmissionDecision(
                    status=AdmissionStatus.REJECT,
                    reason_code=CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_PRIVACY,
                    message="Admission rejected: context contains sensitive credentials or restricted private scope.",
                )
            else:
                self.stats.admission_uncertain += 1
                self.stats.semantic_admission_rejections += 1
                collector.record_admission_outcome(ns, AdmissionStatus.UNCERTAIN.value)
                collector.record_semantic_admission_outcome(ns, accepted=False)
                self.last_admission_decision = AdmissionDecision(
                    status=AdmissionStatus.UNCERTAIN,
                    reason_code=CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN,
                    message="Admission rejected: conversation context is uncertain or incomplete.",
                )
            return False

        req_vis = scope.effective_visibility
        owner_to_bind = scope.effective_owner

        # Only index eligible queries - evaluate policy directly to avoid duplicate request eligibility metrics
        eligibility = self.policy.check_eligibility(request)
        if not eligibility.eligible:
            self.stats.semantic_admission_rejections += 1
            collector.record_semantic_admission_outcome(ns, accepted=False)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.REJECT,
                reason_code=CompatibilityReasonCode.ADMISSION_REJECTED_INELIGIBLE,
                message=eligibility.reason,
            )
            return False

        query_text = extract_effective_user_query(request)
        idx_start = time.perf_counter()

        # 1. Build request-derived v3 metadata and verify actual generated response BEFORE embedding/upsert
        try:
            base_meta = eligibility.metadata or self.policy.extractor.extract(
                request,
                self.policy.configured_model,
                context_assessment=ctx_eval,
            )
            base_meta.validate()
        except Exception as exc:
            self.stats.indexing_failures += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_indexing_failure(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.REJECT,
                reason_code=CompatibilityReasonCode.INVALID_METADATA_FORMAT,
                message=f"Metadata validation failed before verification: {exc}",
            )
            logger.warning(
                "Vector indexing aborted due to metadata validation failure (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

        req_extra = getattr(request, "metadata", None)
        completion_toks = getattr(getattr(result, "usage", None), "completion_tokens", 0) or 0
        verify_start = time.perf_counter()
        try:
            verification = self.verifier.verify(
                response_text=result.text,
                request_metadata=base_meta,
                query_text=query_text,
                finish_reason=getattr(result, "finish_reason", None),
                completion_tokens=int(completion_toks),
                request_max_tokens=request.max_tokens,
                request_extra_metadata=req_extra if isinstance(req_extra, dict) else None,
            )
        except Exception as exc:
            self.stats.admission_uncertain += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_admission_outcome(ns, AdmissionStatus.UNCERTAIN.value)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            self.stats.indexing_failures += 1
            collector.record_indexing_failure(ns)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.UNCERTAIN,
                reason_code=CompatibilityReasonCode.VALIDATOR_EXCEPTION,
                message=f"Response verifier error: {exc}",
            )
            logger.warning(
                "Response verifier raised unexpected error (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False
        verify_lat_ms = (time.perf_counter() - verify_start) * 1000.0

        if verification.status != VerificationStatus.VERIFIED:
            self.stats.semantic_admission_rejections += 1
            if verification.status == VerificationStatus.FAILED:
                self.stats.admission_rejected += 1
                adm_status = AdmissionStatus.REJECT
                adm_code = (
                    verification.reason_code
                    if verification.reason_code in (
                        CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN,
                        CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_PRIVACY,
                    )
                    else CompatibilityReasonCode.ADMISSION_REJECTED_VERIFICATION
                )
            else:
                self.stats.admission_uncertain += 1
                adm_status = AdmissionStatus.UNCERTAIN
                adm_code = (
                    verification.reason_code
                    if verification.reason_code == CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_UNCERTAIN
                    else CompatibilityReasonCode.ADMISSION_UNCERTAIN_VERIFICATION
                )
            if adm_code == CompatibilityReasonCode.ADMISSION_REJECTED_CONTEXT_PRIVACY:
                if req_vis == "tenant_shared":
                    self.stats.tenant_shared_authorization_rejections += 1
                    collector.record_tenant_shared_authorization_rejection(ns)
                else:
                    self.stats.private_scope_rejections += 1
                    collector.record_private_scope_rejection(ns)
            collector.record_admission_outcome(ns, adm_status.value)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            self.last_admission_decision = AdmissionDecision(
                status=adm_status,
                reason_code=adm_code,
                message=verification.message,
                verification=verification,
                diagnostics={"verification_latency_ms": round(verify_lat_ms, 3)},
            )
            logger.info(
                "Semantic cache admission blocked (%s / %s): %s",
                adm_status.value,
                verification.reason_code.value,
                verification.message,
            )
            return False

        variant_key = compute_variant_key(
            base_meta,
            schema_digest=verification.verified_schema_digest,
            visibility=req_vis,
            owner_principal_id=owner_to_bind,
        )
        storage_fp = scope.exact_storage_key or derive_scoped_exact_key(effective_fp, req_vis, owner_to_bind)

        # 2. Mint cryptographically bound response_verified CompatibilityEvidence
        try:
            is_ctx_independent = bool(
                base_meta.context_contract is not None
                and base_meta.context_contract.context_class == ContextClass.CONTEXT_INDEPENDENT
            )
            bound_evidence = self.evidence_authority.mint_response_verified_evidence(
                cache_namespace=ns,
                fingerprint=storage_fp,
                response_text=result.text,
                metadata=base_meta,
                verification=verification,
                context_independent=is_ctx_independent,
                visibility=req_vis,
                owner_principal_id=owner_to_bind,
            )
            verified_meta_obj = SemanticCompatibilityMetadata(
                schema_version=base_meta.schema_version,
                extractor_version=base_meta.extractor_version,
                task_kind=base_meta.task_kind,
                dimensions=base_meta.dimensions,
                generation_contract=base_meta.generation_contract,
                embedding_contract=base_meta.embedding_contract,
                indexed_at=base_meta.indexed_at,
                compatibility_evidence=bound_evidence,
                context_contract=base_meta.context_contract,
            )
            meta = verified_meta_obj.to_dict()
            meta["variant_key"] = variant_key
            meta["truncation_status"] = verification.truncation_status.value
            meta["admission_status"] = AdmissionStatus.ADMIT.value
            meta["source_conversation_digest"] = compute_source_conversation_digest(request)
        except Exception as exc:
            self.stats.indexing_failures += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_indexing_failure(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            logger.warning(
                "Vector indexing aborted during evidence minting (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

        # 3. Generate document embedding (only after verification and admission checks pass)
        embed_start = time.perf_counter()
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.DOCUMENT)
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
        except Exception as exc:
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
            self.stats.indexing_failures += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_indexing_failure(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            logger.warning(
                "Vector indexing failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

        # Validate reported model identity against configured model (fail open)
        if embed_res.model != self.policy.configured_model:
            self.stats.indexing_failures += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_indexing_failure(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            logger.warning(
                "Vector indexing model mismatch: expected '%s', got '%s'",
                self.policy.configured_model,
                embed_res.model,
            )
            return False

        record = VectorRecord(
            cache_namespace=ns,
            fingerprint=storage_fp,
            query_text=query_text,
            vector=embed_res.vector,
            model=self.policy.configured_model,
            metadata=meta,
            variant_key=variant_key,
            response_text=result.text,
            visibility=req_vis,
            owner_principal_id=owner_to_bind,
        )

        # 4. Upsert verified record into vector store
        try:
            await self.vector_store.upsert(record)
            self.stats.admission_verified += 1
            self.stats.semantic_admission_successes += 1
            collector.record_admission_outcome(ns, AdmissionStatus.ADMIT.value)
            collector.record_semantic_admission_outcome(ns, accepted=True)
            self.stats.indexing_successes += 1
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_indexing_success(ns, latency_ms=idx_lat_ms)
            self.last_admission_decision = AdmissionDecision(
                status=AdmissionStatus.ADMIT,
                reason_code=CompatibilityReasonCode.ADMISSION_ADMITTED,
                message="Verified response artifact admitted into semantic cache.",
                variant_key=variant_key,
                verification=verification,
                diagnostics={
                    "verification_latency_ms": round(verify_lat_ms, 3),
                    "total_admission_indexing_latency_ms": round(idx_lat_ms, 3),
                },
            )
            logger.debug(
                "Indexed verified vector record: fingerprint='%s...', variant_key='%s...', namespace='%s'",
                effective_fp[:12],
                variant_key[:20],
                ns,
            )
            return True
        except Exception as exc:
            self.stats.indexing_failures += 1
            self.stats.semantic_admission_rejections += 1
            collector.record_indexing_failure(ns)
            collector.record_semantic_admission_outcome(ns, accepted=False)
            idx_lat_ms = (time.perf_counter() - idx_start) * 1000.0
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            logger.warning(
                "Vector indexing failed during upsert (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

    def get_stats(self, namespace: Optional[str] = None) -> Dict[str, int]:
        """Returns diagnostic observability counters."""
        collector = self._get_collector()
        if namespace is not None:
            snap = collector.get_namespace_snapshot(namespace)
        else:
            snap = collector.get_admin_snapshot().aggregate
        s = snap.semantic
        return {
            "lookups": snap.request_and_cache.semantic_lookups,
            "hits": s.semantic_hits,
            "coalesced_semantic_hits": s.coalesced_semantic_hits,
            "misses": s.semantic_misses,
            "rejected_candidates": s.incompatible_candidate_rejections + s.uncertain_candidate_rejections,
            "uncertain_candidates": s.uncertain_candidate_rejections,
            "ambiguous_candidates": s.ambiguous_candidate_rejections,
            "no_candidate_misses": s.no_candidate_misses,
            "threshold_rejections": s.threshold_rejections,
            "all_candidates_rejected_misses": s.all_candidates_rejected_misses,
            "retrieval_budget_exhaustions": s.retrieval_budget_exhaustions,
            "eligibility_rejections": s.semantic_eligibility_rejections,
            "embedding_failures": s.embedding_failures,
            "vector_store_failures": s.vector_store_failures,
            "indexing_successes": s.indexing_successes,
            "indexing_failures": s.indexing_failures,
            "admission_verified": s.admission_verified,
            "admission_rejected": s.admission_rejected,
            "admission_uncertain": s.admission_uncertain,
            "runtime_disabled_skips": s.semantic_runtime_disabled_skips,
            "candidates_retrieved": s.semantic_candidates_retrieved,
            "visibility_rejections": s.visibility_rejections,
            "principal_isolation_rejections": s.principal_isolation_rejections,
            "evidence_validation_rejections": s.evidence_validation_rejections,
            "upstream_fallbacks_after_miss": s.upstream_fallbacks_after_semantic_miss,
            "runtime_control_changes": s.runtime_control_changes,
            "runtime_control_failures": s.runtime_control_failures,
            "privacy_violation_attempts": s.privacy_violation_attempts,
            "context_independent_requests": s.context_independent_requests,
            "context_dependent_requests": s.context_dependent_requests,
            "context_uncertain_requests": s.context_uncertain_requests,
            "context_compatibility_matches": s.context_compatibility_matches,
            "context_compatibility_rejections": s.context_compatibility_rejections,
            "context_metadata_missing": s.context_metadata_missing,
            "context_fingerprint_mismatches": s.context_fingerprint_mismatches,
            "cross_conversation_semantic_hits": s.cross_conversation_semantic_hits,
            "cross_conversation_semantic_misses": s.cross_conversation_semantic_misses,
            "private_scope_rejections": s.private_scope_rejections,
            "tenant_shared_authorization_rejections": s.tenant_shared_authorization_rejections,
            "semantic_admission_successes": s.semantic_admission_successes,
            "semantic_admission_rejections": s.semantic_admission_rejections,
            "semantic_fallbacks": s.semantic_fallbacks,
        }

    async def close(self) -> None:
        """Closes owned embedding provider and vector store resources independently."""
        errors: list[Exception] = []
        if self.embedding_provider:
            try:
                await self.embedding_provider.close()
            except Exception as exc:
                logger.error("Error closing embedding provider during shutdown: %s", exc)
                errors.append(exc)
        if self.vector_store:
            try:
                await self.vector_store.close()
            except Exception as exc:
                logger.error("Error closing vector store during shutdown: %s", exc)
                errors.append(exc)
        logger.info("SemanticCacheService resources closed.")
        if errors:
            raise errors[0]
