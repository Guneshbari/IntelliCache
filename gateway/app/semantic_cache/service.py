"""
Semantic cache service implementation.
Coordinates query eligibility, embedding generation, vector retrieval, candidate evaluation,
safe failure fallbacks, and vector indexing.
"""

from datetime import datetime, timezone
import logging
from typing import Any, Dict, Optional

from .contracts import EligibilityResult, SemanticCacheStats, SemanticHit
from .interfaces import BaseSemanticCache
from .policy import SemanticCachePolicy
from ..core.contracts import GenerationRequest, GenerationResult
from ..core.errors import sanitize_error_message
from ..embeddings.contracts import EmbeddingTask
from ..embeddings.interfaces import EmbeddingProvider
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
    ):
        self.embedding_provider = embedding_provider
        self.vector_store = vector_store
        self.policy = policy
        self.default_namespace = default_namespace
        self.stats = SemanticCacheStats()

    def is_eligible(self, request: GenerationRequest) -> EligibilityResult:
        """Evaluates whether the request is eligible for semantic caching."""
        return self.policy.check_eligibility(request)

    async def lookup(self, request: GenerationRequest) -> Optional[SemanticHit]:
        """
        Attempts semantic lookup for an eligible request.
        Fails open on embedding or vector storage errors, returning None.
        Never logs raw query text or embedding vectors.
        """
        eligibility = self.is_eligible(request)
        if not eligibility.eligible:
            self.stats.eligibility_rejections += 1
            logger.debug(
                "Request ineligible for semantic caching: %s",
                eligibility.reason,
            )
            return None

        self.stats.lookups += 1
        query_text = request.messages[0].content.strip()
        ns = request.namespace or self.default_namespace

        # 1. Generate query embedding
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.QUERY)
        except Exception as exc:
            self.stats.embedding_failures += 1
            logger.warning(
                "Semantic lookup failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return None

        # 2. Search nearest neighbours in vector store
        try:
            candidates = await self.vector_store.search(
                query_vector=embed_res.vector,
                top_k=self.policy.top_k,
                namespace=ns,
                model=self.policy.configured_model,
            )
        except Exception as exc:
            self.stats.vector_store_failures += 1
            logger.warning(
                "Semantic lookup failed during vector search (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return None

        # 3. Evaluate candidates against policy
        winning_candidate, rejection_reason = self.policy.evaluate_candidates(candidates, request)
        if winning_candidate is None:
            self.stats.misses += 1
            if rejection_reason and "ambiguous" in rejection_reason.lower():
                self.stats.ambiguous_candidates += 1
            elif rejection_reason and "compatible" in rejection_reason.lower():
                self.stats.rejected_candidates += 1

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

        # Only index eligible queries
        eligibility = self.is_eligible(request)
        if not eligibility.eligible:
            return False

        query_text = request.messages[0].content.strip()
        ns = request.namespace or self.default_namespace

        # 1. Generate document embedding
        try:
            embed_res = await self.embedding_provider.embed(query_text, task=EmbeddingTask.DOCUMENT)
        except Exception as exc:
            self.stats.indexing_failures += 1
            logger.warning(
                "Vector indexing failed during embedding (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
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
            logger.debug(
                "Indexed vector record for fingerprint='%s...' in namespace='%s'",
                result.fingerprint[:12],
                ns,
            )
            return True
        except Exception as exc:
            self.stats.indexing_failures += 1
            logger.warning(
                "Vector indexing failed during upsert (%s): %s",
                type(exc).__name__,
                sanitize_error_message(str(exc)),
            )
            return False

    def get_stats(self) -> Dict[str, int]:
        """Returns diagnostic observability counters."""
        return self.stats.to_dict()

    async def close(self) -> None:
        """Closes owned embedding provider and vector store resources."""
        if self.embedding_provider:
            await self.embedding_provider.close()
        if self.vector_store:
            await self.vector_store.close()
        logger.info("SemanticCacheService resources closed.")
