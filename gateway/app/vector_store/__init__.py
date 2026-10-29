"""
Vector storage package for IntelliCache.
Provides provider-independent vector models, nearest-neighbour retrieval,
PostgreSQL + pgvector implementation, and migrations.
"""

from .contracts import (
    VectorRecord,
    VectorSearchResult,
    cosine_distance_to_similarity,
)
from .errors import (
    VectorDimensionMismatchError,
    VectorInvalidError,
    VectorStoreConfigurationError,
    VectorStoreConnectionError,
    VectorStoreError,
)
from .interfaces import BaseVectorStore
from .migration import check_pgvector_available, run_vector_migrations
from .postgres_store import PostgresVectorStore

__all__ = [
    "BaseVectorStore",
    "PostgresVectorStore",
    "VectorDimensionMismatchError",
    "VectorInvalidError",
    "VectorRecord",
    "VectorSearchResult",
    "VectorStoreConfigurationError",
    "VectorStoreConnectionError",
    "VectorStoreError",
    "check_pgvector_available",
    "cosine_distance_to_similarity",
    "run_vector_migrations",
]
