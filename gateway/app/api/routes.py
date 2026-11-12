"""
API layer for the IntelliCache Gateway.
Handles HTTP request parsing, payload validation, SSE formatting, and error mapping.
Delegates caching decisions and inference orchestration to IntelliCache Core.
"""

import asyncio
from datetime import datetime, timezone
import hmac
import ipaddress
import json
import logging
from typing import Any, AsyncGenerator, Callable, Dict, List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field, model_validator

from ..cache.exact_cache import ExactCache, QueryCache
from ..cache.interfaces import BaseCache, CacheEntry, CacheStats
from ..config import Settings, get_settings
from ..context.builder import ContextBuilder
from ..context.contracts import ContextStatus
from ..context.policy import ContextPolicy
from ..conversations.identity import (
    AuthenticatedClient,
    compute_turn_request_hash,
    verify_conversation_client,
)
from ..conversations.repository import (
    ActiveTurnConflictError,
    ConversationNotFoundError,
    IdempotencyConflictError,
)
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
    SingleFlightLeaderCancelledError,
    sanitize_error_message,
)
from ..core.fingerprinting import (
    build_canonical_messages,
    generate_fingerprint,
    normalize_text,
)
from ..core.service import IntelliCacheCore
from ..core.singleflight import SingleFlightCoordinator
from ..metrics import (
    AdminMetricsSnapshot,
    NamespaceMetricsSnapshot,
    RuntimeMetricsCollector,
    get_metrics_collector,
)
from ..providers.factory import ProviderFactory
from ..providers.groq_adapter import GroqProvider
from ..providers.interfaces import LLMProvider
from ..semantic_cache.interfaces import BaseSemanticCache
from ..vector_store.interfaces import BaseVectorStore

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
    turn_id: Optional[str] = None
    context_status: Optional[str] = None
    fingerprint: str
    created_at: str
    cache_type: str = "none"
    idempotent_replay: bool = False


# ---------------------------------------------------------------------------
# Admin & Client Token Dependencies
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


def _is_loopback_address(host: Optional[str]) -> bool:
    """
    Checks if a host/IP string represents a loopback address.
    Supports IPv4 (127.0.0.0/8), IPv6 (::1), and loopback test hosts.
    """
    if not host:
        return False
    host_clean = host.strip().lower()
    if host_clean in ("localhost", "testclient", "testserver"):
        return True
    try:
        ip = ipaddress.ip_address(host_clean)
        return ip.is_loopback
    except ValueError:
        return False


def verify_client_token(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> str:
    """
    Dependency verifying client API token for query endpoints.
    Enforces authentication by default (HTTP 401).
    When ALLOW_UNAUTHENTICATED_LOOPBACK is explicitly true, strictly verifies that
    both the direct peer IP and actual ASGI server address are valid loopback addresses,
    and rejects requests presenting forwarded headers.
    Returns the server-authorized cache_namespace for the authenticated client.
    """
    token_map = settings.get_client_token_map()

    # 1. Extract token candidate from headers
    token_candidate: Optional[str] = request.headers.get("X-Client-Token")
    if not token_candidate:
        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            token_candidate = auth_header[7:].strip()

    # 2. If token candidate is present, verify against configured token_map
    if token_candidate:
        if token_map:
            matched_namespace: Optional[str] = None
            for valid_token, ns in token_map.items():
                if hmac.compare_digest(token_candidate, valid_token):
                    matched_namespace = ns
                    break
            if matched_namespace is not None:
                return matched_namespace

        # Invalid token candidate or token_map empty
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized: Invalid or missing client API token.",
        )

    # 3. If token candidate is NOT present, check if explicit unauthenticated loopback is enabled
    if settings.allow_unauthenticated_loopback:
        # Check forwarded headers first: never allow bypass if forwarded headers are present
        forwarded_headers = ("x-forwarded-for", "x-forwarded-proto", "x-forwarded-host", "forwarded")
        for hdr in forwarded_headers:
            if hdr in request.headers:
                raise HTTPException(
                    status_code=status.HTTP_401_UNAUTHORIZED,
                    detail="Unauthorized: Unauthenticated loopback access rejected when forwarded headers are present.",
                )

        # Validate direct peer IP (request.client.host)
        client_host = request.client.host if request.client else None
        if not _is_loopback_address(client_host):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Direct connection peer is not a valid loopback address.",
            )

        # Validate actual ASGI server address (request.scope.get("server"))
        server_info = request.scope.get("server")
        server_host = server_info[0] if (server_info and len(server_info) > 0) else None
        if not _is_loopback_address(server_host):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Server bind address is not a valid loopback address.",
            )

        # Also verify settings.host is loopback
        if not _is_loopback_address(settings.host):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Unauthorized: Server host setting is not a valid loopback address.",
            )

        return settings.cache_namespace

    # 4. Default: Require authentication! Fail closed with HTTP 401
    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Unauthorized: Invalid or missing client API token.",
    )


