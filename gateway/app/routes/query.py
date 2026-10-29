"""
Query route for IntelliCache Gateway.
Handles LLM query execution via Groq, with hardened exact caching,
code-safe whitespace preservation, cancellation-safe single-flight coordination,
streaming failure isolation, lazy provider client lifecycle, administrative token
protection, and API secret redaction.
"""

import asyncio
from datetime import datetime, timezone
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional
from fastapi import APIRouter, Depends, Header, HTTPException, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator
import groq
from groq import AsyncGroq

from app.config import Settings, get_settings

logger = logging.getLogger("intellicache.gateway.query")

router = APIRouter()


# ---------------------------------------------------------------------------
# Security & Secret Sanitization
# ---------------------------------------------------------------------------

def sanitize_error_message(msg: str) -> str:
    """Masks API keys, tokens, file paths, and network addresses from error messages."""
    if not msg:
        return ""
    # Mask Groq keys
    sanitized = re.sub(r"gsk_[A-Za-z0-9]+", "gsk_***", msg)
    # Mask Bearer tokens
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9_\-\.]+", "Bearer ***", sanitized, flags=re.IGNORECASE)
    # Mask local Unix/Windows absolute file paths
    sanitized = re.sub(r"(/[a-zA-Z0-9_\.\-]+)+", "<redacted_path>", sanitized)
    sanitized = re.sub(r"([a-zA-Z]:\\[a-zA-Z0-9_\.\-\\]+)", "<redacted_path>", sanitized)
    # Mask IPv4 addresses
    sanitized = re.sub(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", "<redacted_ip>", sanitized)
    return sanitized


def verify_admin_token(
    settings: Settings = Depends(get_settings),
    x_admin_token: Optional[str] = Header(None, alias="X-Admin-Token"),
    authorization: Optional[str] = Header(None, alias="Authorization"),
) -> None:
    """
    Verifies administrative token for privileged endpoints (such as cache clear).
    Denies access if ADMIN_API_TOKEN is not configured on the server.
    """
    if not settings.admin_api_token:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrative cache clear is disabled: ADMIN_API_TOKEN is not configured on the server.",
        )

    token = x_admin_token
    if not token and authorization:
        parts = authorization.split()
        if len(parts) == 2 and parts[0].lower() == "bearer":
            token = parts[1]

    if not token or not hmac.compare_digest(token, settings.admin_api_token):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Invalid or missing administrative token.",
        )


# ---------------------------------------------------------------------------
# Data Models & Input Bounds Validation
# ---------------------------------------------------------------------------

class MessageItem(BaseModel):
    """Chat message schema."""
    role: str = Field(..., min_length=1, max_length=50, description="Message role: 'system', 'user', or 'assistant'")
    content: str = Field(..., description="Text content of the message")


class QueryRequest(BaseModel):
    """Inbound query request model with payload size validation."""
    prompt: Optional[str] = Field(None, description="The user query or prompt string")
    query: Optional[str] = Field(None, description="Alternative field name for user prompt")
    messages: Optional[List[MessageItem]] = Field(None, description="Optional full chat messages list")
    system_prompt: Optional[str] = Field(None, description="Optional system instruction")
    model: Optional[str] = Field(None, max_length=100, description="Groq model ID; defaults to server GROQ_MODEL")
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0, description="Sampling temperature (0.0 to 2.0)")
    max_tokens: Optional[int] = Field(None, gt=0, description="Maximum tokens to generate")
    stream: bool = Field(False, description="Whether to stream the response via Server-Sent Events (SSE)")
    bypass_cache: bool = Field(False, description="Bypass cache lookup and write, forcing fresh LLM generation")
    conversation_id: Optional[str] = Field(None, max_length=200, description="Optional conversation identifier")

    @model_validator(mode="before")
    @classmethod
    def validate_content_and_payload_limits(cls, data: Any) -> Any:
        if isinstance(data, dict):
            prompt = data.get("prompt")
            query = data.get("query")
            messages = data.get("messages")
            system_prompt = data.get("system_prompt")

            if not prompt and not query and not messages:
                raise ValueError("Either 'prompt', 'query', or 'messages' must be provided.")

            settings = get_settings()

            # Enforce character payload bounds (payload size limits, distinct from token limits)
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


