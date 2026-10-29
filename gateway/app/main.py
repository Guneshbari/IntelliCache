"""
Main entry point for the IntelliCache Gateway FastAPI application.
Configures CORS, registers query and health routes, and initializes middleware.
"""

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import time
from typing import Any, AsyncGenerator, Dict

from fastapi import Depends, FastAPI
from fastapi.middleware.cors import CORSMiddleware
import uvicorn

from app.config import Settings, get_settings
from app.routes.query import (
    CacheStats,
    QueryCache,
    get_query_cache,
    router as query_router,
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
    logger.info(f"Model: {settings.groq_model}")
    logger.info(f"Host: {settings.host}:{settings.port}")
    logger.info(f"Cache: {'Enabled' if settings.cache_enabled else 'Disabled'} (TTL: {settings.cache_ttl_seconds}s)")
    logger.info("=" * 60)
    yield
    logger.info("Shutting down IntelliCache Gateway.")


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
        cache: QueryCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Health check endpoint for liveness and readiness monitoring."""
        stats = cache.stats(enabled=current_settings.cache_enabled)
        has_api_key = bool(
            current_settings.groq_api_key and current_settings.groq_api_key != "replace_with_your_key"
        )
        return {
            "status": "healthy" if has_api_key else "degraded",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "groq_configured": has_api_key,
            "groq_model": current_settings.groq_model,
            "cache": {
                "enabled": current_settings.cache_enabled,
                "entries": stats.total_entries,
                "hit_rate": stats.hit_rate,
                "hits": stats.hits,
                "misses": stats.misses,
            },
        }

    @app.get("/cache/stats", response_model=CacheStats, tags=["Cache"], summary="Global Cache Statistics")
    async def global_cache_stats(
        current_settings: Settings = Depends(get_settings),
        cache: QueryCache = Depends(get_query_cache),
    ) -> CacheStats:
        """Global cache performance statistics endpoint."""
        return cache.stats(enabled=current_settings.cache_enabled)

    @app.post(
        "/cache/clear",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Global Cache Purge",
    )
    async def global_cache_clear(
        cache: QueryCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Purge all entries from the query cache. Requires administrative token."""
        cache.clear()
        return {"status": "ok", "message": "Global query cache purged successfully."}

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