def verify_metrics_auth(
    request: Request,
    settings: Settings = Depends(get_settings),
) -> tuple[bool, Optional[str]]:
    """
    Dependency verifying either admin token or client token for statistics endpoints.
    Returns (is_admin, authorized_namespace).
    """
    # 1. Try admin token first if configured
    if settings.admin_api_token:
        admin_token_candidate: Optional[str] = request.headers.get("X-Admin-Token")
        if not admin_token_candidate:
            auth_header = request.headers.get("Authorization", "")
            if auth_header.lower().startswith("bearer "):
                admin_token_candidate = auth_header[7:].strip()
        if admin_token_candidate and hmac.compare_digest(admin_token_candidate, settings.admin_api_token):
            return True, None

    # 2. Try client token (enforces loopback checks, forwarded header checks, or token mapping)
    client_ns = verify_client_token(request, settings)
    return False, client_ns


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


_cache_singleton: Optional[BaseCache] = None
_single_flight_singleton: Optional[SingleFlightCoordinator] = None
_core_singleton: Optional[IntelliCacheCore] = None


def set_query_cache(cache: Optional[BaseCache]) -> None:
    """Set the active cache instance (used by lifecycle manager on startup or tests)."""
    global _cache_singleton, _core_singleton
    _cache_singleton = cache
    _core_singleton = None


def get_query_cache(settings: Settings = Depends(get_settings)) -> BaseCache:
    """Dependency returning singleton BaseCache instance (PostgresCache or ExactCache)."""
    global _cache_singleton
    if not isinstance(settings, Settings):
        settings = get_settings()
    if _cache_singleton is None:
        if settings.cache_backend == "postgres":
            from ..cache.postgres_cache import PostgresCache
            # In postgres mode, if pool not started via lifespan, raise informative error
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="PostgreSQL cache backend selected but connection pool is uninitialized. Start via application lifespan.",
            )
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


_semantic_cache_singleton: Optional[BaseSemanticCache] = None
_vector_store_singleton: Optional[BaseVectorStore] = None


def get_semantic_cache() -> Optional[BaseSemanticCache]:
    """Dependency returning configured BaseSemanticCache singleton (or None if disabled)."""
    return _semantic_cache_singleton


def set_semantic_cache(semantic_cache: Optional[BaseSemanticCache]) -> None:
    """Configures the global semantic cache singleton."""
    global _semantic_cache_singleton
    _semantic_cache_singleton = semantic_cache


def get_vector_store() -> Optional[BaseVectorStore]:
    """Dependency returning configured BaseVectorStore singleton (or None if disabled)."""
    return _vector_store_singleton


def set_vector_store(vector_store: Optional[BaseVectorStore]) -> None:
    """Configures the global vector store singleton."""
    global _vector_store_singleton
    _vector_store_singleton = vector_store


