"""
Factory for constructing configured embedding providers.
Independent of LLMProvider and ProviderFactory.
"""

from typing import Any, Dict, Optional, Type
import httpx

from .errors import EmbeddingConfigurationError
from .interfaces import EmbeddingProvider
from .ollama_adapter import OllamaEmbeddingProvider
from .fastembed_adapter import FastEmbedProvider


class EmbeddingProviderFactory:
    """Registry and factory for constructing embedding providers."""

    _registry: Dict[str, Type[EmbeddingProvider]] = {
        "ollama": OllamaEmbeddingProvider,
        "fastembed": FastEmbedProvider,
        "local": FastEmbedProvider,
    }

    @classmethod
    def register(cls, name: str, provider_cls: Type[EmbeddingProvider]) -> None:
        """Register a custom embedding provider class."""
        cls._registry[name.lower().strip()] = provider_cls

    @classmethod
    def create(
        cls,
        provider_name: Optional[str] = None,
        base_url: str = "http://127.0.0.1:11434",
        model: Optional[str] = None,
        timeout_seconds: float = 30.0,
        dimensions: Optional[int] = None,
        client: Optional[httpx.AsyncClient] = None,
        name: Optional[str] = None,
        **kwargs: Any,
    ) -> EmbeddingProvider:
        """
        Creates and returns an EmbeddingProvider for the specified provider name.
        Supports both provider_name and name keyword arguments.
        Applies provider-aware defaults for model and dimensions if not specified.

        Raises:
            EmbeddingConfigurationError: If the provider name is missing or unsupported.
        """
        raw_name = provider_name if provider_name is not None else name
        key = (raw_name or "").lower().strip()
        if not key:
            raise EmbeddingConfigurationError(
                "Embedding provider name must be specified.",
                provider="",
            )

        if key not in cls._registry:
            supported = ", ".join(sorted(cls._registry.keys()))
            raise EmbeddingConfigurationError(
                f"Unsupported embedding provider '{raw_name}'. Supported providers: {supported}",
                provider=raw_name,
            )

        # Provider-aware defaults and dimension conflict validation
        if key in ("fastembed", "local"):
            resolved_model = (model or "BAAI/bge-small-en-v1.5").strip()
            if resolved_model == "BAAI/bge-small-en-v1.5" and dimensions is not None and dimensions > 0 and dimensions != 384:
                raise EmbeddingConfigurationError(
                    f"Configured dimensions ({dimensions}) conflict with FastEmbed model '{resolved_model}' (requires 384 dimensions).",
                    provider="fastembed",
                )
            resolved_dimensions = int(dimensions) if dimensions is not None and dimensions > 0 else 384
        else:  # ollama or other
            resolved_model = (model or "nomic-embed-text").strip()
            if "nomic" in resolved_model.lower() and dimensions is not None and dimensions > 0 and dimensions != 768:
                raise EmbeddingConfigurationError(
                    f"Configured dimensions ({dimensions}) conflict with Ollama model '{resolved_model}' (requires 768 dimensions).",
                    provider="ollama",
                )
            resolved_dimensions = int(dimensions) if dimensions is not None and dimensions > 0 else 768

        provider_cls = cls._registry[key]
        return provider_cls(
            base_url=base_url,
            model=resolved_model,
            timeout_seconds=timeout_seconds,
            dimensions=resolved_dimensions,
            client=client,
            **kwargs,
        )
