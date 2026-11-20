"""
Semantic cache shadow evaluation module (Stage 3.3).
Provides isolated, asynchronous, bounded observation and evaluation of incoming requests
against the semantic cache without mutating production cache entries or vector records,
blocking the API event loop, or delaying/altering responses returned to users.
"""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor
import asyncio
import hashlib
import logging
import random
import time
from typing import Any, Callable, Dict, List, Optional, Sequence

from ..config import Settings
from ..core.contracts import ChatMessage, GenerationRequest, GenerationResult
from ..metrics.collector import RuntimeMetricsCollector, get_metrics_collector
from ..vector_store.contracts import VectorRecord, VectorSearchResult
from ..vector_store.interfaces import BaseVectorStore
from .service import SemanticCacheService, SemanticHit

logger = logging.getLogger("intellicache.gateway.semantic_cache.shadow")


# ---------------------------------------------------------------------------
# Controlled Reason Code Mapping
# ---------------------------------------------------------------------------

def map_eligibility_rejection_code(reason: Optional[str]) -> str:
    """Maps free-form eligibility rejection messages into controlled codes."""
    if not reason:
        return "INELIGIBLE_UNKNOWN"
    r = reason.upper()
    if "CONVERSATION" in r or "MULTI" in r or "TURN" in r:
        return "MULTI_TURN_CONVERSATION"
    if "SYSTEM" in r:
        return "SYSTEM_PROMPT_PRESENT"
    if "TEMPERATURE" in r:
        return "TEMPERATURE_NON_ZERO"
    if "STREAM" in r:
        return "STREAMING_ENABLED"
    if "EMPTY" in r:
        return "EMPTY_QUERY"
    if "LARGE" in r or "PAYLOAD" in r or "CHAR" in r:
        return "PAYLOAD_TOO_LARGE"
    return "INELIGIBLE_POLICY"


def map_compatibility_rejection_code(reason: Optional[str]) -> str:
    """Maps free-form compatibility rejection messages into controlled codes."""
    if not reason:
        return "INCOMPATIBLE_UNKNOWN"
    r = reason.upper()
    if "MODEL" in r:
        return "MODEL_MISMATCH"
    if "SYSTEM" in r:
        return "SYSTEM_PROMPT_MISMATCH"
    if "TEMPERATURE" in r:
        return "TEMPERATURE_MISMATCH"
    if "MAX_TOKENS" in r or "TOKEN" in r:
        return "MAX_TOKENS_MISMATCH"
    if "TASK" in r or "KIND" in r:
        return "TASK_KIND_MISMATCH"
    return "INCOMPATIBLE_CANDIDATE"


# ---------------------------------------------------------------------------
# Task Snapshot Payload
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class ShadowTask:
    """
    Immutable snapshot payload for shadow evaluation.
    Decoupled from mutable FastAPI Request and QueryRequest objects.
    
    Privacy & Pseudonymity Statement:
    - query_text exists transiently in worker process memory ONLY to compute embedding vectors.
    - query_text is NEVER written to shadow_evaluations database, never logged, and never persisted.
    - request_hash is a pseudonymous deterministic SHA-256 hash.
    - actual_response_hash is a SHA-256 hash used solely for candidate verification.
    """
    request_id: str
    request_hash: str
    cache_namespace: str
    provider: str
    requested_model: str
    temperature: float
    max_tokens: Optional[int]
    query_text: str
    actual_response_source: str  # 'exact_cache' | 'upstream_provider' | 'bypass' | 'failed' | 'none'
    actual_response_hash: Optional[str]
    actual_latency_ms: float
    enqueued_at: float
    task_kind: str = "generation"
    system_prompt: Optional[str] = None
    stream: bool = False
    is_multi_turn: bool = False


# ---------------------------------------------------------------------------
# Multi-Layer Write Protection
# ---------------------------------------------------------------------------

