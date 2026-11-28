"""
Stage 3.9 Verification Tests — Distributed SingleFlight Coordination & Cross-Worker Request Coalescing.

Validates:
- Offline unit contracts: configuration defaults/validators, coordination key determinism & privacy,
  migration DDL structure, and metrics snapshot contracts.
- All 18 live PostgreSQL multi-worker acceptance scenarios (SF-01 through SF-18) across
  independent IntelliCacheCore workers with separate SingleFlightCoordinator and asyncpg pools.
- HTTP observability endpoints (/cache/stats, /metrics/me, /admin/metrics) distinguishing local vs
  distributed coalescing counters and latencies.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import asyncpg
import httpx

from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import Settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.distributed_singleflight import (
    DistributedLease,
    DistributedSingleFlightConfig,
    PostgresDistributedSingleFlight,
    compute_distributed_coordination_key,
)
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.interfaces import EmbeddingProvider
from app.main import create_app
from app.metrics import RuntimeMetricsCollector, get_metrics_collector
from app.providers.interfaces import LLMProvider
from app.routes.query import (
    get_llm_provider,
    set_distributed_singleflight,
    set_query_cache,
    set_semantic_cache,
)
from app.semantic_cache.control import (
    PostgresRuntimeControlStore,
    SemanticServingController,
    reset_semantic_serving_controller,
)
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.in_memory_store import InMemoryVectorStore

_TEST_DB_URL = os.getenv("TEST_DATABASE_URL")
_EVIDENCE_SECRET = "s39-evidence-secret-0123456789abcdefABCDEF!"


class _StubEmbedder(EmbeddingProvider):
    @property
    def provider_name(self) -> str:
        return "stub-s39"

    @property
    def model_name(self) -> str:
        return "stub-model"

    @property
    def dimensions(self) -> int:
        return 8

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        lower = text.lower()
        if "python" in lower:
            vec = [1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        elif "rust" in lower:
            vec = [0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        else:
            vec = [0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0]
        return EmbeddingResult(vector=vec, model=self.model_name, dimensions=8)

    async def embed_batch(self, texts: list[str], task: EmbeddingTask = EmbeddingTask.QUERY) -> list[EmbeddingResult]:
        return [await self.embed(t, task=task) for t in texts]

    async def close(self) -> None:
        return None


class _ControlledProvider(LLMProvider):
    """Deterministic fake provider with barrier hooks and per-call telemetry."""

    def __init__(
        self,
        label: str = "PROV",
        *,
        started_event: asyncio.Event | None = None,
        release_event: asyncio.Event | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self.label = label
        self.calls: list[GenerationRequest] = []
        self.started_event = started_event
        self.release_event = release_event
        self.delay_seconds = delay_seconds

    @property
    def name(self) -> str:
        return "groq"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        idx = len(self.calls) + 1
        self.calls.append(request)
        if self.started_event is not None:
            self.started_event.set()
        if self.release_event is not None:
            await self.release_event.wait()
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        return GenerationResult(
            text=f"{self.label}-{idx}: deterministic answer",
            model=request.model,
            usage=UsageMetrics(prompt_tokens=15, completion_tokens=10, total_tokens=25),
            latency_ms=42.0,
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        raise NotImplementedError

    async def close(self) -> None:
        return None


class TestStage39OfflineUnitContracts(unittest.TestCase):
    """Offline unit tests verifying config defaults, validation, privacy key construction, and migration SQL."""

    def tearDown(self) -> None:
        reload_settings()

    def test_safe_defaults_remain_disabled(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Settings()
        self.assertFalse(cfg.distributed_singleflight_enabled)
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertFalse(cfg.semantic_cache_serving_enabled)
        self.assertFalse(cfg.live_provider_evaluation_enabled)
        self.assertEqual(cfg.distributed_singleflight_lease_seconds, 30.0)
        self.assertEqual(cfg.distributed_singleflight_renewal_interval_seconds, 10.0)
        self.assertEqual(cfg.distributed_singleflight_follower_wait_timeout_seconds, 35.0)

    def test_config_validation_rejects_invalid_dsf_settings(self) -> None:
        with patch.dict(
            os.environ,
            {
                "CACHE_BACKEND": "memory",
                "DISTRIBUTED_SINGLEFLIGHT_ENABLED": "true",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

        with patch.dict(
            os.environ,
            {
                "DISTRIBUTED_SINGLEFLIGHT_LEASE_SECONDS": "10.0",
                "DISTRIBUTED_SINGLEFLIGHT_RENEWAL_INTERVAL_SECONDS": "10.0",
            },
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

        with patch.dict(
            os.environ,
            {"DISTRIBUTED_SINGLEFLIGHT_FOLLOWER_WAIT_TIMEOUT_SECONDS": "0"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                Settings()

    def test_coordination_key_is_opaque_sha256_and_isolates_compatibility_and_privacy(self) -> None:
        secret_prompt = "Super secret prompt with API key gsk_test1234567890abcdefghij"
        req_base = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content=secret_prompt)],
            temperature=0.0,
            max_tokens=128,
            metadata={"language": "python", "response_format": "json"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="user_secret_alice",
        )
        key_a = compute_distributed_coordination_key(
            namespace="ns_alpha",
            visibility="private",
            owner_principal_id="user_secret_alice",
            allow_shared_reads=False,
            fingerprint="fp_1",
            request=req_base,
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="context-v1:independent",
        )
        self.assertEqual(len(key_a), 64)
        self.assertNotIn("alice", key_a)
        self.assertNotIn("secret", key_a)
        self.assertNotIn("ns_alpha", key_a)

        # Different principal -> different coordination key
        key_bob = compute_distributed_coordination_key(
            namespace="ns_alpha",
            visibility="private",
            owner_principal_id="user_secret_bob",
            allow_shared_reads=False,
            fingerprint="fp_1",
            request=req_base,
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="context-v1:independent",
        )
        self.assertNotEqual(key_a, key_bob)

        # Different compatibility metadata -> different coordination key
        req_rust = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content=secret_prompt)],
            temperature=0.0,
            max_tokens=128,
            metadata={"language": "rust", "response_format": "json"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="user_secret_alice",
        )
        key_rust = compute_distributed_coordination_key(
            namespace="ns_alpha",
            visibility="private",
            owner_principal_id="user_secret_alice",
            allow_shared_reads=False,
            fingerprint="fp_1",
            request=req_rust,
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="context-v1:independent",
        )
        self.assertNotEqual(key_a, key_rust)

        # Different context class / fingerprint -> different key
        key_ctx_dep = compute_distributed_coordination_key(
            namespace="ns_alpha",
            visibility="private",
            owner_principal_id="user_secret_alice",
            allow_shared_reads=False,
            fingerprint="fp_1",
            request=req_base,
            context_class="CONTEXT_DEPENDENT",
            context_fingerprint="context-v1:abc",
        )
        self.assertNotEqual(key_a, key_ctx_dep)

    def test_migration_008_exists_and_contains_expected_ddl(self) -> None:
        mig_path = Path(__file__).resolve().parents[1] / "migrations" / "008_create_distributed_singleflight.sql"
        self.assertTrue(mig_path.exists())
        sql = mig_path.read_text(encoding="utf-8")
        self.assertIn("CREATE TABLE IF NOT EXISTS distributed_singleflight_leases", sql)
        self.assertIn("fencing_token", sql)
        self.assertIn("owner_token", sql)
        self.assertIn("outcome_type", sql)
        self.assertIn("lease_expires_at", sql)


class TestStage39PgFailurePolicyUnit(unittest.IsolatedAsyncioTestCase):
    """SF-11 offline/mocked DB outage policy verification."""

    async def test_sf11_postgres_unavailable_fails_open_without_unsafe_delivery(self) -> None:
        collector = RuntimeMetricsCollector()
        broken_pool = MagicMock()
        broken_acq = MagicMock()
        broken_acq.__aenter__ = AsyncMock(side_effect=ConnectionRefusedError("PostgreSQL unreachable"))
        broken_acq.__aexit__ = AsyncMock(return_value=False)
        broken_pool.acquire = MagicMock(return_value=broken_acq)

        dsf = PostgresDistributedSingleFlight(
            pool=broken_pool,
            config=DistributedSingleFlightConfig(lease_seconds=10.0, renewal_interval_seconds=4.0),
            metrics_collector=collector,
        )

        # Exact cache read fails open as well when PG is down
        failing_cache = PostgresCache(pool=broken_pool, max_entries=100, ttl_seconds=60)
        core = IntelliCacheCore(
            cache=failing_cache,
            single_flight=SingleFlightCoordinator(),
            dist_single_flight=dsf,
            metrics_collector=collector,
            cache_enabled=True,
        )
        prov = _ControlledProvider("FALLBACK")
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Explain Raft consensus")],
            namespace="ns_pg_down",
            visibility="tenant_shared",
        )
        res = await core.execute_query(req, prov)
        self.assertFalse(res.cached)
        self.assertEqual(res.cache_type, "none")
        self.assertEqual(len(prov.calls), 1)
        self.assertIn("FALLBACK-1", res.text)
        snap = collector.get_namespace_snapshot("ns_pg_down")
        self.assertGreaterEqual(snap.distributed_singleflight.coordination_errors, 1)


@unittest.skipUnless(
    bool(_TEST_DB_URL),
    "PostgreSQL distributed SingleFlight integration tests require TEST_DATABASE_URL.",
)
class TestStage39DistributedSingleFlightPostgres(unittest.IsolatedAsyncioTestCase):
    """
    Live PostgreSQL multi-worker integration tests implementing SF-01 through SF-18
    across independent workers with separate pools and separate local SingleFlight instances.
    """

    async def asyncSetUp(self) -> None:
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)
        self.pool_w1 = await asyncpg.create_pool(dsn=_TEST_DB_URL, min_size=1, max_size=6)
        self.pool_w2 = await asyncpg.create_pool(dsn=_TEST_DB_URL, min_size=1, max_size=6)
        await run_migrations(self.pool_w1)
        await self._truncate_tables()

    async def asyncTearDown(self) -> None:
        try:
            await self._truncate_tables()
        finally:
            await self.pool_w1.close()
            await self.pool_w2.close()
            reset_semantic_serving_controller(False)
            reload_settings()

    async def _truncate_tables(self) -> None:
        async with self.pool_w1.acquire() as conn:
            await conn.execute(
                "TRUNCATE TABLE distributed_singleflight_leases, cache_entries, "
                "semantic_runtime_namespace_overrides, semantic_runtime_control CASCADE"
            )

    def _make_worker(
        self,
        pool: asyncpg.Pool,
        *,
        collector: RuntimeMetricsCollector,
        config: DistributedSingleFlightConfig | None = None,
        shared_vstore: InMemoryVectorStore | None = None,
        semantic_enabled: bool = False,
        control_store: PostgresRuntimeControlStore | None = None,
    ) -> tuple[IntelliCacheCore, PostgresCache, PostgresDistributedSingleFlight, SemanticServingController | None, SemanticCacheService | None]:
        cache = PostgresCache(pool=pool, max_entries=500, ttl_seconds=3600, default_namespace="default")
        dsf = PostgresDistributedSingleFlight(
            pool=pool,
            config=config
            or DistributedSingleFlightConfig(
                lease_seconds=10.0,
                renewal_interval_seconds=4.0,
                follower_wait_timeout_seconds=12.0,
                poll_interval_seconds=0.02,
                poll_max_interval_seconds=0.05,
                completion_retention_seconds=5.0,
            ),
            metrics_collector=collector,
        )
        ctrl = None
        sem_svc = None
        if semantic_enabled:
            assert shared_vstore is not None
            store = control_store or PostgresRuntimeControlStore(pool=pool)
            ctrl = SemanticServingController(initial_enabled=False, shared_store=store, metrics_collector=collector)
            auth = EvidenceAuthority(secret_key=_EVIDENCE_SECRET)
            policy = SemanticCachePolicy(
                min_similarity=0.92,
                min_margin=0.03,
                top_k=5,
                configured_model="stub-model",
                evidence_authority=auth,
            )
            sem_svc = SemanticCacheService(
                embedding_provider=_StubEmbedder(),
                vector_store=shared_vstore,
                policy=policy,
                metrics_collector=collector,
                evidence_authority=auth,
            )
        core = IntelliCacheCore(
            cache=cache,
            single_flight=SingleFlightCoordinator(),
            dist_single_flight=dsf,
            cache_enabled=True,
            semantic_cache=sem_svc,
            metrics_collector=collector,
            semantic_serving_controller=ctrl,
            semantic_cache_startup_enabled=semantic_enabled,
        )
        return core, cache, dsf, ctrl, sem_svc

    async def test_migration_008_table_and_indexes_present(self) -> None:
        async with self.pool_w1.acquire() as conn:
            tbl = await conn.fetchval("SELECT to_regclass('public.distributed_singleflight_leases')")
            self.assertEqual(tbl, "distributed_singleflight_leases")
            indexes = await conn.fetch(
                "SELECT indexname FROM pg_indexes WHERE tablename = 'distributed_singleflight_leases'"
            )
            idx_names = {r["indexname"] for r in indexes}
            self.assertIn("idx_dsf_leases_expires_at", idx_names)
            self.assertIn("idx_dsf_leases_status_updated", idx_names)

    async def test_sf01_and_sf06_two_workers_same_request_one_provider_call_and_exact_handoff(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _c1, _d1, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _c2, _d2, _, _ = self._make_worker(self.pool_w2, collector=collector)

        started = asyncio.Event()
        release = asyncio.Event()
        prov1 = _ControlledProvider("W1", started_event=started, release_event=release)
        prov2 = _ControlledProvider("W2", started_event=started, release_event=release)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="What is CAP theorem?")],
            temperature=0.0,
            max_tokens=128,
            namespace="ns_sf01",
            visibility="tenant_shared",
        )

        # Start Worker 1 first so it deterministically acquires PostgreSQL leader lease and blocks at provider
        t1 = asyncio.create_task(core1.execute_query(req, prov1))
        await asyncio.wait_for(started.wait(), timeout=3.0)

        # Now start Worker 2 while Worker 1 is in-flight
        t2 = asyncio.create_task(core2.execute_query(req, prov2))
        await asyncio.sleep(0.15)  # allow Worker 2 to enter wait_for_outcome

        # Verify cache row not yet leaked and no plaintext stored in lease table
        async with self.pool_w1.acquire() as conn:
            row = await conn.fetchrow("SELECT status, coordination_key, cache_namespace FROM distributed_singleflight_leases")
            self.assertIsNotNone(row)
            self.assertEqual(row["status"], "pending")
            self.assertNotIn("CAP theorem", row["coordination_key"])

        release.set()
        res1, res2 = await asyncio.gather(t1, t2)

        self.assertEqual(len(prov1.calls) + len(prov2.calls), 1)
        self.assertEqual(len(prov1.calls), 1)
        self.assertEqual(len(prov2.calls), 0)
        self.assertFalse(res1.cached)
        self.assertTrue(res2.cached)
        self.assertEqual(res2.cache_type, "exact")
        self.assertEqual(res1.text, res2.text)

        snap = collector.get_namespace_snapshot("ns_sf01")
        self.assertEqual(snap.distributed_singleflight.leader_claims, 1)
        self.assertEqual(snap.distributed_singleflight.follower_joins, 1)
        self.assertEqual(snap.distributed_singleflight.coalesced_successes, 1)
        self.assertEqual(snap.distributed_singleflight.provider_calls_saved, 1)
        self.assertEqual(snap.request_and_cache.coalesced_joins, 0)  # distinguished from local joins

    async def test_sf02_and_sf17_ten_concurrent_requests_atomic_claim_one_generation(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _, dsf1, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _, dsf2, _, _ = self._make_worker(self.pool_w2, collector=collector)

        # SF-17 direct atomic lease test across 8 concurrent claimers
        claims = await asyncio.gather(
            *[
                (dsf1 if i % 2 == 0 else dsf2).try_acquire_or_inspect(
                    coordination_key="a" * 64,
                    namespace="ns_sf17",
                    visibility="tenant_shared",
                )
                for i in range(8)
            ]
        )
        leaders = [lease for lease, _ in claims if lease is not None]
        self.assertEqual(len(leaders), 1)
        self.assertEqual(leaders[0].fencing_token, 1)

        # SF-02 end-to-end 10 concurrent identical requests across 2 independent workers
        prov = _ControlledProvider("SHARED", delay_seconds=0.15)
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Explain ACID properties in databases")],
            temperature=0.0,
            max_tokens=128,
            namespace="ns_sf02",
            visibility="tenant_shared",
        )
        tasks = [
            (core1 if i % 2 == 0 else core2).execute_query(req, prov)
            for i in range(10)
        ]
        results = await asyncio.gather(*tasks)
        self.assertEqual(len(prov.calls), 1)
        texts = {r.text for r in results}
        self.assertEqual(len(texts), 1)
        self.assertEqual(sum(1 for r in results if not r.cached), 1)
        self.assertEqual(sum(1 for r in results if r.cached), 9)

    async def test_sf03_sf04_sf05_isolation_across_model_constraints_namespace_and_principal(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _, _, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _, _, _, _ = self._make_worker(self.pool_w2, collector=collector)

        started1 = asyncio.Event()
        release = asyncio.Event()
        prov1 = _ControlledProvider("P1", started_event=started1, release_event=release)
        prov2 = _ControlledProvider("P2", release_event=release)

        # Base request on Worker 1
        req_base = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content="Return user profile")],
            temperature=0.0,
            max_tokens=100,
            metadata={"output_format": "json", "language": "python"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="alice",
        )
        # SF-03: different model
        req_diff_model = GenerationRequest(
            model="qwen/qwen3.8-27b",
            provider="groq",
            messages=[ChatMessage(role="user", content="Return user profile")],
            temperature=0.0,
            max_tokens=100,
            metadata={"output_format": "json", "language": "python"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="alice",
        )
        # SF-04: different metadata constraint
        req_diff_fmt = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content="Return user profile")],
            temperature=0.0,
            max_tokens=100,
            metadata={"output_format": "yaml", "language": "python"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="alice",
        )
        # SF-05a: different namespace
        req_diff_ns = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content="Return user profile")],
            temperature=0.0,
            max_tokens=100,
            metadata={"output_format": "json", "language": "python"},
            namespace="ns_beta",
            visibility="private",
            principal_id="alice",
        )
        # SF-05b: different principal (same namespace)
        req_diff_user = GenerationRequest(
            model="llama-3.3-70b-versatile",
            provider="groq",
            messages=[ChatMessage(role="user", content="Return user profile")],
            temperature=0.0,
            max_tokens=100,
            metadata={"output_format": "json", "language": "python"},
            namespace="ns_alpha",
            visibility="private",
            principal_id="bob",
        )

        t_base = asyncio.create_task(core1.execute_query(req_base, prov1))
        await asyncio.wait_for(started1.wait(), timeout=3.0)

        t_others = [
            asyncio.create_task(core2.execute_query(req_diff_model, prov2)),
            asyncio.create_task(core2.execute_query(req_diff_fmt, prov2)),
            asyncio.create_task(core2.execute_query(req_diff_ns, prov2)),
            asyncio.create_task(core2.execute_query(req_diff_user, prov2)),
        ]
        # None of these should block waiting on req_base; wait briefly then release provider
        await asyncio.sleep(0.1)
        self.assertEqual(len(prov2.calls), 4)
        release.set()
        await asyncio.gather(t_base, *t_others)
        self.assertEqual(len(prov1.calls), 1)
        self.assertEqual(len(prov2.calls), 4)

    async def test_sf07_sf12_sf13_semantic_outcome_and_follower_independent_checkpoint2(self) -> None:
        collector = RuntimeMetricsCollector()
        shared_vstore = InMemoryVectorStore(dimensions=8, default_model="stub-model")
        ctrl_store_w1 = PostgresRuntimeControlStore(self.pool_w1)
        ctrl_store_w2 = PostgresRuntimeControlStore(self.pool_w2)
        await ctrl_store_w1.ensure_initialized(initial_enabled=True, updated_by="test")

        core1, _, _, ctrl1, _ = self._make_worker(
            self.pool_w1,
            collector=collector,
            shared_vstore=shared_vstore,
            semantic_enabled=True,
            control_store=ctrl_store_w1,
        )
        core2, _, _, ctrl2, _ = self._make_worker(
            self.pool_w2,
            collector=collector,
            shared_vstore=shared_vstore,
            semantic_enabled=True,
            control_store=ctrl_store_w2,
        )
        assert ctrl1 is not None and ctrl2 is not None

        # Seed semantic cache on Worker 1 for team_a and team_b
        prov_seed = _ControlledProvider("SEED")
        for ns in ("team_a", "team_b"):
            seed_req = GenerationRequest(
                model="stub-model",
                messages=[ChatMessage(role="user", content="How to reverse a python list?")],
                temperature=0.0,
                max_tokens=64,
                namespace=ns,
                visibility="tenant_shared",
            )
            await core1.execute_query(seed_req, prov_seed)
        self.assertEqual(len(prov_seed.calls), 2)

        # SF-07: Both workers get semantic hit on paraphrased query; leader signals outcome_type='semantic',
        # follower independently verifies & hits semantic cache without provider call.
        req_para_a = GenerationRequest(
            model="stub-model",
            messages=[ChatMessage(role="user", content="How can I invert a python list?")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_a",
            visibility="tenant_shared",
        )
        res_w1, res_w2 = await asyncio.gather(
            core1.execute_query(req_para_a, prov_seed),
            core2.execute_query(req_para_a, prov_seed),
        )
        self.assertTrue(res_w1.cached and res_w1.cache_type == "semantic")
        self.assertTrue(res_w2.cached)
        self.assertEqual(len(prov_seed.calls), 2)

        # SF-13: End-to-end Worker 2 in execute_query() waiting via wait_for_outcome() while
        # admin disables team_a serving right after Worker 1 publishes outcome_type='semantic'.
        async with self.pool_w1.acquire() as conn:
            await conn.execute("TRUNCATE TABLE cache_entries, distributed_singleflight_leases CASCADE")

        orig_complete = core1.dist_single_flight.complete_success
        orig_lookup = core1.semantic_cache.lookup
        w1_lookup_entered = asyncio.Event()
        w1_lookup_release = asyncio.Event()

        async def _delayed_w1_lookup(req_arg):
            w1_lookup_entered.set()
            await w1_lookup_release.wait()
            return await orig_lookup(req_arg)

        async def _disable_ns_after_publish(lease, *, outcome_type):
            # Disable namespace BEFORE marking the lease completed so that any follower
            # polling wait_for_outcome() observes the serving-disabled state at or before
            # the moment it sees status='completed'. Reversing this order creates a race:
            # the follower can exit wait_for_outcome() and pass Checkpoint 2 before the
            # namespace-disable DB write commits.
            await ctrl_store_w1.set_serving_enabled(False, namespace="team_a", updated_by="admin")
            ok = await orig_complete(lease, outcome_type=outcome_type)
            return ok

        prov_fallback_a = _ControlledProvider("FALLBACK_NS_A")
        req_para_a2 = GenerationRequest(
            model="stub-model",
            messages=[ChatMessage(role="user", content="Python list reversal technique")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_a",
            visibility="tenant_shared",
        )
        with (
            patch.object(core1.semantic_cache, "lookup", side_effect=_delayed_w1_lookup),
            patch.object(core1.dist_single_flight, "complete_success", side_effect=_disable_ns_after_publish),
        ):
            t_w1 = asyncio.create_task(core1.execute_query(req_para_a2, prov_fallback_a))
            await asyncio.wait_for(w1_lookup_entered.wait(), timeout=3.0)
            t_w2 = asyncio.create_task(core2.execute_query(req_para_a2, prov_fallback_a))
            await asyncio.sleep(0.1)  # Worker 2 is now waiting inside wait_for_outcome()
            w1_lookup_release.set()
            r1, r2 = await asyncio.gather(t_w1, t_w2)

        self.assertTrue(r1.cached and r1.cache_type == "semantic")
        self.assertFalse(r2.cached)
        self.assertEqual(r2.cache_type, "none")
        self.assertEqual(len(prov_fallback_a.calls), 1)

        # Meanwhile team_b override remains enabled -> SF-13 verifies namespace precision!
        self.assertTrue(await ctrl2.acheck_serving_enabled("team_b"))

        # SF-12: End-to-end global serving kill switch disabled while Worker 2 waits on team_b semantic hit
        w1_lookup_entered.clear()
        w1_lookup_release.clear()

        async def _disable_global_after_publish(lease, *, outcome_type):
            # Disable global serving BEFORE marking the lease completed for the same
            # reason as _disable_ns_after_publish: prevents the follower from reading
            # the lease as completed before the global-disable write is visible.
            await ctrl_store_w1.set_serving_enabled(False, namespace=None, updated_by="admin")
            ok = await orig_complete(lease, outcome_type=outcome_type)
            return ok

        prov_fallback_b = _ControlledProvider("FALLBACK_GLOBAL_B")
        req_para_b = GenerationRequest(
            model="stub-model",
            messages=[ChatMessage(role="user", content="Python list reversal technique")],
            temperature=0.0,
            max_tokens=64,
            namespace="team_b",
            visibility="tenant_shared",
        )
        with (
            patch.object(core1.semantic_cache, "lookup", side_effect=_delayed_w1_lookup),
            patch.object(core1.dist_single_flight, "complete_success", side_effect=_disable_global_after_publish),
        ):
            t_w1b = asyncio.create_task(core1.execute_query(req_para_b, prov_fallback_b))
            await asyncio.wait_for(w1_lookup_entered.wait(), timeout=3.0)
            t_w2b = asyncio.create_task(core2.execute_query(req_para_b, prov_fallback_b))
            await asyncio.sleep(0.1)
            w1_lookup_release.set()
            r1b, r2b = await asyncio.gather(t_w1b, t_w2b)

        self.assertTrue(r1b.cached and r1b.cache_type == "semantic")
        self.assertFalse(r2b.cached)
        self.assertEqual(len(prov_fallback_b.calls), 1)

    async def test_sf08_and_sf09_lease_expiration_reclaim_and_fencing_rejection(self) -> None:
        collector = RuntimeMetricsCollector()
        short_cfg = DistributedSingleFlightConfig(
            lease_seconds=0.35,
            renewal_interval_seconds=0.15,
            follower_wait_timeout_seconds=3.0,
            poll_interval_seconds=0.02,
            poll_max_interval_seconds=0.05,
        )
        core1, _, dsf1, _, _ = self._make_worker(self.pool_w1, collector=collector, config=short_cfg)
        core2, _, dsf2, _, _ = self._make_worker(self.pool_w2, collector=collector, config=short_cfg)

        # SF-08 & SF-09 at store layer: Worker 1 acquires lease, expires without renewal, Worker 2 reclaims with fencing=2,
        # stale Worker 1 attempts renew & complete_success -> fencing rejects both!
        lease1, _ = await dsf1.try_acquire_or_inspect(
            coordination_key="b" * 64,
            namespace="ns_sf08",
            visibility="tenant_shared",
        )
        self.assertIsNotNone(lease1)
        self.assertEqual(lease1.fencing_token, 1)

        await asyncio.sleep(0.45)  # exceed 0.35s lease without renewal (simulates crashed leader)

        lease2, snap2 = await dsf2.try_acquire_or_inspect(
            coordination_key="b" * 64,
            namespace="ns_sf08",
            visibility="tenant_shared",
        )
        self.assertIsNone(snap2)
        self.assertIsNotNone(lease2)
        self.assertTrue(lease2.reclaimed_expired)
        self.assertEqual(lease2.fencing_token, 2)

        # SF-09: Stale leader 1 tries renew and complete_success -> both rejected by fencing!
        self.assertIsNone(await dsf1.renew_lease(lease1))
        self.assertFalse(await dsf1.complete_success(lease1, outcome_type="exact"))
        self.assertTrue(await dsf2.complete_success(lease2, outcome_type="exact"))

        snap = collector.get_namespace_snapshot("ns_sf08")
        self.assertGreaterEqual(snap.distributed_singleflight.lease_expirations, 1)
        self.assertGreaterEqual(snap.distributed_singleflight.fencing_rejections, 2)

        # End-to-end SF-08: Simulate Worker 1 acquiring lease, crashing before provider call finishes,
        # Worker 2 waits, observes expiry, reclaims lease, calls provider, and succeeds!
        async with self.pool_w1.acquire() as conn:
            await conn.execute("TRUNCATE TABLE distributed_singleflight_leases, cache_entries CASCADE")

        dsf_no_renew = DistributedSingleFlightConfig(
            lease_seconds=0.35,
            renewal_interval_seconds=0.30,
            follower_wait_timeout_seconds=4.0,
            poll_interval_seconds=0.02,
            poll_max_interval_seconds=0.05,
        )
        core2_e2e, _, dsf2_e2e, _, _ = self._make_worker(self.pool_w2, collector=collector, config=dsf_no_renew)
        _ = core1
        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="What is MVCC?")],
            temperature=0.0,
            max_tokens=64,
            namespace="ns_sf08_e2e",
            visibility="tenant_shared",
        )
        # Seed dead Worker 1 pending lease directly matching exact coordination key
        from app.core.fingerprinting import generate_fingerprint, derive_variant_exact_fingerprint
        from app.semantic_cache.context import analyze_request_context, resolve_context_cache_scope

        fp = derive_variant_exact_fingerprint(
            generate_fingerprint(req.messages, req.model, req.temperature, req.max_tokens, req.provider),
            req.metadata,
        )
        ctx = analyze_request_context(req)
        dsf_key = compute_distributed_coordination_key(
            namespace="ns_sf08_e2e",
            visibility="tenant_shared",
            owner_principal_id=None,
            allow_shared_reads=False,
            fingerprint=fp,
            request=req,
            context_class=ctx.context_class.value,
            context_fingerprint=ctx.context_fingerprint,
        )
        dead_lease, _ = await dsf1.try_acquire_or_inspect(
            coordination_key=dsf_key,
            namespace="ns_sf08_e2e",
            visibility="tenant_shared",
        )
        self.assertIsNotNone(dead_lease)

        prov2 = _ControlledProvider("RECOVERED")
        res = await core2_e2e.execute_query(req, prov2)
        self.assertEqual(len(prov2.calls), 1)
        self.assertIn("RECOVERED-1", res.text)
        snap_e2e = collector.get_namespace_snapshot("ns_sf08_e2e")
        self.assertGreaterEqual(snap_e2e.distributed_singleflight.lease_expirations, 1)

    async def test_sf10_follower_wait_timeout_recovers_bounded_safely(self) -> None:
        collector = RuntimeMetricsCollector()
        long_leader_cfg = DistributedSingleFlightConfig(
            lease_seconds=20.0,
            renewal_interval_seconds=5.0,
            follower_wait_timeout_seconds=10.0,
            poll_interval_seconds=0.02,
        )
        short_follower_cfg = DistributedSingleFlightConfig(
            lease_seconds=20.0,
            renewal_interval_seconds=5.0,
            follower_wait_timeout_seconds=0.25,
            poll_interval_seconds=0.02,
            poll_max_interval_seconds=0.05,
        )
        core1, _, _, _, _ = self._make_worker(self.pool_w1, collector=collector, config=long_leader_cfg)
        core2, _, _, _, _ = self._make_worker(self.pool_w2, collector=collector, config=short_follower_cfg)

        started = asyncio.Event()
        release = asyncio.Event()
        prov1 = _ControlledProvider("SLOW_LEADER", started_event=started, release_event=release)
        prov2 = _ControlledProvider("FOLLOWER_FALLBACK")

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Explain Paxos")],
            temperature=0.0,
            max_tokens=64,
            namespace="ns_sf10",
            visibility="tenant_shared",
        )
        t1 = asyncio.create_task(core1.execute_query(req, prov1))
        await asyncio.wait_for(started.wait(), timeout=3.0)

        res2 = await core2.execute_query(req, prov2)
        self.assertEqual(len(prov2.calls), 1)
        self.assertIn("FOLLOWER_FALLBACK-1", res2.text)

        release.set()
        res1 = await t1
        self.assertIn("SLOW_LEADER-1", res1.text)

        snap = collector.get_namespace_snapshot("ns_sf10")
        self.assertGreaterEqual(snap.distributed_singleflight.follower_timeouts, 1)

    async def test_sf14_follower_cancelled_leaves_leader_unaffected(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _, _, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _, _, _, _ = self._make_worker(self.pool_w2, collector=collector)

        started = asyncio.Event()
        release = asyncio.Event()
        prov1 = _ControlledProvider("W1_LEADER", started_event=started, release_event=release)
        prov2 = _ControlledProvider("W2_FOLLOWER")

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Explain two-phase commit")],
            temperature=0.0,
            max_tokens=64,
            namespace="ns_sf14",
            visibility="tenant_shared",
        )
        t1 = asyncio.create_task(core1.execute_query(req, prov1))
        await asyncio.wait_for(started.wait(), timeout=3.0)

        t2 = asyncio.create_task(core2.execute_query(req, prov2))
        await asyncio.sleep(0.1)
        t2.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t2

        release.set()
        res1 = await t1
        self.assertIn("W1_LEADER-1", res1.text)
        self.assertEqual(len(prov1.calls), 1)
        self.assertEqual(len(prov2.calls), 0)
        snap = collector.get_namespace_snapshot("ns_sf14")
        self.assertGreaterEqual(snap.request_and_cache.request_cancellations, 1)

    async def test_sf15_leader_cancelled_releases_followers_for_recovery(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _, _, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _, _, _, _ = self._make_worker(self.pool_w2, collector=collector)

        started = asyncio.Event()
        never_release = asyncio.Event()
        prov1 = _ControlledProvider("W1_CANCELLED", started_event=started, release_event=never_release)
        prov2 = _ControlledProvider("W2_NEW_LEADER")

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="Explain B-tree splitting")],
            temperature=0.0,
            max_tokens=64,
            namespace="ns_sf15",
            visibility="tenant_shared",
        )
        t1 = asyncio.create_task(core1.execute_query(req, prov1))
        await asyncio.wait_for(started.wait(), timeout=3.0)

        t2 = asyncio.create_task(core2.execute_query(req, prov2))
        await asyncio.sleep(0.1)

        t1.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await t1

        res2 = await asyncio.wait_for(t2, timeout=3.0)
        self.assertIn("W2_NEW_LEADER-1", res2.text)
        self.assertEqual(len(prov2.calls), 1)
        snap = collector.get_namespace_snapshot("ns_sf15")
        self.assertGreaterEqual(snap.distributed_singleflight.leader_failures, 1)

    async def test_sf16_semantic_disabled_exact_and_distributed_singleflight_still_work(self) -> None:
        collector = RuntimeMetricsCollector()
        core1, _, _, _, _ = self._make_worker(self.pool_w1, collector=collector, semantic_enabled=False)
        core2, _, _, _, _ = self._make_worker(self.pool_w2, collector=collector, semantic_enabled=False)
        prov = _ControlledProvider("EXACT_ONLY", delay_seconds=0.1)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="What is WAL in PostgreSQL?")],
            temperature=0.0,
            max_tokens=64,
            namespace="ns_sf16",
            visibility="tenant_shared",
        )
        r1, r2 = await asyncio.gather(core1.execute_query(req, prov), core2.execute_query(req, prov))
        self.assertEqual(len(prov.calls), 1)
        self.assertEqual(sum(1 for r in (r1, r2) if not r.cached), 1)
        self.assertEqual(sum(1 for r in (r1, r2) if r.cached), 1)

        # Subsequent request hits exact cache cleanly
        r3 = await core2.execute_query(req, prov)
        self.assertTrue(r3.cached and r3.cache_type == "exact")
        self.assertEqual(len(prov.calls), 1)

    async def test_sf18_cleanup_expired_leases_never_deletes_active_pending_leases(self) -> None:
        collector = RuntimeMetricsCollector()
        _, _, dsf, _, _ = self._make_worker(self.pool_w1, collector=collector)

        active_lease, _ = await dsf.try_acquire_or_inspect(
            coordination_key="c" * 64,
            namespace="ns_sf18",
            visibility="tenant_shared",
        )
        self.assertIsNotNone(active_lease)

        # Insert old expired completed, failed, and expired rows directly
        async with self.pool_w1.acquire() as conn:
            past = datetime.now(timezone.utc) - timedelta(minutes=5)
            await conn.execute(
                """
                INSERT INTO distributed_singleflight_leases (
                    coordination_key, cache_namespace, visibility, owner_token, fencing_token,
                    status, outcome_type, failure_code, lease_expires_at, created_at, updated_at, completed_at
                ) VALUES
                ($1, 'ns_sf18', 'tenant_shared', 't1', 1, 'completed', 'exact', NULL, $4, $4, $4, $4),
                ($2, 'ns_sf18', 'tenant_shared', 't2', 1, 'failed', NULL, 'ERR', $4, $4, $4, $4),
                ($3, 'ns_sf18', 'tenant_shared', 't3', 1, 'pending', NULL, NULL, $4, $4, $4, NULL)
                """,
                "d" * 64,
                "e" * 64,
                "f" * 64,
                past,
            )

        deleted = await dsf.cleanup_expired_leases(batch_size=50, pending_grace_seconds=30.0)
        self.assertEqual(deleted, 3)

        # Verify active pending lease is STILL intact!
        async with self.pool_w1.acquire() as conn:
            remaining = await conn.fetch("SELECT coordination_key, status FROM distributed_singleflight_leases")
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["coordination_key"], "c" * 64)
            self.assertEqual(remaining[0]["status"], "pending")

    async def test_http_stats_and_metrics_expose_distributed_singleflight_counters(self) -> None:
        collector = get_metrics_collector()
        collector.reset()
        core1, cache1, dsf1, _, _ = self._make_worker(self.pool_w1, collector=collector)
        core2, _c2, _d2, _, _ = self._make_worker(self.pool_w2, collector=collector)
        prov = _ControlledProvider("HTTP_PROV", delay_seconds=0.1)

        req = GenerationRequest(
            model="llama-3.3-70b-versatile",
            messages=[ChatMessage(role="user", content="HTTP observability check")],
            temperature=0.0,
            max_tokens=64,
            namespace="default",
            visibility="tenant_shared",
        )
        await asyncio.gather(core1.execute_query(req, prov), core2.execute_query(req, prov))

        with patch.dict(
            os.environ,
            {
                "CACHE_BACKEND": "postgres",
                "DATABASE_URL": _TEST_DB_URL,
                "CLIENT_API_TOKEN": "client-tok-39",
                "ADMIN_API_TOKEN": "admin-tok-39",
                "DISTRIBUTED_SINGLEFLIGHT_ENABLED": "true",
                "SEMANTIC_CACHE_ENABLED": "false",
            },
            clear=False,
        ):
            reload_settings()
            app = create_app()
            set_query_cache(cache1)
            set_distributed_singleflight(dsf1)
            set_semantic_cache(None)
            app.dependency_overrides[get_llm_provider] = lambda: prov
            try:
                transport = httpx.ASGITransport(app=app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                    stats_res = await client.get("/cache/stats", headers={"X-Admin-Token": "admin-tok-39"})
                    self.assertEqual(stats_res.status_code, 200)
                    stats_body = stats_res.json()
                    self.assertIn("distributed_singleflight", stats_body)
                    dsf_stats = stats_body["distributed_singleflight"]
                    self.assertTrue(dsf_stats["enabled"])
                    self.assertEqual(dsf_stats["leader_claims"], 1)
                    self.assertEqual(dsf_stats["follower_joins"], 1)
                    self.assertEqual(dsf_stats["coalesced_successes"], 1)
                    self.assertEqual(dsf_stats["provider_calls_saved"], 1)
                    self.assertEqual(dsf_stats["local_coalesced_joins"], 0)

                    me_res = await client.get("/metrics/me", headers={"Authorization": "Bearer client-tok-39"})
                    self.assertEqual(me_res.status_code, 200)
                    me_body = me_res.json()
                    self.assertEqual(me_body["distributed_singleflight"]["leader_claims"], 1)
                    self.assertEqual(me_body["distributed_singleflight"]["follower_joins"], 1)
                    self.assertGreater(me_body["latencies"]["distributed_singleflight_database_latency_ms"]["sample_count"], 0)

                    adm_res = await client.get("/admin/metrics", headers={"X-Admin-Token": "admin-tok-39"})
                    self.assertEqual(adm_res.status_code, 200)
                    adm_body = adm_res.json()
                    self.assertEqual(adm_body["aggregate"]["distributed_singleflight"]["provider_calls_saved"], 1)
            finally:
                app.dependency_overrides.clear()
                set_distributed_singleflight(None)
                set_query_cache(None)


if __name__ == "__main__":
    unittest.main()
