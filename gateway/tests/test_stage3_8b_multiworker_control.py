"""
Stage 3.8B Verification Tests — Multi-Worker Runtime Serving-State Coordination.

Validates:
1. Offline fail-closed contracts for PostgresRuntimeControlStore and SemanticServingController.
2. Unchanged safe defaults (semantic cache & runtime serving disabled by default).
3. Multi-worker PostgreSQL shared runtime control propagation across independent pools/workers:
   - Worker A toggle immediately visible to Worker B before lookup (Checkpoint 1).
   - Worker A kill switch mid-lookup enforced by Worker B before delivery (Checkpoint 2).
   - Namespace overrides and global disable precedence across workers.
   - Worker restart/scale-out preserves shared live control state (ON CONFLICT DO NOTHING) and
     worker shutdown does not clobber shared DB state for running peer workers.
   - PostgreSQL outage/corruption fails closed on query path (safe upstream fallback) and
     returns 503 on administrative control endpoints.
   - Concurrent writers/readers remain consistent across independent pools.
"""

from __future__ import annotations

import asyncio
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import Settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.main import create_app
from app.metrics import RuntimeMetricsCollector, get_metrics_collector
from app.providers.interfaces import LLMProvider
from app.semantic_cache.control import (
    PostgresRuntimeControlStore,
    SemanticServingController,
    detach_postgres_runtime_control,
    get_semantic_serving_controller,
    reset_semantic_serving_controller,
    sync_semantic_serving_controller_with_postgres,
)
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.in_memory_store import InMemoryVectorStore

_EVIDENCE_SECRET = "s38b-evidence-secret-0123456789abcdefABCDEF!"


class _StubEmbedder(EmbeddingProvider):
    @property
    def provider_name(self) -> str:
        return "stub-s38b"

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def dimensions(self) -> int:
        return 8

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        # Orthogonal unless sharing keyword "python"
        if "python" in text.lower():
            vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        else:
            vec = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        return EmbeddingResult(vector=vec, model=self.model_name, dimensions=8)

    async def embed_batch(self, texts: list[str], task: EmbeddingTask = EmbeddingTask.QUERY) -> list[EmbeddingResult]:
        return [await self.embed(t, task=task) for t in texts]

    async def close(self) -> None:
        return None


