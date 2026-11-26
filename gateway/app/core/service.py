"""
IntelliCache Core Service.
Orchestrates exact caching, fingerprinting, single-flight coordination, and provider dispatch.
This module MUST NOT import FastAPI or any provider-specific SDK.
"""

import asyncio
import logging
import time
from typing import AsyncGenerator, Optional

logger = logging.getLogger("intellicache.gateway.core")

from .contracts import (
    CACHE_VISIBILITY_PRIVATE,
    CACHE_VISIBILITY_TENANT_SHARED,
    CACHE_VISIBILITY_UNKNOWN,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
    derive_scoped_exact_key,
    normalize_cache_visibility,
    normalize_principal_id,
)
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderUpstreamError,
    SingleFlightLeaderCancelledError,
    sanitize_error_message,
)
from .fingerprinting import derive_variant_exact_fingerprint, generate_fingerprint
from .singleflight import SingleFlightCoordinator
from ..cache.interfaces import BaseCache, CacheEntry
from ..metrics import RuntimeMetricsCollector, get_metrics_collector
from ..providers.interfaces import LLMProvider
from ..semantic_cache.control import (
    SemanticServingController,
    get_semantic_serving_controller,
)
from ..semantic_cache.interfaces import BaseSemanticCache


from ..semantic_cache.context import ScopeResolution, analyze_request_context, resolve_context_cache_scope