class ReadOnlyVectorStoreAdapter(BaseVectorStore):
    """
    Structural read-only adapter wrapping a BaseVectorStore.
    Exposes search and metadata inspection while strictly forbidding
    upsert, delete, and clear operations, guaranteeing that shadow evaluations
    cannot mutate production vector tables.
    """

    def __init__(self, inner: BaseVectorStore):
        self._inner = inner

    @property
    def dimensions(self) -> int:
        return getattr(self._inner, "dimensions", 384)

    @property
    def default_namespace(self) -> str:
        return getattr(self._inner, "default_namespace", "default")

    @property
    def default_model(self) -> str:
        return getattr(self._inner, "default_model", "BAAI/bge-small-en-v1.5")

    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[VectorSearchResult]:
        return await self._inner.search(
            query_vector=query_vector,
            top_k=top_k,
            namespace=namespace,
            model=model,
        )

    async def upsert(self, record: VectorRecord) -> None:
        raise PermissionError("ReadOnlyVectorStoreAdapter rejects upsert operations in shadow mode.")

    async def delete(self, fingerprint: str, namespace: Optional[str] = None) -> bool:
        raise PermissionError("ReadOnlyVectorStoreAdapter rejects delete operations in shadow mode.")

    async def clear(self, namespace: Optional[str] = None) -> int:
        raise PermissionError("ReadOnlyVectorStoreAdapter rejects clear operations in shadow mode.")

    async def close(self) -> None:
        await self._inner.close()


class ReadOnlySemanticCacheService(SemanticCacheService):
    """
    Read-only semantic cache service for shadow evaluation.
    Inherits all eligibility checks, FastEmbed inference, pgvector retrieval,
    and compatibility validation from SemanticCacheService, but ensures
    that index() is a strict no-op that never writes to the vector store or cache.
    """

    async def index(
        self,
        request: GenerationRequest,
        response: GenerationResult,
    ) -> bool:
        logger.debug("ReadOnlySemanticCacheService.index() invoked in shadow mode: strictly no-op.")
        return False


# ---------------------------------------------------------------------------
# Shadow Evaluation Service
# ---------------------------------------------------------------------------

