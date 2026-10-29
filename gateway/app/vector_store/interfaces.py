"""
Provider-independent vector storage interface.
Defines contracts for persisting embeddings, nearest-neighbour retrieval,
namespace-scoped deletion, and lifecycle cleanup.
"""

from abc import ABC, abstractmethod
from typing import List, Optional, Sequence

from .contracts import VectorRecord, VectorSearchResult


class BaseVectorStore(ABC):
    """
    Abstract vector storage contract for IntelliCache.
    Implementations must be provider-independent, handle vector dimensionality validation,
    enforce namespace scoping, and order nearest-neighbour results by cosine distance.
    """

    @abstractmethod
    async def upsert(self, record: VectorRecord) -> None:
        """
        Inserts or updates a vector record in the store.
        Must validate dimensionality against the configured store dimension.

        Args:
            record: The VectorRecord to persist.
        """
        pass

    @abstractmethod
    async def search(
        self,
        query_vector: Sequence[float],
        top_k: int = 5,
        namespace: Optional[str] = None,
        model: Optional[str] = None,
    ) -> List[VectorSearchResult]:
        """
        Searches for the top-k nearest records by cosine distance.

        Args:
            query_vector: Sequence of float values representing the query embedding.
            top_k: Number of nearest neighbours to return (must be >= 1).
            namespace: Optional namespace override. Defaults to store default.
            model: Optional model filter. Defaults to store default.

        Returns:
            List of VectorSearchResult objects ordered in ascending cosine distance.
        """
        pass

    @abstractmethod
    async def delete(self, fingerprint: str, namespace: Optional[str] = None) -> bool:
        """
        Deletes a vector record by namespace and exact-cache fingerprint.

        Args:
            fingerprint: The exact fingerprint of the record.
            namespace: Optional namespace override. Defaults to store default.

        Returns:
            True if a record was deleted, False otherwise.
        """
        pass

    @abstractmethod
    async def clear(self, namespace: Optional[str] = None) -> int:
        """
        Deletes all vector records within the specified namespace.

        Args:
            namespace: The cache namespace to clear. Defaults to store default.

        Returns:
            Count of records deleted.
        """
        pass

    @abstractmethod
    async def close(self) -> None:
        """
        Closes any underlying connection pools or resources owned by the store.
        """
        pass