class _StubProvider(LLMProvider):
    def __init__(self, label: str = "UPSTREAM") -> None:
        self.label = label
        self.calls: list[GenerationRequest] = []

    @property
    def name(self) -> str:
        return "groq"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        return GenerationResult(
            text=f"{self.label}-{len(self.calls)}: list(reversed(items))",
            model=request.model,
            usage=UsageMetrics(prompt_tokens=12, completion_tokens=8, total_tokens=20),
            latency_ms=100.0,
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        raise NotImplementedError

    async def close(self) -> None:
        return None


class TestStage38BUnitContracts(unittest.IsolatedAsyncioTestCase):
    """Offline unit tests verifying safe defaults, fail-closed checks, and store delegation."""

    def setUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)

    def tearDown(self) -> None:
        reset_semantic_serving_controller(False)
        reload_settings()

    def test_safe_defaults_remain_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Settings()
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertFalse(cfg.semantic_cache_serving_enabled)
        self.assertFalse(cfg.live_provider_evaluation_enabled)
        ctrl = get_semantic_serving_controller()
        self.assertFalse(ctrl.is_serving_enabled("default"))

    async def test_shared_store_failures_fail_closed_and_ignore_stale_local_true(self) -> None:
        collector = RuntimeMetricsCollector()
        ctrl = SemanticServingController(initial_enabled=True, metrics_collector=collector)

        # Even if local flag was True, authoritative shared store returning False/raising/non-bool MUST return False
        failing_store = MagicMock(spec=PostgresRuntimeControlStore)
        failing_store.is_serving_enabled = AsyncMock(side_effect=RuntimeError("pg connection lost"))
        ctrl.set_shared_store(failing_store)
        self.assertFalse(await ctrl.acheck_serving_enabled("ns_fail"))

        non_bool_store = MagicMock(spec=PostgresRuntimeControlStore)
        non_bool_store.is_serving_enabled = AsyncMock(return_value="true")
        ctrl.set_shared_store(non_bool_store)
        self.assertFalse(await ctrl.acheck_serving_enabled("ns_fail"))

        disabled_store = MagicMock(spec=PostgresRuntimeControlStore)
        disabled_store.is_serving_enabled = AsyncMock(return_value=False)
        ctrl.set_shared_store(disabled_store)
        self.assertFalse(await ctrl.acheck_serving_enabled("ns_fail"))

        snap = collector.get_namespace_snapshot("ns_fail")
        self.assertGreaterEqual(snap.semantic.runtime_control_failures, 2)

    async def test_missing_or_corrupted_pg_rows_raise_in_store_and_fail_closed_in_controller(self) -> None:
        conn_missing = AsyncMock()
        conn_missing.fetchrow = AsyncMock(return_value=None)
        acq_missing = MagicMock()
        acq_missing.__aenter__ = AsyncMock(return_value=conn_missing)
        acq_missing.__aexit__ = AsyncMock(return_value=False)
        pool_missing = MagicMock()
        pool_missing.acquire = MagicMock(return_value=acq_missing)

        store_missing = PostgresRuntimeControlStore(pool_missing)
        with self.assertRaises(RuntimeError):
            await store_missing.is_serving_enabled("default")

        ctrl = SemanticServingController(initial_enabled=True, shared_store=store_missing)
        self.assertFalse(await ctrl.acheck_serving_enabled("default"))

        conn_corrupt = AsyncMock()
        conn_corrupt.fetchrow = AsyncMock(return_value={"global_enabled": "yes", "namespace_override": None})
        acq_corrupt = MagicMock()
        acq_corrupt.__aenter__ = AsyncMock(return_value=conn_corrupt)
        acq_corrupt.__aexit__ = AsyncMock(return_value=False)
        pool_corrupt = MagicMock()
        pool_corrupt.acquire = MagicMock(return_value=acq_corrupt)

        store_corrupt = PostgresRuntimeControlStore(pool_corrupt)
        ctrl.set_shared_store(store_corrupt)
        self.assertFalse(await ctrl.acheck_serving_enabled("default"))

    async def test_detach_does_not_overwrite_shared_store(self) -> None:
        ctrl = get_semantic_serving_controller()
        mock_store = MagicMock(spec=PostgresRuntimeControlStore)
        mock_store.schedule_write = MagicMock()
        mock_store.reset_state = AsyncMock()
        ctrl.set_shared_store(mock_store)
        ctrl.set_serving_enabled(True, reason="active")

        detach_postgres_runtime_control()
        self.assertIsNone(ctrl.get_shared_store())
        self.assertFalse(ctrl.is_serving_enabled())
        mock_store.reset_state.assert_not_awaited()


_TEST_DB_URL = os.getenv("TEST_DATABASE_URL", "").strip()