class ShadowEvaluationService:
    """
    Asynchronous, bounded background service that processes shadow evaluations.
    Manages queueing, pre-enqueue sampling, concurrency control via Semaphore,
    ONNX CPU execution via dedicated ThreadPoolExecutor, and failure isolation.
    """

    def __init__(
        self,
        settings: Settings,
        semantic_cache: SemanticCacheService,
        db_pool: Optional[Any] = None,
        metrics_collector: Optional[RuntimeMetricsCollector] = None,
    ):
        self.settings = settings
        self.semantic_cache = semantic_cache
        self.db_pool = db_pool
        self.metrics_collector = metrics_collector
        self.queue: asyncio.Queue[ShadowTask] = asyncio.Queue(maxsize=settings.shadow_queue_max_size)
        self.semaphore = asyncio.Semaphore(settings.shadow_max_concurrency)
        self.executor = ThreadPoolExecutor(
            max_workers=settings.shadow_max_concurrency,
            thread_name_prefix="shadow-worker",
        )
        self._workers: List[asyncio.Task] = []
        self._stopping = False
        self._running = False

    def _get_collector(self) -> RuntimeMetricsCollector:
        return self.metrics_collector or get_metrics_collector()

    def enqueue(self, task: ShadowTask) -> bool:
        """
        Samples and non-blockingly enqueues a ShadowTask.
        Returns True if enqueued, False if dropped or unsampled.
        Never delays or raises to the caller.
        """
        if not self.settings.shadow_evaluation_enabled or self._stopping:
            return False

        collector = self._get_collector()
        ns = task.cache_namespace

        # 1. Pre-enqueue Sampling Gate
        is_sampled = (
            self.settings.shadow_sampling_rate >= 1.0
            or random.random() < self.settings.shadow_sampling_rate
        )

        if not is_sampled:
            collector.record_shadow_request(ns, sampled=False, queue_depth=self.queue.qsize())
            return False

        collector.record_shadow_request(ns, sampled=True, queue_depth=self.queue.qsize())

        # 2. Non-blocking queue insertion with drop-on-full
        try:
            self.queue.put_nowait(task)
            return True
        except asyncio.QueueFull:
            collector.record_shadow_drop(ns)
            logger.warning(
                "Shadow evaluation queue saturated (%d capacity). Dropping request '%s'.",
                self.queue.maxsize,
                task.request_id,
            )
            return False

    async def _worker_loop(self) -> None:
        """Worker loop continuously draining and evaluating shadow tasks."""
        while not self._stopping or not self.queue.empty():
            try:
                task = await asyncio.wait_for(self.queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            try:
                async with self.semaphore:
                    await self._evaluate_task(task)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error("Unexpected error in shadow worker loop: %s", exc, exc_info=True)
            finally:
                self.queue.task_done()

    async def _evaluate_task(self, task: ShadowTask) -> None:
        """
        Executes real SemanticCacheService.lookup() under isolation and records results.
        Enforces controlled outcomes, timeouts, non-interference, and match verification.
        """
        start_time = time.perf_counter()
        collector = self._get_collector()
        ns = task.cache_namespace

        # Reconstruct canonical GenerationRequest for lookup
        messages: List[ChatMessage] = []
        if task.system_prompt:
            messages.append(ChatMessage(role="system", content=task.system_prompt))
        if task.is_multi_turn:
            messages.append(ChatMessage(role="user", content="previous question"))
            messages.append(ChatMessage(role="assistant", content="previous answer"))
        messages.append(ChatMessage(role="user", content=task.query_text))

        gen_req = GenerationRequest(
            model=task.requested_model,
            messages=messages,
            temperature=task.temperature,
            max_tokens=task.max_tokens,
            provider=task.provider,
            namespace=task.cache_namespace,
            stream=task.stream,
            metadata={"request_id": task.request_id, "task_kind": task.task_kind},
        )

        # Hook diagnostics
        captured_diag: Dict[str, Any] = {}

        def _capture(diag: Dict[str, Any]) -> None:
            captured_diag.update(diag)

        # Temporarily attach our diagnostics capture to the semantic cache instance
        original_hook = self.semantic_cache.diagnostics_hook
        self.semantic_cache.diagnostics_hook = _capture

        eligibility_status = "eligible"
        eligibility_rejection_reason: Optional[str] = None
        embedding_model = getattr(self.semantic_cache.embedding_provider, "model", None)
        retrieval_status = "not_attempted"
        candidate_count = 0
        top_similarity: Optional[float] = None
        runner_up_similarity: Optional[float] = None
        similarity_margin: Optional[float] = None
        compatibility_validation_result: Optional[str] = None
        compatibility_rejection_reason: Optional[str] = None
        ambiguity_decision: Optional[str] = None
        hypothetical_cache_outcome = "shadow_execution_failure"
        candidate_response_matched: Optional[bool] = None
        embedding_duration_ms = 0.0
        retrieval_duration_ms = 0.0
        policy_duration_ms = 0.0
        failure_category: Optional[str] = None
        hit: Optional[SemanticHit] = None

        try:
            # 1. Eligibility Check: Short-circuit if ineligible (skip ONNX & pgvector)
            eligibility = self.semantic_cache.is_eligible(gen_req)
            if not eligibility.eligible:
                eligibility_status = "ineligible"
                eligibility_rejection_reason = map_eligibility_rejection_code(eligibility.reason)
                hypothetical_cache_outcome = "ineligible_request"
            else:
                retrieval_status = "attempted"
                # 2. Real lookup with bounded timeout
                try:
                    hit = await asyncio.wait_for(
                        self.semantic_cache.lookup(gen_req),
                        timeout=self.settings.shadow_timeout_seconds,
                    )
                except asyncio.TimeoutError:
                    hypothetical_cache_outcome = "shadow_timeout"
                    failure_category = "SHADOW_TIMEOUT"
                except Exception as exc:
                    hypothetical_cache_outcome = "shadow_execution_failure"
                    failure_category = "SHADOW_EXECUTION_FAILURE"
                    logger.warning("Shadow lookup error for '%s': %s", task.request_id, exc)

                # Extract diagnostics if lookup finished
                if failure_category is None:
                    embedding_duration_ms = max(0.0, float(captured_diag.get("embed_latency_ms") or 0.0))
                    retrieval_duration_ms = max(0.0, float(captured_diag.get("search_latency_ms") or 0.0))
                    policy_duration_ms = max(0.0, float(captured_diag.get("eval_latency_ms") or 0.0))

                    candidates: List[VectorSearchResult] = captured_diag.get("candidates") or []
                    candidate_count = len(candidates)

                    if candidate_count > 0:
                        top_similarity = round(float(candidates[0].similarity), 4)
                        if candidate_count > 1:
                            runner_up_similarity = round(float(candidates[1].similarity), 4)
                            similarity_margin = round(float(top_similarity - runner_up_similarity), 4)

                    rejection = captured_diag.get("rejection_reason") or ""

                    if hit is not None:
                        hypothetical_cache_outcome = "eligible_candidate_hit"
                        compatibility_validation_result = "compatible"
                        retrieval_status = "hit"

                        # Candidate response match comparison
                        if task.actual_response_hash and hit.cached_response:
                            cand_hash = hashlib.sha256(hit.cached_response.strip().encode("utf-8")).hexdigest()
                            candidate_response_matched = (cand_hash == task.actual_response_hash)
                    else:
                        retrieval_status = "miss"
                        if "embedding failure" in rejection.lower() or "model mismatch" in rejection.lower():
                            hypothetical_cache_outcome = "embedding_failure"
                            failure_category = "EMBEDDING_ERROR"
                        elif "vector search failure" in rejection.lower():
                            hypothetical_cache_outcome = "vector_store_failure"
                            failure_category = "VECTOR_STORE_ERROR"
                        elif candidate_count == 0:
                            hypothetical_cache_outcome = "eligible_no_candidate"
                        elif "ambigu" in rejection.lower() or "margin" in rejection.lower():
                            hypothetical_cache_outcome = "ambiguity_rejection"
                            ambiguity_decision = "AMBIGUOUS_MARGIN_TOO_SMALL"
                        elif "compat" in rejection.lower() or "mismatch" in rejection.lower():
                            hypothetical_cache_outcome = "compatibility_rejection"
                            compatibility_validation_result = "incompatible"
                            compatibility_rejection_reason = map_compatibility_rejection_code(rejection)
                        else:
                            hypothetical_cache_outcome = "threshold_rejection"

        except Exception as err:
            hypothetical_cache_outcome = "shadow_execution_failure"
            failure_category = "SHADOW_EXECUTION_FAILURE"
            logger.error("Unexpected error in shadow evaluation: %s", err, exc_info=True)
        finally:
            self.semantic_cache.diagnostics_hook = original_hook

        exec_duration_ms = max(0.0, (time.perf_counter() - start_time) * 1000.0)

        # Record metrics independently (shadow hits never increment production cache hits)
        collector.record_shadow_outcome(
            ns,
            outcome=hypothetical_cache_outcome,
            execution_latency_ms=exec_duration_ms,
            queue_depth=self.queue.qsize(),
        )

        # Persist to database (if PostgreSQL pool is configured)
        if self.db_pool is not None:
            try:
                await self._persist_record(
                    request_id=task.request_id,
                    request_hash=task.request_hash,
                    cache_namespace=task.cache_namespace,
                    provider=task.provider,
                    requested_model=task.requested_model,
                    task_kind=task.task_kind,
                    eligibility_status=eligibility_status,
                    eligibility_rejection_reason=eligibility_rejection_reason,
                    embedding_model=embedding_model,
                    retrieval_status=retrieval_status,
                    candidate_count=candidate_count,
                    top_similarity=top_similarity,
                    runner_up_similarity=runner_up_similarity,
                    similarity_margin=similarity_margin,
                    compatibility_validation_result=compatibility_validation_result,
                    compatibility_rejection_reason=compatibility_rejection_reason,
                    ambiguity_decision=ambiguity_decision,
                    hypothetical_cache_outcome=hypothetical_cache_outcome,
                    actual_response_source=task.actual_response_source,
                    candidate_response_matched=candidate_response_matched,
                    shadow_execution_duration_ms=round(exec_duration_ms, 2),
                    embedding_duration_ms=round(embedding_duration_ms, 2),
                    retrieval_duration_ms=round(retrieval_duration_ms, 2),
                    policy_evaluation_duration_ms=round(policy_duration_ms, 2),
                    failure_category=failure_category,
                )
            except Exception as db_err:
                logger.warning(
                    "Failed to persist shadow evaluation record for request '%s': %s",
                    task.request_id,
                    db_err,
                )

    async def _persist_record(self, **kwargs: Any) -> None:
        """Executes isolated parameterized INSERT into shadow_evaluations."""
        sql = """
        INSERT INTO shadow_evaluations (
            request_id,
            timestamp,
            request_hash,
            cache_namespace,
            provider,
            requested_model,
            task_kind,
            eligibility_status,
            eligibility_rejection_reason,
            embedding_model,
            retrieval_status,
            candidate_count,
            top_similarity,
            runner_up_similarity,
            similarity_margin,
            compatibility_validation_result,
            compatibility_rejection_reason,
            ambiguity_decision,
            hypothetical_cache_outcome,
            actual_response_source,
            candidate_response_matched,
            shadow_execution_duration_ms,
            embedding_duration_ms,
            retrieval_duration_ms,
            policy_evaluation_duration_ms,
            failure_category
        ) VALUES (
            $1, NOW(), $2, $3, $4, $5, $6, $7, $8, $9, $10,
            $11, $12, $13, $14, $15, $16, $17, $18, $19, $20,
            $21, $22, $23, $24, $25
        )
        """
        async with self.db_pool.acquire() as conn:
            await conn.execute(
                sql,
                kwargs["request_id"],
                kwargs["request_hash"],
                kwargs["cache_namespace"],
                kwargs["provider"],
                kwargs["requested_model"],
                kwargs["task_kind"],
                kwargs["eligibility_status"],
                kwargs["eligibility_rejection_reason"],
                kwargs["embedding_model"],
                kwargs["retrieval_status"],
                kwargs["candidate_count"],
                kwargs["top_similarity"],
                kwargs["runner_up_similarity"],
                kwargs["similarity_margin"],
                kwargs["compatibility_validation_result"],
                kwargs["compatibility_rejection_reason"],
                kwargs["ambiguity_decision"],
                kwargs["hypothetical_cache_outcome"],
                kwargs["actual_response_source"],
                kwargs["candidate_response_matched"],
                kwargs["shadow_execution_duration_ms"],
                kwargs["embedding_duration_ms"],
                kwargs["retrieval_duration_ms"],
                kwargs["policy_evaluation_duration_ms"],
                kwargs["failure_category"],
            )

    async def cleanup_old_records(self, retention_days: Optional[int] = None, batch_size: int = 500) -> int:
        """
        Deletes records older than retention_days in bounded batches.
        Prevents unbounded table locks or transaction log bloat.
        """
        if not self.db_pool:
            return 0
        days = retention_days if retention_days is not None else self.settings.shadow_retention_days
        total_deleted = 0
        max_batches = 20

        sql = """
        DELETE FROM shadow_evaluations
        WHERE id IN (
            SELECT id FROM shadow_evaluations
            WHERE timestamp < NOW() - INTERVAL '1 day' * $1
            ORDER BY timestamp ASC
            LIMIT $2
        )
        """
        async with self.db_pool.acquire() as conn:
            for _ in range(max_batches):
                res = await conn.execute(sql, float(days), int(batch_size))
                parts = res.split()
                count = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
                total_deleted += count
                if count < batch_size:
                    break
        return total_deleted

    async def start(self) -> None:
        """Starts worker tasks in the background."""
        if not self.settings.shadow_evaluation_enabled or self._running:
            return
        self._running = True
        self._stopping = False
        num_workers = max(1, min(self.settings.shadow_max_concurrency, 8))
        for i in range(num_workers):
            task = asyncio.create_task(self._worker_loop(), name=f"shadow-worker-{i}")
            self._workers.append(task)
        logger.info(
            "ShadowEvaluationService started with %d workers (queue_size=%d, concurrency=%d, sampling=%.2f)",
            num_workers,
            self.settings.shadow_queue_max_size,
            self.settings.shadow_max_concurrency,
            self.settings.shadow_sampling_rate,
        )

    async def stop(self, drain_timeout: float = 3.0) -> None:
        """
        Gracefully stops workers with a bounded drain period.
        Shuts down ThreadPoolExecutor cleanly.
        """
        if not self._running:
            return
        self._stopping = True
        logger.info("Stopping ShadowEvaluationService (draining up to %.1fs)...", drain_timeout)

        # Drain queue up to drain_timeout
        if not self.queue.empty():
            try:
                await asyncio.wait_for(self.queue.join(), timeout=drain_timeout)
            except asyncio.TimeoutError:
                remaining = self.queue.qsize()
                logger.warning(
                    "Shadow queue drain timed out after %.1fs (%d observations remaining dropped).",
                    drain_timeout,
                    remaining,
                )

        # Cancel workers
        for worker in self._workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()

        # Shut down executor
        self.executor.shutdown(wait=True)
        self._running = False
        logger.info("ShadowEvaluationService stopped.")
