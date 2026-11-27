"""
Stage 3.7.1 Regression & HTTP End-to-End Closure Tests:
1. Unit contracts for shared query construction, specialization allowlist, and planner option validation.
2. Live PostgreSQL + pgvector deterministic tests (`TEST_DATABASE_URL` / localhost):
   - Generic-plan regression reproduction (unspecialized) vs specialized `CONTEXT_INDEPENDENT` HNSW plan.
   - `CONTEXT_DEPENDENT` parameterized pre-LIMIT filtering & rank-31 wrong-context exclusion.
   - `private` + `allow_shared_reads=True` specialization & cross-tenant / private isolation under generic plan.
   - Skewed HNSW low-recall trap vs exact-scan reference vs `ef_search` recovery + full compatibility validation.
3. Live HTTP + PostgreSQL + pgvector + Real FastEmbed end-to-end verification (`GEN-001`..`GEN-010`)
   plus Phase 6 `/admin/metrics` & `/cache/semantic/stats` observability & non-disclosure checks.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sys
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest.mock import patch
from urllib.parse import urlsplit

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

import asyncpg
import httpx

from app.api.routes import get_llm_provider
from app.cache.migration import run_migrations
from app.config import Settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest
from app.main import create_app
from app.privacy import derive_scoped_exact_key
from app.semantic_cache.compatibility import CompatibilityExtractor
from app.semantic_cache.contracts import SemanticCompatibilityMetadata
from app.semantic_cache.control import reset_semantic_serving_controller
from app.semantic_cache.evidence import EvidenceAuthority
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import VectorSearchResult
from app.vector_store.errors import VectorInvalidError, VectorStoreError
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore
from scripts.evaluate_stage3_5_5_operational import (
    ControlledEvalProvider,
    DEFAULT_CLIENT_TOKENS,
    FASTEMBED_DIM,
    FASTEMBED_MODEL,
    STAGE_3_5_5_ADMIN_TOKEN,
    STAGE_3_5_5_EVIDENCE_SECRET,
    STAGE_3_5_5_IDENTITY_SECRET,
    build_forbidden_disclosure_values,
    reset_pg_tables,
    resolve_fastembed_database_url,
    verify_no_sensitive_leaks,
)
from scripts.evaluate_stage3_7_workload import build_stage37_env
from scripts.evaluate_stage3_7_1_closure import (
    detect_pg_vector_dimensions,
    format_vec_literal,
    seed_mixed_context_corpus,
    seed_skewed_hnsw_fixture,
)


def _redact_url(db_url: str) -> str:
    parsed = urlsplit(db_url)
    if parsed.password:
        return db_url.replace(f":{parsed.password}@", ":***@")
    return db_url


class TestStage371UnitContracts(unittest.TestCase):
    """Fast unit checks for specialization security & `explain_search` input validation."""

    def test_safe_application_defaults_unchanged(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            cfg = Settings()
        self.assertFalse(cfg.semantic_cache_enabled)
        self.assertFalse(cfg.semantic_cache_serving_enabled)
        self.assertEqual(cfg.semantic_cache_default_visibility, "private")
        self.assertEqual(cfg.semantic_cache_min_similarity, 0.92)
        self.assertEqual(cfg.semantic_cache_min_margin, 0.03)
        self.assertEqual(cfg.semantic_cache_top_k, 5)
        self.assertEqual(cfg.semantic_cache_max_retrieval_candidates, 25)
        self.assertFalse(hasattr(cfg, "redis_url"))

    def test_specialization_never_inlines_untrusted_or_dependent_inputs(self) -> None:
        vs = PostgresVectorStore(pool=None, dimensions=384)
        vec_lit = "[" + ",".join(["0.0"] * 384) + "]"

        # Trusted CONTEXT_INDEPENDENT with canonical fingerprint specializes cleanly
        _, cq_ind, p_ind, fail, spec_ind = vs._prepare_search_queries(
            vec_str=vec_lit,
            namespace="ns",
            target_model="m",
            top_k=25,
            visibility="tenant_shared",
            context_class=" context_independent ",
            context_fingerprint=" ctx-v1:independent ",
        )
        self.assertFalse(fail)
        self.assertTrue(spec_ind)
        self.assertEqual(len(p_ind), 4)
        self.assertIn("->>'context_class') = 'CONTEXT_INDEPENDENT'", cq_ind)
        self.assertIn("->>'context_fingerprint') = 'ctx-v1:independent'", cq_ind)

        # Untrusted or dependent inputs must NEVER trigger literal specialization
        malicious_inputs = [
            ("CONTEXT_DEPENDENT", "ctx-v1:abc123"),
            ("CONTEXT_UNCERTAIN", "ctx-v1:uncertain"),
            ("CONTEXT_INDEPENDENT", "ctx-v1:independent' OR '1'='1"),
            ("CONTEXT_INDEPENDENT'; DROP TABLE vector_records;--", "ctx-v1:independent"),
        ]
        for cls_val, fp_val in malicious_inputs:
            _, cq_bad, p_bad, fail_bad, spec_bad = vs._prepare_search_queries(
                vec_str=vec_lit,
                namespace="ns",
                target_model="m",
                top_k=25,
                visibility="tenant_shared",
                context_class=cls_val,
                context_fingerprint=fp_val,
            )
            self.assertFalse(fail_bad)
            self.assertFalse(spec_bad, f"Must not specialize {cls_val!r}/{fp_val!r}")
            self.assertEqual(len(p_bad), 6)
            self.assertEqual(p_bad[-1], fp_val.strip())
            self.assertNotIn("DROP TABLE", cq_bad or "")
            self.assertNotIn("OR '1'='1", cq_bad or "")

    def test_explain_search_rejects_non_allowlisted_planner_options(self) -> None:
        import asyncio

        vs = PostgresVectorStore(pool=None, dimensions=384)
        vec = [0.0] * 384

        async def _run() -> None:
            with self.assertRaises(VectorInvalidError):
                await vs.explain_search(vec, plan_cache_mode="force_generic_plan; DROP TABLE cache_entries")
            with self.assertRaises(VectorInvalidError):
                await vs.explain_search(vec, ef_search=0)
            with self.assertRaises(VectorInvalidError):
                await vs.explain_search(vec, ef_search=True)  # type: ignore[arg-type]

        asyncio.run(_run())


class TestStage371PostgresPlannerAndHNSW(unittest.IsolatedAsyncioTestCase):
    """
    Live PostgreSQL + pgvector regression suite verifying generic-plan specialization,
    pre-LIMIT context prefiltering, tenant/principal isolation, and skewed HNSW low-recall behavior.
    Runs automatically in both 768-d `gateway-postgres-integration` and 384-d `gateway-fastembed-real-inference`.
    """

    async def asyncSetUp(self) -> None:
        explicit_url = os.getenv("TEST_DATABASE_URL")
        self.is_explicit_url = explicit_url is not None
        self.db_url = explicit_url or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test"
        safe_url = _redact_url(self.db_url)
        parsed = urlsplit(self.db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.fail(f"Refusing to run destructive PG tests against non-test DB {safe_url}")

        self.pool: Optional[asyncpg.Pool] = None
        self.vec_dim = 384 if os.getenv("RUN_FASTEMBED_PGVECTOR_TEST") == "1" else 768
        try:
            probe_conn = await asyncpg.connect(self.db_url)
            try:
                self.vec_dim = await detect_pg_vector_dimensions(probe_conn, default_dim=self.vec_dim)
            finally:
                await probe_conn.close()
            self.pool = await asyncpg.create_pool(dsn=self.db_url, min_size=2, max_size=6)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=self.vec_dim)
            await reset_pg_tables(self.db_url)
        except Exception as exc:
            if self.pool is not None:
                await self.pool.close()
                self.pool = None
            msg = f"PostgreSQL/pgvector unavailable at {safe_url}: {exc}"
            if self.is_explicit_url:
                self.fail(msg)
            self.skipTest(msg)

        self.authority = EvidenceAuthority(STAGE_3_5_5_EVIDENCE_SECRET, allow_ephemeral=False)

    async def asyncTearDown(self) -> None:
        if self.pool is not None:
            await self.pool.close()

    async def _run_unspecialized_prepared_explain(
        self,
        sql: str,
        params: List[Any],
        *,
        plan_cache_mode: str,
    ) -> Tuple[List[str], int]:
        conn = await asyncpg.connect(self.db_url, statement_cache_size=0)
        try:
            async with conn.transaction(readonly=True):
                await conn.execute(f"SET LOCAL plan_cache_mode = '{plan_cache_mode}';")
                types_str = ", ".join(["text", "text", "text", "integer"] + ["text"] * max(0, len(params) - 4))
                args_str = ", ".join(PostgresVectorStore._format_execute_literal(p) for p in params)
                await conn.execute(f"PREPARE _ic_unspec_dbg ({types_str}) AS {sql}")
                rows = await conn.fetch(f"EXECUTE _ic_unspec_dbg({args_str})")
                plan_rows = await conn.fetch(f"EXPLAIN (ANALYZE, BUFFERS) EXECUTE _ic_unspec_dbg({args_str})")
                await conn.execute("DEALLOCATE _ic_unspec_dbg;")
            return [r[0] for r in plan_rows], len(rows)
        finally:
            await conn.close()

    async def test_generic_plan_regression_reproduction_and_specialization(self) -> None:
        assert self.pool is not None
        ns = "s371_pg_gp"
        model_id = "s371-test-embed-v1"
        seeded = await seed_mixed_context_corpus(
            self.pool,
            dim=self.vec_dim,
            namespace=ns,
            embed_model=model_id,
            authority=self.authority,
            total_rows=4500,
        )
        vs = PostgresVectorStore(
            pool=self.pool,
            default_namespace=ns,
            default_model=model_id,
            dimensions=self.vec_dim,
        )

        # 1. Reproduce regression on UNSPECIALIZED parameterized SQL under force_generic_plan:
        #    Bound `$5/$6` forces planner away from HNSW onto context B-tree scan + full sort.
        sq_u, cq_u, sp_u, fail_u = vs._build_search_queries(
            visibility="tenant_shared",
            principal_id=None,
            allow_shared_reads=False,
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
        )
        self.assertFalse(fail_u)
        unspec_sql = cq_u or sq_u
        unspec_params = [format_vec_literal(seeded["indep_prompt_vec"]), ns, model_id, 25, *sp_u]
        self.assertEqual(len(unspec_params), 6)
        unspec_plan_lines, unspec_cnt = await self._run_unspecialized_prepared_explain(
            unspec_sql, unspec_params, plan_cache_mode="force_generic_plan"
        )
        unspec_text = " ".join(unspec_plan_lines).lower()
        self.assertEqual(unspec_cnt, 25)
        self.assertNotIn("idx_semantic_variant_records_hnsw", unspec_text)

        # 2. Verify production PostgresVectorStore `explain_search` + `search` specialization:
        #    BOTH `force_generic_plan` and `force_custom_plan` use HNSW on CONTEXT_INDEPENDENT.
        gen_plan = await vs.explain_search(
            seeded["indep_prompt_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            analyze=True,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
            plan_cache_mode="force_generic_plan",
        )
        cust_plan = await vs.explain_search(
            seeded["indep_prompt_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            analyze=True,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
            plan_cache_mode="force_custom_plan",
        )
        self.assertTrue(gen_plan["sql_specialized"])
        self.assertTrue(cust_plan["sql_specialized"])
        self.assertTrue(
            gen_plan["vector_index_used"],
            f"Expected HNSW under force_generic_plan, got: {gen_plan['execution_plan_summary']}",
        )
        self.assertTrue(
            cust_plan["vector_index_used"],
            f"Expected HNSW under force_custom_plan, got: {cust_plan['execution_plan_summary']}",
        )

        # Verify shared query builder also covers `private` + `allow_shared_reads=True` under force_generic_plan
        priv_shared_plan = await vs.explain_search(
            seeded["indep_prompt_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            analyze=True,
            visibility="private",
            principal_id="prin_alice",
            allow_shared_reads=True,
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
            plan_cache_mode="force_generic_plan",
        )
        self.assertTrue(priv_shared_plan["sql_specialized"])
        self.assertTrue(priv_shared_plan["vector_index_used"])

        # 3. Execute actual `search()` and verify retrieved candidate undergoes full compatibility check
        results = await vs.search(
            seeded["indep_prompt_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
        )
        fps = [r.fingerprint for r in results]
        self.assertIn(seeded["indep_target_fp"], fps)
        self.assertNotIn(seeded["private_other_fp"], fps)
        self.assertNotIn(seeded["cross_tenant_fp"], fps)

        # Compatibility validation must STILL reject if request contract differs
        mismatch_req = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content="Write a Rust helper to reverse a string")],
            temperature=0.0,
            namespace=ns,
            visibility="tenant_shared",
        )
        ext = CompatibilityExtractor()
        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model=model_id,
            extractor=ext,
            retrieval_budget=25,
            evidence_authority=self.authority,
        )
        rejected, reason = policy.evaluate_candidates(candidates=results, request=mismatch_req)
        self.assertIsNone(rejected)
        self.assertIsNotNone(reason)

        # 4. CONTEXT_DEPENDENT stays parameterized ($5/$6), uses context index before LIMIT, blocks rank-31 distractors
        dep_plan = await vs.explain_search(
            seeded["dep_query_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            analyze=True,
            visibility="tenant_shared",
            context_class="CONTEXT_DEPENDENT",
            context_fingerprint=seeded["dep_ctx_fp"],
            plan_cache_mode="force_generic_plan",
        )
        self.assertFalse(dep_plan["sql_specialized"])
        self.assertEqual(dep_plan["parameters_count"], 6)
        self.assertTrue(dep_plan["context_index_used"])
        dep_results = await vs.search(
            seeded["dep_query_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            visibility="tenant_shared",
            context_class="CONTEXT_DEPENDENT",
            context_fingerprint=seeded["dep_ctx_fp"],
        )
        self.assertEqual([r.fingerprint for r in dep_results], [seeded["dep_target_fp"]])

    async def test_hnsw_low_recall_fixture_vs_exact_and_recovery_validation(self) -> None:
        assert self.pool is not None
        ns = "s371_pg_hnsw"
        model_id = "s371-test-embed-v1"
        sing_prompt = "How do I sort a list cleanly in Python using sorted()?"
        fixture = await seed_skewed_hnsw_fixture(
            self.pool,
            dim=self.vec_dim,
            namespace=ns,
            embed_model=model_id,
            authority=self.authority,
            singleton_prompt=sing_prompt,
        )
        vs = PostgresVectorStore(
            pool=self.pool,
            default_namespace=ns,
            default_model=model_id,
            dimensions=self.vec_dim,
        )
        ext = CompatibilityExtractor()
        policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model=model_id,
            extractor=ext,
            retrieval_budget=25,
            evidence_authority=self.authority,
        )
        req_python = GenerationRequest(
            model="mock-qwen-27b",
            provider="mock",
            messages=[ChatMessage(role="user", content=sing_prompt)],
            temperature=0.0,
            namespace=ns,
            visibility="tenant_shared",
        )

        # 1. Exact-scan reference establishes true rank-1 closest match (`hnsw_singleton`)
        exec_single, exec_combined, params, fail_closed, _ = vs._prepare_search_queries(
            vec_str=format_vec_literal(fixture["query_vec"]),
            namespace=ns,
            target_model=model_id,
            top_k=25,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
        )
        self.assertFalse(fail_closed)
        conn = await asyncpg.connect(self.db_url, statement_cache_size=0)
        try:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;")
                exact_rows = await conn.fetch(exec_combined or exec_single, *params)
            self.assertGreater(len(exact_rows), 0)
            self.assertEqual(exact_rows[0]["fingerprint"], fixture["singleton_fp"])
            self.assertLess(float(exact_rows[0]["distance"]), 0.02)
        finally:
            await conn.close()

        # 2. Controlled low-recall HNSW (`ef_search=40`) uses HNSW index and misses `hnsw_singleton`
        low_ef_plan = await vs.explain_search(
            fixture["query_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            analyze=True,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
            ef_search=40,
        )
        self.assertTrue(low_ef_plan["vector_index_used"])

        low_ef_results = await vs.search(
            fixture["query_vec"],
            top_k=25,
            namespace=ns,
            model=model_id,
            visibility="tenant_shared",
            context_class="CONTEXT_INDEPENDENT",
            context_fingerprint="ctx-v1:independent",
        )
        self.assertEqual(len(low_ef_results), 25)
        self.assertNotIn(fixture["singleton_fp"], [r.fingerprint for r in low_ef_results])
        # All 25 incompatible Rust distractors must be rejected cleanly (no inferred compatibility)
        selected_low, reason_low = policy.evaluate_candidates(candidates=low_ef_results, request=req_python)
        self.assertIsNone(selected_low)
        self.assertIn("ALL_CANDIDATES_REJECTED", reason_low or "")

        # 3. Elevated `ef_search=400` recovers `hnsw_singleton`, which STILL undergoes full compatibility & HMAC checks
        high_pool = await asyncpg.create_pool(
            dsn=self.db_url,
            min_size=1,
            max_size=2,
            setup=lambda c: c.execute("SET hnsw.ef_search = 400;"),
        )
        try:
            vs_high = PostgresVectorStore(
                pool=high_pool,
                default_namespace=ns,
                default_model=model_id,
                dimensions=self.vec_dim,
            )
            high_ef_plan = await vs_high.explain_search(
                fixture["query_vec"],
                top_k=25,
                namespace=ns,
                model=model_id,
                analyze=True,
                visibility="tenant_shared",
                context_class="CONTEXT_INDEPENDENT",
                context_fingerprint="ctx-v1:independent",
                ef_search=400,
            )
            self.assertGreater(len(high_ef_plan["execution_plan_summary"]), 0)

            recovered = await vs_high.search(
                fixture["query_vec"],
                top_k=25,
                namespace=ns,
                model=model_id,
                visibility="tenant_shared",
                context_class="CONTEXT_INDEPENDENT",
                context_fingerprint="ctx-v1:independent",
            )
            self.assertIn(fixture["singleton_fp"], [r.fingerprint for r in recovered])

            # Valid recovered candidate is accepted
            hit_high, _ = policy.evaluate_candidates(candidates=recovered, request=req_python)
            self.assertIsNotNone(hit_high)
            self.assertEqual(hit_high.fingerprint, fixture["singleton_fp"])

            # Recovered candidate with incompatible requirement MUST STILL BE REJECTED
            req_go = GenerationRequest(
                model="mock-qwen-27b",
                provider="mock",
                messages=[ChatMessage(role="user", content="How do I sort a list in Go?")],
                temperature=0.0,
                namespace=ns,
                visibility="tenant_shared",
            )
            rej_high, _ = policy.evaluate_candidates(candidates=recovered, request=req_go)
            self.assertIsNone(rej_high)
        finally:
            await high_pool.close()


@unittest.skipUnless(
    os.getenv("RUN_FASTEMBED_PGVECTOR_TEST") == "1",
    "Set RUN_FASTEMBED_PGVECTOR_TEST=1 to run Stage 3.7.1 HTTP + FastEmbed + PostgreSQL closure tests.",
)
class TestStage371HttpEndToEndClosure(unittest.IsolatedAsyncioTestCase):
    """
    Live FastAPI HTTP + PostgreSQL + pgvector + Real FastEmbed end-to-end verification
    covering scenarios `GEN-001` through `GEN-010`, observability, and non-disclosure.
    """

    async def asyncSetUp(self) -> None:
        self.db_url = await resolve_fastembed_database_url()
        reset_semantic_serving_controller()
        self.forbidden_values, _ = build_forbidden_disclosure_values(
            list(DEFAULT_CLIENT_TOKENS.keys()),
            STAGE_3_5_5_IDENTITY_SECRET,
            extra_secrets=[STAGE_3_5_5_ADMIN_TOKEN, STAGE_3_5_5_EVIDENCE_SECRET],
        )

    async def asyncTearDown(self) -> None:
        await reset_pg_tables(self.db_url, include_runtime_control=True)
        reset_semantic_serving_controller()

    async def test_gen001_through_gen010_http_closure_and_observability(self) -> None:
        await reset_pg_tables(self.db_url, include_runtime_control=True)
        env = build_stage37_env(self.db_url)
        indep_seed = "How do I reverse a string cleanly in Python?"
        indep_para = "Reverse a string cleanly in Python"
        dep_schema_a = "CREATE TABLE orders (order_id BIGINT PRIMARY KEY, total_cents INT);"
        dep_schema_b = "CREATE TABLE invoices (invoice_id BIGINT PRIMARY KEY, amount_cents INT);"
        dep_q = "Add a structured pagination helper for the schema above"
        hnsw_prompt = "Sort a list cleanly in Python using sorted()"
        novel_prompt = "Explain QUIC 0-RTT replay protection window sizing in distributed gateways"
        tamper_prompt = "How do I compute a SHA256 hex digest in Python?"
        tamper_para = "Compute a SHA256 hex digest in Python"
        kill_prompt = "How do I parse a JSON file safely in Python?"
        kill_para = "Parse a JSON file safely in Python"

        resp_map = {
            indep_seed: "```python\ndef reverse_str(s: str) -> str:\n    return s[::-1]\n```",
            dep_q: "```sql\nSELECT * FROM table_scoped ORDER BY 1 DESC LIMIT 25;\n```",
            hnsw_prompt: "```python\ndef sort_items(xs):\n    return sorted(xs)\n```",
            novel_prompt: "UPSTREAM_NOVEL_QUIC_ANSWER",
            tamper_prompt: "```python\nimport hashlib\nhashlib.sha256(b'x').hexdigest()\n```",
            tamper_para: "UPSTREAM_AFTER_TAMPER_REJECTION",
            kill_prompt: "```python\nimport json\ndef load_safe(p): return json.load(open(p))\n```",
            kill_para: "UPSTREAM_WHILE_RUNTIME_DISABLED",
        }
        provider = ControlledEvalProvider(delay_seconds=0.0, tag="S371_GEN", response_map=resp_map)

        with patch.dict(os.environ, env, clear=False):
            reload_settings()
            app = create_app()
            app.dependency_overrides[get_llm_provider] = lambda: provider
            async with app.router.lifespan_context(app):
                sem = app.state.semantic_cache
                vs = app.state.vector_store
                assert sem is not None and vs is not None
                self.assertTrue(sem.sql_context_prefilter)
                authority = sem.evidence_authority

                async with httpx.AsyncClient(
                    transport=httpx.ASGITransport(app=app),
                    base_url="http://testserver",
                ) as client:
                    hdr_carol = {"X-Client-Token": "tok_carol"}   # team_beta, tenant_shared
                    hdr_dave = {"X-Client-Token": "tok_dave"}     # team_beta, tenant_shared (different user)
                    hdr_frank = {"X-Client-Token": "tok_frank"}   # team_gamma, tenant_shared
                    hdr_alice = {"X-Client-Token": "tok_alice"}   # team_alpha, private
                    hdr_bob = {"X-Client-Token": "tok_bob"}       # team_alpha, private
                    hdr_admin = {"X-Admin-Token": STAGE_3_5_5_ADMIN_TOKEN}

                    # Unauthenticated /admin/metrics must fail closed
                    unauth_m = await client.get("/admin/metrics")
                    self.assertIn(unauth_m.status_code, (401, 403))

                    # --- GEN-001: Context-aware HTTP lookup under repeated executions (generic-plan regime) + 422 guard ---
                    # Seed mixed background rows into team_beta so planner runs generic-plan path over real corpus
                    anchor_vecs = [r.vector for r in await sem.embedding_provider.embed_batch(
                        [indep_seed, hnsw_prompt, novel_prompt, tamper_prompt, kill_prompt, dep_q]
                    )]
                    await seed_mixed_context_corpus(
                        vs.pool,
                        dim=FASTEMBED_DIM,
                        namespace="team_beta",
                        embed_model=FASTEMBED_MODEL,
                        authority=authority,
                        total_rows=1500,
                        base_vectors=anchor_vecs,
                    )
                    seed_indep = await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": indep_seed}]})
                    self.assertEqual(seed_indep.status_code, 200)
                    # Execute >=7 semantic paraphrases across second principal to cross generic-plan threshold
                    for _ in range(7):
                        hit_r = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": indep_para}]})
                        self.assertEqual(hit_r.status_code, 200)
                        self.assertTrue(hit_r.json()["cached"], f"GEN-001 expected semantic hit: {hit_r.json()}")
                        self.assertEqual(hit_r.json()["cache_type"], "semantic")
                    # Seed CONTEXT_DEPENDENT schema A and verify compatible follow-up hits under generic plan
                    await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": dep_schema_a}, {"role": "user", "content": dep_q}]})
                    dep_hit = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": dep_schema_a}, {"role": "user", "content": dep_q}]})
                    self.assertEqual(dep_hit.status_code, 200)
                    self.assertTrue(dep_hit.json()["cached"])
                    # Client-supplied forbidden context fields rejected with HTTP 422
                    inj_r = await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": indep_para}], "context_fingerprint": "ctx-v1:spoofed"})
                    self.assertEqual(inj_r.status_code, 422)

                    # --- GEN-002: Context-independent request with no suitable candidate -> upstream fallback ---
                    gen002 = await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": novel_prompt}]})
                    self.assertEqual(gen002.status_code, 200)
                    self.assertFalse(gen002.json()["cached"])
                    self.assertEqual(gen002.json()["cache_type"], "none")
                    self.assertEqual(gen002.json()["response"], resp_map[novel_prompt])

                    # --- GEN-003: Context-dependent request with incompatible schema B -> rejected/prefiltered + upstream fallback ---
                    gen003 = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": dep_schema_b}, {"role": "user", "content": dep_q}]})
                    self.assertEqual(gen003.status_code, 200)
                    self.assertFalse(gen003.json()["cached"])
                    self.assertEqual(gen003.json()["cache_type"], "none")

                    # --- GEN-004 & GEN-005: Skewed HNSW miss at ef=40 -> safe upstream fallback; recovery at ef=400 -> full validation ---
                    await reset_pg_tables(self.db_url)
                    hnsw_q_vec = (await sem.embedding_provider.embed(hnsw_prompt)).vector
                    await seed_skewed_hnsw_fixture(
                        vs.pool,
                        dim=FASTEMBED_DIM,
                        namespace="team_beta",
                        embed_model=FASTEMBED_MODEL,
                        authority=authority,
                        anchor_vec=hnsw_q_vec,
                        singleton_prompt=hnsw_prompt,
                        singleton_resp=resp_map[hnsw_prompt],
                        temperature=0.7,
                    )
                    # GEN-004: Normal ef_search=40 HTTP path misses isolated singleton, sees 25 incompatible Rust rows -> upstream fallback
                    stats_before_g4 = (await client.get("/cache/semantic/stats", headers=hdr_admin)).json()
                    gen004 = await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": hnsw_prompt}]})
                    self.assertEqual(gen004.status_code, 200)
                    self.assertFalse(gen004.json()["cached"])
                    stats_after_g4 = (await client.get("/cache/semantic/stats", headers=hdr_admin)).json()
                    self.assertGreater(
                        stats_after_g4["retrieval_budget_exhaustions"],
                        stats_before_g4["retrieval_budget_exhaustions"],
                    )

                    # Re-seed a pristine skewed HNSW fixture for GEN-005 so GEN-004 fallback rows do not short-circuit exact/HNSW lookup
                    await reset_pg_tables(self.db_url)
                    await seed_skewed_hnsw_fixture(
                        vs.pool,
                        dim=FASTEMBED_DIM,
                        namespace="team_beta",
                        embed_model=FASTEMBED_MODEL,
                        authority=authority,
                        anchor_vec=hnsw_q_vec,
                        singleton_prompt=hnsw_prompt,
                        singleton_resp=resp_map[hnsw_prompt],
                        temperature=0.7,
                    )

                    # GEN-005: With higher ef_search=400 on PG session, HTTP path retrieves singleton AND runs full compatibility validation
                    orig_pool = vs.pool
                    high_pool = await asyncpg.create_pool(
                        dsn=self.db_url,
                        min_size=1,
                        max_size=2,
                        setup=lambda c: c.execute("SET hnsw.ef_search = 400;"),
                    )
                    vs.pool = high_pool
                    try:
                        # Compatible request recovers singleton via HNSW ef=400 and passes compatibility + HMAC
                        gen005_hit = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": hnsw_prompt}]})
                        self.assertEqual(gen005_hit.status_code, 200)
                        self.assertTrue(gen005_hit.json()["cached"])
                        self.assertEqual(gen005_hit.json()["cache_type"], "semantic")
                        self.assertEqual(gen005_hit.json()["response"], resp_map[hnsw_prompt])
                        # Incompatible prompt paraphrase ("temperature": 0.9) STILL rejected by compatibility validation despite ef=400 retrieval
                        gen005_rej = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "temperature": 0.9, "messages": [{"role": "user", "content": "Sort a list cleanly in Python with sorted()"}]})
                        self.assertEqual(gen005_rej.status_code, 200)
                        self.assertFalse(gen005_rej.json()["cached"])
                    finally:
                        vs.pool = orig_pool
                        await high_pool.close()

                    # --- GEN-006: Cross-tenant candidate remains inaccessible ---
                    gen006 = await client.post("/query", headers=hdr_frank, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": hnsw_prompt}]})
                    self.assertEqual(gen006.status_code, 200)
                    self.assertFalse(gen006.json()["cached"])

                    # --- GEN-007: Private candidate belonging to another principal remains inaccessible ---
                    p_priv = "Write a Python helper to check palindromes"
                    await client.post("/query", headers=hdr_alice, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": p_priv}]})
                    gen007 = await client.post("/query", headers=hdr_bob, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "Check palindromes in Python"}]})
                    self.assertEqual(gen007.status_code, 200)
                    self.assertFalse(gen007.json()["cached"])

                    # --- GEN-008: Vector retrieval exception fails open to upstream generation ---
                    with patch.object(vs, "search", side_effect=VectorStoreError("injected PG transient error")):
                        gen008 = await client.post("/query", headers=hdr_alice, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": "Unseen safe fallback question in Python"}]})
                    self.assertEqual(gen008.status_code, 200)
                    self.assertFalse(gen008.json()["cached"])

                    # --- GEN-009: Invalid HMAC compatibility evidence rejected + upstream fallback ---
                    s_tamper = await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": tamper_prompt}]})
                    self.assertEqual(s_tamper.status_code, 200)
                    async with vs.pool.acquire() as conn:
                        for tbl in ("semantic_variant_records", "vector_records"):
                            await conn.execute(
                                f"""
                                UPDATE {tbl}
                                SET metadata = jsonb_set(metadata, '{{compatibility_evidence,evidence_signature}}', '"0000tampered_sig0000"'::jsonb)
                                WHERE cache_namespace = 'team_beta' AND query_text = $1;
                                """,
                                tamper_prompt,
                            )
                    gen009 = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": tamper_para}]})
                    self.assertEqual(gen009.status_code, 200)
                    self.assertFalse(gen009.json()["cached"])
                    self.assertEqual(gen009.json()["response"], resp_map[tamper_para])

                    # --- GEN-010: Runtime semantic serving disabled -> no subsequent semantic response served ---
                    await client.post("/query", headers=hdr_carol, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": kill_prompt}]})
                    off_res = await client.post("/admin/semantic-cache/serving", headers=hdr_admin, json={"enabled": False, "reason": "gen010_kill"})
                    self.assertEqual(off_res.status_code, 200)
                    gen010 = await client.post("/query", headers=hdr_dave, json={"model": "mock-qwen-27b", "messages": [{"role": "user", "content": kill_para}]})
                    self.assertEqual(gen010.status_code, 200)
                    self.assertFalse(gen010.json()["cached"])
                    self.assertEqual(gen010.json()["response"], resp_map[kill_para])

                    # --- Phase 6 Observability & Non-Disclosure Verification ---
                    admin_metrics = (await client.get("/admin/metrics", headers=hdr_admin)).json()
                    sem_stats = (await client.get("/cache/semantic/stats", headers=hdr_admin)).json()
                    agg = admin_metrics.get("aggregate", {})
                    global_sem = agg.get("semantic", {})
                    lat_block = agg.get("latencies", {})

                    self.assertGreater(global_sem.get("semantic_lookup_attempts", 0), 0)
                    self.assertGreater(global_sem.get("semantic_candidates_retrieved", 0), 0)
                    self.assertGreater(global_sem.get("retrieval_budget_exhaustions", 0), 0)
                    self.assertGreater(global_sem.get("incompatible_candidate_rejections", 0), 0)
                    self.assertGreater(global_sem.get("semantic_hits", 0), 0)
                    self.assertGreater(global_sem.get("upstream_fallbacks_after_semantic_miss", 0), 0)
                    self.assertGreater(global_sem.get("vector_store_failures", 0), 0)
                    self.assertGreater(global_sem.get("evidence_validation_rejections", 0), 0)
                    self.assertGreater(global_sem.get("semantic_runtime_disabled_skips", 0), 0)
                    self.assertGreater(lat_block.get("vector_search_latency_ms", {}).get("sample_count", 0), 0)
                    self.assertGreater(lat_block.get("compatibility_validation_latency_ms", {}).get("sample_count", 0), 0)
                    self.assertGreater(sem_stats.get("lookups", 0), 0)

                    # Verify zero sensitive tokens, secrets, or derived principal IDs leak in metrics/stats
                    verify_no_sensitive_leaks(admin_metrics, self.forbidden_values, "admin_metrics", check_forbidden_keys=False)
                    verify_no_sensitive_leaks(sem_stats, self.forbidden_values, "semantic_stats", check_forbidden_keys=False)
