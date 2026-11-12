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
from app.metrics import (
    AdminMetricsSnapshot,
    NamespaceMetricsSnapshot,
    RuntimeMetricsCollector,
    get_metrics_collector,
)
from app.semantic_cache import SemanticCachePolicy, SemanticCacheService
from app.vector_store import (
    InMemoryVectorStore,
    PostgresVectorStore,
    run_vector_migrations,
)
from app.routes.query import (
    BaseCache,
    CacheStats,
    ExactCache,
    PostgresCache,
    QueryCache,
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    router as query_router,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
    verify_admin_token,
    verify_client_token,
    verify_metrics_auth,
)

# Configure logger
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [%(name)s] %(message)s",
)
logger = logging.getLogger("intellicache.gateway")

# Track startup timestamp
_STARTUP_TIME = time.time()


async def _cleanup_gateway_resources(
    semantic_cache: Optional[SemanticCacheService] = None,
    vector_store: Optional[BaseVectorStore] = None,
    embedding_provider: Optional[Any] = None,
    cache: Optional[BaseCache] = None,
    pool: Optional[asyncpg.Pool] = None,
) -> list[Exception]:
    """
    Cleans up gateway infrastructure resources independently.
    Ensures failure in one resource cleanup does not prevent subsequent resources from closing.
    Avoids double-closing the PostgreSQL pool (closed by PostgresCache if cache exists, or pool directly).
    """
    errors: list[Exception] = []

    # 1. Close semantic cache (which closes owned embedding provider and vector store independently)
    if semantic_cache is not None:
        try:
            await semantic_cache.close()
        except Exception as exc:
            logger.error("Error closing semantic cache: %s", exc)
            errors.append(exc)
    else:
        # If semantic_cache was not created, clean up partially initialized vector_store or embedding_provider
        if vector_store is not None:
            try:
                await vector_store.close()
            except Exception as exc:
                logger.error("Error closing vector store: %s", exc)
                errors.append(exc)
        if embedding_provider is not None:
            try:
                await embedding_provider.close()
            except Exception as exc:
                logger.error("Error closing embedding provider: %s", exc)
                errors.append(exc)

    # 2. Close exact cache (PostgresCache owns and closes pool) or raw pool if cache was not yet created
    if cache is not None:
        try:
            await cache.close()
        except Exception as exc:
            logger.error("Error closing cache: %s", exc)
            errors.append(exc)
    elif pool is not None:
        try:
            await pool.close()
        except Exception as exc:
            logger.error("Error closing database pool: %s", exc)
            errors.append(exc)

    return errors


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    """Application lifespan manager for startup and shutdown events."""
    settings = get_settings()
    collector = get_metrics_collector()
    app.state.metrics_collector = collector
    logger.info("=" * 60)
    logger.info("Starting IntelliCache Gateway v1.0.0")
    logger.info("Model: %s", settings.groq_model)
    logger.info("Host: %s:%s", settings.host, settings.port)
    logger.info("Cache Backend: %s (Namespace: %s)", settings.cache_backend, settings.cache_namespace)
    logger.info("Cache: %s (TTL: %ss)", "Enabled" if settings.cache_enabled else "Disabled", settings.cache_ttl_seconds)
    logger.info("=" * 60)

    # Track resources locally as they are created
    pool: Optional[asyncpg.Pool] = None
    cache: Optional[BaseCache] = None
    vector_store: Optional[BaseVectorStore] = None
    embedding_provider: Optional[Any] = None
    semantic_cache: Optional[SemanticCacheService] = None

    try:
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
            cache = PostgresCache(
                pool=pool,
                max_entries=settings.cache_max_entries,
                ttl_seconds=settings.cache_ttl_seconds,
                default_namespace=settings.cache_namespace,
            )

            if settings.semantic_cache_enabled:
                if settings.vector_store_backend != "postgres":
                    raise ValueError(
                        f"Unsupported vector_store_backend '{settings.vector_store_backend}' for CACHE_BACKEND='postgres'"
                    )
                logger.info("Semantic cache enabled. Running vector store migrations...")
                await run_vector_migrations(pool, dimensions=settings.vector_dimensions)
                vector_store = PostgresVectorStore(
                    pool=pool,
                    default_namespace=settings.cache_namespace,
                    default_model=settings.embedding_model,
                    dimensions=settings.vector_dimensions,
                )

                # Instantiate embedding provider and semantic cache policy/service
                embedding_provider = EmbeddingProviderFactory.create(
                    provider_name=settings.embedding_provider,
                    model=settings.embedding_model,
                    dimensions=settings.vector_dimensions,
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
                logger.info(
                    "Semantic cache initialized: dimensions=%d, model=%s, min_sim=%.2f, min_margin=%.2f, top_k=%d",
                    settings.vector_dimensions,
                    settings.embedding_model,
                    settings.semantic_cache_min_similarity,
                    settings.semantic_cache_min_margin,
                    settings.semantic_cache_top_k,
                )
        else:
            cache = ExactCache(
                max_entries=settings.cache_max_entries,
                ttl_seconds=settings.cache_ttl_seconds,
            )

            if settings.semantic_cache_enabled:
                if settings.vector_store_backend != "memory":
                    raise ValueError(
                        f"Unsupported vector_store_backend '{settings.vector_store_backend}' for CACHE_BACKEND='memory'"
                    )
                vector_store = InMemoryVectorStore(
                    dimensions=settings.vector_dimensions,
                    default_namespace=settings.cache_namespace,
                    default_model=settings.embedding_model,
                    exact_cache=cache,
                    max_records=settings.vector_store_max_entries,
                )

                embedding_provider = EmbeddingProviderFactory.create(
                    provider_name=settings.embedding_provider,
                    model=settings.embedding_model,
                    dimensions=settings.vector_dimensions,
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
                logger.info(
                    "In-memory semantic cache initialized: dimensions=%d, model=%s, min_sim=%.2f, min_margin=%.2f, top_k=%d",
                    settings.vector_dimensions,
                    settings.embedding_model,
                    settings.semantic_cache_min_similarity,
                    settings.semantic_cache_min_margin,
                    settings.semantic_cache_top_k,
                )

        # Register initialized components in app.state and global singletons only after full success
        app.state.db_pool = pool
        app.state.cache = cache
        app.state.vector_store = vector_store
        app.state.semantic_cache = semantic_cache

        set_query_cache(cache)
        set_vector_store(vector_store)
        set_semantic_cache(semantic_cache)

    except Exception as startup_err:
        logger.error("Startup initialization failed: %s", startup_err)
        try:
            await _cleanup_gateway_resources(
                semantic_cache=semantic_cache,
                vector_store=vector_store,
                embedding_provider=embedding_provider,
                cache=cache,
                pool=pool,
            )
        finally:
            set_semantic_cache(None)
            set_vector_store(None)
            set_query_cache(None)
            app.state.semantic_cache = None
            app.state.vector_store = None
            app.state.cache = None
            app.state.db_pool = None
        raise

    try:
        yield
    finally:
        logger.info("Shutting down IntelliCache Gateway.")
        shutdown_errors: list[Exception] = []
        try:
            shutdown_errors = await _cleanup_gateway_resources(
                semantic_cache=semantic_cache,
                vector_store=vector_store,
                embedding_provider=embedding_provider,
                cache=cache,
                pool=pool,
            )
        finally:
            set_semantic_cache(None)
            set_vector_store(None)
            set_query_cache(None)
            app.state.semantic_cache = None
            app.state.vector_store = None
            app.state.cache = None
            app.state.db_pool = None

        if shutdown_errors:
            raise shutdown_errors[0]


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

    from app.api.conversations import router as conversations_router
    app.include_router(conversations_router)

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
                "conversations": "GET/POST /conversations",
                "health": "GET /health",
                "cache_stats": "GET /cache/stats",
                "cache_clear": "POST /cache/clear",
                "metrics_me": "GET /metrics/me",
                "admin_metrics": "GET /admin/metrics",
                "docs": "GET /docs",
            },
        }

    @app.get("/metrics/me", response_model=NamespaceMetricsSnapshot, tags=["Metrics"], summary="Tenant Runtime Metrics")
    async def get_tenant_metrics(
        client_namespace: str = Depends(verify_client_token),
        collector: RuntimeMetricsCollector = Depends(get_metrics_collector),
    ) -> NamespaceMetricsSnapshot:
        """Tenant runtime metrics snapshot isolated to the authenticated namespace."""
        return collector.get_namespace_snapshot(client_namespace)

    @app.get(
        "/admin/metrics",
        response_model=AdminMetricsSnapshot,
        dependencies=[Depends(verify_admin_token)],
        tags=["Metrics"],
        summary="Administrator System Metrics",
    )
    async def get_admin_metrics(
        collector: RuntimeMetricsCollector = Depends(get_metrics_collector),
    ) -> AdminMetricsSnapshot:
        """Global administrative metrics snapshot with aggregate totals and per-namespace breakdown."""
        return collector.get_admin_snapshot()

    @app.get("/health", summary="Health Check")
    async def health_check(
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Health check endpoint for liveness and readiness monitoring."""
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
            "database": {
                "backend": current_settings.cache_backend,
                "status": db_status,
                "connected": db_connected,
            },
            "cache": {
                "enabled": current_settings.cache_enabled,
                "backend": current_settings.cache_backend,
            },
        }

    @app.get("/cache/stats", response_model=CacheStats, tags=["Cache"], summary="Global Cache Statistics")
    async def global_cache_stats(
        request: Request,
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
        auth_info: tuple[bool, Optional[str]] = Depends(verify_metrics_auth),
    ) -> CacheStats:
        """Cache performance statistics endpoint. Scoped to authenticated client namespace or global for admin."""
        is_admin, client_ns = auth_info
        target_ns = None if is_admin else client_ns
        stats = await cache.stats(
            enabled=current_settings.cache_enabled,
            namespace=target_ns,
        )
        if hasattr(request.app.state, "semantic_cache") and request.app.state.semantic_cache is not None:
            stats.semantic = request.app.state.semantic_cache.get_stats(namespace=target_ns)
        return stats

    @app.get("/cache/semantic/stats", tags=["Cache"], summary="Semantic Cache Statistics")
    async def semantic_cache_stats(
        request: Request,
        auth_info: tuple[bool, Optional[str]] = Depends(verify_metrics_auth),
    ) -> Dict[str, Any]:
        """Observability counters for semantic cache operations. Scoped to authenticated client namespace or global for admin."""
        is_admin, client_ns = auth_info
        target_ns = None if is_admin else client_ns
        if hasattr(request.app.state, "semantic_cache") and request.app.state.semantic_cache is not None:
            stats_dict = request.app.state.semantic_cache.get_stats(namespace=target_ns)
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