class IntelliCacheCore:
    """
    Core engine managing caching decisions, single-flight coalescing,
    and provider-independent LLM inference dispatch.
    """

    def __init__(
        self,
        cache: BaseCache,
        single_flight: Optional[SingleFlightCoordinator] = None,
        cache_enabled: bool = True,
        semantic_cache: Optional[BaseSemanticCache] = None,
        metrics_collector: Optional[RuntimeMetricsCollector] = None,
        semantic_serving_controller: Optional[SemanticServingController] = None,
        semantic_cache_startup_enabled: Optional[bool] = None,
    ):
        self.cache = cache
        self.single_flight = single_flight or SingleFlightCoordinator()
        self.cache_enabled = cache_enabled
        self.semantic_cache = semantic_cache
        self.metrics_collector = metrics_collector
        self.semantic_serving_controller = semantic_serving_controller
        self.semantic_cache_startup_enabled = (
            (semantic_cache is not None)
            if semantic_cache_startup_enabled is None
            else bool(semantic_cache_startup_enabled)
        )

    def _get_collector(self) -> RuntimeMetricsCollector:
        return self.metrics_collector or get_metrics_collector()

    def _get_serving_controller(self) -> Optional[SemanticServingController]:
        return (
            self.semantic_serving_controller
            if self.semantic_serving_controller is not None
            else get_semantic_serving_controller()
        )

    def _check_semantic_serving_runtime(
        self,
        ns: str,
        collector: RuntimeMetricsCollector,
    ) -> bool:
        """
        Fail-closed runtime serving kill-switch check.
        Effective serving requires BOTH startup enablement AND runtime controller True.
        """
        if self.semantic_cache is None or not self.semantic_cache_startup_enabled:
            return False
        controller = self._get_serving_controller()
        if controller is None:
            collector.record_runtime_control_failure(ns)
            return False
        try:
            allowed = controller.is_serving_enabled(ns)
        except Exception:
            collector.record_runtime_control_failure(ns)
            return False
        if not isinstance(allowed, bool):
            collector.record_runtime_control_failure(ns)
            return False
        return allowed

    def _record_exact_scope_rejection(
        self,
        collector: RuntimeMetricsCollector,
        ns: str,
        request: GenerationRequest,
        scope: ScopeResolution,
    ) -> None:
        """Records telemetry when exact/semantic scope resolution fails closed or bypasses caching."""
        reason = getattr(scope, "privacy_rejection_reason", None)
        if reason == "VISIBILITY_MISSING_OR_UNKNOWN" or scope.effective_visibility == CACHE_VISIBILITY_UNKNOWN:
            collector.record_visibility_rejection(ns)
            self._record_semantic_stat_attr(self.semantic_cache, "visibility_rejections")
            return
        if reason == "ADMISSION_REJECTED_CONTEXT_PRIVACY":
            raw_vis = normalize_cache_visibility(getattr(request, "visibility", CACHE_VISIBILITY_PRIVATE))
            if raw_vis == CACHE_VISIBILITY_TENANT_SHARED:
                collector.record_tenant_shared_authorization_rejection(ns)
                self._record_semantic_stat_attr(self.semantic_cache, "tenant_shared_authorization_rejections")
            else:
                collector.record_private_scope_rejection(ns)
                self._record_semantic_stat_attr(self.semantic_cache, "private_scope_rejections")
            if scope.effective_owner is None:
                collector.record_principal_isolation_rejection(ns)
                self._record_semantic_stat_attr(self.semantic_cache, "principal_isolation_rejections")
            return
        collector.record_principal_isolation_rejection(ns)
        self._record_semantic_stat_attr(self.semantic_cache, "principal_isolation_rejections")
        collector.record_private_scope_rejection(ns)
        self._record_semantic_stat_attr(self.semantic_cache, "private_scope_rejections")

    @staticmethod
    def _record_semantic_stat_attr(semantic_cache: Optional[BaseSemanticCache], attr: str) -> None:
        if semantic_cache is None:
            return
        stats = getattr(semantic_cache, "stats", None)
        if stats is not None and hasattr(stats, attr):
            try:
                setattr(stats, attr, getattr(stats, attr) + 1)
            except Exception:
                pass

    async def execute_query(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
    ) -> GenerationResult:
        """
        Executes a non-streaming query request using exact and semantic caching.
        Comprehensive cancellation lifecycle guarantees exactly-once cancellation recording.
        """
        start_time = time.perf_counter()
        collector = self._get_collector()
        ns = request.namespace or "default"
        try:
            return await self._execute_query_impl(
                request=request,
                provider=provider,
                start_time=start_time,
                collector=collector,
                ns=ns,
            )
        except asyncio.CancelledError:
            total_latency_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_cancellation(ns)
            collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
            raise

    async def _execute_query_impl(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
        start_time: float,
        collector: RuntimeMetricsCollector,
        ns: str,
    ) -> GenerationResult:
        """
        Internal implementation of non-streaming query request.
        1. Generates deterministic SHA-256 fingerprint.
        2. Checks exact cache (skipped if bypass_cache=True or cache_enabled=False).
        3. If exact hit: returns cached result immediately without calling provider.
        4. Coordinates in-flight requests via single-flight.
        5. If leader and semantic cache enabled: performs semantic lookup.
        6. If semantic hit: completes single-flight for followers and returns cached result.
        7. If semantic miss: calls configured LLM provider.
        8. Writes exact-cache entry first, releases flight key early, then indexes vector record.
        """
        # Consistent model identity across fingerprint and generation
        base_fingerprint = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )
        fingerprint = derive_variant_exact_fingerprint(base_fingerprint, request.metadata)

        req_ctx = analyze_request_context(request)
        scope = resolve_context_cache_scope(request, req_ctx, fingerprint)
        req_vis = scope.effective_visibility
        req_own = normalize_principal_id(getattr(request, "principal_id", None))
        allow_shared = scope.allow_shared_reads
        can_use_exact_cache = bool(scope.can_cache_exact and scope.exact_storage_key)
        entry_owner = scope.effective_owner
        exact_storage_key = scope.exact_storage_key

        # 1. Exact Cache Read
        if self.cache_enabled and not request.bypass_cache:
            if not can_use_exact_cache:
                self._record_exact_scope_rejection(collector, ns, request, scope)
                collector.record_exact_lookup(ns, latency_ms=0.0)
                collector.record_exact_miss(ns)
            else:
                lookup_start = time.perf_counter()
                cached_entry = await self.cache.get(
                    exact_storage_key,
                    namespace=ns,
                    visibility=req_vis,
                    principal_id=entry_owner,
                    allow_shared_reads=False,
                )
                if cached_entry is None and req_vis == CACHE_VISIBILITY_PRIVATE and allow_shared:
                    cached_entry = await self.cache.get(
                        fingerprint,
                        namespace=ns,
                        visibility=CACHE_VISIBILITY_TENANT_SHARED,
                        principal_id=None,
                        allow_shared_reads=False,
                    )
                lookup_latency_ms = (time.perf_counter() - lookup_start) * 1000.0
                collector.record_exact_lookup(ns, latency_ms=lookup_latency_ms)

                if cached_entry is not None:
                    total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                    latency_saved_ms = max(0.0, cached_entry.latency_ms - total_latency_ms)
                    p_tok = getattr(cached_entry.usage, "prompt_tokens", 0) or 0
                    c_tok = getattr(cached_entry.usage, "completion_tokens", 0) or 0
                    t_tok = getattr(cached_entry.usage, "total_tokens", 0) or 0

                    collector.record_exact_hit(
                        ns,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        latency_saved_ms=latency_saved_ms,
                    )
                    collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)

                    return GenerationResult(
                        text=cached_entry.response,
                        model=cached_entry.model,
                        usage=cached_entry.usage,
                        latency_ms=round(total_latency_ms, 2),
                        cached=True,
                        cache_hit_count=cached_entry.hit_count,
                        fingerprint=fingerprint,
                        metadata=request.metadata,
                        cache_type="exact",
                    )
                else:
                    collector.record_exact_miss(ns)

        # 2. Strict Bypass or Fail-Closed Unidentifiable Privacy Scope
        if request.bypass_cache or not self.cache_enabled or not can_use_exact_cache:
            await self.cache.record_miss()
            collector.record_provider_attempt(ns)
            prov_start = time.perf_counter()
            try:
                result = await provider.generate(request)
                prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                p_tok = getattr(result.usage, "prompt_tokens", 0) or 0
                c_tok = getattr(result.usage, "completion_tokens", 0) or 0
                t_tok = getattr(result.usage, "total_tokens", 0) or 0

                collector.record_provider_success(
                    ns,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    latency_ms=prov_latency_ms,
                )
                collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)

                result.fingerprint = fingerprint
                result.metadata = request.metadata
                result.cache_type = "none"
                return result
            except asyncio.CancelledError:
                prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
                raise
            except BaseException:
                prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                collector.record_provider_failure(ns)
                collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
                collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
                raise

        # 3. Single-Flight Coordination (strictly isolated by namespace, visibility, owner principal, and fingerprint)
        flight_key = (ns, req_vis, entry_owner or "", fingerprint)
        is_leader, shared_future = await self.single_flight.register_or_wait(flight_key)

        if not is_leader:
            # Follower joined in-flight work -> record join immediately
            await self.cache.record_coalesced_join()
            collector.record_coalesced_join(ns)
            try:
                # Follower awaits leader's shared future safely via wait_flight (shielding without event-loop noise)
                cached_entry: CacheEntry = await self.single_flight.wait_flight(
                    shared_future, key=flight_key
                )
                total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                p_tok = getattr(cached_entry.usage, "prompt_tokens", 0) or 0
                c_tok = getattr(cached_entry.usage, "completion_tokens", 0) or 0
                t_tok = getattr(cached_entry.usage, "total_tokens", 0) or 0

                # Record successful reuse for tokens; followers receive the leader's result
                # (including semantic hits with cache_type="semantic") without an independent
                # Checkpoint 2 check and are tracked via coalesced_joins/coalesced_successes
                # (and coalesced_semantic_hits when riding a semantic leader) rather than
                # incrementing semantic_hits twice.
                cache_type = cached_entry.metadata.get("cache_type", "exact")
                await self.cache.record_coalesced_success(cached_entry)
                collector.record_coalesced_success(
                    ns,
                    prompt_tokens=p_tok,
                    completion_tokens=c_tok,
                    total_tokens=t_tok,
                    cache_type=cache_type,
                )
                collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)

                return GenerationResult(
                    text=cached_entry.response,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(total_latency_ms, 2),
                    cached=True,
                    cache_hit_count=cached_entry.hit_count,
                    fingerprint=fingerprint,
                    metadata=request.metadata,
                    cache_type=cache_type,
                )
            except asyncio.CancelledError:
                # Follower cancelled - leader continues unhindered; outer wrapper records cancellation
                raise
            except SingleFlightLeaderCancelledError:
                # Leader cancelled - follower experiences a controlled coalesced failure
                total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                collector.record_coalesced_failure(ns)
                collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
                raise
            except BaseException:
                # Leader failed - error propagated to follower
                total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                collector.record_coalesced_failure(ns)
                collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
                raise

        # Leader execution lifecycle
        flight_completed = False
        try:
            # 4. Leader: Semantic Cache Lookup (two-point runtime serving control check)
            if self.semantic_cache is not None and self.semantic_cache_startup_enabled:
                if not self._check_semantic_serving_runtime(ns, collector):
                    # Checkpoint 1 (pre-lookup): runtime kill switch disabled or failed closed
                    collector.record_semantic_runtime_disabled_skip(ns)
                    self._record_semantic_stat_attr(self.semantic_cache, "runtime_disabled_skips")
                else:
                    semantic_hit = await self.semantic_cache.lookup(request)

                    if semantic_hit is not None:
                        if not self._check_semantic_serving_runtime(ns, collector):
                            # Checkpoint 2 (pre-delivery): runtime serving disabled while lookup was in-flight
                            collector.record_semantic_runtime_disabled_skip(ns)
                            collector.record_upstream_fallback_after_semantic_miss(ns)
                            self._record_semantic_stat_attr(self.semantic_cache, "runtime_disabled_skips")
                            self._record_semantic_stat_attr(self.semantic_cache, "upstream_fallbacks_after_miss")
                            self._record_semantic_stat_attr(self.semantic_cache, "semantic_fallbacks")
                        else:
                            # Intended operational policy: once Checkpoint 2 authorizes delivery,
                            # the leader completes metrics commit, SingleFlight follower resolution,
                            # and response return even if runtime control flips afterward.
                            total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                            latency_saved_ms = max(0.0, semantic_hit.original_latency_ms - total_latency_ms)
                            tokens_saved = semantic_hit.total_tokens

                            # Commit semantic hit counters ONLY after Checkpoint 2 passes
                            if hasattr(self.semantic_cache, "record_served_hit"):
                                try:
                                    self.semantic_cache.record_served_hit(
                                        ns,
                                        cross_conversation=bool(getattr(semantic_hit, "is_cross_conversation", False)),
                                    )
                                except TypeError:
                                    self.semantic_cache.record_served_hit(ns)
                            await self.cache.record_semantic_hit(tokens_saved, latency_saved_ms)
                            collector.record_semantic_hit(
                                ns,
                                prompt_tokens=semantic_hit.prompt_tokens,
                                completion_tokens=semantic_hit.completion_tokens,
                                total_tokens=semantic_hit.total_tokens,
                                latency_saved_ms=latency_saved_ms,
                            )
                            collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)

                            original_usage = UsageMetrics(
                                prompt_tokens=semantic_hit.prompt_tokens,
                                completion_tokens=semantic_hit.completion_tokens,
                                total_tokens=semantic_hit.total_tokens,
                            )

                            hit_entry = CacheEntry(
                                response=semantic_hit.cached_response,
                                model=request.model,
                                usage=original_usage,
                                latency_ms=round(total_latency_ms, 2),
                                provider=request.provider,
                                visibility=req_vis,
                                owner_principal_id=entry_owner,
                                metadata={
                                    "cache_type": "semantic",
                                    "candidate_fingerprint": semantic_hit.fingerprint,
                                    "tokens_saved": tokens_saved,
                                    "latency_saved_ms": round(latency_saved_ms, 2),
                                },
                            )
                            # Complete single-flight for followers with the semantic hit entry
                            flight_completed = True
                            await self.single_flight.complete_success(
                                flight_key, hit_entry, expected_future=shared_future, release=True
                            )
                            return GenerationResult(
                                text=semantic_hit.cached_response,
                                model=request.model,
                                usage=original_usage,
                                latency_ms=round(total_latency_ms, 2),
                                cached=True,
                                cache_hit_count=1,
                                fingerprint=fingerprint,
                                metadata=request.metadata,
                                cache_type="semantic",
                            )
                    else:
                        collector.record_upstream_fallback_after_semantic_miss(ns)
                        self._record_semantic_stat_attr(self.semantic_cache, "upstream_fallbacks_after_miss")
                        self._record_semantic_stat_attr(self.semantic_cache, "semantic_fallbacks")

            # 5. Leader: Semantic miss or disabled -> call provider
            await self.cache.record_miss()
            collector.record_provider_attempt(ns)
            prov_start = time.perf_counter()
            try:
                result = await provider.generate(request)
            except asyncio.CancelledError:
                prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
                raise
            except BaseException:
                prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                collector.record_provider_failure(ns)
                collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
                raise

            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            p_tok = getattr(result.usage, "prompt_tokens", 0) or 0
            c_tok = getattr(result.usage, "completion_tokens", 0) or 0
            t_tok = getattr(result.usage, "total_tokens", 0) or 0

            collector.record_provider_success(
                ns,
                prompt_tokens=p_tok,
                completion_tokens=c_tok,
                total_tokens=t_tok,
                latency_ms=prov_latency_ms,
            )

            result.fingerprint = fingerprint
            result.metadata = request.metadata
            result.cache_type = "none"

            # 6. Complete SingleFlight for followers IMMEDIATELY with the generated response
            entry = CacheEntry(
                response=result.text,
                model=result.model,
                usage=result.usage,
                latency_ms=result.latency_ms,
                provider=request.provider,
                visibility=req_vis,
                owner_principal_id=entry_owner,
                metadata={"cache_type": "exact"},
            )
            flight_completed = True
            await self.single_flight.complete_success(
                flight_key, entry, expected_future=shared_future, release=False
            )

            # 7. Write exact-cache entry under privacy-scoped key
            write_start = time.perf_counter()
            cache_write_succeeded = False
            try:
                await self.cache.set(exact_storage_key, entry, namespace=ns)
                write_latency_ms = (time.perf_counter() - write_start) * 1000.0
                collector.record_latency(ns, "exact_cache_write_latency_ms", write_latency_ms)
                cache_write_succeeded = True
            except asyncio.CancelledError:
                raise
            except Exception as write_err:
                write_latency_ms = (time.perf_counter() - write_start) * 1000.0
                collector.record_latency(ns, "exact_cache_write_latency_ms", write_latency_ms)
                collector.record_cache_write_failure(ns)
                logger.warning(
                    "Exact cache write failed for namespace='%s': %s",
                    ns,
                    sanitize_error_message(str(write_err)),
                )
            finally:
                if cache_write_succeeded:
                    # Early flight release: If exact-cache persistence succeeded, release SingleFlight key immediately,
                    # BEFORE optional/slow semantic indexing, so subsequent requests hit exact cache without waiting
                    await self.single_flight.release(flight_key, expected_future=shared_future)

            # 8. Post-generation: index in vector store if startup-enabled and exact persistence succeeded
            if self.semantic_cache is not None and self.semantic_cache_startup_enabled:
                try:
                    await self.semantic_cache.index(request, result)
                except asyncio.CancelledError:
                    raise
                except Exception as idx_err:
                    logger.warning(
                        "Semantic indexing failed for namespace='%s': %s",
                        ns,
                        sanitize_error_message(str(idx_err)),
                    )
                finally:
                    # If exact-cache write failed, retain flight key during semantic indexing so concurrent identical
                    # requests join the completed in-flight leader rather than triggering duplicate upstream generation.
                    if not cache_write_succeeded:
                        await self.single_flight.release(flight_key, expected_future=shared_future)

            total_latency_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
            return result

        except asyncio.CancelledError as exc:
            if not flight_completed:
                flight_completed = True
                await self.single_flight.complete_failure(
                    flight_key, exc, expected_future=shared_future
                )
            raise
        except BaseException as exc:
            total_latency_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)
            if not flight_completed:
                flight_completed = True
                await self.single_flight.complete_failure(
                    flight_key, exc, expected_future=shared_future
                )
            raise
        finally:
            if not flight_completed:
                try:
                    await self.single_flight.complete_failure(
                        flight_key,
                        SingleFlightLeaderCancelledError("SingleFlight leader aborted unexpectedly"),
                        expected_future=shared_future,
                    )
                except Exception:
                    pass
            else:
                try:
                    await self.single_flight.release(flight_key, expected_future=shared_future)
                except Exception:
                    pass

    async def execute_stream(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Executes a streaming query request.
        Yields normalized StreamChunk objects.
        Caches complete successful streams upon completion.
        Comprehensive cancellation lifecycle guarantees exactly-once cancellation recording.
        """
        start_time = time.perf_counter()
        collector = self._get_collector()
        ns = request.namespace or "default"
        try:
            async for chunk in self._execute_stream_impl(
                request=request,
                provider=provider,
                start_time=start_time,
                collector=collector,
                ns=ns,
            ):
                yield chunk
        except asyncio.CancelledError:
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_cancellation(ns)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            raise

    async def _execute_stream_impl(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
        start_time: float,
        collector: RuntimeMetricsCollector,
        ns: str,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Internal generator implementation for streaming query request.
        Yields normalized StreamChunk objects.
        Caches complete successful streams upon completion.
        """
        base_fingerprint = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )
        fingerprint = derive_variant_exact_fingerprint(base_fingerprint, request.metadata)

        req_ctx = analyze_request_context(request)
        scope = resolve_context_cache_scope(request, req_ctx, fingerprint)
        req_vis = scope.effective_visibility
        req_own = normalize_principal_id(getattr(request, "principal_id", None))
        allow_shared = scope.allow_shared_reads
        can_use_exact_cache = bool(scope.can_cache_exact and scope.exact_storage_key)
        entry_owner = scope.effective_owner
        exact_storage_key = scope.exact_storage_key

        # 1. Cache Read for Streaming
        if self.cache_enabled and not request.bypass_cache:
            if not can_use_exact_cache:
                self._record_exact_scope_rejection(collector, ns, request, scope)
                collector.record_exact_lookup(ns, latency_ms=0.0)
                collector.record_exact_miss(ns)
            else:
                lookup_start = time.perf_counter()
                cached_entry = await self.cache.get(
                    exact_storage_key,
                    namespace=ns,
                    visibility=req_vis,
                    principal_id=entry_owner,
                    allow_shared_reads=False,
                )
                if cached_entry is None and req_vis == CACHE_VISIBILITY_PRIVATE and allow_shared:
                    cached_entry = await self.cache.get(
                        fingerprint,
                        namespace=ns,
                        visibility=CACHE_VISIBILITY_TENANT_SHARED,
                        principal_id=None,
                        allow_shared_reads=False,
                    )
                lookup_latency_ms = (time.perf_counter() - lookup_start) * 1000.0
                collector.record_exact_lookup(ns, latency_ms=lookup_latency_ms)

                if cached_entry is not None:
                    total_latency_ms = (time.perf_counter() - start_time) * 1000.0
                    latency_saved_ms = max(0.0, cached_entry.latency_ms - total_latency_ms)
                    p_tok = getattr(cached_entry.usage, "prompt_tokens", 0) or 0
                    c_tok = getattr(cached_entry.usage, "completion_tokens", 0) or 0
                    t_tok = getattr(cached_entry.usage, "total_tokens", 0) or 0

                    collector.record_exact_hit(
                        ns,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        latency_saved_ms=latency_saved_ms,
                    )
                    collector.record_latency(ns, "total_request_latency_ms", total_latency_ms)

                    yield StreamChunk(
                        delta=cached_entry.response,
                        done=False,
                        cached=True,
                        cache_type="exact",
                        fingerprint=fingerprint,
                        model=cached_entry.model,
                    )
                    yield StreamChunk(
                        delta="",
                        done=True,
                        cached=True,
                        cache_hit_count=cached_entry.hit_count,
                        cache_type="exact",
                        fingerprint=fingerprint,
                        model=cached_entry.model,
                        usage=cached_entry.usage,
                        latency_ms=round(total_latency_ms, 2),
                    )
                    return
                else:
                    collector.record_exact_miss(ns)

        # 2. Streaming Miss
        await self.cache.record_miss()
        collector.record_provider_attempt(ns)
        prov_start = time.perf_counter()
        accumulated_text: list[str] = []
        stream_success = False

        try:
            async for chunk in provider.stream(request):
                if chunk.delta:
                    accumulated_text.append(chunk.delta)
                if not chunk.done:
                    yield StreamChunk(
                        delta=chunk.delta,
                        done=False,
                        cached=False,
                        fingerprint=fingerprint,
                        model=chunk.model or request.model,
                    )
                else:
                    # Final completion chunk
                    stream_success = True
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
                    full_text = "".join(accumulated_text)
                    usage = chunk.usage or UsageMetrics()
                    p_tok = getattr(usage, "prompt_tokens", 0) or 0
                    c_tok = getattr(usage, "completion_tokens", 0) or 0
                    t_tok = getattr(usage, "total_tokens", 0) or 0

                    collector.record_provider_success(
                        ns,
                        prompt_tokens=p_tok,
                        completion_tokens=c_tok,
                        total_tokens=t_tok,
                        latency_ms=prov_latency_ms,
                    )
                    collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)

                    if stream_success and full_text.strip() and self.cache_enabled and not request.bypass_cache and can_use_exact_cache:
                        entry = CacheEntry(
                            response=full_text,
                            model=chunk.model or request.model,
                            usage=usage,
                            latency_ms=round(elapsed_ms, 2),
                            provider=request.provider,
                            visibility=req_vis,
                            owner_principal_id=entry_owner,
                        )
                        write_start = time.perf_counter()
                        try:
                            await self.cache.set(exact_storage_key, entry, namespace=ns)
                            write_latency_ms = (time.perf_counter() - write_start) * 1000.0
                            collector.record_latency(ns, "exact_cache_write_latency_ms", write_latency_ms)
                        except Exception as write_err:
                            write_latency_ms = (time.perf_counter() - write_start) * 1000.0
                            collector.record_latency(ns, "exact_cache_write_latency_ms", write_latency_ms)
                            collector.record_cache_write_failure(ns)
                            logger.warning(
                                "Streaming exact cache write failed for namespace='%s': %s",
                                ns,
                                sanitize_error_message(str(write_err)),
                            )

                    yield StreamChunk(
                        delta="",
                        done=True,
                        cached=False,
                        fingerprint=fingerprint,
                        model=chunk.model or request.model,
                        usage=usage,
                        latency_ms=round(elapsed_ms, 2),
                    )
        except asyncio.CancelledError:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            raise
        except ProviderAuthenticationError as exc:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_provider_failure(ns)
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Authentication Failed: {exc.message}",
                status_code=401,
                fingerprint=fingerprint,
            )
        except ProviderRateLimitError as exc:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_provider_failure(ns)
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Rate Limit Exceeded: {exc.message}",
                status_code=429,
                fingerprint=fingerprint,
            )
        except ProviderConnectionError as exc:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_provider_failure(ns)
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider API connection failed: {exc.message}",
                status_code=503,
                fingerprint=fingerprint,
            )
        except ProviderUpstreamError as exc:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_provider_failure(ns)
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Upstream Error ({exc.status_code}): {exc.message}",
                status_code=exc.status_code,
                fingerprint=fingerprint,
            )
        except BaseException as exc:
            prov_latency_ms = (time.perf_counter() - prov_start) * 1000.0
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            collector.record_provider_failure(ns)
            collector.record_latency(ns, "provider_generation_latency_ms", prov_latency_ms)
            collector.record_latency(ns, "total_request_latency_ms", elapsed_ms)
            sanitized = sanitize_error_message(str(exc))
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Internal Gateway Error: {sanitized}",
                status_code=500,
                fingerprint=fingerprint,
            )
