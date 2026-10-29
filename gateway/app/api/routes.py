"""
API layer for the IntelliCache Gateway.
Handles HTTP request parsing, payload validation, SSE formatting, and error mapping.
Delegates caching decisions and inference orchestration to IntelliCache Core.
"""

from datetime import datetime, timezone
import hmac
import json
import logging
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..cache.exact_cache import ExactCache, QueryCache
from ..cache.interfaces import BaseCache, CacheEntry, CacheStats
from ..config import Settings, get_settings
from ..core.contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from ..core.errors import (
    IntelliCacheError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUpstreamError,
    sanitize_error_message,
)
from ..core.fingerprinting import (
    build_canonical_messages,
    generate_fingerprint,
    normalize_text,
)
from ..core.service import IntelliCacheCore
from ..core.singleflight import SingleFlightCoordinator
from ..providers.factory import ProviderFactory
from ..providers.groq_adapter import GroqProvider
from ..providers.interfaces import LLMProvider

logger = logging.getLogger("intellicache.gateway.api")
router = APIRouter()


# ---------------------------------------------------------------------------
# HTTP Request & Response Schemas
# ---------------------------------------------------------------------------

class MessageItem(BaseModel):
    """Chat message turn."""
    role: str
    content: str


class QueryRequest(BaseModel):
    """Inbound request payload for LLM query execution."""
    prompt: Optional[str] = None
    query: Optional[str] = None
    messages: Optional[List[MessageItem]] = None
    system_prompt: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    stream: bool = False
    bypass_cache: bool = False
    conversation_id: Optional[str] = None

    @model_validator(mode="before")
    @classmethod
    def validate_request_payload(cls, data: Any) -> Any:
        if isinstance(data, dict):
            prompt = data.get("prompt")
            query = data.get("query")
            messages = data.get("messages")
            system_prompt = data.get("system_prompt")

            if not prompt and not query and not messages:
                raise ValueError(
                    "At least one of 'prompt', 'query', or 'messages' must be provided."
                )

            settings = get_settings()
            if prompt and len(prompt) > settings.max_prompt_chars:
                raise ValueError(
                    f"Prompt exceeds payload size limit of {settings.max_prompt_chars} characters."
                )
            if query and len(query) > settings.max_prompt_chars:
                raise ValueError(
                    f"Query exceeds payload size limit of {settings.max_prompt_chars} characters."
                )
            if system_prompt and len(system_prompt) > settings.max_system_prompt_chars:
                raise ValueError(
                    f"System prompt exceeds payload size limit of {settings.max_system_prompt_chars} characters."
                )
            if messages:
                if len(messages) > settings.max_messages_count:
                    raise ValueError(
                        f"Messages count ({len(messages)}) exceeds maximum allowed ({settings.max_messages_count})."
                    )
                for i, m in enumerate(messages):
                    content = m.get("content") if isinstance(m, dict) else getattr(m, "content", None)
                    if content and len(content) > settings.max_message_chars:
                        raise ValueError(
                            f"Message at index {i} exceeds payload limit of {settings.max_message_chars} characters."
                        )

        return data

    def get_effective_prompt(self) -> str:
        """Returns the primary query text from prompt, query, or messages."""
        if self.prompt:
            return self.prompt
        if self.query:
            return self.query
        if self.messages:
            for m in reversed(self.messages):
                if m.role == "user":
                    return m.content
            return self.messages[-1].content
        return ""


class QueryResponse(BaseModel):
    """Outbound response payload for completed LLM query."""
    response: str
    cached: bool = False
    model: str
    latency_ms: float = 0.0
    usage: UsageMetrics = Field(default_factory=UsageMetrics)
    cache_hit_count: int = 0
    conversation_id: Optional[str] = None
    fingerprint: str
    created_at: str


# ---------------------------------------------------------------------------
# Admin Token Dependency
# ---------------------------------------------------------------------------