@unittest.skipUnless(
    bool(_TEST_DB_URL),
    "PostgreSQL multi-worker coordination tests require TEST_DATABASE_URL.",
)
class TestStage38BPostgresMultiWorkerCoordination(unittest.IsolatedAsyncioTestCase):
    """
    Live PostgreSQL integration tests simulating two independent gateway workers
    with separate asyncpg pools and separate SemanticServingController instances.
    """

    async def asyncSetUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)
        self.pool_w1 = await asyncpg.create_pool(dsn=_TEST_DB_URL, min_size=1, max_size=4)
        self.pool_w2 = await asyncpg.create_pool(dsn=_TEST_DB_URL, min_size=1, max_size=4)
        await run_migrations(self.pool_w1)
        async with self.pool_w1.acquire() as conn:
            await conn.execute("TRUNCATE TABLE semantic_runtime_namespace_overrides, semantic_runtime_control CASCADE")

    async def asyncTearDown(self) -> None:
        try:
            async with self.pool_w1.acquire() as conn:
                await conn.execute("TRUNCATE TABLE semantic_runtime_namespace_overrides, semantic_runtime_control CASCADE")
        finally:
            await self.pool_w1.close()
            await self.pool_w2.close()
            reset_semantic_serving_controller(False)
            reload_settings()

    def _build_worker_core(
        self,
        *,
        pool: asyncpg.Pool,
        shared_vstore: InMemoryVectorStore,
        shared_exact_cache: ExactCache,
        collector: RuntimeMetricsCollector,
        provider_label: str,
    ) -> tuple[SemanticServingController, SemanticCacheService, IntelliCacheCore, _StubProvider]:
        store = PostgresRuntimeControlStore(pool=pool)
        ctrl = SemanticServingController(initial_enabled=False, shared_store=store, metrics_collector=collector)
        auth = EvidenceAuthority(secret_key=_EVIDENCE_SECRET)
        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="stub-model",
            evidence_authority=auth,
        )
        svc = SemanticCacheService(
            embedding_provider=_StubEmbedder(),
            vector_store=shared_vstore,
            policy=policy,
            metrics_collector=collector,
            evidence_authority=auth,
        )
        core = IntelliCacheCore(
            cache=shared_exact_cache,
            semantic_cache=svc,
            metrics_collector=collector,
            cache_enabled=True,
            semantic_serving_controller=ctrl,
            semantic_cache_startup_enabled=True,
        )
        return ctrl, svc, core, _StubProvider(provider_label)

    async def test_two_workers_share_kill_switch_namespace_overrides_and_checkpoint2(self) -> None:
        collector = RuntimeMetricsCollector()
        store_seed = PostgresRuntimeControlStore(self.pool_w1)
        init_state = await store_seed.ensure_initialized(initial_enabled=False, updated_by="startup")
        self.assertFalse(init_state.runtime_enabled)

        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        vstore = InMemoryVectorStore(dimensions=8, default_model="stub-model", exact_cache=exact_cache)

        ctrl_w1, _svc1, core_w1, prov_w1 = self._build_worker_core(
            pool=self.pool_w1,
            shared_vstore=vstore,
            shared_exact_cache=exact_cache,
            collector=collector,
            provider_label="W1",
        )
        ctrl_w2, svc2, core_w2, prov_w2 = self._build_worker_core(
            pool=self.pool_w2,
            shared_vstore=vstore,
            shared_exact_cache=exact_cache,
            collector=collector,
            provider_label="W2",
        )

        # Both workers start fail-closed (FALSE) matching safe default
        self.assertFalse(await ctrl_w1.acheck_serving_enabled("team_a"))
        self.assertFalse(await ctrl_w2.acheck_serving_enabled("team_a"))

        # Seed verified candidate via Worker 1 while serving is OFF (miss on W1, indexed into shared store)
        seed_req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="How to reverse a python list?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_a",
            visibility="tenant_shared",
        )
        r_seed = await core_w1.execute_query(seed_req, prov_w1)
        self.assertFalse(r_seed.cached)
        self.assertEqual(len(prov_w1.calls), 1)

        # Seed a second namespace team_b
        seed_b = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="How to reverse a python tuple?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_b",
            visibility="tenant_shared",
        )
        await core_w1.execute_query(seed_b, prov_w1)

        # Admin toggles serving ON via Worker 1 ONLY
        new_state = await ctrl_w1.aset_serving_enabled(True, updated_by="ops_admin", reason="enable_cluster")
        self.assertTrue(new_state.runtime_enabled)
        self.assertGreater(new_state.version, 1)

        # Worker 2 IMMEDIATELY sees runtime_enabled=True from PostgreSQL and serves semantic hit!
        self.assertTrue(await ctrl_w2.acheck_serving_enabled("team_a"))
        probe_a = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Way to invert a python sequence?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_a",
            visibility="tenant_shared",
        )
        r_hit_w2 = await core_w2.execute_query(probe_a, prov_w2)
        self.assertTrue(r_hit_w2.cached)
        self.assertEqual(r_hit_w2.cache_type, "semantic")
        self.assertEqual(len(prov_w2.calls), 0)

        # Worker 1 disables ONLY team_a namespace override -> Worker 2 stops serving team_a, still serves team_b
        await ctrl_w1.aset_serving_enabled(False, namespace="team_a", updated_by="ops_admin", reason="pause_team_a")
        self.assertFalse(await ctrl_w2.acheck_serving_enabled("team_a"))
        self.assertTrue(await ctrl_w2.acheck_serving_enabled("team_b"))

        r_miss_a = await core_w2.execute_query(probe_a, prov_w2)
        self.assertFalse(r_miss_a.cached)
        self.assertEqual(len(prov_w2.calls), 1)

        probe_b = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Way to invert a python sequence?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_b",
            visibility="tenant_shared",
        )
        r_hit_b = await core_w2.execute_query(probe_b, prov_w2)
        self.assertTrue(r_hit_b.cached)
        self.assertEqual(r_hit_b.cache_type, "semantic")

        # Clear override on Worker 1, then test mid-lookup Checkpoint 2 kill-switch across Worker 2 & Worker 1!
        await ctrl_w1.aset_serving_enabled(True, namespace="team_a", clear_namespace_override=True, reason="resume")
        self.assertTrue(await ctrl_w2.acheck_serving_enabled("team_a"))

        # Explicit deterministic precedence check:
        # 1) Global enabled + explicit namespace=True -> enabled for team_a
        await ctrl_w1.aset_serving_enabled(True, namespace="team_a", updated_by="ops_admin", reason="allow_team_a")
        # 2) Global disabled -> overrides team_a=True across all workers (global kill switch wins)
        await ctrl_w1.aset_serving_enabled(False, updated_by="ops_admin", reason="global_kill_precedence")
        self.assertFalse(await ctrl_w1.acheck_serving_enabled("team_a"))
        self.assertFalse(await ctrl_w2.acheck_serving_enabled("team_a"))
        state_w2_killed = await ctrl_w2.aget_state()
        self.assertFalse(state_w2_killed.to_dict(startup_enabled=True, namespace="team_a")["namespace_runtime_enabled"])
        self.assertTrue(state_w2_killed.namespace_overrides["team_a"])
        # 3) Global re-enabled -> team_a override (True) applies and unoverridden team_b inherits global True
        await ctrl_w1.aset_serving_enabled(True, updated_by="ops_admin", reason="global_re_enable")
        self.assertTrue(await ctrl_w2.acheck_serving_enabled("team_a"))
        self.assertTrue(await ctrl_w2.acheck_serving_enabled("team_b"))
        await ctrl_w1.aset_serving_enabled(True, namespace="team_a", clear_namespace_override=True, reason="cleanup_override")

        orig_lookup = svc2.lookup
        in_lookup = asyncio.Event()
        allow_return = asyncio.Event()

        async def _gated_lookup(req: GenerationRequest):
            res = await orig_lookup(req)
            in_lookup.set()
            await allow_return.wait()
            return res

        svc2.lookup = _gated_lookup  # type: ignore[method-assign]
        try:
            probe_cp2 = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="Python reverse syntax check?")],
                temperature=0.0,
                max_tokens=64,
                namespace="team_a",
                visibility="tenant_shared",
            )
            w2_task = asyncio.create_task(core_w2.execute_query(probe_cp2, prov_w2))
            await in_lookup.wait()

            # Worker 1 flips global kill-switch OFF while Worker 2's lookup was in-flight!
            await ctrl_w1.aset_serving_enabled(False, updated_by="incident_commander", reason="cross_worker_cp2_kill")
            allow_return.set()

            w2_result = await w2_task
            self.assertFalse(w2_result.cached)
            self.assertEqual(w2_result.cache_type, "none")
            self.assertEqual(len(prov_w2.calls), 2)
        finally:
            svc2.lookup = orig_lookup  # type: ignore[method-assign]

    async def test_worker_startup_preserves_live_pg_state_and_shutdown_preserves_peer(self) -> None:
        # Simulate Worker 1 starting with default False, admin enabling True, then Worker 2 booting with default False
        settings_false = type("S", (), {"semantic_cache_serving_enabled": False})()
        ctrl1 = await sync_semantic_serving_controller_with_postgres(settings_false, self.pool_w1)
        self.assertFalse(await ctrl1.acheck_serving_enabled())

        await ctrl1.aset_serving_enabled(True, updated_by="admin", reason="enable_live")
        self.assertTrue(await ctrl1.acheck_serving_enabled())

        # Worker 2 joins with SEMANTIC_CACHE_SERVING_ENABLED=false -> must preserve shared True!
        store2 = PostgresRuntimeControlStore(self.pool_w2)
        seeded2 = await store2.ensure_initialized(initial_enabled=False, updated_by="worker2_boot")
        self.assertTrue(seeded2.runtime_enabled)

        # Worker 1 shuts down (detach) -> Worker 2's shared PostgreSQL state MUST remain True!
        detach_postgres_runtime_control()
        self.assertTrue(await store2.is_serving_enabled())

    async def test_concurrent_cross_worker_toggles_and_pg_outage_fails_closed(self) -> None:
        store1 = PostgresRuntimeControlStore(self.pool_w1)
        store2 = PostgresRuntimeControlStore(self.pool_w2)
        await store1.ensure_initialized(initial_enabled=False)

        ctrl1 = SemanticServingController(initial_enabled=False, shared_store=store1)
        ctrl2 = SemanticServingController(initial_enabled=False, shared_store=store2)

        async def _writer(ctrl: SemanticServingController, count: int) -> None:
            for i in range(count):
                await ctrl.aset_serving_enabled((i % 2) == 0, reason=f"toggle-{i}")

        async def _reader(ctrl: SemanticServingController, count: int) -> None:
            for _ in range(count):
                val = await ctrl.acheck_serving_enabled("team_a")
                self.assertIsInstance(val, bool)

        await asyncio.gather(
            _writer(ctrl1, 15),
            _writer(ctrl2, 15),
            _reader(ctrl1, 30),
            _reader(ctrl2, 30),
        )
        final_state = await ctrl1.aget_state()
        self.assertGreaterEqual(final_state.version, 31)

        # Simulate DB outage after startup -> acheck fails closed cleanly (returns False)
        broken_pool = MagicMock()
        broken_pool.acquire = MagicMock(side_effect=ConnectionError("pg unavailable"))
        broken_store = PostgresRuntimeControlStore(broken_pool)
        collector = RuntimeMetricsCollector()
        ctrl_broken = SemanticServingController(initial_enabled=True, shared_store=broken_store, metrics_collector=collector)
        self.assertFalse(await ctrl_broken.acheck_serving_enabled("team_a"))
        snap = collector.get_namespace_snapshot("team_a")
        self.assertGreaterEqual(snap.semantic.runtime_control_failures, 1)

    async def test_http_admin_endpoints_use_shared_pg_control_and_return_503_on_outage(self) -> None:
        admin_tok = "s38b-admin-token"
        env_vars = {
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": _TEST_DB_URL,
            "ADMIN_API_TOKEN": admin_tok,
            "CLIENT_API_TOKENS": '{"tok_s38b": {"namespace": "ns_http", "visibility": "private"}}',
            "SEMANTIC_CACHE_ENABLED": "false",
            "SEMANTIC_CACHE_SERVING_ENABLED": "false",
            "GROQ_API_KEY": "gsk_test",
        }
        with patch.dict(os.environ, env_vars, clear=True):
            reload_settings()
            app = create_app()
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    r_get = await client.get("/admin/semantic-cache/serving", headers={"X-Admin-Token": admin_tok})
                    self.assertEqual(r_get.status_code, 200)
                    body = r_get.json()
                    self.assertFalse(body["runtime_enabled"])
                    self.assertEqual(body["coordination_backend"], "postgres_shared")

                    r_post = await client.post(
                        "/admin/semantic-cache/serving",
                        headers={"X-Admin-Token": admin_tok},
                        json={"enabled": True, "reason": "http_test"},
                    )
                    self.assertEqual(r_post.status_code, 200)
                    self.assertTrue(r_post.json()["runtime_enabled"])

                    # Verify second independent pool immediately sees enabled=True!
                    peer_store = PostgresRuntimeControlStore(self.pool_w2)
                    self.assertTrue(await peer_store.is_serving_enabled())

                    # Break controller store -> admin endpoints must return 503 cleanly (not 200/500), stats fails closed
                    ctrl = get_semantic_serving_controller()
                    broken_pool = MagicMock()
                    broken_pool.acquire = MagicMock(side_effect=ConnectionError("pg down"))
                    ctrl.set_shared_store(PostgresRuntimeControlStore(broken_pool))

                    r_out_get = await client.get("/admin/semantic-cache/serving", headers={"X-Admin-Token": admin_tok})
                    self.assertEqual(r_out_get.status_code, 503)

                    r_out_post = await client.post(
                        "/admin/semantic-cache/serving",
                        headers={"X-Admin-Token": admin_tok},
                        json={"enabled": False, "reason": "outage"},
                    )
                    self.assertEqual(r_out_post.status_code, 503)

                    r_stats = await client.get("/cache/semantic/stats", headers={"X-Admin-Token": admin_tok})
                    self.assertEqual(r_stats.status_code, 200)
                    self.assertFalse(r_stats.json()["runtime_serving_enabled"])

    async def test_follower_enforces_checkpoint2_across_workers_and_preserves_exact_cache(self) -> None:
        """
        Stage 3.8B.1:
        1) Worker 1 disables global serving AFTER Worker 2 leader passes Checkpoint 2 and BEFORE
           Worker 2 SingleFlight follower delivers -> follower independently checks PostgreSQL Checkpoint 2,
           rejects the coalesced semantic entry, falls back to upstream, and records 0 coalesced_semantic_hits.
        2) With semantic serving disabled, exact-cache SingleFlight coalescing and exact cache hits remain active.
        """
        collector = RuntimeMetricsCollector()
        store_seed = PostgresRuntimeControlStore(self.pool_w1)
        await store_seed.ensure_initialized(initial_enabled=False, updated_by="startup")

        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        vstore = InMemoryVectorStore(dimensions=8, default_model="stub-model", exact_cache=exact_cache)

        ctrl_w1, _svc1, core_w1, prov_w1 = self._build_worker_core(
            pool=self.pool_w1,
            shared_vstore=vstore,
            shared_exact_cache=exact_cache,
            collector=collector,
            provider_label="W1",
        )
        ctrl_w2, svc2, core_w2, prov_w2 = self._build_worker_core(
            pool=self.pool_w2,
            shared_vstore=vstore,
            shared_exact_cache=exact_cache,
            collector=collector,
            provider_label="W2",
        )

        # Seed semantic entry while serving is OFF
        seed_req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="How to reverse a python list?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_sf_38b1",
            visibility="tenant_shared",
        )
        await core_w1.execute_query(seed_req, prov_w1)

        # Turn serving ON via Worker 1
        await ctrl_w1.aset_serving_enabled(True, updated_by="admin", reason="enable_for_follower_test")

        sf_w2 = SingleFlightCoordinator()
        core_w2.single_flight = sf_w2
        in_lookup = asyncio.Event()
        release_lookup = asyncio.Event()
        orig_lookup = svc2.lookup
        orig_complete = sf_w2.complete_success
        kill_after_leader_cp2 = asyncio.Event()

        async def _paused_lookup(req: GenerationRequest):
            res = await orig_lookup(req)
            in_lookup.set()
            await release_lookup.wait()
            return res

        async def _kill_between_leader_cp2_and_follower_wake(key, value, **kwargs):
            # Flip global kill switch OFF on Worker 1 AFTER leader Checkpoint 2 passed
            # and BEFORE waiting follower runs its independent Checkpoint 2 read!
            await ctrl_w1.aset_serving_enabled(False, updated_by="incident_ops", reason="mid_coalesce_global_kill")
            kill_after_leader_cp2.set()
            return await orig_complete(key, value, **kwargs)

        svc2.lookup = _paused_lookup  # type: ignore[method-assign]
        sf_w2.complete_success = _kill_between_leader_cp2_and_follower_wake  # type: ignore[method-assign]
        try:
            para_req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="Invert a python sequence cleanly?")],
                temperature=0.0,
                max_tokens=64,
                namespace="team_sf_38b1",
                visibility="tenant_shared",
            )
            leader_task = asyncio.create_task(core_w2.execute_query(para_req, prov_w2))
            await in_lookup.wait()
            follower_task = asyncio.create_task(core_w2.execute_query(para_req, prov_w2))
            await asyncio.sleep(0.03)
            release_lookup.set()
            leader_res, follower_res = await asyncio.gather(leader_task, follower_task)
        finally:
            svc2.lookup = orig_lookup  # type: ignore[method-assign]
            sf_w2.complete_success = orig_complete  # type: ignore[method-assign]

        self.assertTrue(kill_after_leader_cp2.is_set())
        # Leader passed CP2 prior to kill switch
        self.assertTrue(leader_res.cached)
        self.assertEqual(leader_res.cache_type, "semantic")
        # Follower independently checked CP2 against PostgreSQL, saw disabled=True->False, and fell back!
        self.assertFalse(follower_res.cached)
        self.assertEqual(follower_res.cache_type, "none")
        self.assertEqual(len(prov_w2.calls), 1)
        snap = collector.get_namespace_snapshot("team_sf_38b1")
        self.assertEqual(snap.semantic.coalesced_semantic_hits, 0)
        self.assertGreaterEqual(snap.semantic.semantic_runtime_disabled_skips, 1)

        # While semantic serving remains disabled, exact-cache coalescing + hits continue working normally!
        in_exact_prov = asyncio.Event()
        release_exact_prov = asyncio.Event()

        class _GatedProvider(_StubProvider):
            async def generate(self, request: GenerationRequest) -> GenerationResult:
                if not in_exact_prov.is_set():
                    in_exact_prov.set()
                    await release_exact_prov.wait()
                return await super().generate(request)

        gated_prov = _GatedProvider("EXACT_SF")
        exact_req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Exact cache preservation check?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_sf_38b1",
            visibility="tenant_shared",
        )
        ex_leader_t = asyncio.create_task(core_w2.execute_query(exact_req, gated_prov))
        await in_exact_prov.wait()
        ex_follower_t = asyncio.create_task(core_w2.execute_query(exact_req, gated_prov))
        await asyncio.sleep(0.03)
        release_exact_prov.set()
        ex_leader, ex_follower = await asyncio.gather(ex_leader_t, ex_follower_t)

        self.assertFalse(ex_leader.cached)
        self.assertTrue(ex_follower.cached)
        self.assertEqual(ex_follower.cache_type, "exact")
        self.assertEqual(len(gated_prov.calls), 1)

        ex_repeat = await core_w2.execute_query(exact_req, gated_prov)
        self.assertTrue(ex_repeat.cached)
        self.assertEqual(ex_repeat.cache_type, "exact")
        self.assertEqual(len(gated_prov.calls), 1)

    async def test_concurrent_request_local_admission_telemetry_and_bypass_isolation(self) -> None:
        """
        Stage 3.8B.1: Concurrent ADMIT, BYPASS, and REJECT requests preserve per-request
        admission_status/admission_reason_code without cross-request contamination from shared state.
        """
        collector = RuntimeMetricsCollector()
        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        vstore = InMemoryVectorStore(dimensions=8, default_model="stub-model", exact_cache=exact_cache)
        _ctrl, svc, core, _prov = self._build_worker_core(
            pool=self.pool_w1,
            shared_vstore=vstore,
            shared_exact_cache=exact_cache,
            collector=collector,
            provider_label="CONC",
        )

        class _SlowAdmitProvider(_StubProvider):
            async def generate(self, request: GenerationRequest) -> GenerationResult:
                text_in = request.messages[-1].content.lower()
                if "slow admit" in text_in:
                    # Sleep long enough for bypass + reject queries to finish first and overwrite shared state
                    await asyncio.sleep(0.06)
                self.calls.append(request)
                return GenerationResult(
                    text="list(reversed(items))",
                    model=request.model,
                    usage=UsageMetrics(prompt_tokens=10, completion_tokens=6, total_tokens=16),
                    latency_ms=40.0,
                    finish_reason="stop",
                )

        # Also test concurrency AFTER index_with_decision completes for ADMIT vs BYPASS overlap
        admit_started = asyncio.Event()
        allow_admit_return = asyncio.Event()
        orig_index_wd = svc.index_with_decision

        async def _paused_index_with_decision(req: GenerationRequest, res: GenerationResult):
            admitted, dec = await orig_index_wd(req, res)
            if "overlapping admit" in req.messages[-1].content.lower():
                admit_started.set()
                await allow_admit_return.wait()
            return admitted, dec

        svc.index_with_decision = _paused_index_with_decision  # type: ignore[method-assign]
        prov = _SlowAdmitProvider("CONC")
        try:
            admit_req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="Overlapping admit python reverse list")],
                temperature=0.0,
                max_tokens=64,
                namespace="team_adm_conc",
                visibility="tenant_shared",
            )
            bypass_req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="Sensitive bypass query do not cache")],
                temperature=0.0,
                max_tokens=64,
                namespace="team_adm_conc",
                visibility="tenant_shared",
                bypass_cache=True,
            )
            reject_req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="High temperature unsupported admit")],
                temperature=0.9,
                max_tokens=64,
                namespace="team_adm_conc",
                visibility="tenant_shared",
            )
            sensitive_req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content="Store token sk-proj-sensitive1234567890abcdef")],
                temperature=0.0,
                max_tokens=64,
                namespace="team_adm_conc",
                visibility="tenant_shared",
            )

            t_admit = asyncio.create_task(core.execute_query(admit_req, prov))
            await admit_started.wait()
            # While ADMIT is paused before returning, BYPASS, sensitive auto-bypass, and REJECT finish concurrently
            res_bypass, res_sensitive, res_reject = await asyncio.gather(
                core.execute_query(bypass_req, prov),
                core.execute_query(sensitive_req, prov),
                core.execute_query(reject_req, prov),
            )
            allow_admit_return.set()
            res_admit = await t_admit
        finally:
            svc.index_with_decision = orig_index_wd  # type: ignore[method-assign]

        # Each result reflects its own request-local admission decision despite concurrent overlap
        self.assertEqual(res_admit.admission_status, "ADMIT")
        self.assertEqual(res_admit.admission_reason_code, "ADMISSION_ADMITTED")
        self.assertIsNotNone(res_admit.admission_decision)
        self.assertEqual(res_bypass.admission_status, "BYPASS")
        self.assertEqual(res_bypass.admission_reason_code, "CACHE_BYPASSED_SENSITIVE_OR_INELIGIBLE")
        self.assertIsNone(res_bypass.admission_decision)
        self.assertEqual(res_sensitive.admission_status, "BYPASS")
        self.assertEqual(res_sensitive.admission_reason_code, "CACHE_BYPASSED_SENSITIVE_OR_INELIGIBLE")
        self.assertIsNone(res_sensitive.admission_decision)
        self.assertEqual(res_reject.admission_status, "REJECT")
        self.assertEqual(res_reject.admission_reason_code, "ADMISSION_REJECTED_INELIGIBLE")
        # Legacy shared mirror ended on REJECT, proving res_admit/res_bypass did NOT rely on shared mutable state
        self.assertEqual(svc.last_admission_decision.status.value, "REJECT")
