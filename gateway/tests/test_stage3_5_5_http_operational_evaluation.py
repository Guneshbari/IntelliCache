"""
Stage 3.5.5 Operational Verification & Real HTTP End-to-End Evaluation Suite.

Covers two test layers cleanly:
1. ``TestStage355HttpLifecycleInMemory`` (always executes in unit and PG CI discovery):
   Verifies the complete FastAPI HTTP lifecycle (``POST /query``,
   ``GET/POST /admin/semantic-cache/serving``, ``GET /cache/semantic/stats``,
   ``GET /metrics/me``, ``GET /admin/metrics``) across Scenarios A-J, privacy
   isolation, SingleFlight semantic coalescing & ``coalesced_semantic_hits`` vs
   ``semantic_hits`` separation, 2x2 startup/runtime control matrix, mid-lookup
   and post-Checkpoint-2 toggles, and strict secret/token/principal non-disclosure.
2. ``TestStage355RealFastEmbedPostgresHttpEvaluation`` (executes when
   ``RUN_FASTEMBED_PGVECTOR_TEST=1`` or ``RUN_FASTEMBED_SMOKE=1`` with live
   384-d PostgreSQL + pgvector and real FastEmbed ``BAAI/bge-small-en-v1.5``):
   Runs the complete unmocked FastEmbed + PostgresCache + PostgresVectorStore
   HTTP lifecycle across Scenarios A-J, ``semantic_variant_records`` SQL checks,
   legacy ``visibility='unknown'`` DB blocking, tampered HMAC rejection, HTTP
   SingleFlight coalescing, and >=20-sample latency & token-savings evaluation.
"""

from __future__ import annotations

import asyncio
import json
import os
from typing import Any, Dict, List
from unittest.mock import patch

import asyncpg
import httpx
import unittest

from app.api.routes import (
    get_llm_provider,
    get_query_cache,
    get_semantic_cache,
    get_vector_store,
    set_query_cache,
    set_semantic_cache,
    set_vector_store,
)
from app.config import reload_settings
from app.core.contracts import GenerationRequest
from app.main import create_app
from app.metrics import get_metrics_collector
from app.semantic_cache.control import reset_semantic_serving_controller
from scripts.evaluate_stage3_5_5_operational import (
    ControlledEvalProvider,
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    TopicDeterministicEmbedder,
    build_response_map_from_dataset,
    load_stage355_dataset,
    resolve_fastembed_database_url,
    run_operational_evaluation,
)


