"""
Groq LLM Provider Adapter.
Implements LLMProvider using the Groq Python SDK.
"""

import asyncio
import inspect
import logging
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

import groq
from groq import AsyncGroq

from ..core.contracts import (
    ChatMessage,
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
    ProviderUpstreamError,
)

logger = logging.getLogger("intellicache.gateway.groq")


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
    ):
        self.api_key = api_key
        self.default_model = default_model
        self.client_factory = client_factory
        self._cached_client: Optional[AsyncGroq] = None

    def _create_client(self) -> AsyncGroq:
        """Lazily creates and returns an AsyncGroq instance."""
        if self.client_factory is not None:
            return self.client_factory()

        if not self.api_key or self.api_key.strip() == "" or self.api_key == "replace_with_your_key":
            raise ProviderConfigurationError(
                "GROQ_API_KEY is not configured or contains a placeholder value. "
                "Please configure GROQ_API_KEY in gateway/.env"
            )
        return AsyncGroq(api_key=self.api_key.strip())

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

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """Executes non-streaming generation via Groq completions API."""
        start_time = time.perf_counter()
        effective_model = (request.model or self.default_model).strip()
        client = self._create_client()

        groq_messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        try:
            kwargs: Dict[str, Any] = {
                "model": effective_model,
                "messages": groq_messages,
                "temperature": request.temperature,
                "stream": False,
            }
            if request.max_tokens is not None:
                kwargs["max_tokens"] = request.max_tokens

            response = await client.chat.completions.create(**kwargs)
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0

            content = ""
            if response.choices and len(response.choices) > 0:
                msg = response.choices[0].message
                content = msg.content or ""

            usage = UsageMetrics()
            if hasattr(response, "usage") and response.usage:
                u = response.usage
                usage = UsageMetrics(
                    prompt_tokens=getattr(u, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(u, "completion_tokens", 0) or 0,
                    total_tokens=getattr(u, "total_tokens", 0) or 0,
                )

            return GenerationResult(
                text=content,
                model=effective_model,
                usage=usage,
                latency_ms=round(elapsed_ms, 2),
            )
        except groq.AuthenticationError as err:
            raise ProviderAuthenticationError(err.message) from err
        except groq.RateLimitError as err:
            raise ProviderRateLimitError(err.message) from err
        except groq.APIConnectionError as err:
            raise ProviderConnectionError(err.message) from err
        except groq.APIStatusError as err:
            raise ProviderUpstreamError(err.message, status_code=err.status_code) from err
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(str(err)) from err
        finally:
            await self._close_client(client)

    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        """Executes streaming generation via Groq completions API."""
        effective_model = (request.model or self.default_model).strip()
        client = self._create_client()
        start_time = time.perf_counter()

        groq_messages = [
            {"role": m.role, "content": m.content}
            for m in request.messages
        ]

        try:
            kwargs: Dict[str, Any] = {
                "model": effective_model,
                "messages": groq_messages,
                "temperature": request.temperature,
                "stream": True,
            }
            if request.max_tokens is not None:
                kwargs["max_tokens"] = request.max_tokens

            stream_resp = await client.chat.completions.create(**kwargs)
            async for chunk in stream_resp:
                delta_text = ""
                if chunk.choices and len(chunk.choices) > 0:
                    choice = chunk.choices[0]
                    if hasattr(choice, "delta") and choice.delta and hasattr(choice.delta, "content"):
                        delta_text = choice.delta.content or ""
                yield StreamChunk(delta=delta_text, done=False, model=effective_model)

            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            yield StreamChunk(
                delta="",
                done=True,
                model=effective_model,
                usage=UsageMetrics(),
                latency_ms=round(elapsed_ms, 2),
            )
        except groq.AuthenticationError as err:
            raise ProviderAuthenticationError(err.message) from err
        except groq.RateLimitError as err:
            raise ProviderRateLimitError(err.message) from err
        except groq.APIConnectionError as err:
            raise ProviderConnectionError(err.message) from err
        except groq.APIStatusError as err:
            raise ProviderUpstreamError(err.message, status_code=err.status_code) from err
        except asyncio.CancelledError:
            raise
        except ProviderError:
            raise
        except Exception as err:
            raise ProviderError(str(err)) from err
        finally:
            await self._close_client(client)

    async def close(self) -> None:
        """Closes any cached or active client."""
        if self._cached_client is not None:
            await self._close_client(self._cached_client)
            self._cached_client = None
