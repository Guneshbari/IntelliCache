"""
Embedding layer for IntelliCache.
Provides provider-independent embedding contracts, interfaces, exceptions,
and adapters (e.g., OllamaEmbeddingProvider).
"""

from .contracts import EmbeddingResult, EmbeddingTask
from .errors import (
    EmbeddingConfigurationError,
    EmbeddingConnectionError,
    EmbeddingError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from .factory import EmbeddingProviderFactory
from .interfaces import EmbeddingProvider
from .ollama_adapter import OllamaEmbeddingProvider

__all__ = [
    "EmbeddingConfigurationError",
    "EmbeddingConnectionError",
    "EmbeddingError",
    "EmbeddingProvider",
    "EmbeddingProviderFactory",
    "EmbeddingResponseError",
    "EmbeddingResult",
    "EmbeddingTask",
    "EmbeddingTimeoutError",
    "OllamaEmbeddingProvider",
]