class TestStage355HttpLifecycleInMemory(unittest.IsolatedAsyncioTestCase):
    """Deterministic HTTP lifecycle, control-plane, privacy, and SingleFlight suite."""

    async def asyncSetUp(self) -> None:
        self._saved_cache = get_query_cache()
        self._saved_sem = get_semantic_cache()
        self._saved_vs = get_vector_store()
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)
        self.dataset = load_stage355_dataset()

    async def asyncTearDown(self) -> None:
        set_query_cache(self._saved_cache)
        set_semantic_cache(self._saved_sem)
        set_vector_store(self._saved_vs)
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()
        reload_settings()

    async def test_full_http_operational_scenarios_in_memory(self) -> None:
        """Runs all Stage 3.5.5 operational HTTP scenarios cleanly without external services."""
        result = await run_operational_evaluation(
            dataset=self.dataset,
            use_postgres=False,
            use_real_fastembed=False,
            benchmark_iterations=10,
            simulated_upstream_delay_ms=15.0,
        )
        self.assertTrue(result["all_scenarios_passed"], result)
        scenario_ids = {s["scenario_id"] for s in result["scenarios"]}
        self.assertEqual(
            scenario_ids,
            {
                "scenario_a_b_exact_and_semantic_reuse",
                "scenario_c_incompatible_and_ambiguous_fallback",
                "scenario_d_runtime_kill_switch_http",
                "scenario_e_f_g_privacy_isolation_and_db_safety",
                "scenario_h_http_singleflight_semantic_coalescing",
                "scenario_i_j_degraded_dependency_fallback",
            },
        )
        bench = result["benchmark"]
        self.assertEqual(bench["iterations_per_mode"], 10)
        self.assertGreater(bench["metrics_snapshot_semantic_phase"]["total_tokens_saved"], 0)
        self.assertEqual(bench["metrics_snapshot_semantic_phase"]["semantic_hits"], 10)

    async def test_startup_runtime_control_matrix_over_http(self) -> None:
        """Verifies all four (startup, runtime) combinations serve only when both are True."""
        cases = [
            (False, False, False),
            (False, True, False),
            (True, False, False),
            (True, True, True),
        ]
        response_map = build_response_map_from_dataset(self.dataset)
        for startup_on, runtime_on, expected_hit in cases:
            with self.subTest(startup=startup_on, runtime=runtime_on):
                provider = ControlledEvalProvider(response_map=response_map)
                embedder = TopicDeterministicEmbedder()
                env = {
                    "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
                    "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
                    "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
                    "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
                    "SEMANTIC_CACHE_ENABLED": str(startup_on).lower(),
                    "SEMANTIC_CACHE_SERVING_ENABLED": str(runtime_on).lower(),
                    "CACHE_BACKEND": "memory",
                    "VECTOR_STORE_BACKEND": "memory",
                    "EMBEDDING_PROVIDER": "fastembed",
                    "EMBEDDING_MODEL": FASTEMBED_MODEL,
                    "VECTOR_DIMENSIONS": str(FASTEMBED_DIM),
                    "LLM_PROVIDER": "mock",
                }
                get_metrics_collector().reset()
                reset_semantic_serving_controller(False)
                with patch.dict(os.environ, env, clear=False):
                    reload_settings()
                    with patch("app.main.EmbeddingProviderFactory.create", return_value=embedder):
                        app = create_app()
                        app.dependency_overrides[get_llm_provider] = lambda: provider
                        async with app.router.lifespan_context(app):
                            async with httpx.AsyncClient(
                                transport=httpx.ASGITransport(app=app),
                                base_url="http://testserver",
                            ) as client:
                                s_resp = await client.post(
                                    "/query",
                                    headers={"X-Client-Token": "tok_alice"},
                                    json={
                                        "model": "mock-qwen-27b",
                                        "messages": [{"role": "user", "content": "How do I reverse a string in Python?"}],
                                        "temperature": 0.0,
                                    },
                                )
                                self.assertEqual(s_resp.status_code, 200)
                                p_resp = await client.post(
                                    "/query",
                                    headers={"X-Client-Token": "tok_alice"},
                                    json={
                                        "model": "mock-qwen-27b",
                                        "messages": [
                                            {"role": "user", "content": "What is the Python syntax to reverse a string?"}
                                        ],
                                        "temperature": 0.0,
                                    },
                                )
                                self.assertEqual(p_resp.status_code, 200)
                                pj = p_resp.json()
                                self.assertEqual(pj["cached"], expected_hit)
                                self.assertEqual(pj["cache_type"], "semantic" if expected_hit else "none")
                                st = (
                                    await client.get("/cache/semantic/stats", headers={"X-Client-Token": "tok_alice"})
                                ).json()
                                self.assertEqual(st["enabled"], startup_on)
                                self.assertEqual(st["runtime_serving_enabled"], runtime_on)
                                self.assertEqual(st["effective_enabled"], expected_hit)