def verify_admin_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> None:
    """Dependency verifying administrative API token."""
    if not settings.admin_api_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative cache management is disabled: ADMIN_API_TOKEN is not configured on the gateway.",
        )

    token_candidate: Optional[str] = request.headers.get("X-Admin-Token")
    if not token_candidate:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token_candidate = auth_header[7:].strip()

    if not token_candidate or not hmac.compare_digest(token_candidate, settings.admin_api_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forbidden: Invalid or missing administrative token.",
        )


# ---------------------------------------------------------------------------
# Dependency Providers (Singletons & Factories)
# ---------------------------------------------------------------------------

ClientFactory = Any

def get_groq_client_factory(settings: Settings = Depends(get_settings)) -> Any:
    """Dependency provider for AsyncGroq client factory."""
    def _create_client() -> Any:
        if not settings.groq_api_key or settings.groq_api_key == "replace_with_your_key":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "GROQ_API_KEY is not configured or contains a placeholder value. "
                    "Please configure GROQ_API_KEY in gateway/.env"
                ),
            )
        from groq import AsyncGroq
        return AsyncGroq(api_key=settings.groq_api_key)

    return _create_client


def get_groq_client(settings: Settings = Depends(get_settings)) -> Any:
    """Direct provider kept for backwards compatibility in utility calls."""
    factory = get_groq_client_factory(settings)
    return factory()


_cache_singleton: Optional[ExactCache] = None
_single_flight_singleton: Optional[SingleFlightCoordinator] = None
_core_singleton: Optional[IntelliCacheCore] = None


def get_query_cache(settings: Settings = Depends(get_settings)) -> ExactCache:
    """Dependency returning singleton ExactCache instance."""
    global _cache_singleton
    if not isinstance(settings, Settings):
        settings = get_settings()
    if _cache_singleton is None:
        _cache_singleton = ExactCache(
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
        )
    return _cache_singleton


def get_single_flight_coordinator() -> SingleFlightCoordinator:
    """Dependency returning singleton SingleFlightCoordinator."""
    global _single_flight_singleton
    if _single_flight_singleton is None:
        _single_flight_singleton = SingleFlightCoordinator()
    return _single_flight_singleton


def get_core_service(
    settings: Settings = Depends(get_settings),
    cache: ExactCache = Depends(get_query_cache),
    single_flight: SingleFlightCoordinator = Depends(get_single_flight_coordinator),
) -> IntelliCacheCore:
    """Dependency returning IntelliCacheCore orchestration service."""
    global _core_singleton
    if not isinstance(settings, Settings):
        settings = get_settings()
    if _core_singleton is None or _core_singleton.cache is not cache:
        _core_singleton = IntelliCacheCore(
            cache=cache,
            single_flight=single_flight,
            cache_enabled=settings.cache_enabled,
        )
    return _core_singleton


def get_llm_provider(
    settings: Settings = Depends(get_settings),
    client_factory: Any = Depends(get_groq_client_factory),
) -> LLMProvider:
    """
    Dependency creating an LLMProvider adapter based on configuration.
    Uses lazy initialization: client construction and API key validation
    occur only when inference is actually executed on cache misses.
    """
    if not isinstance(settings, Settings):
        settings = get_settings()
    if settings.llm_provider == "groq":
        return GroqProvider(
            api_key=settings.groq_api_key,
            default_model=settings.groq_model,
            client_factory=client_factory,
        )
    return ProviderFactory.create(
        name=settings.llm_provider,
        api_key=settings.groq_api_key,
        default_model=settings.groq_model,
    )


# ---------------------------------------------------------------------------
# API Route Handlers
# ---------------------------------------------------------------------------

