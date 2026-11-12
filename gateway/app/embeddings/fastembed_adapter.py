"""
FastEmbed embedding adapter.
Provides local, CPU-based vector embeddings with zero external daemon requirements.
Uses ONNX Runtime under the hood via the fastembed library.
"""

import asyncio
import logging
from typing import Optional, Union
import httpx

from .contracts import EmbeddingResult, EmbeddingTask
from .errors import (
    EmbeddingConnectionError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from .interfaces import EmbeddingProvider

logger = logging.getLogger("intellicache.gateway.embeddings.fastembed")


class FastEmbedProvider(EmbeddingProvider):
    """
    Local embedding provider utilizing fastembed (ONNX Runtime CPU).
    Generates high-performance embeddings in-process without network overhead.
    """

    def __init__(
        self,
        base_url: str = "",
        model: str = "BAAI/bge-small-en-v1.5",
        timeout_seconds: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url
        self.model_name = (model or "BAAI/bge-small-en-v1.5").strip()
        self.timeout_seconds = float(timeout_seconds) if timeout_seconds > 0 else 30.0
        self._model = None
        self._lock = asyncio.Lock()

    def _get_model(self):
        """Lazily initialize the underlying FastEmbed model."""
        if self._model is None:
            try:
                from fastembed import TextEmbedding
                self._model = TextEmbedding(model_name=self.model_name)
                logger.info("Loaded FastEmbed model: '%s'", self.model_name)
            except Exception as exc:
                raise EmbeddingConnectionError(
                    f"Failed to initialize FastEmbed model '{self.model_name}': {exc}",
                    provider="fastembed",
                ) from exc
        return self._model

    async def embed(
        self,
        text: str,
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> EmbeddingResult:
        """
        Generates a vector embedding for the input text using local FastEmbed model.
        Executes ONNX CPU inference in a separate worker thread to avoid blocking asyncio loop.
        """
        if not text or not text.strip():
            raise EmbeddingResponseError("Input text cannot be empty.", provider="fastembed")

        raw_text = text.strip()

        async with self._lock:
            model = self._get_model()

        def _compute() -> list[float]:
            embeddings = list(model.embed([raw_text]))
            if not embeddings:
                raise EmbeddingResponseError("FastEmbed returned an empty embeddings list.", provider="fastembed")
            return [float(v) for v in embeddings[0]]

        try:
            vector = await asyncio.wait_for(
                asyncio.to_thread(_compute),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise EmbeddingTimeoutError(
                f"FastEmbed embedding generation timed out after {self.timeout_seconds:.1f}s.",
                provider="fastembed",
            ) from exc
        except Exception as exc:
            if isinstance(exc, (EmbeddingResponseError, EmbeddingTimeoutError, EmbeddingConnectionError)):
                raise
            raise EmbeddingResponseError(
                f"FastEmbed embedding error: {exc}",
                provider="fastembed",
            ) from exc

        return EmbeddingResult(
            vector=vector,
            model=self.model_name,
            dimensions=len(vector),
        )

    async def close(self) -> None:
        """Releases the loaded model and associated memory."""
        async with self._lock:
            self._model = None
        logger.info("FastEmbedProvider closed.")
