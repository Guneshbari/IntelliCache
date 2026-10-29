"""
IntelliCache Core Domain and Orchestration exports.
"""

from .contracts import (
    ChatMessage,
    GenerationRequest,
    GenerationResult,
    StreamChunk,
    UsageMetrics,
)
from .errors import (
    IntelliCacheError,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderConnectionError,
    ProviderError,
    ProviderRateLimitError,
    ProviderUpstreamError,
    sanitize_error_message,
)
from .fingerprinting import (
    build_canonical_messages,
    generate_fingerprint,
    normalize_text,
)
from .singleflight import SingleFlightCoordinator
from .service import IntelliCacheCore

__all__ = [
    "ChatMessage",
    "GenerationRequest",
    "GenerationResult",
    "StreamChunk",
    "UsageMetrics",
    "IntelliCacheError",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderConnectionError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderUpstreamError",
    "sanitize_error_message",
    "build_canonical_messages",
    "generate_fingerprint",
    "normalize_text",
    "SingleFlightCoordinator",
    "IntelliCacheCore",
]
