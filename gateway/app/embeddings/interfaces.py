"""
Abstract interface for provider-independent embedding implementations.
"""

from abc import ABC, abstractmethod
from typing import Union
from .contracts import EmbeddingResult, EmbeddingTask


class EmbeddingProvider(ABC):
    """
    Abstract provider-independent interface for generating vector embeddings.
    Implementations must handle task prefixes, network requests, timeouts, and resource cleanup.
    """

    @abstractmethod
    async def embed(
        self,
        text: str,
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> EmbeddingResult:
        """
        Generates a vector embedding for the input text and task.

        Args:
            text: Raw input text to embed. Must not be empty.
            task: Explicit task: 'query' or 'document'.

        Returns:
            EmbeddingResult containing vector, model, and dimensions.

        Raises:
            EmbeddingConnectionError: Network / connection failure.
            EmbeddingTimeoutError: Request exceeded configured timeout.
            EmbeddingResponseError: Malformed response, invalid vectors, or HTTP error.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Closes any underlying network connections and releases resources cleanly."""
        pass