class TestStage355RealFastEmbedPostgresHttpEvaluation(unittest.IsolatedAsyncioTestCase):
    """
    Unmocked FastEmbed (BAAI/bge-small-en-v1.5, 384-d) + live PostgreSQL/pgvector
    HTTP request-path operational verification and latency/savings benchmark.
    """

    async def asyncSetUp(self) -> None:
        self._saved_cache = get_query_cache()
        self._saved_sem = get_semantic_cache()
        self._saved_vs = get_vector_store()
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)

        strict = os.environ.get("REQUIRE_FASTEMBED_PGVECTOR_TEST") == "1"
        enabled = (
            os.environ.get("RUN_FASTEMBED_PGVECTOR_TEST") == "1"
            or os.environ.get("RUN_FASTEMBED_SMOKE") == "1"
            or strict
        )
        if not enabled:
            self.skipTest("Opt-in test: set RUN_FASTEMBED_PGVECTOR_TEST=1 to run Stage 3.5.5 FastEmbed + Postgres HTTP suite.")

        try:
            self.db_url = await resolve_fastembed_database_url()
        except Exception as exc:
            if strict:
                self.fail(f"Required PostgreSQL setup failed: {exc}")
            self.skipTest(f"PostgreSQL unavailable: {exc}")

    async def asyncTearDown(self) -> None:
        set_query_cache(self._saved_cache)
        set_semantic_cache(self._saved_sem)
        set_vector_store(self._saved_vs)
        reset_semantic_serving_controller(False)
        get_metrics_collector().reset()
        reload_settings()

    async def test_real_fastembed_postgres_full_http_operational_evaluation(self) -> None:
        """
        Executes complete Scenarios A-J, privacy matrix, PostgreSQL legacy/tamper DB checks,
        HTTP SingleFlight semantic coalescing, and 20-sample latency/savings evaluation
        against real FastEmbed + real PostgreSQL 16 + pgvector over FastAPI HTTP routes.
        """
        result = await run_operational_evaluation(
            use_postgres=True,
            db_url=self.db_url,
            use_real_fastembed=True,
            benchmark_iterations=20,
            simulated_upstream_delay_ms=120.0,
        )
        self.assertTrue(result["all_scenarios_passed"], result)
        env = result["environment"]
        self.assertEqual(env["backend_cache"], "postgres")
        self.assertEqual(env["backend_vector"], "postgres")
        self.assertEqual(env["embedding_mode"], "real_fastembed")
        self.assertEqual(env["embedding_model"], FASTEMBED_MODEL)
        self.assertEqual(env["vector_dimensions"], FASTEMBED_DIM)
        self.assertEqual(env["min_similarity"], 0.92)
        self.assertEqual(env["min_margin"], 0.03)

        bench = result["benchmark"]
        self.assertEqual(bench["iterations_per_mode"], 20)
        self.assertEqual(bench["metrics_snapshot_semantic_phase"]["semantic_hits"], 20)
        self.assertEqual(bench["metrics_snapshot_semantic_phase"]["provider_calls_attempted"], 1)
        self.assertEqual(bench["metrics_snapshot_semantic_phase"]["provider_calls_avoided"], 20)
        self.assertGreater(bench["metrics_snapshot_semantic_phase"]["total_tokens_saved"], 0)
        self.assertLess(
            bench["semantic_hit"]["p50_ms"],
            bench["baseline_with_simulated_upstream"]["p50_ms"],
        )

    async def test_postgres_variant_coexistence_and_sql_privacy_rows(self) -> None:
        """
        Verifies PostgreSQL semantic_variant_records persists scoped variants with
        distinct visibility/owner_principal_id columns and serves them over HTTP POST /query.
        """
        ds = load_stage355_dataset()
        response_map = build_response_map_from_dataset(ds)
        provider = ControlledEvalProvider(response_map=response_map)
        env = {
            "CLIENT_API_TOKENS": json.dumps(DEFAULT_CLIENT_TOKENS),
            "ADMIN_API_TOKEN": STAGE_3_5_5_ADMIN_TOKEN,
            "PRINCIPAL_IDENTITY_SECRET": STAGE_3_5_5_IDENTITY_SECRET,
            "SEMANTIC_CACHE_EVIDENCE_SECRET": STAGE_3_5_5_EVIDENCE_SECRET,
            "SEMANTIC_CACHE_ENABLED": "true",
            "SEMANTIC_CACHE_SERVING_ENABLED": "true",
            "CACHE_BACKEND": "postgres",
            "VECTOR_STORE_BACKEND": "postgres",
            "DATABASE_URL": self.db_url,
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": FASTEMBED_MODEL,
            "VECTOR_DIMENSIONS": str(FASTEMBED_DIM),
            "LLM_PROVIDER": "mock",
        }
        get_metrics_collector().reset()
        reset_semantic_serving_controller(False)
        with patch.dict(os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: provider
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    await client.post("/cache/clear?namespace=team_alpha", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN})
                    await client.post("/cache/clear?namespace=team_beta", headers={"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN})
                    # Seed JSON id/name + Markdown format variants over HTTP
                    r1 = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_alice"},
                        json={
                            "model": "mock-qwen-27b",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Write a Python helper to serialize a dict and output JSON with required keys: id, name",
                                }
                            ],
                            "temperature": 0.0,
                        },
                    )
                    r2 = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_alice"},
                        json={
                            "model": "mock-qwen-27b",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Write a Python helper to serialize a dict and output JSON with required keys: id, email",
                                }
                            ],
                            "temperature": 0.0,
                        },
                    )
                    self.assertEqual(r1.status_code, 200)
                    self.assertEqual(r2.status_code, 200)
                    self.assertFalse(r1.json()["cached"])
                    self.assertFalse(r2.json()["cached"])

                    # Verify PostgreSQL semantic_variant_records & scope columns directly
                    conn = await asyncpg.connect(self.db_url)
                    try:
                        rows = await conn.fetch(
                            """
                            SELECT cache_namespace, visibility, owner_principal_id
                            FROM semantic_variant_records
                            WHERE cache_namespace = 'team_alpha'
                            """
                        )
                        self.assertGreaterEqual(len(rows), 2)
                        for row in rows:
                            self.assertEqual(row["visibility"], "private")
                            self.assertTrue(row["owner_principal_id"])
                            self.assertNotIn("tok_alice", row["owner_principal_id"])
                    finally:
                        await conn.close()

                    # Paraphrase with id, name hits semantically over HTTP and returns id+name JSON
                    hit_name = await client.post(
                        "/query",
                        headers={"X-Client-Token": "tok_alice"},
                        json={
                            "model": "mock-qwen-27b",
                            "messages": [
                                {
                                    "role": "user",
                                    "content": "Create a Python helper to serialize a dictionary and output JSON with required keys: id, name",
                                }
                            ],
                            "temperature": 0.0,
                        },
                    )
                    self.assertEqual(hit_name.status_code, 200)
                    hj = hit_name.json()
                    self.assertTrue(hj["cached"], hj)
                    self.assertEqual(hj["cache_type"], "semantic")
                    self.assertIn('"name"', hj["response"])


if __name__ == "__main__":
    unittest.main()
