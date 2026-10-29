"""
Database migration runner for PostgreSQL exact cache.
"""

import logging
from pathlib import Path
from typing import Union
import asyncpg

logger = logging.getLogger("intellicache.gateway.migration")

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


async def run_migrations(target: Union[asyncpg.Pool, asyncpg.Connection]) -> None:
    """
    Applies all versioned SQL migrations in sorted sequence.
    Safe for repeated executions (idempotent DDL).
    """
    if not MIGRATIONS_DIR.exists():
        logger.warning("Migrations directory not found at %s", MIGRATIONS_DIR)
        return

    migration_files = sorted(MIGRATIONS_DIR.glob("*.sql"))
    if not migration_files:
        logger.info("No migration files found in %s", MIGRATIONS_DIR)
        return

    for sql_file in migration_files:
        logger.info("Applying database migration: %s", sql_file.name)
        sql_content = sql_file.read_text(encoding="utf-8")
        if isinstance(target, asyncpg.Pool):
            async with target.acquire() as conn:
                async with conn.transaction():
                    await conn.execute(sql_content)
        else:
            async with target.transaction():
                await target.execute(sql_content)
        logger.info("Successfully applied migration: %s", sql_file.name)
