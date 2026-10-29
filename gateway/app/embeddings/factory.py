"""
Factory for constructing configured embedding providers.
Independent of LLMProvider and ProviderFactory.
"""

from typing import Dict, Optional, Type
import httpx

from .errors import EmbeddingConfigurationError
from .interfaces import EmbeddingProvider
from .ollama_adapter import OllamaEmbeddingProvider


class EmbeddingProviderFactory:
    """Registry and factory for constructing embedding providers."""

    _registry: Dict[str, Type[EmbeddingProvider]] = {
        "ollama": OllamaEmbeddingProvider,
    }

    @classmethod
    def register(cls, name: str, provider_cls: Type[EmbeddingProvider]) -> None:
        """Register a custom embedding provider class."""
        cls._registry[name.lower().strip()] = provider_cls

    @classmethod
    def create(
        cls,
        provider_name: str,
        base_url: str = "http://127.0.0.1:11434",
        model: str = "nomic-embed-text",
        timeout_seconds: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> EmbeddingProvider:
        """
        Creates and returns an EmbeddingProvider for the specified provider name.

        Raises:
            EmbeddingConfigurationError: If the provider is unsupported.
        """
        key = (provider_name or "").lower().strip()
        if key not in cls._registry:
            supported = ", ".join(sorted(cls._registry.keys()))
            raise EmbeddingConfigurationError(
                f"Unsupported embedding provider '{provider_name}'. Supported providers: {supported}",
                provider=provider_name,
            )

        provider_cls = cls._registry[key]
        return provider_cls(
            base_url=base_url,
            model=model,
            timeout_seconds=timeout_seconds,
            client=client,
        )
