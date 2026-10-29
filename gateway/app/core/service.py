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
    ):
        self.cache = cache
        self.single_flight = single_flight or SingleFlightCoordinator()
        self.cache_enabled = cache_enabled

    async def execute_query(
        self,
        request: GenerationRequest,
        provider: LLMProvider,
    ) -> GenerationResult:
        """
        Executes a non-streaming query request using exact-match caching.
        1. Generates deterministic SHA-256 fingerprint.
        2. Checks cache (skipped if bypass_cache=True or cache_enabled=False).
        3. If hit: returns cached result immediately without calling provider.
        4. If miss and not bypassed: coordinates in-flight requests via single-flight.
        5. Caches successful completed responses. Never caches failures.
        """
        start_time = time.perf_counter()

        # PRE-02: Consistent model identity across fingerprint and generation
        fingerprint = generate_fingerprint(
            messages=request.messages,
            model=request.model,
            temperature=request.temperature,
            max_tokens=request.max_tokens,
            provider=request.provider,
        )

        # 1. Cache Read
        if self.cache_enabled and not request.bypass_cache:
            cached_entry = self.cache.get(fingerprint)
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
                )

        # 2. Strict Bypass Semantics
        if request.bypass_cache or not self.cache_enabled:
            self.cache.record_miss()
            result = await provider.generate(request)
            result.fingerprint = fingerprint
            result.metadata = request.metadata
            return result

        # 3. Single-Flight Coordination
        is_leader, shared_future = await self.single_flight.register_or_wait(fingerprint)

        if not is_leader:
            # PRE-01: Follower joined in-flight work -> record join immediately
            self.cache.record_coalesced_join()
            try:
                # Follower awaits leader's shielded future so follower cancellation cannot cancel leader
                cached_entry: CacheEntry = await asyncio.shield(shared_future)
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                # PRE-01: Record successful reuse for tokens and latency saved
                self.cache.record_coalesced_success(cached_entry)
                return GenerationResult(
                    text=cached_entry.response,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(latency_ms, 2),
                    cached=True,
                    cache_hit_count=cached_entry.hit_count,
                    fingerprint=fingerprint,
                    metadata=request.metadata,
                )
            except asyncio.CancelledError:
                # Follower cancelled - leader continues unhindered
                raise
            except BaseException:
                # Leader failed - error propagated to follower, follower already counted in coalesced_requests
                raise

        # Leader: record miss and execute provider generation
        self.cache.record_miss()
        try:
            result = await provider.generate(request)
            result.fingerprint = fingerprint
            result.metadata = request.metadata
            entry = CacheEntry(
                response=result.text,
                model=result.model,
                usage=result.usage,
                latency_ms=result.latency_ms,
            )
            self.cache.set(fingerprint, entry)
            await self.single_flight.complete_success(fingerprint, entry)
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
            cached_entry = self.cache.get(fingerprint)
            if cached_entry is not None:
                latency_ms = (time.perf_counter() - start_time) * 1000.0
                yield StreamChunk(
                    delta=cached_entry.response,
                    done=False,
                    cached=True,
                    fingerprint=fingerprint,
                    model=cached_entry.model,
                )
                yield StreamChunk(
                    delta="",
                    done=True,
                    cached=True,
                    fingerprint=fingerprint,
                    model=cached_entry.model,
                    usage=cached_entry.usage,
                    latency_ms=round(latency_ms, 2),
                )
                return

        # 2. Streaming Miss
        self.cache.record_miss()
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
                        )
                        self.cache.set(fingerprint, entry)

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
