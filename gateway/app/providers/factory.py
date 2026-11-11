"""
Provider Factory and Registry for IntelliCache LLM Providers.
"""

from typing import Any, Callable, Dict, Optional, Type
from ..core.errors import ProviderConfigurationError
from .interfaces import LLMProvider
from .groq_adapter import GroqProvider


class ProviderFactory:
    """Registry and factory for LLM provider instances."""

    _registry: Dict[str, Type[LLMProvider]] = {
        "groq": GroqProvider,
    }

    @classmethod
    def register(cls, name: str, provider_cls: Type[LLMProvider]) -> None:
        """Register a new LLMProvider implementation."""
        cls._registry[name.lower().strip()] = provider_cls

    @classmethod
    def get_provider_class(cls, name: str) -> Type[LLMProvider]:
        """Look up registered provider class by name."""
        key = name.lower().strip()
        if key == "mock":
            from .mock_adapter import MockProvider
            return MockProvider
        if key not in cls._registry:
            available = ", ".join(sorted(list(cls._registry.keys()) + ["mock"]))
            raise ProviderConfigurationError(
                f"Unknown LLM provider '{name}'. Available providers: [{available}]"
            )
        return cls._registry[key]

    @classmethod
    def create(
        cls,
        name: str = "groq",
        api_key: Optional[str] = None,
        default_model: str = "qwen/qwen3.8-27b",
        **kwargs: Any,
    ) -> LLMProvider:
        """Instantiate a provider by registered name."""
        provider_cls = cls.get_provider_class(name)
        if provider_cls is GroqProvider:
            return GroqProvider(api_key=api_key, default_model=default_model, **kwargs)
        if name.lower().strip() == "mock":
            from .mock_adapter import MockProvider
            return MockProvider(api_key=api_key, default_model=default_model, **kwargs)
        return provider_cls(**kwargs)