class UsageMetrics(BaseModel):
    """Token usage counters."""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class QueryResponse(BaseModel):
    """Outbound query response model."""
    response: str = Field(..., description="The generated or cached response text")
    cached: bool = Field(..., description="True if served from cache, False if generated by LLM")
    model: str = Field(..., description="Model identifier used for the query")
    latency_ms: float = Field(..., description="Response latency in milliseconds")
    usage: UsageMetrics = Field(default_factory=UsageMetrics, description="Token usage statistics")
    cache_hit_count: Optional[int] = Field(None, description="Number of times this cached response has been reused")
    conversation_id: Optional[str] = Field(None, description="Conversation ID if supplied in the request")
    fingerprint: str = Field(..., description="SHA-256 fingerprint of the normalized query request")
    created_at: str = Field(..., description="ISO 8601 UTC timestamp")


class CacheStats(BaseModel):
    """Cache performance and memory metrics."""
    total_entries: int
    max_entries: int
    hits: int
    misses: int
    coalesced_requests: int = 0
    total_requests: int
    hit_rate: float
    total_tokens_saved: int
    total_latency_saved_ms: float
    ttl_seconds: int
    enabled: bool


# ---------------------------------------------------------------------------
# Conservative Code-Safe Query Cache
# ---------------------------------------------------------------------------

class CacheEntry:
    """Internal cache entry representation."""
    __slots__ = ("response", "model", "usage", "created_at", "last_accessed", "hit_count", "latency_ms")

    def __init__(self, response: str, model: str, usage: UsageMetrics, latency_ms: float):
        self.response = response
        self.model = model
        self.usage = usage
        self.created_at = time.time()
        self.last_accessed = self.created_at
        self.hit_count = 0
        self.latency_ms = latency_ms

    def is_expired(self, ttl_seconds: int) -> bool:
        if ttl_seconds <= 0:
            return False
        return (time.time() - self.created_at) > ttl_seconds


