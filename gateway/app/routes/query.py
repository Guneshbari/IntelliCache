"""
Query Route & Compatibility Facade.
Maintains backward compatibility for existing imports and test overrides
while delegating core caching and provider logic to the IntelliCache Core.
"""

from typing import Any, Callable, Optional
from ..api.routes import (
    ClientFactory,
    MessageItem,
    QueryRequest,
    QueryResponse,
    execute_query,
    get_core_service,
    get_groq_client,
    get_groq_client_factory,
    get_llm_provider,
    get_query_cache,
    get_single_flight_coordinator,
    router,
    verify_admin_token,
)
from ..cache.exact_cache import ExactCache, QueryCache
from ..cache.interfaces import BaseCache, CacheEntry, CacheStats
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

__all__ = [
    "BaseCache",
    "CacheEntry",
    "CacheStats",
    "ChatMessage",
    "ClientFactory",
    "ExactCache",
    "GenerationRequest",
    "GenerationResult",
    "GroqProvider",
    "IntelliCacheCore",
    "IntelliCacheError",
    "LLMProvider",
    "MessageItem",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderFactory",
    "ProviderRateLimitError",
    "ProviderUpstreamError",
    "QueryCache",
    "QueryRequest",
    "QueryResponse",
    "SingleFlightCoordinator",
    "StreamChunk",
    "UsageMetrics",
    "build_canonical_messages",
    "execute_query",
    "generate_fingerprint",
    "get_core_service",
    "get_groq_client",
    "get_groq_client_factory",
    "get_llm_provider",
    "get_query_cache",
    "get_single_flight_coordinator",
    "normalize_text",
    "router",
    "sanitize_error_message",
    "verify_admin_token",
]
