"""
IntelliCache Core Service.
Orchestrates exact caching, fingerprinting, single-flight coordination, and provider dispatch.
This module MUST NOT import FastAPI or any provider-specific SDK.
"""

import asyncio
import time
from typing import AsyncGenerator, Optional

from .contracts import (
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from .errors import (
    ProviderAuthenticationError,
    ProviderConnectionError,
    ProviderRateLimitError,
    ProviderUpstreamError,
    sanitize_error_message,
)
from .fingerprinting import generate_fingerprint
from .singleflight import SingleFlightCoordinator
from ..cache.interfaces import BaseCache, CacheEntry
from ..providers.interfaces import LLMProvider
from ..semantic_cache.interfaces import BaseSemanticCache


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
    ):
        self.cache = cache
        self.single_flight = single_flight or SingleFlightCoordinator()
        self.cache_enabled = cache_enabled
        self.semantic_cache = semantic_cache

    async def execute_query(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
    ) -> GenerationResult:
        """
        Executes a non-streaming query request using exact and semantic caching.
        1. Generates deterministic SHA-256 fingerprint.
        2. Checks exact cache (skipped if bypass_cache=True or cache_enabled=False).
        3. If exact hit: returns cached result immediately without calling provider.
        4. Coordinates in-flight requests via single-flight.
        5. If leader and semantic cache enabled: performs semantic lookup.
        6. If semantic hit: completes single-flight for followers and returns cached result.
        7. If semantic miss: calls configured LLM provider.
        8. Writes exact-cache entry first, then indexes vector record if semantic cache enabled.
        """
        start_time = time.perf_counter()

        # Consistent model identity across fingerprint and generation
        fingerprint = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )

        # 1. Exact Cache Read
        if self.cache_enabled and not request.bypass_cache:
            cached_entry = await self.cache.get(fingerprint, namespace=request.namespace)
            if cached_entry is not None:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                return GenerationResult(
                    text=cached_entry.response,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(latency_ms, 2),
                    cached=True,
                    cache_hit_count=cached_entry.hit_count,
                    fingerprint=fingerprint,
                    metadata=request.metadata,
                    cache_type="exact",
                )

        # 2. Strict Bypass Semantics
        if request.bypass_cache or not self.cache_enabled:
            await self.cache.record_miss()
            result = await provider.generate(request)
            result.fingerprint = fingerprint
            result.metadata = request.metadata
            result.cache_type = "none"
            return result

        # 3. Single-Flight Coordination
        is_leader, shared_future = await self.single_flight.register_or_wait(fingerprint)

        if not is_leader:
            # Follower joined in-flight work -> record join immediately
            await self.cache.record_coalesced_join()
            try:
                # Follower awaits leader's shielded future so follower cancellation cannot cancel leader
                cached_entry: CacheEntry = await asyncio.shield(shared_future)
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                # Record successful reuse for tokens and latency saved
                await self.cache.record_coalesced_success(cached_entry)
                cache_type = cached_entry.metadata.get("cache_type", "exact")
                return GenerationResult(
                    text=cached_entry.response,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(latency_ms, 2),
                    cached=True,
                    cache_hit_count=cached_entry.hit_count,
                    fingerprint=fingerprint,
                    metadata=request.metadata,
                    cache_type=cache_type,
                )
            except asyncio.CancelledError:
                # Follower cancelled - leader continues unhindered
                raise
            except BaseException:
                # Leader failed - error propagated to follower, follower already counted in coalesced_requests
                raise

        # 4. Leader: Semantic Cache Lookup (if enabled)
        if self.semantic_cache is not None:
            semantic_hit = await self.semantic_cache.lookup(request)
            if semantic_hit is not None:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                hit_entry = CacheEntry(
                    response=semantic_hit.cached_response,
                    model=request.model,
                    usage=UsageMetrics(),
                    latency_ms=round(latency_ms, 2),
                    provider=request.provider,
                    metadata={
                        "cache_type": "semantic",
                        "candidate_fingerprint": semantic_hit.fingerprint,
                    },
                )
                # Complete single-flight for followers with the semantic hit entry
                # Do not write semantic hit under a new exact fingerprint (Rule 8)
                await self.single_flight.complete_success(fingerprint, hit_entry)
                return GenerationResult(
                    text=semantic_hit.cached_response,
                    model=request.model,
                    usage=UsageMetrics(),
                    latency_ms=round(latency_ms, 2),
                    cached=True,
                    cache_hit_count=1,
                    fingerprint=fingerprint,
                    metadata=request.metadata,
                    cache_type="semantic",
                )

        # 5. Leader: Semantic miss or disabled -> call provider
        await self.cache.record_miss()
        try:
            result = await provider.generate(request)
            result.fingerprint = fingerprint
            result.metadata = request.metadata
            result.cache_type = "none"

            # 6. Write exact-cache entry first
            entry = CacheEntry(
                response=result.text,
                model=result.model,
                usage=result.usage,
                latency_ms=result.latency_ms,
                provider=request.provider,
                metadata={"cache_type": "exact"},
            )
            await self.cache.set(fingerprint, entry, namespace=request.namespace)

            # Complete single-flight for followers
            await self.single_flight.complete_success(fingerprint, entry)

            # 7. Post-generation: index in vector store if semantic cache enabled
            if self.semantic_cache is not None:
                await self.semantic_cache.index(request, result)

            return result
        except BaseException as exc:
            await self.single_flight.complete_failure(fingerprint, exc)
            raise

    async def execute_stream(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
    ) -> AsyncGenerator[StreamChunk, None]:
        """
        Executes a streaming query request.
        Yields normalized StreamChunk objects.
        Caches complete successful streams upon completion.
        """
        start_time = time.perf_counter()

        fingerprint = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )

        # 1. Cache Read for Streaming
        if self.cache_enabled and not request.bypass_cache:
            cached_entry = await self.cache.get(fingerprint, namespace=request.namespace)
            if cached_entry is not None:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
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
                    cache_type="exact",
                    fingerprint=fingerprint,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(latency_ms, 2),
                )
                return

        # 2. Streaming Miss
        await self.cache.record_miss()
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
                    full_text = "".join(accumulated_text)

                    if stream_success and full_text.strip() and self.cache_enabled and not request.bypass_cache:
                        entry = CacheEntry(
                            response=full_text,
                            model=chunk.model or request.model,
                            usage=chunk.usage or UsageMetrics(),
                            latency_ms=round(elapsed_ms, 2),
                            provider=request.provider,
                        )
                        await self.cache.set(fingerprint, entry, namespace=request.namespace)

                    yield StreamChunk(
                        delta="",
                        done=True,
                        cached=False,
                        fingerprint=fingerprint,
                        model=chunk.model or request.model,
                        usage=chunk.usage or UsageMetrics(),
                        latency_ms=round(elapsed_ms, 2),
                    )
        except asyncio.CancelledError:
            raise
        except ProviderAuthenticationError as exc:
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Authentication Failed: {exc.message}",
                status_code=401,
                fingerprint=fingerprint,
            )
        except ProviderRateLimitError as exc:
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Rate Limit Exceeded: {exc.message}",
                status_code=429,
                fingerprint=fingerprint,
            )
        except ProviderConnectionError as exc:
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider API connection failed: {exc.message}",
                status_code=503,
                fingerprint=fingerprint,
            )
        except ProviderUpstreamError as exc:
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Provider Upstream Error ({exc.status_code}): {exc.message}",
                status_code=exc.status_code,
                fingerprint=fingerprint,
            )
        except BaseException as exc:
            sanitized = sanitize_error_message(str(exc))
            yield StreamChunk(
                delta="",
                done=True,
                error=f"Internal Gateway Error: {sanitized}",
                status_code=500,
                fingerprint=fingerprint,
            )
