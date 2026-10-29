"""
Database migration runner for pgvector and vector storage.
Applies vector-specific migrations independently from exact-cache migrations.
"""

import logging
from pathlib import Path
from typing import Union
import asyncpg

from .errors import (
    VectorDimensionMismatchError,
    VectorStoreConfigurationError,
)

logger = logging.getLogger("intellicache.gateway.vector_migration")

VECTOR_MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations" / "vector"


async def check_pgvector_available(conn: asyncpg.Connection) -> bool:
    """Checks whether the 'vector' extension is available in PostgreSQL."""
    row = await conn.fetchrow(
        "SELECT 1 FROM pg_available_extensions WHERE name = 'vector'"
    )
    return row is not None


async def run_vector_migrations(
    target: Union[asyncpg.Pool, asyncpg.Connection],
    dimensions: int = 768,
) -> None:
    """
    Applies vector storage migrations with runtime dimension configuration.
    Validates extension availability and checks existing table dimension compatibility.

    Args:
        target: asyncpg Connection or Pool instance.
        dimensions: Configured vector dimensionality (defaults to 768 for Nomic).

    Raises:
        VectorStoreConfigurationError: If the pgvector extension is not available.
        VectorDimensionMismatchError: If the table already exists with incompatible dimensions.
    """
    if dimensions <= 0:
        raise VectorStoreConfigurationError(
            f"Configured vector dimensions must be positive, got {dimensions}."
        )

    if isinstance(target, asyncpg.Pool):
        async with target.acquire() as conn:
            await _apply_migrations(conn, dimensions)
    else:
        await _apply_migrations(target, dimensions)


async def _apply_migrations(conn: asyncpg.Connection, dimensions: int) -> None:
    # 1. Verify pgvector extension availability
    is_available = await check_pgvector_available(conn)
    if not is_available:
        raise VectorStoreConfigurationError(
            "The pgvector extension 'vector' is not available in PostgreSQL. "
            "Please install pgvector on the database server or disable semantic caching (SEMANTIC_CACHE_ENABLED=false)."
        )

    # 2. Check if vector_records table already exists with different dimension
    try:
        existing_dim = await conn.fetchval(
            """
            SELECT atttypmod FROM pg_attribute
            WHERE attrelid = 'vector_records'::regclass AND attname = 'vector'
            """
        )
        if existing_dim is not None and existing_dim > 0 and existing_dim != dimensions:
            raise VectorDimensionMismatchError(
                f"Existing vector_records table dimension is {existing_dim}, which does not match "
                f"configured dimension {dimensions}. Changing embedding models or vector dimensions "
                f"requires a deliberate migration or re-embedding strategy."
            )
    except asyncpg.UndefinedTableError:
        # Table does not exist yet; proceed with creation
        pass

    # 3. Apply vector SQL migration files
    if not VECTOR_MIGRATIONS_DIR.exists():
        logger.warning("Vector migrations directory not found at %s", VECTOR_MIGRATIONS_DIR)
        return

    sql_files = sorted(VECTOR_MIGRATIONS_DIR.glob("*.sql"))
    if not sql_files:
        logger.info("No vector migration files found in %s", VECTOR_MIGRATIONS_DIR)
        return

    for sql_file in sql_files:
        logger.info("Applying vector migration: %s (dimensions=%d)", sql_file.name, dimensions)
        sql_content = sql_file.read_text(encoding="utf-8")

        # Replace default 768 dimension with configured dimension if different
        if dimensions != 768:
            sql_content = sql_content.replace("vector(768)", f"vector({dimensions})")

        async with conn.transaction():
            await conn.execute(sql_content)

        logger.info("Successfully applied vector migration: %s", sql_file.name)