class QueryCache:
    """
    In-memory LRU + TTL Cache for LLM Query responses.
    Features conservative exact normalization preserving code indentation and line breaks.
    """

    def __init__(self, max_entries: int = 1000, ttl_seconds: int = 3600):
        self.max_entries = max_entries
        self.ttl_seconds = ttl_seconds
        self._entries: Dict[str, CacheEntry] = {}
        self._hits: int = 0
        self._misses: int = 0
        self._coalesced_requests: int = 0
        self._tokens_saved: int = 0
        self._latency_saved_ms: float = 0.0

    @staticmethod
    def normalize_text(text: str) -> str:
        """
        Conservative normalization:
        1. Normalizes CRLF (\\r\\n) and CR (\\r) line endings to LF (\\n).
        2. Trims overall outer leading and trailing whitespace of the prompt/message.
        Does NOT strip trailing whitespace from individual lines (preserving Markdown linebreaks).
        Does NOT collapse spaces, tabs, or newlines within lines (preserving code indentation).
        """
        if not text:
            return ""
        normalized = text.replace("\r\n", "\n").replace("\r", "\n")
        return normalized.strip()

    @classmethod
    def build_canonical_messages(
        cls,
        prompt: Optional[str] = None,
        query: Optional[str] = None,
        messages: Optional[List[MessageItem]] = None,
        system_prompt: Optional[str] = None,
    ) -> List[Dict[str, str]]:
        """
        Builds a deterministic, normalized sequence of messages from request parameters.
        Prepends normalized system_prompt if provided.
        """
        canonical_list: List[Dict[str, str]] = []

        if system_prompt:
            norm_system = cls.normalize_text(system_prompt)
            if norm_system:
                canonical_list.append({"role": "system", "content": norm_system})

        if messages:
            for m in messages:
                role = m.role.strip().lower()
                content = cls.normalize_text(m.content)
                canonical_list.append({"role": role, "content": content})
        else:
            effective = prompt or query or ""
            norm_prompt = cls.normalize_text(effective)
            canonical_list.append({"role": "user", "content": norm_prompt})

        return canonical_list

    @classmethod
    def generate_fingerprint(
        cls,
        messages: List[Dict[str, str]],
        model: str,
        temperature: float,
        max_tokens: Optional[int] = None,
    ) -> str:
        """
        Generates a deterministic SHA-256 fingerprint from all answer-affecting inputs:
        - Exact normalized message sequence (role and content)
        - Model identifier
        - Effective temperature (unrounded to preserve exact equivalence)
        - Effective max output tokens
        Uses deterministic JSON serialization with sorted keys.
        """
        canonical_payload = {
            "max_tokens": max_tokens,
            "messages": messages,
            "model": model.strip().lower(),
            "temperature": temperature,
        }
        serialized = json.dumps(
            canonical_payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[CacheEntry]:
        """
        Retrieve an unexpired entry from cache and increment hit counter.
        Does not increment miss counters (delegated to actual generation execution).
        """
        entry = self._entries.get(key)
        if entry is None:
            return None

        if entry.is_expired(self.ttl_seconds):
            del self._entries[key]
            return None

        # Sequential cache hit
        entry.hit_count += 1
        entry.last_accessed = time.time()
        self._hits += 1
        self._tokens_saved += entry.usage.total_tokens
        self._latency_saved_ms += entry.latency_ms
        return entry

    def record_miss(self) -> None:
        """Record an independent request that required upstream generation."""
        self._misses += 1

    def record_coalesced(self, entry: CacheEntry) -> None:
        """Record a follower that shared an in-flight single-flight generation."""
        entry.hit_count += 1
        entry.last_accessed = time.time()
        self._coalesced_requests += 1
        self._tokens_saved += entry.usage.total_tokens
        self._latency_saved_ms += entry.latency_ms

    def set(self, key: str, entry: CacheEntry) -> None:
        """Add or update a cache entry with LRU eviction when capacity is reached."""
        if len(self._entries) >= self.max_entries and key not in self._entries:
            oldest_key = min(self._entries, key=lambda k: self._entries[k].last_accessed)
            del self._entries[oldest_key]

        self._entries[key] = entry

    def clear(self) -> None:
        """Clears all cached entries and resets statistics."""
        self._entries.clear()
        self._hits = 0
        self._misses = 0
        self._coalesced_requests = 0
        self._tokens_saved = 0
        self._latency_saved_ms = 0.0

    def stats(self, enabled: bool = True) -> CacheStats:
        """Returns statistical metrics on cache usage."""
        total_req = self._hits + self._misses + self._coalesced_requests
        reused_req = self._hits + self._coalesced_requests
        hit_rate = (reused_req / total_req) if total_req > 0 else 0.0
        return CacheStats(
            total_entries=len(self._entries),
            max_entries=self.max_entries,
            hits=self._hits,
            misses=self._misses,
            coalesced_requests=self._coalesced_requests,
            total_requests=total_req,
            hit_rate=round(hit_rate, 4),
            total_tokens_saved=self._tokens_saved,
            total_latency_saved_ms=round(self._latency_saved_ms, 2),
            ttl_seconds=self.ttl_seconds,
            enabled=enabled,
        )


# Global cache instance
_cache_instance: Optional[QueryCache] = None


def get_query_cache(settings: Settings = Depends(get_settings)) -> QueryCache:
    """Dependency provider for QueryCache singleton."""
    global _cache_instance
    if not isinstance(settings, Settings):
        settings = get_settings()
    if _cache_instance is None:
        _cache_instance = QueryCache(
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
        )
    return _cache_instance


# ---------------------------------------------------------------------------
# Single-Flight Concurrency Coordinator
# ---------------------------------------------------------------------------

class SingleFlightCoordinator:
    """
    Coordinates concurrent identical cache misses so that simultaneous
    requests share a single in-flight LLM generation.
    Followers await the leader's future safely without cancelling the leader.
    """

    def __init__(self):
        self._in_flight: Dict[str, asyncio.Future[CacheEntry]] = {}
        self._lock = asyncio.Lock()

    async def register_or_wait(
        self, fingerprint: str
    ) -> tuple[bool, asyncio.Future[CacheEntry]]:
        """
        Atomically checks or registers in-flight work for this fingerprint.
        Returns (is_leader, future).
        """
        async with self._lock:
            if fingerprint in self._in_flight:
                return False, self._in_flight[fingerprint]

            loop = asyncio.get_running_loop()
            fut: asyncio.Future[CacheEntry] = loop.create_future()
            self._in_flight[fingerprint] = fut
            return True, fut

    async def complete_success(self, fingerprint: str, entry: CacheEntry) -> None:
        """Leader marks the shared future as resolved with success."""
        async with self._lock:
            fut = self._in_flight.pop(fingerprint, None)
            if fut is not None and not fut.done():
                fut.set_result(entry)

    async def complete_failure(self, fingerprint: str, exc: BaseException) -> None:
        """
        Leader marks the shared future resolved with exception or cancellation.
        Consumes the exception immediately via fut.exception() to prevent
        unretrieved-future warnings when 0 followers are waiting.
        """
        async with self._lock:
            fut = self._in_flight.pop(fingerprint, None)
            if fut is not None and not fut.done():
                if isinstance(exc, asyncio.CancelledError):
                    fut.cancel()
                else:
                    fut.set_exception(exc)
                    # Marking exception as retrieved prevents 'Future exception was never retrieved'
                    try:
                        fut.exception()
                    except Exception:
                        pass


_single_flight_instance: Optional[SingleFlightCoordinator] = None


def get_single_flight_coordinator() -> SingleFlightCoordinator:
    """Returns the singleton SingleFlightCoordinator instance."""
    global _single_flight_instance
    if _single_flight_instance is None:
        _single_flight_instance = SingleFlightCoordinator()
    return _single_flight_instance


# ---------------------------------------------------------------------------
# Groq Client Factory & Provider (Lazy Initialization)
# ---------------------------------------------------------------------------

ClientFactory = Callable[[], AsyncGroq]


def get_groq_client_factory(settings: Settings = Depends(get_settings)) -> ClientFactory:
    """
    Dependency provider for AsyncGroq client factory.
    Does not construct the client immediately, allowing cache hits to execute
    without requiring API keys or network client allocations.
    """
    def _create_client() -> AsyncGroq:
        if not settings.groq_api_key or settings.groq_api_key == "replace_with_your_key":
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail=(
                    "GROQ_API_KEY is not configured or contains a placeholder value. "
                    "Please configure GROQ_API_KEY in gateway/.env"
                ),
            )
        return AsyncGroq(api_key=settings.groq_api_key)

    return _create_client


