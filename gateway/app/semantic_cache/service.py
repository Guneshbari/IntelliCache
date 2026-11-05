"""
Semantic cache service implementation.
Coordinates query eligibility, embedding generation, vector retrieval, candidate evaluation,
safe failure fallbacks, and vector indexing.
"""

from datetime import datetime, timezone
import logging
import time
from typing import Any, Dict, Optional

from .contracts import EligibilityResult, SemanticCacheStats, SemanticHit
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from ..core.contracts import GenerationRequest, GenerationResult
from ..core.errors import sanitize_error_message
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
    ):
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.policy = policy
        self.default_namespace = default_namespace
        self.stats = SemanticCacheStats()
        self.metrics_collector = metrics_collector

    def _get_collector(self) -> RuntimeMetricsCollector:
        return self.metrics_collector or get_metrics_collector()

    def is_eligible(self, request: GenerationRequest) -> EligibilityResult:
        """Evaluates whether the request is eligible for semantic caching."""
        res = self.policy.check_eligibility(request)
        ns = request.namespace or self.default_namespace
        self._get_collector().record_semantic_eligibility_check(ns, eligible=res.eligible)
        return res

    async def lookup(self, request: GenerationRequest) -> Optional[SemanticHit]:
        """
        Attempts semantic lookup for an eligible request.
        Fails open on embedding or vector storage errors, returning None.
        Never logs raw query text or embedding vectors.
        """
        collector = self._get_collector()
        ns = request.namespace or self.default_namespace
        lookup_start = time.perf_counter()

        eligibility = self.is_eligible(request)
        if not eligibility.eligible:
            self.stats.eligibility_rejections += 1
            logger.debug(
                "Request ineligible for semantic caching: %s",
                eligibility.reason,
            )
            return None

        self.stats.lookups += 1
        collector.record_semantic_lookup(ns)
        query_text = request.messages[0].content.strip()

        # 1. Generate query embedding
        embed_start = time.perf_counter()
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.QUERY)
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
        except Exception as exc:
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
            self.stats.embedding_failures += 1
            collector.record_embedding_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return None

        # Validate reported model identity against configured model (fail open)
        if embed_res.model != self.policy.configured_model:
            self.stats.embedding_failures += 1
            collector.record_embedding_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup model mismatch: expected '%s', got '%s'",
                self.policy.configured_model,
                embed_res.model,
            )
            return None

        # 2. Search nearest neighbours in vector store
        search_start = time.perf_counter()
        try:
            candidates = await self.vector_store.search(
                query_vector=embed_res.vector,
                top_k=self.policy.top_k,
                namespace=ns,
                model=self.policy.configured_model,
            )
            search_lat_ms = (time.perf_counter() - search_start) * 1000.0
            collector.record_latency(ns, "vector_search_latency_ms", search_lat_ms)
        except Exception as exc:
            search_lat_ms = (time.perf_counter() - search_start) * 1000.0
            collector.record_latency(ns, "vector_search_latency_ms", search_lat_ms)
            self.stats.vector_store_failures += 1
            collector.record_vector_store_failure(ns)
            sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
            collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)
            logger.warning(
                "Semantic lookup failed during vector search (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return None

        # 3. Evaluate candidates against policy
        eval_start = time.perf_counter()
        winning_candidate, rejection_reason = self.policy.evaluate_candidates(candidates, request)
        eval_lat_ms = (time.perf_counter() - eval_start) * 1000.0
        collector.record_latency(ns, "candidate_policy_latency_ms", eval_lat_ms)
        sem_lat_ms = (time.perf_counter() - lookup_start) * 1000.0
        collector.record_latency(ns, "semantic_lookup_latency_ms", sem_lat_ms)

        if winning_candidate is None:
            self.stats.misses += 1
            collector.record_semantic_miss(ns)
            if rejection_reason and "ambiguous" in rejection_reason.lower():
                self.stats.ambiguous_candidates += 1
                collector.record_ambiguous_candidate(ns)
            elif rejection_reason and "compatible" in rejection_reason.lower():
                self.stats.rejected_candidates += 1
                collector.record_incompatible_candidate(ns)

            logger.debug("Semantic lookup miss: %s", rejection_reason)
            return None

        # 4. Successful semantic cache hit
        self.stats.hits += 1
        logger.info(
            "Semantic cache hit: namespace='%s', fingerprint='%s...', distance=%.4f, similarity=%.4f",
            winning_candidate.cache_namespace,
            winning_candidate.fingerprint[:12],
            winning_candidate.distance,
            winning_candidate.similarity,
        )

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
        )

    async def index(self, request: GenerationRequest, result: GenerationResult) -> bool:
        """
        Indexes an eligible, successful generation result into the vector store.
        Does not index semantic hits (avoids recursive indexing).
        Fails safely without raising exceptions or invalidating the generation result.
        """
        # Rule 8: Do not index responses from semantic hits or exact hits
        if result.cached or result.cache_type in ("exact", "semantic"):
            return False

        # Only index eligible queries - evaluate policy directly to avoid duplicate request eligibility metrics
        eligibility = self.policy.check_eligibility(request)
        if not eligibility.eligible:
            return False

        query_text = request.messages[0].content.strip()
        ns = request.namespace or self.default_namespace

        collector = self._get_collector()

        # 1. Generate document embedding
        embed_start = time.perf_counter()
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.DOCUMENT)
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
        except Exception as exc:
            embed_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "embedding_latency_ms", embed_lat_ms)
            self.stats.indexing_failures += 1
            collector.record_indexing_failure(ns)
            collector.record_latency(ns, "vector_indexing_latency_ms", embed_lat_ms)
            logger.warning(
                "Vector indexing failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

        # Validate reported model identity against configured model (fail open)
        if embed_res.model != self.policy.configured_model:
            self.stats.indexing_failures += 1
            collector.record_indexing_failure(ns)
            idx_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_latency(ns, "vector_indexing_latency_ms", idx_lat_ms)
            logger.warning(
                "Vector indexing model mismatch: expected '%s', got '%s'",
                self.policy.configured_model,
                embed_res.model,
            )
            return False

        # 2. Prepare generation compatibility metadata
        meta = {
            "llm_provider": request.provider,
            "llm_model": request.model,
            "temperature": request.temperature,
            "max_tokens": request.max_tokens,
            "indexed_at": datetime.now(timezone.utc).isoformat(),
        }

        record = VectorRecord(
            cache_namespace=ns,
            fingerprint=result.fingerprint,
            query_text=query_text,
            vector=embed_res.vector,
            model=self.policy.configured_model,
            metadata=meta,
        )

        # 3. Upsert into vector store
        try:
            await self.vector_store.upsert(record)
            self.stats.indexing_successes += 1
            idx_lat_ms = (time.perf_counter() - embed_start) * 1000.0
            collector.record_indexing_success(ns, latency_ms=idx_lat_ms)
            logger.debug(
                "Indexed vector record for fingerprint='%s...' in namespace='%s'",
                result.fingerprint[:12],
                ns,
            )
            return True
        except Exception as exc:
            self.stats.indexing_failures += 1
            collector.record_indexing_failure(ns)
            idx_lat_ms = (time.perf_counter() - embed_start) * 1000.0
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
            "misses": s.semantic_misses,
            "rejected_candidates": s.incompatible_candidate_rejections,
            "ambiguous_candidates": s.ambiguous_candidate_rejections,
            "eligibility_rejections": s.semantic_eligibility_rejections,
            "embedding_failures": s.embedding_failures,
            "vector_store_failures": s.vector_store_failures,
            "indexing_successes": s.indexing_successes,
            "indexing_failures": s.indexing_failures,
        }

    async def close(self) -> None:
        """Closes owned embedding provider and vector store resources."""
        if self.embedding_provider:
            await self.embedding_provider.close()
        if self.vector_store:
            await self.vector_store.close()
        logger.info("SemanticCacheService resources closed.")
