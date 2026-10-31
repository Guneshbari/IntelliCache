"""
Ollama embedding provider adapter for IntelliCache.
Communicates with Ollama's POST /api/embed endpoint using asynchronous HTTP requests.
Applies task-specific prefixes for asymmetric embedding models (e.g., Nomic Embed Text).
"""

import json
import logging
from typing import Any, Dict, List, Optional, Union
import httpx

from .contracts import EmbeddingResult, EmbeddingTask
from .errors import (
    EmbeddingConnectionError,
    EmbeddingResponseError,
    EmbeddingTimeoutError,
)
from .interfaces import EmbeddingProvider

logger = logging.getLogger("intellicache.gateway.embeddings.ollama")


class OllamaEmbeddingProvider(EmbeddingProvider):
    """
    Adapter for generating embeddings via Ollama's local REST API.
    Handles asymmetric task prefixes (e.g. search_query: / search_document: for Nomic models).
    """

    def __init__(
        self,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout_seconds: float = 30.0,
        dimensions: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.model = model.strip()
        self.timeout_seconds = float(timeout_seconds)
        self._dimensions = int(dimensions) if dimensions is not None else (768 if "nomic" in self.model.lower() else None)
        self._custom_client = client
        self._client: Optional[httpx.AsyncClient] = client
        self._owns_client = client is None

    @property
    def model_name(self) -> str:
        """Returns the configured embedding model identifier."""
        return self.model

    @property
    def dimensions(self) -> int:
        """Returns the embedding vector dimensionality (defaults to 768 for Nomic models)."""
        return self._dimensions or 768

    def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout_seconds),
            )
            self._owns_client = True
        return self._client

    def _format_input(self, text: str, task: Union[EmbeddingTask, str]) -> str:
        """
        Applies model-specific retrieval prefixes.
        For Nomic embedding models, task-specific prefixes are required for asymmetric search:
        - search_query: for query embeddings
        - search_document: for document embeddings
        """
        task_str = task.value.lower() if isinstance(task, EmbeddingTask) else str(task).lower().strip()
        if task_str not in ("query", "document"):
            raise EmbeddingResponseError(
                f"Invalid embedding task '{task}'. Expected 'query' or 'document'.",
                provider="ollama",
            )

        if "nomic" in self.model.lower():
            if task_str == "query":
                return f"search_query: {text}"
            elif task_str == "document":
                return f"search_document: {text}"

        return text

    async def embed(
        self,
        text: str,
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> EmbeddingResult:
        """
        Generates a normalized vector embedding using Ollama's POST /api/embed endpoint.
        """
        if not text or not text.strip():
            raise EmbeddingResponseError("Input text to embed cannot be empty or blank.", provider="ollama")

        formatted_input = self._format_input(text, task)
        task_str = task.value if isinstance(task, EmbeddingTask) else str(task)

        logger.debug(
            "Requesting embedding from Ollama (model: %s, task: %s, chars: %d)",
            self.model,
            task_str,
            len(text),
        )

        client = self._get_client()
        request_payload = {
            "model": self.model,
            "input": formatted_input,
        }

        try:
            response = await client.post("/api/embed", json=request_payload)
        except httpx.TimeoutException as exc:
            raise EmbeddingTimeoutError(
                f"Ollama embedding request timed out after {self.timeout_seconds}s: {exc}",
                provider="ollama",
            ) from exc
        except (httpx.ConnectError, httpx.NetworkError) as exc:
            raise EmbeddingConnectionError(
                f"Failed to connect to Ollama at {self.base_url}: {exc}",
                provider="ollama",
            ) from exc
        except httpx.HTTPError as exc:
            raise EmbeddingResponseError(
                f"HTTP transport failure communicating with Ollama: {exc}",
                provider="ollama",
            ) from exc

        if response.status_code != 200:
            error_preview = response.text[:200] if response.text else "No error details"
            raise EmbeddingResponseError(
                f"Ollama returned HTTP {response.status_code}: {error_preview}",
                provider="ollama",
            )

        try:
            data: Dict[str, Any] = response.json()
        except (json.JSONDecodeError, ValueError) as exc:
            raise EmbeddingResponseError(
                f"Malformed JSON response received from Ollama: {exc}",
                provider="ollama",
            ) from exc

        if not isinstance(data, dict):
            raise EmbeddingResponseError(
                f"Expected JSON object from Ollama, received {type(data).__name__}",
                provider="ollama",
            )

        embeddings = data.get("embeddings")
        if not isinstance(embeddings, list) or len(embeddings) == 0:
            raise EmbeddingResponseError(
                "Ollama response missing 'embeddings' array or array is empty.",
                provider="ollama",
            )

        if len(embeddings) != 1:
            raise EmbeddingResponseError(
                f"Expected exactly 1 vector in Ollama response for single text, received {len(embeddings)}.",
                provider="ollama",
            )

        vector = embeddings[0]
        if not isinstance(vector, list) or len(vector) == 0:
            raise EmbeddingResponseError(
                "Vector returned in Ollama 'embeddings[0]' is not a valid non-empty array of numbers.",
                provider="ollama",
            )

        model_name = data.get("model") or self.model
        self._dimensions = len(vector)

        # Instantiating EmbeddingResult validates finite numeric values and dimensionality
        return EmbeddingResult(
            vector=vector,
            model=model_name,
            dimensions=len(vector),
        )

    async def close(self) -> None:
        """Closes the HTTP client if owned by this provider instance."""
        if self._owns_client and self._client is not None and not self._client.is_closed:
            await self._client.aclose()
            self._client = None
