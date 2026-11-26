"""
Main entry point for the IntelliCache Gateway FastAPI application.
Configures CORS, registers query and health routes, and manages database lifecycles.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
import logging
import time
from typing import Any, AsyncGenerator, Dict, Optional

import asyncpg
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, StrictBool
import uvicorn

from app.cache.migration import run_migrations
from app.config import Settings, get_settings
from app.core.contracts import CACHE_VISIBILITY_UNKNOWN, normalize_cache_visibility, normalize_principal_id
from app.embeddings.factory import EmbeddingProviderFactory
from app.metrics import (
    AdminMetricsSnapshot,
    NamespaceMetricsSnapshot,
    RuntimeMetricsCollector,
    get_metrics_collector,
)
from app.semantic_cache import (
    SemanticCachePolicy,
    SemanticCacheService,
    get_default_evidence_authority,
)
from app.semantic_cache.control import (
    get_semantic_serving_controller,
    sync_semantic_serving_controller_from_settings,
)
from app.semantic_cache.shadow import (
    ReadOnlySemanticCacheService,
    ReadOnlyVectorStoreAdapter,
    ShadowEvaluationService,
)
from app.vector_store import (
    BaseVectorStore,
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
    get_shadow_service,
    get_vector_store,
    router as query_router,
    set_query_cache,
    set_semantic_cache,
    set_shadow_service,
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
    shadow_service: Optional[ShadowEvaluationService] = None,
) -> list[Exception]:
    """
    Cleans up gateway infrastructure resources independently.
    Ensures failure in one resource cleanup does not prevent subsequent resources from closing.
    Avoids double-closing the PostgreSQL pool (closed by PostgresCache if cache exists, or pool directly).
    """
    errors: list[Exception] = []

    # 0. Gracefully stop shadow evaluation service (bounded drain)
    if shadow_service is not None:
        try:
            await shadow_service.stop()
        except Exception as exc:
            logger.error("Error stopping shadow evaluation service: %s", exc)
            errors.append(exc)

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
    shadow_service: Optional[ShadowEvaluationService] = None
    shadow_vector_store: Optional[BaseVectorStore] = None
    shadow_embedding_provider: Optional[Any] = None

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
                    retrieval_budget=settings.semantic_cache_max_retrieval_candidates,
                    evidence_authority=get_default_evidence_authority(
                        settings.semantic_cache_evidence_secret,
                        previous_secret_keys=settings.semantic_cache_evidence_previous_secrets,
                        allow_ephemeral=False,
                    ),
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
                    retrieval_budget=settings.semantic_cache_max_retrieval_candidates,
                    evidence_authority=get_default_evidence_authority(
                        settings.semantic_cache_evidence_secret,
                        previous_secret_keys=settings.semantic_cache_evidence_previous_secrets,
                        allow_ephemeral=True,
                    ),
                )
                semantic_cache = SemanticCacheService(
                    embedding_provider=embedding_provider,
                    vector_store=vector_store,
                    policy=policy,
                    default_namespace=settings.cache_namespace,
                )
                logger.info(
                    "In-memory semantic cache initialized: dimensions=%d, model=%s, min_sim=%.2f, min_margin=%.2f, top_k=%d, retrieval_budget=%d",
                    settings.vector_dimensions,
                    settings.embedding_model,
                    settings.semantic_cache_min_similarity,
                    settings.semantic_cache_min_margin,
                    settings.semantic_cache_top_k,
                    settings.semantic_cache_max_retrieval_candidates,
                )

        # Shadow Evaluation Initialization (Stage 3.3)
        if settings.shadow_evaluation_enabled:
            logger.info("Initializing shadow evaluation components (Stage 3.3)...")
            from concurrent.futures import ThreadPoolExecutor
            shadow_executor = ThreadPoolExecutor(
                max_workers=settings.shadow_max_concurrency,
                thread_name_prefix="shadow-inference",
            )

            # 1. Dedicated Shadow Candidate Store (shadow_vector_records / isolated in-memory)
            if settings.cache_backend == "postgres":
                logger.info("Running vector store migrations for shadow evaluation...")
                await run_vector_migrations(pool, dimensions=settings.vector_dimensions)
                shadow_candidate_store = PostgresVectorStore(
                    pool=pool,
                    default_namespace=settings.cache_namespace,
                    default_model=settings.embedding_model,
                    dimensions=settings.vector_dimensions,
                    table_name="shadow_vector_records",
                )
            else:
                shadow_candidate_store = InMemoryVectorStore(
                    dimensions=settings.vector_dimensions,
                    default_namespace=settings.cache_namespace,
                    default_model=settings.embedding_model,
                    max_records=settings.vector_store_max_entries,
                )
            shadow_vector_store = shadow_candidate_store

            # Wrap in structural read-only adapter with database-level read-only enforcement
            read_only_vec = ReadOnlyVectorStoreAdapter(shadow_candidate_store, enforce_db_readonly=True)

            # 2. Embedding provider (bound to dedicated thread pool executor)
            shadow_embedding_provider = EmbeddingProviderFactory.create(
                provider_name=settings.embedding_provider,
                model=settings.embedding_model,
                dimensions=settings.vector_dimensions,
                base_url=settings.embedding_base_url,
                timeout_seconds=settings.embedding_timeout_seconds,
                executor=shadow_executor,
            )
            shadow_embed_prov = shadow_embedding_provider
            # Warm up model off-thread if supported
            if hasattr(shadow_embed_prov, "initialize"):
                await shadow_embed_prov.initialize()

            # 3. Policy & ReadOnlySemanticCacheService (uses isolated metrics collector and overrides index())
            shadow_policy = SemanticCachePolicy(
                min_similarity=settings.semantic_cache_min_similarity,
                min_margin=settings.semantic_cache_min_margin,
                top_k=settings.semantic_cache_top_k,
                configured_model=settings.embedding_model,
            )
            shadow_sem_cache = ReadOnlySemanticCacheService(
                embedding_provider=shadow_embed_prov,
                vector_store=read_only_vec,
                policy=shadow_policy,
                default_namespace=settings.cache_namespace,
            )

            # 4. ShadowEvaluationService with candidate ingestion support
            shadow_service = ShadowEvaluationService(
                settings=settings,
                semantic_cache=shadow_sem_cache,
                db_pool=pool,
                metrics_collector=collector,
                shadow_vector_store=shadow_candidate_store,
            )
            # Re-use the executor if already created
            shadow_service.executor = shadow_executor
            await shadow_service.start()
            app.state.shadow_service = shadow_service
            set_shadow_service(shadow_service)

        # Register initialized components in app.state and global singletons only after full success
        app.state.db_pool = pool
        app.state.cache = cache
        app.state.vector_store = vector_store
        app.state.semantic_cache = semantic_cache
        app.state.shadow_service = shadow_service
        app.state.semantic_serving_controller = sync_semantic_serving_controller_from_settings(settings)

        set_query_cache(cache)
        set_vector_store(vector_store)
        set_semantic_cache(semantic_cache)
        set_shadow_service(shadow_service)

    except Exception as startup_err:
        logger.error("Startup initialization failed: %s", startup_err)
        try:
            await _cleanup_gateway_resources(
                semantic_cache=semantic_cache,
                vector_store=vector_store or shadow_vector_store,
                embedding_provider=embedding_provider or shadow_embedding_provider,
                cache=cache,
                pool=pool,
                shadow_service=shadow_service,
            )
        finally:
            sync_semantic_serving_controller_from_settings(settings)
            set_shadow_service(None)
            set_semantic_cache(None)
            set_vector_store(None)
            set_query_cache(None)
            app.state.shadow_service = None
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
                vector_store=vector_store or shadow_vector_store,
                embedding_provider=embedding_provider or shadow_embedding_provider,
                cache=cache,
                pool=pool,
                shadow_service=shadow_service,
            )
        finally:
            sync_semantic_serving_controller_from_settings(settings)
            set_shadow_service(None)
            set_semantic_cache(None)
            set_vector_store(None)
            set_query_cache(None)
            app.state.shadow_service = None
            app.state.semantic_cache = None
            app.state.vector_store = None
            app.state.cache = None
            app.state.db_pool = None

        if shutdown_errors:
            raise shutdown_errors[0]


class SemanticServingControlUpdateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: StrictBool
    namespace: Optional[str] = None
    clear_namespace_override: StrictBool = False
    reason: Optional[str] = None


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
        current_settings: Settings = Depends(get_settings),
        auth_info: tuple[bool, Optional[str]] = Depends(verify_metrics_auth),
    ) -> Dict[str, Any]:
        """Observability counters for semantic cache operations. Scoped to authenticated client namespace or global for admin."""
        is_admin, client_ns = auth_info
        target_ns = None if is_admin else client_ns
        controller = get_semantic_serving_controller()
        runtime_enabled = controller.is_serving_enabled(target_ns)
        if hasattr(request.app.state, "semantic_cache") and request.app.state.semantic_cache is not None:
            stats_dict = request.app.state.semantic_cache.get_stats(namespace=target_ns)
            stats_dict["enabled"] = bool(current_settings.semantic_cache_enabled)
            stats_dict["runtime_serving_enabled"] = runtime_enabled
            stats_dict["effective_enabled"] = bool(current_settings.semantic_cache_enabled and runtime_enabled)
            return stats_dict
        return {
            "enabled": False,
            "runtime_serving_enabled": runtime_enabled,
            "effective_enabled": False,
            "lookups": 0,
            "hits": 0,
            "coalesced_semantic_hits": 0,
            "misses": 0,
            "rejected_candidates": 0,
            "ambiguous_candidates": 0,
            "eligibility_rejections": 0,
            "embedding_failures": 0,
            "vector_store_failures": 0,
            "indexing_successes": 0,
            "indexing_failures": 0,
            "runtime_disabled_skips": 0,
            "candidates_retrieved": 0,
            "visibility_rejections": 0,
            "principal_isolation_rejections": 0,
            "evidence_validation_rejections": 0,
            "upstream_fallbacks_after_miss": 0,
            "runtime_control_changes": 0,
            "runtime_control_failures": 0,
            "privacy_violation_attempts": 0,
            "context_independent_requests": 0,
            "context_dependent_requests": 0,
            "context_uncertain_requests": 0,
            "context_compatibility_matches": 0,
            "context_compatibility_rejections": 0,
            "context_metadata_missing": 0,
            "context_fingerprint_mismatches": 0,
            "cross_conversation_semantic_hits": 0,
            "cross_conversation_semantic_misses": 0,
            "private_scope_rejections": 0,
            "tenant_shared_authorization_rejections": 0,
            "semantic_admission_successes": 0,
            "semantic_admission_rejections": 0,
            "semantic_fallbacks": 0,
        }

    @app.get(
        "/admin/semantic-cache/serving",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Get Runtime Semantic Cache Serving Status",
    )
    async def get_semantic_cache_serving_control(
        namespace: Optional[str] = None,
        current_settings: Settings = Depends(get_settings),
    ) -> Dict[str, Any]:
        """Inspects server-side runtime semantic serving control state. Requires administrative token."""
        controller = get_semantic_serving_controller()
        state = controller.get_state()
        return {
            "status": "ok",
            **state.to_dict(
                startup_enabled=current_settings.semantic_cache_enabled,
                namespace=namespace,
            ),
        }

    @app.post(
        "/admin/semantic-cache/serving",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Update Runtime Semantic Cache Serving Kill Switch",
    )
    async def set_semantic_cache_serving_control(
        payload: SemanticServingControlUpdateRequest,
        current_settings: Settings = Depends(get_settings),
    ) -> Dict[str, Any]:
        """Atomically enables or disables runtime semantic cache serving. Requires administrative token."""
        controller = get_semantic_serving_controller()
        try:
            state = controller.set_serving_enabled(
                payload.enabled,
                namespace=payload.namespace,
                clear_namespace_override=payload.clear_namespace_override,
                updated_by="admin_api",
                reason=payload.reason,
            )
        except ValueError as val_err:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=str(val_err),
            )
        return {
            "status": "ok",
            **state.to_dict(
                startup_enabled=current_settings.semantic_cache_enabled,
                namespace=payload.namespace,
            ),
        }

    @app.post(
        "/cache/clear",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Clear Configured Cache Namespace",
    )
    @app.post(
        "/admin/cache/clear",
        dependencies=[Depends(verify_admin_token)],
        tags=["Cache"],
        summary="Clear Configured Cache Namespace (Admin Alias)",
    )
    async def clear_cache_namespace(
        request: Request,
        current_settings: Settings = Depends(get_settings),
        cache: BaseCache = Depends(get_query_cache),
    ) -> Dict[str, Any]:
        """Purge entries from the server-configured cache namespace (optionally scoped by visibility/principal). Requires administrative token."""
        target_ns = request.query_params.get("namespace", current_settings.cache_namespace)
        raw_vis = request.query_params.get("visibility")
        raw_principal = request.query_params.get("principal_id")
        clear_vis: Optional[str] = None
        if raw_vis is not None:
            clear_vis = normalize_cache_visibility(raw_vis)
            if clear_vis == CACHE_VISIBILITY_UNKNOWN:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail="Invalid visibility query parameter; expected 'private' or 'tenant_shared'.",
                )
        clear_principal = normalize_principal_id(raw_principal) if raw_principal is not None else None
        ns_arg = None if target_ns == "all" else target_ns
        await cache.clear(
            namespace=ns_arg,
            visibility=clear_vis,
            principal_id=clear_principal,
        )
        vec_store = getattr(request.app.state, "vector_store", None)
        if vec_store is not None and ns_arg is not None:
            await vec_store.clear(
                namespace=ns_arg,
                visibility=clear_vis,
                principal_id=clear_principal,
            )
        return {
            "status": "ok",
            "message": f"Query cache namespace '{target_ns}' cleared successfully.",
            "namespace": target_ns,
        }

    @app.post(
        "/admin/metrics/reset",
        dependencies=[Depends(verify_admin_token)],
        tags=["Metrics"],
        summary="Reset In-Memory Metrics Counters",
    )
    async def reset_metrics() -> Dict[str, str]:
        """Resets in-memory runtime telemetry counters. Requires administrative token."""
        get_metrics_collector().reset()
        return {"status": "ok", "message": "Metrics counters reset successfully."}

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