def get_groq_client(settings: Settings = Depends(get_settings)) -> AsyncGroq:
    """Direct provider kept for backwards compatibility in standalone utility calls."""
    factory = get_groq_client_factory(settings)
    return factory()


# ---------------------------------------------------------------------------
# Query Routes
# ---------------------------------------------------------------------------

@router.post("", response_model=QueryResponse, summary="Execute or reuse cached LLM query")
@router.post("/", response_model=QueryResponse, include_in_schema=False)
async def execute_query(
    request: QueryRequest,
    settings: Settings = Depends(get_settings),
    cache: QueryCache = Depends(get_query_cache),
    client_factory: ClientFactory = Depends(get_groq_client_factory),
    single_flight: SingleFlightCoordinator = Depends(get_single_flight_coordinator),
) -> Any:
    """
    Execute a natural language query against Groq LLM or return a cached response.
    Supports conservative exact matching, single-flight consolidation, and streaming.
    """
    start_time = time.perf_counter()

    # Resolve effective parameters against application defaults
    effective_model = (request.model or settings.groq_model).strip()
    effective_temperature = (
        float(request.temperature) if request.temperature is not None else settings.default_temperature
    )
    effective_max_tokens = (
        request.max_tokens if request.max_tokens is not None else settings.default_max_tokens
    )

    # Build canonical message sequence
    canonical_messages = cache.build_canonical_messages(
        prompt=request.prompt,
        query=request.query,
        messages=request.messages,
        system_prompt=request.system_prompt,
    )

    # Compute deterministic exact-match SHA-256 fingerprint
    fingerprint = cache.generate_fingerprint(
        messages=canonical_messages,
        model=effective_model,
        temperature=effective_temperature,
        max_tokens=effective_max_tokens,
    )

    # -----------------------------------------------------------------------
    # 1. Cache Read (Skipped if bypass_cache=True)
    # -----------------------------------------------------------------------
    if settings.cache_enabled and not request.bypass_cache:
        cached_entry = cache.get(fingerprint)
        if cached_entry is not None:
            latency_ms = (time.perf_counter() - start_time) * 1000.0

            if request.stream:
                async def cached_stream() -> AsyncGenerator[str, None]:
                    chunk_payload = {
                        "delta": cached_entry.response,
                        "done": False,
                        "cached": True,
                        "fingerprint": fingerprint,
                    }
                    yield f"data: {json.dumps(chunk_payload)}\n\n"
                    final_payload = {
                        "delta": "",
                        "done": True,
                        "cached": True,
                        "latency_ms": round(latency_ms, 2),
                        "model": cached_entry.model,
                        "usage": cached_entry.usage.model_dump(),
                        "fingerprint": fingerprint,
                    }
                    yield f"data: {json.dumps(final_payload)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(cached_stream(), media_type="text/event-stream")

            return QueryResponse(
                response=cached_entry.response,
                cached=True,
                model=cached_entry.model,
                latency_ms=round(latency_ms, 2),
                usage=cached_entry.usage,
                cache_hit_count=cached_entry.hit_count,
                conversation_id=request.conversation_id,
                fingerprint=fingerprint,
                created_at=datetime.now(timezone.utc).isoformat(),
            )

    # -----------------------------------------------------------------------
    # 2. Handle Streaming Miss (Streaming bypasses single-flight for now)
    # -----------------------------------------------------------------------
    if request.stream:
        cache.record_miss()

        async def groq_stream_generator() -> AsyncGenerator[str, None]:
            accumulated_text: List[str] = []
            stream_success = False
            client = client_factory()
            try:
                stream = await client.chat.completions.create(
                    model=effective_model,
                    messages=canonical_messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                    stream=True,
                )
                async for chunk in stream:
                    delta_text = ""
                    if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                        delta_text = chunk.choices[0].delta.content
                        accumulated_text.append(delta_text)
                    chunk_payload = {
                        "delta": delta_text,
                        "done": False,
                        "cached": False,
                        "fingerprint": fingerprint,
                    }
                    yield f"data: {json.dumps(chunk_payload)}\n\n"

                stream_success = True
            except asyncio.CancelledError:
                logger.info("Client cancelled stream for fingerprint [%s]", fingerprint[:12])
                raise
            except groq.AuthenticationError as err:
                sanitized_err = sanitize_error_message(err.message)
                logger.error("Provider authentication error during streaming: %s", sanitized_err)
                err_payload = {"error": f"Provider Authentication Failed: {sanitized_err}", "status_code": 401, "done": True}
                yield f"data: {json.dumps(err_payload)}\n\n"
            except groq.RateLimitError as err:
                sanitized_err = sanitize_error_message(err.message)
                logger.warning("Provider rate limit during streaming: %s", sanitized_err)
                err_payload = {"error": f"Provider Rate Limit Exceeded: {sanitized_err}", "status_code": 429, "done": True}
                yield f"data: {json.dumps(err_payload)}\n\n"
            except groq.APIConnectionError as err:
                sanitized_err = sanitize_error_message(err.message)
                logger.error("Provider connection error during streaming: %s", sanitized_err)
                err_payload = {"error": f"Provider API connection failed: {sanitized_err}", "status_code": 503, "done": True}
                yield f"data: {json.dumps(err_payload)}\n\n"
            except groq.APIStatusError as err:
                sanitized_err = sanitize_error_message(err.message)
                logger.error("Provider API error (%s) during streaming: %s", err.status_code, sanitized_err)
                err_payload = {"error": f"Provider Upstream Error ({err.status_code}): {sanitized_err}", "status_code": err.status_code, "done": True}
                yield f"data: {json.dumps(err_payload)}\n\n"
            except Exception as err:
                sanitized_err = sanitize_error_message(str(err))
                logger.error("Unexpected error during streaming for [%s]: %s", fingerprint[:12], sanitized_err)
                err_payload = {"error": f"Internal Gateway Error: {sanitized_err}", "status_code": 500, "done": True}
                yield f"data: {json.dumps(err_payload)}\n\n"
            finally:
                await client.close()

            # Cache ONLY on complete, successful stream (and when not bypassed)
            full_response = "".join(accumulated_text)
            if stream_success and full_response.strip():
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                if settings.cache_enabled and not request.bypass_cache:
                    entry = CacheEntry(
                        response=full_response,
                        model=effective_model,
                        usage=UsageMetrics(),
                        latency_ms=round(elapsed_ms, 2),
                    )
                    cache.set(fingerprint, entry)

                final_payload = {
                    "delta": "",
                    "done": True,
                    "cached": False,
                    "latency_ms": round(elapsed_ms, 2),
                    "model": effective_model,
                    "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                    "fingerprint": fingerprint,
                }
                yield f"data: {json.dumps(final_payload)}\n\n"
                yield "data: [DONE]\n\n"

        return StreamingResponse(groq_stream_generator(), media_type="text/event-stream")

    # -----------------------------------------------------------------------
    # 3. Non-Streaming Execution with Single-Flight Coordination
    # -----------------------------------------------------------------------
    # Bypass requests never join or register in single-flight
    if request.bypass_cache or not settings.cache_enabled:
        cache.record_miss()
        return await _execute_groq_direct(
            client_factory=client_factory,
            effective_model=effective_model,
            canonical_messages=canonical_messages,
            effective_temperature=effective_temperature,
            effective_max_tokens=effective_max_tokens,
            fingerprint=fingerprint,
            conversation_id=request.conversation_id,
            start_time=start_time,
            save_to_cache=False,
            cache=cache,
        )

    # Single-flight coordination for eligible cache misses
    is_leader, shared_future = await single_flight.register_or_wait(fingerprint)

    if not is_leader:
        # Follower: await leader's shielded future without being able to cancel leader
        try:
            cached_entry = await asyncio.shield(shared_future)
            latency_ms = (time.perf_counter() - start_time) * 1000.0
            # Record coalesced hit statistics (distinct from sequential cache hits and misses)
            cache.record_coalesced(cached_entry)
            return QueryResponse(
                response=cached_entry.response,
                cached=True,
                model=cached_entry.model,
                latency_ms=round(latency_ms, 2),
                usage=cached_entry.usage,
                cache_hit_count=cached_entry.hit_count,
                conversation_id=request.conversation_id,
                fingerprint=fingerprint,
                created_at=datetime.now(timezone.utc).isoformat(),
            )
        except asyncio.CancelledError:
            # Follower cancelled - leader continues uninhibited
            raise
        except HTTPException:
            raise
        except Exception as exc:
            # Re-raise leader's exception cleanly
            raise exc

    # Leader: record miss and execute Groq API call, then resolve shared future
    cache.record_miss()
    try:
        response_obj, entry = await _execute_groq_and_cache(
            client_factory=client_factory,
            effective_model=effective_model,
            canonical_messages=canonical_messages,
            effective_temperature=effective_temperature,
            effective_max_tokens=effective_max_tokens,
            fingerprint=fingerprint,
            conversation_id=request.conversation_id,
            start_time=start_time,
            cache=cache,
        )
        await single_flight.complete_success(fingerprint, entry)
        return response_obj
    except BaseException as exc:
        await single_flight.complete_failure(fingerprint, exc)
        raise


