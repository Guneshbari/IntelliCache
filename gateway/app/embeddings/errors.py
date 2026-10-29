"""
Embedding-specific exception hierarchy for IntelliCache.
Provides distinct error classes for timeouts, connections, malformed responses, and configuration.
"""


class EmbeddingError(Exception):
    """Base exception for all embedding-related failures."""

    def __init__(self, message: str, provider: str = "unknown"):
        super().__init__(message)
        self.message = message
        self.provider = provider

    def __str__(self) -> str:
        return f"[{self.provider}] {self.message}"


class EmbeddingConnectionError(EmbeddingError):
    """Raised when connection to the embedding service fails."""
    pass


class EmbeddingTimeoutError(EmbeddingError):
    """Raised when the embedding request times out."""
    pass


class EmbeddingResponseError(EmbeddingError):
    """Raised when the embedding provider returns an error, invalid status, or malformed data."""
    pass


class EmbeddingConfigurationError(EmbeddingError):
    """Raised when embedding configuration is invalid or provider is unsupported."""
    pass
