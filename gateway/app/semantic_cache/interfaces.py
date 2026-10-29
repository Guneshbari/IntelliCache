"""
Provider-independent interface for semantic caching.
Can be injected into IntelliCacheCore without importing FastAPI or SDK classes.
"""

from abc import ABC, abstractmethod
from typing import Dict, Optional

from .contracts import EligibilityResult, SemanticHit
from ..core.contracts import GenerationRequest, GenerationResult


class BaseSemanticCache(ABC):
    """Abstract interface for semantic caching operations."""

    @abstractmethod
    def is_eligible(self, request: GenerationRequest) -> EligibilityResult:
        """Evaluates whether a request is eligible for semantic caching."""
        pass

    @abstractmethod
    async def lookup(self, request: GenerationRequest) -> Optional[SemanticHit]:
        """
        Attempts semantic lookup for an eligible request.
        Fails open on infrastructure errors (returns None).
        """
        pass

    @abstractmethod
    async def index(self, request: GenerationRequest, result: GenerationResult) -> bool:
        """
        Indexes an eligible, completed LLM response into the vector store.
        Fails safely on infrastructure errors (returns False).
        """
        pass

    @abstractmethod
    def get_stats(self) -> Dict[str, int]:
        """Returns diagnostic observability counters."""
        pass

    @abstractmethod
    async def close(self) -> None:
        """Closes any underlying resources."""
        pass
