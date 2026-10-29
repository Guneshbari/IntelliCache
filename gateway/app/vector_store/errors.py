"""
Error hierarchy for vector storage operations.
All errors are isolated from FastAPI, Groq SDK, and Ollama SDK details.
"""

from typing import Optional


class VectorStoreError(Exception):
    """Base exception for all vector storage failures."""

    def __init__(self, message: str, details: Optional[str] = None):
        super().__init__(message)
        self.message = message
        self.details = details

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} ({self.details})"
        return self.message


class VectorDimensionMismatchError(VectorStoreError):
    """Raised when an embedding vector's dimensionality does not match expected store dimensions."""
    pass


class VectorInvalidError(VectorStoreError):
    """Raised when a vector contains invalid, non-numeric, empty, or non-finite elements."""
    pass


class VectorStoreConfigurationError(VectorStoreError):
    """Raised when vector store configuration or database extension requirements are not met."""
    pass


class VectorStoreConnectionError(VectorStoreError):
    """Raised when vector storage cannot connect or communicates with the database failure."""
    pass
