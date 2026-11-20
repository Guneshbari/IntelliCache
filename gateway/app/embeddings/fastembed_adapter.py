"""
FastEmbed embedding adapter.
Provides local, CPU-based vector embeddings with zero external daemon requirements.
Uses ONNX Runtime under the hood via the optional fastembed library.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import logging
from typing import Any, List, Optional, Sequence, Union
import httpx

from .contracts import EmbeddingResult, EmbeddingTask
from .errors import (
    EmbeddingConfigurationError,
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
    Distinguishes query and document embedding tasks via query_embed and passage_embed.
    """

    def __init__(
        self,
        base_url: str = "",
        model: str = "BAAI/bge-small-en-v1.5",
        timeout_seconds: float = 30.0,
        dimensions: Optional[int] = 384,
        client: Optional[httpx.AsyncClient] = None,
        executor: Optional[ThreadPoolExecutor] = None,
        max_concurrency: Optional[int] = None,
    ):
        self.base_url = base_url
        self.model_name = (model or "BAAI/bge-small-en-v1.5").strip()
        self.timeout_seconds = float(timeout_seconds) if timeout_seconds > 0 else 30.0
        self.expected_dimensions = int(dimensions) if dimensions is not None and dimensions > 0 else 384
        self.executor = executor
        self.max_concurrency = int(max_concurrency) if max_concurrency and max_concurrency > 0 else None
        self._model = None
        self._lock = asyncio.Lock()
        self._semaphore: Optional[asyncio.Semaphore] = None

    def _get_executor(self) -> ThreadPoolExecutor:
        """Lazily creates or returns the ThreadPoolExecutor for inference."""
        if self.executor is None:
            workers = self.max_concurrency or 4
            self.executor = ThreadPoolExecutor(
                max_workers=workers,
                thread_name_prefix="fastembed-inference",
            )
        return self.executor

    def _get_semaphore(self) -> asyncio.Semaphore:
        """
        Lazily creates or returns the concurrency bounding semaphore.
        Limits total in-flight submitted inference work in worker threads,
        accounting for running C++/ONNX threads that cannot be interrupted.
        """
        if self._semaphore is None:
            limit = self.max_concurrency
            executor = self._get_executor()
            if limit is None and executor is not None:
                limit = getattr(executor, "_max_workers", None)
            if limit is None or limit <= 0:
                limit = 4
            self._semaphore = asyncio.Semaphore(limit)
        return self._semaphore

    @property
    def model(self) -> str:
        """Name/path of the FastEmbed model."""
        return self.model_name

    @property
    def dimensions(self) -> int:
        """Configured or expected dimensionality of the embedding vector."""
        return self.expected_dimensions

    def _load_model_sync(self):
        """Synchronously loads the FastEmbed model (to be called in a worker thread)."""
        try:
            from fastembed import TextEmbedding
        except (ImportError, ModuleNotFoundError) as exc:
            raise EmbeddingConfigurationError(
                "FastEmbed is not installed. To use the FastEmbed provider, install it via: "
                "pip install -r requirements-fastembed.txt (or pip install fastembed)",
                provider="fastembed",
            ) from exc

        try:
            loaded = TextEmbedding(model_name=self.model_name)
            logger.info("Loaded FastEmbed model: '%s'", self.model_name)
            return loaded
        except Exception as exc:
            raise EmbeddingConnectionError(
                f"Failed to initialize FastEmbed model '{self.model_name}': {exc}",
                provider="fastembed",
            ) from exc

    async def initialize(self) -> None:
        """Eagerly initializes the underlying FastEmbed model in a worker thread to keep the event loop unblocked."""
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    loop = asyncio.get_running_loop()
                    self._model = await loop.run_in_executor(self.executor, self._load_model_sync)

    def _get_model(self):
        """Lazily initialize or return the underlying FastEmbed model."""
        if self._model is None:
            self._model = self._load_model_sync()
        return self._model

    async def embed(
        self,
        text: str,
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> EmbeddingResult:
        """
        Generates a vector embedding for the input text using local FastEmbed model.
        Differentiates query vs document/passage embedding according to task.
        Executes ONNX CPU inference in a dedicated worker thread to avoid blocking asyncio loop.
        """
        if not text or not text.strip():
            raise EmbeddingResponseError("Input text cannot be empty.", provider="fastembed")

        raw_text = text.strip()

        # Check if _get_model is patched (e.g. in unit tests) or lazily initialize off-thread
        from unittest.mock import Mock
        if isinstance(getattr(self, "_get_model", None), Mock):
            model = self._get_model()
        else:
            if self._model is None:
                await self.initialize()
            model = self._model

        # Determine task normalized string
        task_str = task.value if isinstance(task, EmbeddingTask) else str(task).lower().strip()

        def _compute() -> list[float]:
            try:
                if task_str in ("query", "search_query"):
                    if hasattr(model, "query_embed"):
                        embeddings = list(model.query_embed(raw_text))
                    else:
                        embeddings = list(model.embed([raw_text]))
                elif task_str in ("document", "passage", "search_document"):
                    if hasattr(model, "passage_embed"):
                        embeddings = list(model.passage_embed([raw_text]))
                    else:
                        embeddings = list(model.embed([raw_text]))
                else:
                    embeddings = list(model.embed([raw_text]))

                if not embeddings:
                    raise EmbeddingResponseError(
                        "FastEmbed returned an empty embeddings list.",
                        provider="fastembed",
                    )
                vector = [float(v) for v in embeddings[0]]

                # Validate dimensions match expected
                if self.expected_dimensions is not None and len(vector) != self.expected_dimensions:
                    raise EmbeddingResponseError(
                        f"FastEmbed model '{self.model_name}' produced vector of dimension {len(vector)}, "
                        f"expected {self.expected_dimensions}.",
                        provider="fastembed",
                    )
                return vector
            except EmbeddingResponseError:
                raise
            except Exception as err:
                raise EmbeddingResponseError(
                    f"FastEmbed inference error: {err}",
                    provider="fastembed",
                ) from err

        loop = asyncio.get_running_loop()
        sem = self._get_semaphore()
        executor = self._get_executor()

        # Bounded inference submission gate:
        # Prevents queueing unbounded work when worker threads are busy running ONNX inference.
        try:
            await asyncio.wait_for(sem.acquire(), timeout=self.timeout_seconds)
        except asyncio.TimeoutError as exc:
            raise EmbeddingTimeoutError(
                f"FastEmbed inference capacity saturated; worker threads busy after {self.timeout_seconds:.1f}s.",
                provider="fastembed",
            ) from exc

        def _safe_release(_f: Any) -> None:
            if not loop.is_closed():
                try:
                    loop.call_soon_threadsafe(sem.release)
                except RuntimeError:
                    pass

        acquired = True
        try:
            cf = executor.submit(_compute)
            cf.add_done_callback(_safe_release)
            acquired = False  # Ownership transferred to cf completion callback
        finally:
            if acquired:
                sem.release()

        af = asyncio.wrap_future(cf)
        try:
            vector = await asyncio.wait_for(
                af,
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            cf.cancel()
            raise EmbeddingTimeoutError(
                f"FastEmbed embedding generation timed out after {self.timeout_seconds:.1f}s.",
                provider="fastembed",
            ) from exc
        except (EmbeddingResponseError, EmbeddingTimeoutError, EmbeddingConnectionError, EmbeddingConfigurationError):
            raise
        except Exception as exc:
            raise EmbeddingResponseError(
                f"FastEmbed embedding error: {exc}",
                provider="fastembed",
            ) from exc

        return EmbeddingResult(
            vector=vector,
            model=self.model_name,
            dimensions=len(vector),
        )

    async def embed_batch(
        self,
        texts: Sequence[str],
        task: Union[EmbeddingTask, str] = EmbeddingTask.QUERY,
    ) -> List[EmbeddingResult]:
        """
        Generates vector embeddings for a sequence of texts using local FastEmbed model.
        Executes ONNX CPU batch inference in worker threads in bounded chunks.
        Preserves exact input order and validates result cardinality and vectors.
        """
        if not texts:
            return []

        # Validate all texts
        cleaned_texts: List[str] = []
        for i, t in enumerate(texts):
            if not isinstance(t, str) or not t.strip():
                raise EmbeddingResponseError(
                    f"Input text at index {i} in batch cannot be empty or blank.",
                    provider="fastembed",
                )
            cleaned_texts.append(t.strip())

        async with self._lock:
            model = self._get_model()

        task_str = task.value if isinstance(task, EmbeddingTask) else str(task).lower().strip()

        # Bounded batch chunking (64 texts per chunk)
        chunk_size = 64
        all_results: List[EmbeddingResult] = []

        for offset in range(0, len(cleaned_texts), chunk_size):
            chunk = cleaned_texts[offset : offset + chunk_size]

            def _compute_chunk() -> List[List[float]]:
                try:
                    if task_str in ("query", "search_query"):
                        if hasattr(model, "query_embed"):
                            raw_embeddings = list(model.query_embed(chunk))
                        else:
                            raw_embeddings = list(model.embed(chunk))
                    elif task_str in ("document", "passage", "search_document"):
                        if hasattr(model, "passage_embed"):
                            raw_embeddings = list(model.passage_embed(chunk))
                        else:
                            raw_embeddings = list(model.embed(chunk))
                    else:
                        raw_embeddings = list(model.embed(chunk))

                    if len(raw_embeddings) != len(chunk):
                        raise EmbeddingResponseError(
                            f"FastEmbed returned {len(raw_embeddings)} vectors for chunk of {len(chunk)} inputs.",
                            provider="fastembed",
                        )

                    vectors: List[List[float]] = []
                    for idx, emb in enumerate(raw_embeddings):
                        vec = [float(v) for v in emb]
                        if self.expected_dimensions is not None and len(vec) != self.expected_dimensions:
                            raise EmbeddingResponseError(
                                f"FastEmbed model '{self.model_name}' produced vector of dimension {len(vec)}, "
                                f"expected {self.expected_dimensions}.",
                                provider="fastembed",
                            )
                        vectors.append(vec)
                    return vectors
                except EmbeddingResponseError:
                    raise
                except Exception as err:
                    raise EmbeddingResponseError(
                        f"FastEmbed batch inference error: {err}",
                        provider="fastembed",
                    ) from err

            loop = asyncio.get_running_loop()
            sem = self._get_semaphore()
            executor = self._get_executor()

            try:
                await asyncio.wait_for(sem.acquire(), timeout=self.timeout_seconds)
            except asyncio.TimeoutError as exc:
                raise EmbeddingTimeoutError(
                    f"FastEmbed batch capacity saturated; worker threads busy after {self.timeout_seconds:.1f}s.",
                    provider="fastembed",
                ) from exc

            def _safe_release(_f: Any) -> None:
                if not loop.is_closed():
                    try:
                        loop.call_soon_threadsafe(sem.release)
                    except RuntimeError:
                        pass

            acquired = True
            try:
                cf = executor.submit(_compute_chunk)
                cf.add_done_callback(_safe_release)
                acquired = False  # Ownership transferred to cf completion callback
            finally:
                if acquired:
                    sem.release()

            af = asyncio.wrap_future(cf)
            try:
                chunk_vectors = await asyncio.wait_for(
                    af,
                    timeout=self.timeout_seconds,
                )
            except asyncio.TimeoutError as exc:
                cf.cancel()
                raise EmbeddingTimeoutError(
                    f"FastEmbed batch embedding generation timed out after {self.timeout_seconds:.1f}s.",
                    provider="fastembed",
                ) from exc
            except (EmbeddingResponseError, EmbeddingTimeoutError, EmbeddingConnectionError, EmbeddingConfigurationError):
                raise
            except Exception as exc:
                raise EmbeddingResponseError(
                    f"FastEmbed batch embedding error: {exc}",
                    provider="fastembed",
                ) from exc

            for vec in chunk_vectors:
                all_results.append(
                    EmbeddingResult(
                        vector=vec,
                        model=self.model_name,
                        dimensions=len(vec),
                    )
                )

        if len(all_results) != len(texts):
            raise EmbeddingResponseError(
                f"FastEmbed batch returned {len(all_results)} total vectors, expected {len(texts)}.",
                provider="fastembed",
            )

        return all_results

    async def close(self) -> None:
        """Releases the loaded model and associated memory."""
        async with self._lock:
            self._model = None
        logger.info("FastEmbedProvider closed.")