@router.post("", response_model=QueryResponse, summary="Execute or reuse cached LLM query")
@router.post("/", response_model=QueryResponse, include_in_schema=False)
async def execute_query(
    request: QueryRequest,
    settings: Settings = Depends(get_settings),
    core: IntelliCacheCore = Depends(get_core_service),
    provider: LLMProvider = Depends(get_llm_provider),
) -> Any:
    """
    Execute a natural language query against the configured LLM provider
    or return a cached exact response.
    Supports conservative exact matching, single-flight consolidation, and streaming.
    """
    # PRE-02: Resolve effective model once consistently
    effective_model = (request.model or settings.groq_model).strip()
    effective_temperature = (
        float(request.temperature) if request.temperature is not None else settings.default_temperature
    )
    effective_max_tokens = (
        request.max_tokens if request.max_tokens is not None else settings.default_max_tokens
    )

    canonical_messages = build_canonical_messages(
        prompt=request.prompt,
        query=request.query,
        messages=request.messages,
        system_prompt=request.system_prompt,
    )

    gen_request = GenerationRequest(
        model=effective_model,
        messages=canonical_messages,
        temperature=effective_temperature,
        max_tokens=effective_max_tokens,
        provider=settings.llm_provider,
        stream=request.stream,
        bypass_cache=request.bypass_cache,
        conversation_id=request.conversation_id,
    )

    # 1. Streaming Query Execution
    if request.stream:
        async def sse_event_stream() -> AsyncGenerator[str, None]:
            try:
                async for chunk in core.execute_stream(gen_request, provider):
                    if chunk.error is not None:
                        err_payload = {
                            "error": chunk.error,
                            "status_code": chunk.status_code or 500,
                            "done": True,
                        }
                        yield f"data: {json.dumps(err_payload)}\n\n"
                    elif not chunk.done:
                        chunk_payload = {
                            "delta": chunk.delta,
                            "done": False,
                            "cached": chunk.cached,
                            "fingerprint": chunk.fingerprint,
                        }
                        yield f"data: {json.dumps(chunk_payload)}\n\n"
                    else:
                        final_payload = {
                            "delta": "",
                            "done": True,
                            "cached": chunk.cached,
                            "latency_ms": chunk.latency_ms or 0.0,
                            "model": chunk.model or effective_model,
                            "usage": chunk.usage.to_dict() if chunk.usage else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "fingerprint": chunk.fingerprint,
                        }
                        yield f"data: {json.dumps(final_payload)}\n\n"
                        yield "data: [DONE]\n\n"
            finally:
                await provider.close()

        return StreamingResponse(sse_event_stream(), media_type="text/event-stream")

    # 2. Non-Streaming Query Execution
    try:
        result: GenerationResult = await core.execute_query(gen_request, provider)
        return QueryResponse(
            response=result.text,
            cached=result.cached,
            model=result.model,
            latency_ms=result.latency_ms,
            usage=result.usage,
            cache_hit_count=result.cache_hit_count,
            conversation_id=request.conversation_id,
            fingerprint=result.fingerprint,
            created_at=result.created_at,
        )
    except ProviderAuthenticationError as err:
        logger.error("Provider authentication failed for [%s]: %s", err.message[:12], err.message)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Provider Authentication Failed: {err.message}",
        )
    except ProviderRateLimitError as err:
        logger.warning("Provider rate limit reached: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Provider Rate Limit Exceeded: {err.message}",
        )
    except ProviderConnectionError as err:
        logger.error("Provider connection failed: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider API Connection Failed: {err.message}",
        )
    except ProviderUpstreamError as err:
        logger.error("Provider upstream error (%s): %s", err.status_code, err.message)
        raise HTTPException(
            status_code=err.status_code,
            detail=f"Provider Upstream Error ({err.status_code}): {err.message}",
        )
    except ProviderConfigurationError as err:
        logger.error("Provider configuration error: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err.message,
        )
    except HTTPException:
        raise
    except Exception as err:
        sanitized = sanitize_error_message(str(err))
        logger.error("Unexpected error during query execution: %s", sanitized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Gateway Error: {sanitized}",
        )
    finally:
        await provider.close()
