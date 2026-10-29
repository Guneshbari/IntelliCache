"""
Main entry point for the IntelliCache Gateway FastAPI application.
Configures CORS, registers query and health routes, and manages database lifecycles.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional

import asyncpg
from fastapi import Depends, FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.cache.migration import run_migrations
from app.config import Settings, get_settings
from app.embeddings.factory import EmbeddingProviderFactory
from app.semantic_cache import SemanticCachePolicy, SemanticCacheService
from app.vector_store import PostgresVectorStore, run_vector_migrations
from app.routes.query import (
    BaseCache,
    CacheStats,
    ExactCache,
    PostgresCache,
    QueryCache,
    get_query_cache,
    get_semantic_cache,
    router as query_router,
    set_query_cache,
    set_semantic_cache,
    verify_admin_token,
)

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("intellicache.gateway")

# Track startup timestamp
_STARTUP_TIME = time.time()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown events."""
    settings = get_settings()
    logger.info("=" * 60)
    logger.info("Starting IntelliCache Gateway v1.0.0")
    logger.info("Model: %s", settings.groq_model)
    logger.info("Host: %s:%s", settings.host, settings.port)
    logger.info("Cache Backend: %s (Namespace: %s)", settings.cache_backend, settings.cache_namespace)
    logger.info("Cache: %s (TTL: %ss)", "Enabled" if settings.cache_enabled else "Disabled", settings.cache_ttl_seconds)
    logger.info("=" * 60)

    pg_cache: Optional[PostgresCache] = None
    if settings.cache_backend == "postgres":
        if not settings.database_url:
            raise RuntimeError("DATABASE_URL must be configured when CACHE_BACKEND='postgres'.")
        logger.info("Initializing PostgreSQL connection pool...")
        pool = await asyncpg.create_pool(
            dsn=settings.database_url,
            min_size=settings.db_pool_min_size,
            max_size=settings.db_pool_max_size,
        )
        logger.info("Applying database migrations...")
        await run_migrations(pool)
        pg_cache = PostgresCache(
            pool=pool,
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
            default_namespace=settings.cache_namespace,
        )
        set_query_cache(pg_cache)
        app.state.db_pool = pool
        app.state.cache = pg_cache

        if settings.semantic_cache_enabled:
            logger.info("Semantic cache enabled. Running vector store migrations...")
            await run_vector_migrations(pool, dimensions=settings.vector_dimensions)
            vector_store = PostgresVectorStore(
                pool=pool,
                default_namespace=settings.cache_namespace,
                default_model=settings.embedding_model,
                dimensions=settings.vector_dimensions,
            )
            app.state.vector_store = vector_store

            # Instantiate embedding provider and semantic cache policy/service
            embedding_provider = EmbeddingProviderFactory.create(
                name=settings.embedding_provider,
                model=settings.embedding_model,
                base_url=settings.embedding_base_url,
                timeout_seconds=settings.embedding_timeout_seconds,
            )
            policy = SemanticCachePolicy(
                min_similarity=settings.semantic_cache_min_similarity,
                min_margin=settings.semantic_cache_min_margin,
                top_k=settings.semantic_cache_top_k,
                configured_model=settings.embedding_model,
            )
            semantic_cache = SemanticCacheService(
                embedding_provider=embedding_provider,
                vector_store=vector_store,
                policy=policy,
                default_namespace=settings.cache_namespace,
            )
            set_semantic_cache(semantic_cache)
            app.state.semantic_cache = semantic_cache
            logger.info(
                "Semantic cache initialized: dimensions=%d, model=%s, min_sim=%.2f, min_margin=%.2f, top_k=%d",
                settings.vector_dimensions,
                settings.embedding_model,
                settings.semantic_cache_min_similarity,
                settings.semantic_cache_min_margin,
                settings.semantic_cache_top_k,
            )
        else:
            set_semantic_cache(None)
            app.state.vector_store = None
            app.state.semantic_cache = None
    else:
        mem_cache = ExactCache(
            max_entries=settings.cache_max_entries,
            ttl_seconds=settings.cache_ttl_seconds,
        )
        set_query_cache(mem_cache)
        set_semantic_cache(None)
        app.state.cache = mem_cache
        app.state.vector_store = None
        app.state.semantic_cache = None

    yield

    logger.info("Shutting down IntelliCache Gateway.")
    if hasattr(app.state, "semantic_cache") and app.state.semantic_cache is not None:
        await app.state.semantic_cache.close()
    elif hasattr(app.state, "vector_store") and app.state.vector_store is not None:
        await app.state.vector_store.close()
    active_cache = get_query_cache()
    if active_cache is not None:
        await active_cache.close()