# ---------------------------------------------------------------------------
# Internal Groq Execution Helpers
# ---------------------------------------------------------------------------

async def _call_groq_api(
    client: AsyncGroq,
    model: str,
    messages: List[Dict[str, str]],
    temperature: float,
    max_tokens: Optional[int],
    fingerprint_prefix: str = "",
) -> Any:
    """Executes chat completion with Groq SDK with secret-sanitized exceptions."""
    try:
        return await client.chat.completions.create(
            model=model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except groq.AuthenticationError as err:
        sanitized = sanitize_error_message(err.message)
        logger.error("Provider authentication failed for fingerprint [%s]: %s", fingerprint_prefix, sanitized)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Provider Authentication Failed: {sanitized}",
        )
    except groq.RateLimitError as err:
        sanitized = sanitize_error_message(err.message)
        logger.warning("Provider rate limit reached for fingerprint [%s]: %s", fingerprint_prefix, sanitized)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Provider Rate Limit Exceeded: {sanitized}",
        )
    except groq.APIConnectionError as err:
        sanitized = sanitize_error_message(err.message)
        logger.error("Provider connection failed for fingerprint [%s]: %s", fingerprint_prefix, sanitized)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider API connection failed: {sanitized}",
        )
    except groq.APIStatusError as err:
        sanitized = sanitize_error_message(err.message)
        logger.error("Provider API status error (%s) for fingerprint [%s]: %s", err.status_code, fingerprint_prefix, sanitized)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Provider Upstream Error ({err.status_code}): {sanitized}",
        )
    except HTTPException:
        raise
    except Exception as err:
        sanitized = sanitize_error_message(str(err))
        logger.error("Unexpected error for fingerprint [%s]: %s", fingerprint_prefix, sanitized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Gateway Error: {sanitized}",
        )


