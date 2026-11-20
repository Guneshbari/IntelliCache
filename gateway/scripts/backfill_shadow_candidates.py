#!/usr/bin/env python3
"""
CLI utility for offline backfill of shadow semantic candidates from historical exact cache entries (Stage 3.3).
Populates shadow_vector_records without mutating production cache_entries or vector_records.
"""

import argparse
import asyncio
import logging
import os
import sys

import asyncpg

# Add parent directory to path to enable app imports
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.embeddings.factory import EmbeddingProviderFactory
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.shadow import backfill_shadow_candidates_from_cache
from app.vector_store.postgres_store import PostgresVectorStore
from app.vector_store.migration import run_vector_migrations

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("intellicache.backfill")


async def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill shadow semantic candidates from exact cache entries.")
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "postgresql://postgres:postgres@localhost:5432/intellicache_fastembed_test"),
        help="PostgreSQL database URL.",
    )
    parser.add_argument("--namespace", default="default", help="Target cache namespace.")
    parser.add_argument("--limit", type=int, default=1000, help="Maximum entries to backfill.")
    parser.add_argument("--model", default="BAAI/bge-small-en-v1.5", help="Embedding model.")
    parser.add_argument("--dimensions", type=int, default=384, help="Embedding vector dimensions.")
    args = parser.parse_args()

    logger.info("Connecting to database: %s", args.database_url)
    pool = await asyncpg.create_pool(args.database_url, min_size=1, max_size=4)

    try:
        logger.info("Verifying vector migrations on database...")
        await run_vector_migrations(pool, dimensions=args.dimensions)

        shadow_vector_store = PostgresVectorStore(
            pool=pool,
            default_namespace=args.namespace,
            default_model=args.model,
            dimensions=args.dimensions,
            table_name="shadow_vector_records",
        )

        embedding_provider = EmbeddingProviderFactory.create(
            provider_name="fastembed",
            model=args.model,
            dimensions=args.dimensions,
        )
        if hasattr(embedding_provider, "initialize"):
            await embedding_provider.initialize()

        policy = SemanticCachePolicy(configured_model=args.model)

        logger.info("Starting shadow backfill for namespace '%s' (limit=%d)...", args.namespace, args.limit)
        count = await backfill_shadow_candidates_from_cache(
            db_pool=pool,
            shadow_vector_store=shadow_vector_store,
            embedding_provider=embedding_provider,
            policy=policy,
            namespace=args.namespace,
            limit=args.limit,
        )
        logger.info("Successfully backfilled %d shadow candidates into shadow_vector_records.", count)
    finally:
        await pool.close()


if __name__ == "__main__":
    asyncio.run(main())
