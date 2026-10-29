"""
Abstract provider interface for LLM backends.
"""

from abc import ABC, abstractmethod
from typing import AsyncGenerator
from ..core.contracts import GenerationRequest, GenerationResult, StreamChunk


class LLMProvider(ABC):
    """Abstract interface defining required methods for all LLM providers."""

    @abstractmethod
    async def generate(self, request: GenerationRequest) -> GenerationResult:
        """
        Execute a non-streaming LLM generation.
        Returns a normalized GenerationResult.
        Raises ProviderError on failure.
        """
        pass

    @abstractmethod
    async def stream(self, request: GenerationRequest) -> AsyncGenerator[StreamChunk, None]:
        """
        Execute a streaming LLM generation.
        Yields normalized StreamChunk instances.
        Raises ProviderError or yields error chunks on failure.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """Close any open client sessions or network resources."""
        pass