async def _execute_groq_and_cache(
    client_factory: ClientFactory,
    effective_model: str,
    canonical_messages: List[Dict[str, str]],
    effective_temperature: float,
    effective_max_tokens: Optional[int],
    fingerprint: str,
    conversation_id: Optional[str],
    start_time: float,
    cache: QueryCache,
) -> tuple[QueryResponse, CacheEntry]:
    """Leader execution: calls Groq with client lifecycle management, populates cache."""
    client = client_factory()
    try:
        completion = await _call_groq_api(
            client=client,
            model=effective_model,
            messages=canonical_messages,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            fingerprint_prefix=fingerprint[:12],
        )
    finally:
        await client.close()

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    response_text = ""
    if completion.choices and completion.choices[0].message:
        response_text = completion.choices[0].message.content or ""

    usage = UsageMetrics()
    if completion.usage:
        usage = UsageMetrics(
            prompt_tokens=completion.usage.prompt_tokens or 0,
            completion_tokens=completion.usage.completion_tokens or 0,
            total_tokens=completion.usage.total_tokens or 0,
        )

    entry = CacheEntry(
        response=response_text,
        model=effective_model,
        usage=usage,
        latency_ms=round(latency_ms, 2),
    )
    cache.set(fingerprint, entry)

    response_obj = QueryResponse(
        response=response_text,
        cached=False,
        model=effective_model,
        latency_ms=round(latency_ms, 2),
        usage=usage,
        cache_hit_count=0,
        conversation_id=conversation_id,
        fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    return response_obj, entry


async def _execute_groq_direct(
    client_factory: ClientFactory,
    effective_model: str,
    canonical_messages: List[Dict[str, str]],
    effective_temperature: float,
    effective_max_tokens: Optional[int],
    fingerprint: str,
    conversation_id: Optional[str],
    start_time: float,
    save_to_cache: bool,
    cache: QueryCache,
) -> QueryResponse:
    """Direct execution for bypassed requests: never writes to cache."""
    client = client_factory()
    try:
        completion = await _call_groq_api(
            client=client,
            model=effective_model,
            messages=canonical_messages,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            fingerprint_prefix=fingerprint[:12],
        )
    finally:
        await client.close()

    latency_ms = (time.perf_counter() - start_time) * 1000.0

    response_text = ""
    if completion.choices and completion.choices[0].message:
        response_text = completion.choices[0].message.content or ""

    usage = UsageMetrics()
    if completion.usage:
        usage = UsageMetrics(
            prompt_tokens=completion.usage.prompt_tokens or 0,
            completion_tokens=completion.usage.completion_tokens or 0,
            total_tokens=completion.usage.total_tokens or 0,
        )

    if save_to_cache:
        entry = CacheEntry(
            response=response_text,
            model=effective_model,
            usage=usage,
            latency_ms=round(latency_ms, 2),
        )
        cache.set(fingerprint, entry)

    return QueryResponse(
        response=response_text,
        cached=False,
        model=effective_model,
        latency_ms=round(latency_ms, 2),
        usage=usage,
        cache_hit_count=0,
        conversation_id=conversation_id,
        fingerprint=fingerprint,
        created_at=datetime.now(timezone.utc).isoformat(),
    )


# ---------------------------------------------------------------------------
# Cache Management Endpoints
# ---------------------------------------------------------------------------

@router.get("/cache/stats", response_model=CacheStats, summary="Retrieve cache performance statistics")
async def get_cache_statistics(
    settings: Settings = Depends(get_settings),
    cache: QueryCache = Depends(get_query_cache),
) -> CacheStats:
    """Retrieve real-time cache statistics, hit rate, and resource savings."""
    return cache.stats(enabled=settings.cache_enabled)


@router.post(
    "/cache/clear",
    dependencies=[Depends(verify_admin_token)],
    summary="Clear query cache (Admin token required)",
)
async def clear_query_cache(
    cache: QueryCache = Depends(get_query_cache),
) -> Dict[str, Any]:
    """Purge all entries from the query cache and reset statistics. Requires administrative token."""
    cache.clear()
    return {"status": "ok", "message": "Cache successfully cleared."}