def create_app() -> FastAPI:
    """Create and configure the FastAPI application instance."""
    settings = get_settings()

    app = FastAPI(
        title="IntelliCache Gateway",
        description=(
            "Intelligent semantic caching gateway and LLM proxy for IntelliCache. "
            "Intercepts, normalizes, deduplicates, and caches LLM queries to reduce latency and API costs."
        ),
        version="1.0.0",
        docs_url="/docs",
        redoc_url="/redoc",
        lifespan=lifespan,
    )

    # Configure CORS (RFC compliant: disable allow_credentials if wildcard origin)
    allow_credentials = False if settings.cors_origins == ["*"] else True
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=allow_credentials,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # Mount query routes at /query and /api/v1/query
    app.include_router(query_router, prefix="/query", tags=["Query"])
    app.include_router(query_router, prefix="/api/v1/query", tags=["Query (v1)"])

    @app.get("/", summary="Gateway Root Status")
    async def root_status(current_settings: Settings = Depends(get_settings)) -> Dict[str, Any]:
        """Root status endpoint returning service info and endpoints."""
        uptime_seconds = round(time.time() - _STARTUP_TIME, 2)
        return {
            "service": "IntelliCache Gateway",
            "version": "1.0.0",
            "status": "online",
            "uptime_seconds": uptime_seconds,
            "configured_model": current_settings.groq_model,
            "cache_backend": current_settings.cache_backend,
            "cache_namespace": current_settings.cache_namespace,
            "cache_enabled": current_settings.cache_enabled,
            "endpoints": {
                "query": "POST /query",
                "query_v1": "POST /api/v1/query",
                "health": "GET /health",
                "cache_stats": "GET /cache/stats",
                "cache_clear": "POST /cache/clear",
                "docs": "GET /docs",
            },
        }

    @app.get("/health", summary="Health Check")
    async def health_check(
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Health check endpoint for liveness and readiness monitoring."""
        stats = await cache.stats(
            enabled=current_settings.cache_enabled,
            namespace=current_settings.cache_namespace,
        )
        has_api_key = bool(
            current_settings.groq_api_key and current_settings.groq_api_key != "replace_with_your_key"
        )

        db_connected = True
        db_status = "n/a"
        if current_settings.cache_backend == "postgres":
            pool = getattr(cache, "pool", None)
            if pool is not None:
                try:
                    async with pool.acquire() as conn:
                        await conn.fetchval("SELECT 1")
                    db_status = "connected"
                except Exception as e:
                    db_status = f"unreachable: {type(e).__name__}"
                    db_connected = False
            else:
                db_status = "uninitialized"
                db_connected = False

        is_healthy = has_api_key and (current_settings.cache_backend != "postgres" or db_connected)
        return {
            "status": "healthy" if is_healthy else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "groq_configured": has_api_key,
            "groq_model": current_settings.groq_model,
            "cache_backend": current_settings.cache_backend,
            "cache_namespace": current_settings.cache_namespace,
            "database": {
                "backend": current_settings.cache_backend,
                "status": db_status,
                "connected": db_connected,
            },
            "cache": {
                "enabled": current_settings.cache_enabled,
                "backend": stats.backend,
                "entries": stats.total_entries,
                "hit_rate": stats.hit_rate,
                "hits": stats.hits,
                "misses": stats.misses,
            },
        }

    @app.get("/cache/stats", response_model=CacheStats, tags=["Cache"], summary="Global Cache Statistics")
    async def global_cache_stats(
        request: Request,
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
    ) -> CacheStats:
        """Global cache performance statistics endpoint."""
        stats = await cache.stats(
            enabled=current_settings.cache_enabled,
            namespace=current_settings.cache_namespace,
        )
        if hasattr(request.app.state, "semantic_cache") and request.app.state.semantic_cache is not None:
            stats.semantic = request.app.state.semantic_cache.get_stats()
        return stats

    @app.get("/cache/semantic/stats", tags=["Cache"], summary="Semantic Cache Statistics")
    async def semantic_cache_stats(
        request: Request,
    ) -> Dict[str, Any]:
        """Observability counters for semantic cache operations."""
        if hasattr(request.app.state, "semantic_cache") and request.app.state.semantic_cache is not None:
            stats_dict = request.app.state.semantic_cache.get_stats()
            stats_dict["enabled"] = True
            return stats_dict
        return {
            "enabled": False,
            "lookups": 0,
            "hits": 0,
            "misses": 0,
            "rejected_candidates": 0,
            "ambiguous_candidates": 0,
            "eligibility_rejections": 0,
            "embedding_failures": 0,
            "vector_store_failures": 0,
            "indexing_successes": 0,
            "indexing_failures": 0,
        }

    @app.post(
        "/cache/clear",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Clear Configured Cache Namespace",
    )
    async def clear_cache_namespace(
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Purge entries from the server-configured cache namespace. Requires administrative token."""
        target_ns = current_settings.cache_namespace
        await cache.clear(namespace=target_ns)
        return {
            "status": "ok",
            "message": f"Query cache namespace '{target_ns}' cleared successfully.",
            "namespace": target_ns,
        }

    return app


app = create_app()

if __name__ == "__main__":
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        reload=settings.debug,
    )
