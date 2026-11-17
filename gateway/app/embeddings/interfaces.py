"""
Abstract interface for provider-independent embedding implementations.
"""

from abc import ABC, abstractmethod
from typing import List, Sequence, Union
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

    async def embed_batch(
        self,
        texts: Sequence[str],
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> List[EmbeddingResult]:
        """
        Generates vector embeddings for a sequence of input texts.
        Preserves input ordering and guarantees len(results) == len(texts).
        Default implementation calls embed() sequentially. Subclasses may override
        with native batch execution.

        Args:
            texts: Sequence of raw input strings to embed.
            task: Explicit task: 'query' or 'document'.

        Returns:
            List of EmbeddingResult objects corresponding to each input text in order.
            Returns an empty list immediately without calling embed() if texts is empty.

        Raises:
            EmbeddingConnectionError: Network / connection failure.
            EmbeddingTimeoutError: Request exceeded configured timeout.
            EmbeddingResponseError: Malformed response, invalid vectors, or HTTP error.
        """
        if not texts:
            return []
        results: List[EmbeddingResult] = []
        for text in texts:
            res = await self.embed(text, task=task)
            results.append(res)
        return results

    @abstractmethod
    async def close(self) -> None:
        """Closes any underlying network connections and releases resources cleanly."""
        pass
