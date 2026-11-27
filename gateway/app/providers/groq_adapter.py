"""
Groq LLM Provider Adapter.
Implements LLMProvider using the Groq Python SDK with bounded timeouts,
retry classification, streaming usage extraction, and lazy client lifecycle.
"""

import asyncio
import inspect
import logging
import random
import time
from typing import Any, AsyncGenerator, Callable, Dict, Optional, Tuple

import groq
from groq import AsyncGroq

from ..core.contracts import (
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from ..core.errors import (
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderTimeoutError,
    ProviderUpstreamError,
)

logger = logging.getLogger("intellicache.gateway.groq")

_MAX_RETRY_BACKOFF_SECONDS = 4.0


class GroqProvider:
    """
    Adapter for the Groq Cloud API.
    Translates provider-neutral contracts to Groq SDK requests and exceptions.
    Maintains lazy client construction to avoid instantiating connections on cache hits.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        default_model: str = "qwen/qwen3.8-27b",
        client_factory: Optional[Callable[[], AsyncGroq]] = None,
        timeout_seconds: float = 30.0,
        max_retries: int = 0,
        retry_backoff_seconds: float = 0.25,
        attempt_guard: Optional[Any] = None,
    ):
        if isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
            raise ProviderConfigurationError(f"timeout_seconds must be > 0, got {timeout_seconds}.")
        if isinstance(max_retries, bool) or max_retries < 0 or max_retries > 5:
            raise ProviderConfigurationError(f"max_retries must be between 0 and 5, got {max_retries}.")
        self.api_key = api_key
        self.default_model = default_model
        self.client_factory = client_factory
        self.timeout_seconds = float(timeout_seconds)
        self.max_retries = int(max_retries)
        self.retry_backoff_seconds = max(0.0, float(retry_backoff_seconds))
        self.attempt_guard = attempt_guard
        self._cached_client: Optional[AsyncGroq] = None

    def set_attempt_guard(self, guard: Optional[Any]) -> None:
        """Configures or clears the physical attempt reservation guard."""
        self.attempt_guard = guard

    def _create_client(self) -> AsyncGroq:
        """Lazily creates and returns an AsyncGroq instance with bounded timeout."""
        if self.client_factory is not None:
            return self.client_factory()

        if not self.api_key or self.api_key.strip() == "" or self.api_key == "replace_with_your_key":
            raise ProviderConfigurationError(
                "GROQ_API_KEY is not configured or contains a placeholder value. "
                "Please configure GROQ_API_KEY in gateway/.env"
            )
        return AsyncGroq(
            api_key=self.api_key.strip(),
            timeout=self.timeout_seconds,
            max_retries=0,
        )

    @staticmethod
    async def _close_client(client: Optional[AsyncGroq]) -> None:
        """Explicitly closes an AsyncGroq client."""
        if client is None:
            return
        try:
            close_method = getattr(client, "close", None)
            if close_method is not None:
                if inspect.iscoroutinefunction(close_method):
                    await close_method()
                else:
                    close_method()
        except Exception as e:
            logger.debug("Error closing Groq client: %s", e)

    @staticmethod
    def _extract_usage(usage_obj: Any) -> Tuple[UsageMetrics, bool]:
        """Extracts real integer token usage when present without fabricating counts."""
        if usage_obj is None:
            return UsageMetrics(), False
        pt_raw = getattr(usage_obj, "prompt_tokens", None)
        ct_raw = getattr(usage_obj, "completion_tokens", None)
        tt_raw = getattr(usage_obj, "total_tokens", None)
        has_pt = isinstance(pt_raw, int) and not isinstance(pt_raw, bool)
        has_ct = isinstance(ct_raw, int) and not isinstance(ct_raw, bool)
        has_tt = isinstance(tt_raw, int) and not isinstance(tt_raw, bool)
        if not (has_pt or has_ct or has_tt):
            return UsageMetrics(), False
        pt = pt_raw if has_pt else 0
        ct = ct_raw if has_ct else 0
        tt = tt_raw if has_tt else (pt + ct)
        return UsageMetrics(prompt_tokens=pt, completion_tokens=ct, total_tokens=tt), True

    @staticmethod
    def _is_retryable_exception(err: BaseException) -> bool:
        if isinstance(err, ProviderError):
            return False
        timeout_cls = getattr(groq, "APITimeoutError", None)
        if timeout_cls is not None and isinstance(err, timeout_cls):
            return True
        if isinstance(err, (groq.RateLimitError, groq.APIConnectionError)):
            return True
        if isinstance(err, groq.APIStatusError):
            code = getattr(err, "status_code", 0) or 0
            return code == 429 or code >= 500
        return False

    @staticmethod
    def _translate_exception(err: BaseException, *, retry_count: int = 0) -> ProviderError:
        if isinstance(err, ProviderError):
            err.retry_count = max(err.retry_count, retry_count)
            return err
        timeout_cls = getattr(groq, "APITimeoutError", None)
        if timeout_cls is not None and isinstance(err, timeout_cls):
            return ProviderTimeoutError(getattr(err, "message", str(err)), retry_count=retry_count)
        if isinstance(err, groq.AuthenticationError):
            return ProviderAuthenticationError(err.message, retry_count=retry_count)
        if isinstance(err, groq.RateLimitError):
            return ProviderRateLimitError(err.message, retry_count=retry_count)
        if isinstance(err, groq.APIConnectionError):
            return ProviderConnectionError(err.message, retry_count=retry_count)
        if isinstance(err, groq.APIStatusError):
            return ProviderUpstreamError(err.message, status_code=err.status_code, retry_count=retry_count)
        return ProviderError(str(err), retry_count=retry_count)

    @staticmethod
    def _parse_retry_after_seconds(err: Optional[BaseException]) -> Optional[float]:
        resp = getattr(err, "response", None) if err is not None else None
        headers = getattr(resp, "headers", None) if resp is not None else None
        if not headers:
            return None
        raw = headers.get("retry-after") or headers.get("Retry-After")
        if raw is None:
            return None
        try:
            val = float(str(raw).strip())
            if val > 0:
                return min(10.0, val)
        except (TypeError, ValueError):
            return None
        return None

    async def _sleep_backoff(self, attempt: int, err: Optional[BaseException] = None) -> None:
        if self.retry_backoff_seconds <= 0:
            return
        delay = min(_MAX_RETRY_BACKOFF_SECONDS, self.retry_backoff_seconds * (2 ** attempt))
        jitter = random.uniform(0.0, delay * 0.2)
        wait_s = delay + jitter
        retry_after = self._parse_retry_after_seconds(err)
        if retry_after is not None:
            wait_s = max(wait_s, min(10.0, retry_after + 0.15))
        await asyncio.sleep(wait_s)

    @staticmethod
    def _build_kwargs(request: GenerationRequest, effective_model: str, *, stream: bool) -> Dict[str, Any]:
        groq_messages = [{"role": m.role, "content": m.content} for m in request.messages]
        kwargs: Dict[str, Any] = {
            "model": effective_model,
            "messages": groq_messages,
            "temperature": request.temperature,
            "stream": stream,
        }
        if request.max_tokens is not None:
            kwargs["max_tokens"] = request.max_tokens
        return kwargs

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Executes non-streaming generation via Groq completions API with bounded retries."""
        start_time = time.perf_counter()
        effective_model = (request.model or self.default_model).strip()
        kwargs = self._build_kwargs(request, effective_model, stream=False)

        retries_executed = 0
        for attempt in range(self.max_retries + 1):
            reservation = None
            if self.attempt_guard is not None:
                reservation = await self.attempt_guard.reserve_attempt(request, attempt=attempt, is_streaming=False)
            client = self._create_client()
            try:
                response = await client.chat.completions.create(**kwargs)
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

                content = ""
                finish_reason: Optional[str] = None
                if response.choices and len(response.choices) > 0:
                    msg = response.choices[0].message
                    content = msg.content or ""
                    raw_fr = getattr(response.choices[0], "finish_reason", None)
                    finish_reason = raw_fr if isinstance(raw_fr, str) else None

                usage, usage_available = self._extract_usage(getattr(response, "usage", None))
                if reservation is not None:
                    await reservation.record_success(usage, usage_available=usage_available)
                return GenerationResult(
                    text=content,
                    model=effective_model,
                    usage=usage,
                    latency_ms=round(elapsed_ms, 2),
                    finish_reason=finish_reason,
                    metadata={
                        "usage_available": usage_available,
                        "retry_count": retries_executed,
                    },
                )
            except asyncio.CancelledError as cancel_err:
                if reservation is not None:
                    await reservation.record_failure(cancel_err)
                raise
            except Exception as err:
                if reservation is not None:
                    await reservation.record_failure(err)
                if attempt < self.max_retries and self._is_retryable_exception(err):
                    retries_executed += 1
                    logger.warning(
                        "Retryable Groq error on attempt %d/%d (%s); retrying.",
                        attempt + 1,
                        self.max_retries + 1,
                        type(err).__name__,
                    )
                    await self._sleep_backoff(attempt, err)
                    continue
                raise self._translate_exception(err, retry_count=retries_executed) from err
            finally:
                await self._close_client(client)

        raise ProviderError("Groq retry loop exited unexpectedly.", retry_count=retries_executed)

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        """Executes streaming generation via Groq completions API without retrying mid-stream."""
        effective_model = (request.model or self.default_model).strip()
        kwargs = self._build_kwargs(request, effective_model, stream=True)
        start_time = time.perf_counter()

        retries_executed = 0
        client: Optional[AsyncGroq] = None
        stream_resp: Any = None
        reservation = None
        for attempt in range(self.max_retries + 1):
            if self.attempt_guard is not None:
                reservation = await self.attempt_guard.reserve_attempt(request, attempt=attempt, is_streaming=True)
            client = self._create_client()
            try:
                stream_resp = await client.chat.completions.create(**kwargs)
                break
            except asyncio.CancelledError as cancel_err:
                if reservation is not None:
                    await reservation.record_failure(cancel_err)
                await self._close_client(client)
                raise
            except Exception as err:
                if reservation is not None:
                    await reservation.record_failure(err)
                await self._close_client(client)
                if attempt < self.max_retries and self._is_retryable_exception(err):
                    retries_executed += 1
                    await self._sleep_backoff(attempt, err)
                    continue
                raise self._translate_exception(err, retry_count=retries_executed) from err

        try:
            last_finish_reason: Optional[str] = None
            captured_usage: Optional[UsageMetrics] = None
            usage_available = False

            async for chunk in stream_resp:
                delta_text = ""
                if chunk.choices and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    raw_fr = getattr(choice, "finish_reason", None)
                    if isinstance(raw_fr, str) and raw_fr:
                        last_finish_reason = raw_fr
                    if hasattr(choice, "delta") and choice.delta and hasattr(choice.delta, "content"):
                        delta_text = choice.delta.content or ""

                # Check standard chunk.usage and Groq-specific chunk.x_groq.usage
                for usage_candidate in (
                    getattr(chunk, "usage", None),
                    getattr(getattr(chunk, "x_groq", None), "usage", None),
                ):
                    parsed_usage, has_usage = self._extract_usage(usage_candidate)
                    if has_usage:
                        captured_usage = parsed_usage
                        usage_available = True

                yield StreamChunk(delta=delta_text, done=False, model=effective_model)

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            if reservation is not None:
                await reservation.record_success(captured_usage, usage_available=usage_available)
            yield StreamChunk(
                delta="",
                done=True,
                model=effective_model,
                usage=captured_usage or UsageMetrics(),
                latency_ms=round(elapsed_ms, 2),
                finish_reason=last_finish_reason,
                metadata={
                    "usage_available": usage_available,
                    "retry_count": retries_executed,
                },
            )
        except asyncio.CancelledError as cancel_err:
            if reservation is not None:
                await reservation.record_failure(cancel_err)
            raise
        except Exception as err:
            if reservation is not None:
                await reservation.record_failure(err)
            raise self._translate_exception(err, retry_count=retries_executed) from err
        finally:
            await self._close_client(client)

    async def close(self) -> None:
        """Closes any cached or active client."""
        if self._cached_client is not None:
            await self._close_client(self._cached_client)
            self._cached_client = None