def get_core_service(
    settings: Settings = Depends(get_settings),
    cache: BaseCache = Depends(get_query_cache),
    single_flight: SingleFlightCoordinator = Depends(get_single_flight_coordinator),
    semantic_cache: Optional[BaseSemanticCache] = Depends(get_semantic_cache),
) -> IntelliCacheCore:
    """Dependency returning IntelliCacheCore orchestration service."""
    global _core_singleton
    if not isinstance(settings, Settings):
        settings = get_settings()
    if (
        _core_singleton is None
        or _core_singleton.cache is not cache
        or _core_singleton.semantic_cache is not semantic_cache
    ):
        _core_singleton = IntelliCacheCore(
            cache=cache,
            single_flight=single_flight,
            cache_enabled=settings.cache_enabled,
            semantic_cache=semantic_cache,
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

class TurnLeaseHeartbeat:
    """
    Heartbeat manager that periodically extends the lease of an active conversation turn.
    Guarantees active long-running LLM inferences do not lose their lease to concurrent turns.
    If the lease cannot be renewed (turn was lost, cancelled, recovered, or expired),
    flags lease_lost and triggers immediate generation cancellation.
    """

    def __init__(
        self,
        conv_service: Any,
        turn_id: str,
        lease_token: Optional[str] = None,
        lease_seconds: int = 120,
        interval: Optional[float] = None,
        cancel_callback: Optional[Callable[[], None]] = None,
    ):
        self.conv_service = conv_service
        self.turn_id = turn_id
        self.lease_token = lease_token
        self.lease_seconds = lease_seconds
        # Interval is at most lease_seconds / 3, bounded between 1s and 30s (unless explicitly overridden)
        self.interval = interval if interval is not None else max(1.0, min(30.0, float(lease_seconds) / 3.0))
        self.cancel_callback = cancel_callback
        self._task: Optional[asyncio.Task] = None
        self._stop_event = asyncio.Event()
        self.lease_lost = False

    async def _heartbeat_loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                await asyncio.wait_for(self._stop_event.wait(), timeout=self.interval)
                break
            except asyncio.TimeoutError:
                pass

            if self._stop_event.is_set():
                break

            try:
                renewed = await self.conv_service.renew_turn_lease(
                    self.turn_id,
                    lease_token=self.lease_token,
                    additional_seconds=self.lease_seconds,
                )
                if not renewed:
                    logger.warning(
                        "Turn '%s' lease renewal failed: turn expired, status changed, or lease token mismatch.",
                        self.turn_id,
                    )
                    self.lease_lost = True
                    if self.cancel_callback:
                        try:
                            self.cancel_callback()
                        except Exception:
                            pass
                    break
            except Exception as err:
                logger.warning("Turn '%s' lease heartbeat exception: %s", self.turn_id, err)
                self.lease_lost = True
                if self.cancel_callback:
                    try:
                        self.cancel_callback()
                    except Exception:
                        pass
                break

    def start(self) -> None:
        if self.conv_service and self.turn_id:
            self._task = asyncio.create_task(self._heartbeat_loop())

    async def stop(self) -> None:
        self._stop_event.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None


def enqueue_queue_terminal_signal(queue: asyncio.Queue[Any], item: Any) -> None:
    """
    Non-blockingly delivers a terminal signal (cancellation or exception) to a bounded queue.
    If the queue is full, evicts one pending item to prevent producer deadlocks.
    """
    try:
        queue.put_nowait(item)
    except asyncio.QueueFull:
        try:
            queue.get_nowait()
        except asyncio.QueueEmpty:
            pass
        try:
            queue.put_nowait(item)
        except asyncio.QueueFull:
            pass


@router.post("", response_model=QueryResponse, summary="Execute or reuse cached LLM query", dependencies=[Depends(verify_client_token)])
@router.post("/", response_model=QueryResponse, include_in_schema=False, dependencies=[Depends(verify_client_token)])
async def execute_query(
    request: QueryRequest,
    raw_request: Request,
    settings: Settings = Depends(get_settings),
    core: IntelliCacheCore = Depends(get_core_service),
    provider: LLMProvider = Depends(get_llm_provider),
) -> Any:
    """
    Execute a natural language query against the configured LLM provider
    or return a cached exact response.
    Supports conservative exact matching, single-flight consolidation, streaming,
    and server-managed persistent conversation turns.
    """
    client_namespace = verify_client_token(raw_request, settings)

    # PRE-02: Resolve effective model once consistently
    effective_model = (request.model or settings.groq_model).strip()
    effective_temperature = (
        float(request.temperature) if request.temperature is not None else settings.default_temperature
    )
    effective_max_tokens = (
        request.max_tokens if request.max_tokens is not None else settings.default_max_tokens
    )

    is_server_managed = bool(
        request.conversation_id
        and not request.messages
        and (request.prompt or request.query)
    )

    conv_service = None
    turn_record = None
    effective_context_status: Optional[str] = None
    effective_bypass_cache = request.bypass_cache

    if is_server_managed:
        from .conversations import get_conversation_service

        client = verify_conversation_client(raw_request, settings)

        idempotency_key = raw_request.headers.get("Idempotency-Key") or raw_request.headers.get(
            "X-Idempotency-Key"
        )
        if not idempotency_key or not idempotency_key.strip():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Idempotency-Key header is required for server-managed conversation turns.",
            )
        idempotency_key = idempotency_key.strip()

        conv_service = get_conversation_service(raw_request)
        user_query_text = (request.prompt or request.query or "").strip()

        request_hash = compute_turn_request_hash(
            prompt=user_query_text,
            system_prompt=request.system_prompt,
            model=effective_model,
            temperature=effective_temperature,
            max_tokens=effective_max_tokens,
            stream=request.stream,
            provider=settings.llm_provider,
            bypass_cache=request.bypass_cache,
        )

        request_options = {
            "bypass_cache": request.bypass_cache,
            "model": request.model,
            "temperature": request.temperature,
            "system_prompt": request.system_prompt,
            "stream": request.stream,
        }

        try:
            turn_record, existing_msg, is_replay = await conv_service.begin_turn(
                conversation_id=request.conversation_id,
                owner_principal_id=client.principal_id,
                idempotency_key=idempotency_key,
                user_content=user_query_text,
                request_hash=request_hash,
                lease_duration_seconds=settings.conversation_turn_lease_seconds,
                request_options=request_options,
            )
        except ConversationNotFoundError:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"Conversation '{request.conversation_id}' was not found or is not accessible.",
            )
        except (ActiveTurnConflictError, IdempotencyConflictError) as err:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=str(err),
            )

        if is_replay:
            if request.stream:
                async def replay_sse_stream() -> AsyncGenerator[str, None]:
                    resp_text = existing_msg.content if existing_msg else ""
                    chunk_payload = {
                        "delta": resp_text,
                        "done": False,
                        "cached": False,
                        "cache_type": "none",
                        "fingerprint": "idempotent_replay",
                    }
                    yield f"data: {json.dumps(chunk_payload)}\n\n"
                    final_payload = {
                        "delta": "",
                        "done": True,
                        "cached": False,
                        "cache_type": "none",
                        "latency_ms": 0.0,
                        "model": effective_model,
                        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                        "fingerprint": "idempotent_replay",
                        "conversation_id": request.conversation_id,
                        "turn_id": turn_record.id,
                        "context_status": "complete",
                        "idempotent_replay": True,
                    }
                    yield f"data: {json.dumps(final_payload)}\n\n"
                    yield "data: [DONE]\n\n"

                return StreamingResponse(
                    replay_sse_stream(),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

            return QueryResponse(
                response=existing_msg.content if existing_msg else "",
                cached=False,
                model=effective_model,
                latency_ms=0.0,
                usage=UsageMetrics(),
                cache_hit_count=0,
                conversation_id=request.conversation_id,
                turn_id=turn_record.id,
                context_status="complete",
                fingerprint="idempotent_replay",
                created_at=existing_msg.created_at.isoformat()
                if existing_msg
                else datetime.now(timezone.utc).isoformat(),
                cache_type="none",
                idempotent_replay=True,
            )

        try:
            history, is_incomplete = await conv_service.get_validated_completed_history(
                request.conversation_id,
                client.principal_id,
            )
            context_policy = ContextPolicy(
                max_context_messages=settings.conversation_max_context_messages,
                max_context_chars=settings.conversation_max_context_chars,
            )
            builder = ContextBuilder(context_policy)
            context_result = builder.build_context(
                current_query=user_query_text,
                history=history,
                system_prompt=request.system_prompt,
                is_history_incomplete=is_incomplete,
            )
            canonical_messages = context_result.messages
            effective_context_status = context_result.status.value

            # Exact and semantic cache bypass when context is compacted or incomplete
            if context_result.status != ContextStatus.COMPLETE:
                effective_bypass_cache = True
        except ValueError as val_err:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PAYLOAD_TOO_LARGE",
                lease_token=turn_record.lease_token,
            )
            raise HTTPException(
                status_code=status.HTTP_413_CONTENT_TOO_LARGE,
                detail=str(val_err),
            )
        except Exception as err:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="CONTEXT_BUILD_FAILED",
                lease_token=turn_record.lease_token,
            )
            raise
    else:
        canonical_messages = build_canonical_messages(
            prompt=request.prompt,
            query=request.query,
            messages=request.messages,
            system_prompt=request.system_prompt,
        )

    # Server-derived trusted namespace (clients cannot override via request body)
    gen_request = GenerationRequest(
        model=effective_model,
        messages=canonical_messages,
        temperature=effective_temperature,
        max_tokens=effective_max_tokens,
        provider=settings.llm_provider,
        namespace=client_namespace,
        stream=request.stream,
        bypass_cache=effective_bypass_cache,
        conversation_id=request.conversation_id,
        metadata={
            "turn_id": turn_record.id if turn_record else None,
            "context_status": effective_context_status,
        },
    )

    collector = get_metrics_collector()
    collector.record_request(
        client_namespace,
        stream=request.stream,
        bypass=effective_bypass_cache or not settings.cache_enabled,
    )

    # 1. Streaming Query Execution
    if request.stream:
        async def sse_event_stream() -> AsyncGenerator[str, None]:
            accumulated_text: List[str] = []
            completed_cleanly = False
            turn_conflict_handled = False
            queue: asyncio.Queue[Any] = asyncio.Queue(maxsize=128)
            _STREAM_DONE = object()

            async def stream_producer() -> None:
                try:
                    async for chunk in core.execute_stream(gen_request, provider):
                        await queue.put(chunk)
                    await queue.put(_STREAM_DONE)
                except asyncio.CancelledError:
                    enqueue_queue_terminal_signal(queue, asyncio.CancelledError())
                    raise
                except Exception as exc:
                    enqueue_queue_terminal_signal(queue, exc)

            stream_task = asyncio.create_task(stream_producer())

            heartbeat = None
            if conv_service and turn_record:
                heartbeat = TurnLeaseHeartbeat(
                    conv_service,
                    turn_record.id,
                    lease_token=turn_record.lease_token,
                    lease_seconds=settings.conversation_turn_lease_seconds,
                    cancel_callback=stream_task.cancel,
                )
                heartbeat.start()

            try:
                while True:
                    item = await queue.get()

                    if item is _STREAM_DONE:
                        break

                    if isinstance(item, asyncio.CancelledError) or (heartbeat and heartbeat.lease_lost):
                        if conv_service and turn_record:
                            turn_conflict_handled = True
                            await conv_service.fail_turn(
                                turn_record.id,
                                error_code="LEASE_LOST_CONFLICT",
                                lease_token=turn_record.lease_token,
                            )
                        err_payload = {
                            "error": f"Turn '{turn_record.id if turn_record else ''}' lost its lease during generation.",
                            "status_code": 409,
                            "done": True,
                        }
                        yield f"data: {json.dumps(err_payload)}\n\n"
                        return

                    if isinstance(item, Exception):
                        raise item

                    chunk = item
                    if chunk.error is not None:
                        if conv_service and turn_record:
                            await conv_service.fail_turn(
                                turn_record.id,
                                error_code="STREAM_ERROR",
                                lease_token=turn_record.lease_token,
                            )
                        err_payload = {
                            "error": chunk.error,
                            "status_code": chunk.status_code or 500,
                            "done": True,
                        }
                        yield f"data: {json.dumps(err_payload)}\n\n"
                    elif not chunk.done:
                        if chunk.delta:
                            accumulated_text.append(chunk.delta)
                        chunk_payload = {
                            "delta": chunk.delta,
                            "done": False,
                            "cached": chunk.cached,
                            "cache_type": chunk.cache_type,
                            "fingerprint": chunk.fingerprint,
                        }
                        yield f"data: {json.dumps(chunk_payload)}\n\n"
                    else:
                        if conv_service and turn_record:
                            final_stream_text = "".join(accumulated_text)
                            try:
                                await conv_service.complete_turn(
                                    turn_id=turn_record.id,
                                    assistant_content=final_stream_text,
                                    lease_token=turn_record.lease_token,
                                    metadata={
                                        "model": chunk.model or effective_model,
                                        "fingerprint": chunk.fingerprint,
                                        "cache_type": chunk.cache_type,
                                        "cached": chunk.cached,
                                        "latency_ms": chunk.latency_ms or 0.0,
                                        "cache_hit_count": getattr(chunk, "cache_hit_count", 0),
                                        "usage": chunk.usage.to_dict() if chunk.usage else None,
                                        "context_status": effective_context_status,
                                    },
                                )
                                completed_cleanly = True
                            except ActiveTurnConflictError as conf_err:
                                logger.warning(
                                    "Turn '%s' completion conflict: lease expired or ownership lost: %s",
                                    turn_record.id,
                                    conf_err,
                                )
                                turn_conflict_handled = True
                                await conv_service.fail_turn(
                                    turn_record.id,
                                    error_code="LEASE_LOST_CONFLICT",
                                    lease_token=turn_record.lease_token,
                                )
                                err_payload = {
                                    "error": f"Turn '{turn_record.id}' failed to complete due to lease conflict.",
                                    "status_code": 409,
                                    "done": True,
                                }
                                yield f"data: {json.dumps(err_payload)}\n\n"
                                return
                        else:
                            completed_cleanly = True

                        final_payload = {
                            "delta": "",
                            "done": True,
                            "cached": chunk.cached,
                            "cache_type": chunk.cache_type,
                            "cache_hit_count": chunk.cache_hit_count,
                            "latency_ms": chunk.latency_ms or 0.0,
                            "model": chunk.model or effective_model,
                            "usage": chunk.usage.to_dict()
                            if chunk.usage
                            else {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
                            "fingerprint": chunk.fingerprint,
                            "conversation_id": request.conversation_id,
                            "turn_id": turn_record.id if turn_record else None,
                            "context_status": effective_context_status,
                        }
                        yield f"data: {json.dumps(final_payload)}\n\n"
                        yield "data: [DONE]\n\n"
            except asyncio.CancelledError:
                if conv_service and turn_record:
                    if heartbeat and heartbeat.lease_lost:
                        turn_conflict_handled = True
                        await conv_service.fail_turn(
                            turn_record.id,
                            error_code="LEASE_LOST_CONFLICT",
                            lease_token=turn_record.lease_token,
                        )
                    else:
                        await conv_service.cancel_turn(turn_record.id, lease_token=turn_record.lease_token)
                raise
            except ActiveTurnConflictError as act_err:
                if conv_service and turn_record and not completed_cleanly:
                    turn_conflict_handled = True
                    await conv_service.fail_turn(
                        turn_record.id,
                        error_code="LEASE_LOST_CONFLICT",
                        lease_token=turn_record.lease_token,
                    )
                err_payload = {
                    "error": str(act_err),
                    "status_code": 409,
                    "done": True,
                }
                yield f"data: {json.dumps(err_payload)}\n\n"
                return
            except Exception as e:
                if conv_service and turn_record and not completed_cleanly:
                    await conv_service.fail_turn(
                        turn_record.id,
                        error_code=type(e).__name__,
                        lease_token=turn_record.lease_token,
                    )
                raise
            finally:
                if heartbeat:
                    await heartbeat.stop()
                if not stream_task.done():
                    stream_task.cancel()
                    try:
                        await stream_task
                    except (asyncio.CancelledError, Exception):
                        pass
                if conv_service and turn_record and not completed_cleanly:
                    if not turn_conflict_handled:
                        if heartbeat and heartbeat.lease_lost:
                            await conv_service.fail_turn(
                                turn_record.id,
                                error_code="LEASE_LOST_CONFLICT",
                                lease_token=turn_record.lease_token,
                            )
                        else:
                            await conv_service.cancel_turn(turn_record.id, lease_token=turn_record.lease_token)
                await provider.close()

        return StreamingResponse(sse_event_stream(), media_type="text/event-stream")

    # 2. Non-Streaming Query Execution
    gen_task = asyncio.create_task(core.execute_query(gen_request, provider))
    heartbeat = None
    if conv_service and turn_record:
        heartbeat = TurnLeaseHeartbeat(
            conv_service,
            turn_record.id,
            lease_token=turn_record.lease_token,
            lease_seconds=settings.conversation_turn_lease_seconds,
            cancel_callback=gen_task.cancel,
        )
        heartbeat.start()

    completed_cleanly = False
    try:
        result: GenerationResult = await gen_task
        if heartbeat and heartbeat.lease_lost:
            raise ActiveTurnConflictError(f"Turn '{turn_record.id}' lost its lease during generation.")

        if conv_service and turn_record:
            await conv_service.complete_turn(
                turn_id=turn_record.id,
                assistant_content=result.text,
                lease_token=turn_record.lease_token,
                metadata={
                    "model": result.model,
                    "fingerprint": result.fingerprint,
                    "cache_type": result.cache_type,
                    "cached": result.cached,
                    "latency_ms": result.latency_ms,
                    "cache_hit_count": result.cache_hit_count,
                    "usage": result.usage.to_dict() if result.usage else None,
                    "context_status": effective_context_status,
                },
            )
        completed_cleanly = True
        return QueryResponse(
            response=result.text,
            cached=result.cached,
            model=result.model,
            latency_ms=result.latency_ms,
            usage=result.usage,
            cache_hit_count=result.cache_hit_count,
            conversation_id=request.conversation_id,
            turn_id=turn_record.id if turn_record else None,
            context_status=effective_context_status,
            fingerprint=result.fingerprint,
            created_at=result.created_at,
            cache_type=result.cache_type,
        )
    except asyncio.CancelledError:
        if heartbeat and heartbeat.lease_lost:
            if conv_service and turn_record:
                await conv_service.fail_turn(
                    turn_record.id,
                    error_code="LEASE_LOST_CONFLICT",
                    lease_token=turn_record.lease_token,
                )
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=f"Turn '{turn_record.id}' lost its lease during generation.",
            )
        if conv_service and turn_record and not completed_cleanly:
            await conv_service.cancel_turn(turn_record.id, lease_token=turn_record.lease_token)
        raise
    except ActiveTurnConflictError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="LEASE_LOST_CONFLICT",
                lease_token=turn_record.lease_token,
            )
        logger.warning("Active turn conflict during generation: %s", err)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(err),
        )
    except ProviderAuthenticationError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PROVIDER_AUTH_ERROR",
                lease_token=turn_record.lease_token,
            )
        logger.error("Provider authentication failed for [%s]: %s", err.message[:12], err.message)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Provider Authentication Failed: {err.message}",
        )
    except ProviderRateLimitError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PROVIDER_RATE_LIMIT",
                lease_token=turn_record.lease_token,
            )
        logger.warning("Provider rate limit reached: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=f"Provider Rate Limit Exceeded: {err.message}",
        )
    except ProviderConnectionError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PROVIDER_CONNECTION_ERROR",
                lease_token=turn_record.lease_token,
            )
        logger.error("Provider connection failed: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Provider API Connection Failed: {err.message}",
        )
    except ProviderUpstreamError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PROVIDER_UPSTREAM_ERROR",
                lease_token=turn_record.lease_token,
            )
        logger.error("Provider upstream error (%s): %s", err.status_code, err.message)
        raise HTTPException(
            status_code=err.status_code,
            detail=f"Provider Upstream Error ({err.status_code}): {err.message}",
        )
    except ProviderConfigurationError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="PROVIDER_CONFIG_ERROR",
                lease_token=turn_record.lease_token,
            )
        logger.error("Provider configuration error: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=err.message,
        )
    except SingleFlightLeaderCancelledError as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="LEADER_CANCELLED",
                lease_token=turn_record.lease_token,
            )
        logger.warning("SingleFlight leader query was cancelled: %s", err.message)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Upstream leader request was cancelled. Please retry.",
        )
    except HTTPException:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code="HTTP_EXCEPTION",
                lease_token=turn_record.lease_token,
            )
        raise
    except Exception as err:
        if conv_service and turn_record:
            await conv_service.fail_turn(
                turn_record.id,
                error_code=type(err).__name__,
                lease_token=turn_record.lease_token,
            )
        sanitized = sanitize_error_message(str(err))
        logger.error("Unexpected error during query execution: %s", sanitized)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Internal Gateway Error: {sanitized}",
        )
    finally:
        if heartbeat:
            await heartbeat.stop()
        await provider.close()
