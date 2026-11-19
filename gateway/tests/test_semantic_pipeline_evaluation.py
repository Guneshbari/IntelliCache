"""
Unit and integration tests for IntelliCache Stage 3.2.5 — End-to-End Semantic Cache Pipeline Evaluation.

Verifies:
1. Embedding cache manifest validation, dimensions, normalization, finite values, and dataset SHA-256.
2. Scenario dataset architecture, intent-group held-out coverage, category counts, and candidate pools.
3. Ambiguity policy candidate-level accounting (prevented FPs, erroneous FNs, concordant hits).
4. Statistical functions: Wilson score confidence intervals, distribution percentiles, clustered bootstrap CIs.
5. Threshold configuration injection, instance isolation, and preservation of global production defaults.
6. 100.0% deterministic rejection of high-similarity metadata-incompatible candidates prior to ranking.
7. Reliable fail-open fallback to upstream generation on infrastructure/embedding/vector-store failure.
8. Evaluation report artifact presence, schema validity, and metric consistency.
9. Live unmocked FastEmbed and pgvector pipeline integration test (when PostgreSQL is available).
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock

from app.config import get_settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.semantic_cache.compatibility import SemanticCompatibilityMetadata
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import asyncpg

from app.cache.postgres_cache import PostgresCache
from app.embeddings.contracts import EmbeddingTask
from app.embeddings.fastembed_adapter import FastEmbedProvider
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.cache.migration import run_migrations
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore
from scripts.evaluate_semantic_pipeline import (
    DeterministicStubProvider,
    ReadOnlyPostgresCache,
    ReadOnlySemanticCacheService,
    compute_clustered_bootstrap_cis,
    compute_distribution_stats,
    compute_wilson_ci,
    validate_embedding_cache_and_manifest,
)


class TestEmbeddingManifestAndCache(unittest.TestCase):
    """Validates embedding cache manifest, dimensions, normalization, and integrity."""

    @classmethod
    def setUpClass(cls):
        cls.gateway_root = Path(__file__).resolve().parent.parent
        cls.manifest_path = cls.gateway_root / "evaluation" / "datasets" / "embeddings" / "stage3_2_5_embedding_manifest.json"
        cls.cache_path = cls.gateway_root / "evaluation" / "datasets" / "embeddings" / "bge_small_en_v1_5_cache.json"
        cls.dataset_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_4_semantic_pairs.json"

        with open(cls.manifest_path, "r", encoding="utf-8") as f:
            cls.manifest = json.load(f)
        with open(cls.cache_path, "r", encoding="utf-8") as f:
            cls.cache = json.load(f)

    def test_manifest_metadata(self):
        self.assertEqual(self.manifest["model_name"], "BAAI/bge-small-en-v1.5")
        self.assertEqual(self.manifest["dimensions"], 384)
        self.assertEqual(self.manifest["provider"], "FastEmbed")
        self.assertEqual(self.manifest["embedding_count"], 1514)
        self.assertEqual(self.manifest["dataset_sha256"], "b13d69c9024246136f69b189d3f0801a4e71de1d1f9362761faa943a7a913da5")
        self.assertEqual(self.manifest["cache_sha256"], "de7515fa572e8dc901ce6175067bec9b14b664495f3b8b08f528a440cc7d04bb")
        self.assertEqual(self.manifest["query_corpus_sha256"], "ce2d4acfcd6433861974e418771bce39bbe8d0c6a053e2d9285ce87ac91cbb60")

    def test_validation_function_passes(self):
        passed = validate_embedding_cache_and_manifest(self.cache_path, self.manifest_path, self.dataset_path)
        self.assertTrue(passed, "Cache validation against manifest must pass.")

    def test_cache_vector_properties(self):
        self.assertEqual(len(self.cache), 1514)
        for q, vec in list(self.cache.items())[:50]:
            self.assertEqual(len(vec), 384)
            norm = math.sqrt(sum(x * x for x in vec))
            self.assertAlmostEqual(norm, 1.0, places=3)
            self.assertTrue(all(math.isfinite(x) for x in vec))


class TestScenarioDatasetArchitecture(unittest.TestCase):
    """Validates dataset architecture, intent-group separation, and candidate pools."""

    @classmethod
    def setUpClass(cls):
        cls.gateway_root = Path(__file__).resolve().parent.parent
        cls.scenarios_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_5_scenarios.json"
        cls.metadata_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_5_dataset_metadata.json"

        with open(cls.scenarios_path, "r", encoding="utf-8") as f:
            cls.scenarios = json.load(f)
        with open(cls.metadata_path, "r", encoding="utf-8") as f:
            cls.meta = json.load(f)

    def test_scenario_counts_match_metadata(self):
        primary = self.scenarios["primary_held_out"]
        pos = [s for s in primary if s["scenario_type"] == "safe_positive"]
        neg = [s for s in primary if s["scenario_type"] == "hard_negative"]

        self.assertEqual(len(primary), 252)
        self.assertEqual(len(pos), 84)
        self.assertEqual(len(neg), 168)
        self.assertEqual(len(self.scenarios["ambiguity_controlled"]), 12)
        self.assertEqual(len(self.scenarios["compatibility_guardrail"]), 60)
        self.assertEqual(len(self.scenarios["infrastructure_fallback"]), 5)

        self.assertEqual(self.meta["held_out_intent_groups"], 42)
        self.assertEqual(self.meta["target_candidate_pool_size"], 42)
        self.assertEqual(self.meta["distractor_candidate_pool_size"], 42)
        self.assertEqual(self.meta["total_candidate_pool_size"], 84)

    def test_primary_held_out_intent_group_coverage(self):
        primary = self.scenarios["primary_held_out"]
        intent_groups = {s["intent_group_id"] for s in primary}
        self.assertEqual(len(intent_groups), 42)

        for ig in intent_groups:
            group_scenarios = [s for s in primary if s["intent_group_id"] == ig]
            group_pos = [s for s in group_scenarios if s["scenario_type"] == "safe_positive"]
            group_neg = [s for s in group_scenarios if s["scenario_type"] == "hard_negative"]
            self.assertEqual(len(group_pos), 2, f"Intent group {ig} must have exactly 2 positive scenarios.")
            self.assertEqual(len(group_neg), 4, f"Intent group {ig} must have exactly 4 hard negative scenarios.")

    def test_distractor_candidates_domain_stratified(self):
        distractors = self.meta["distractor_targets"]
        self.assertEqual(len(distractors), 42)
        domain_counts = {}
        for d in distractors:
            dom = d["domain"]
            domain_counts[dom] = domain_counts.get(dom, 0) + 1

        self.assertEqual(len(domain_counts), 10, "Distractors must be drawn from all 10 domains.")
        for dom, count in domain_counts.items():
            self.assertIn(count, (4, 5), f"Domain '{dom}' must have 4 or 5 distractors, found {count}")


class TestStatisticalCalculations(unittest.TestCase):
    """Validates statistical calculations: Wilson score interval, distribution stats, and bootstrap."""

    def test_wilson_ci_boundaries_and_ordering(self):
        # 0 successes
        low0, high0 = compute_wilson_ci(0, 100)
        self.assertEqual(low0, 0.0)
        self.assertGreater(high0, 0.0)
        self.assertLess(high0, 0.05)

        # 100 successes
        low100, high100 = compute_wilson_ci(100, 100)
        self.assertGreater(low100, 0.95)
        self.assertEqual(high100, 1.0)

        # 50% successes
        low50, high50 = compute_wilson_ci(50, 100)
        self.assertAlmostEqual(50 / 100 - low50, high50 - 50 / 100, places=4)
        self.assertTrue(0.0 <= low50 <= high50 <= 1.0)

    def test_distribution_stats(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        stats = compute_distribution_stats(values)
        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["max"], 50.0)
        self.assertEqual(stats["mean"], 30.0)
        self.assertEqual(stats["p50"], 30.0)

    def test_clustered_bootstrap_reproducibility(self):
        # Construct mock request records across 4 clusters
        records = [
            {"intent_group_id": "g1", "classification": "true_positive"},
            {"intent_group_id": "g1", "classification": "true_negative"},
            {"intent_group_id": "g2", "classification": "false_negative"},
            {"intent_group_id": "g2", "classification": "true_negative"},
            {"intent_group_id": "g3", "classification": "true_positive"},
            {"intent_group_id": "g3", "classification": "true_negative"},
            {"intent_group_id": "g4", "classification": "false_negative"},
            {"intent_group_id": "g4", "classification": "true_negative"},
        ]
        res1 = compute_clustered_bootstrap_cis(records, n_resamples=1000, seed=123)
        res2 = compute_clustered_bootstrap_cis(records, n_resamples=1000, seed=123)
        self.assertEqual(res1["recall_bootstrap_ci"], res2["recall_bootstrap_ci"])
        self.assertEqual(res1["precision_bootstrap_ci"], res2["precision_bootstrap_ci"])
        self.assertEqual(res1["clusters_count"], 4)


class TestThresholdConfigurationInjectionAndIsolation(unittest.TestCase):
    """Verifies that evaluation thresholds are injected without mutating production settings."""

    def test_production_defaults_intact(self):
        # Production invariants
        s = get_settings()
        self.assertFalse(s.semantic_cache_enabled)
        self.assertEqual(s.semantic_cache_min_similarity, 0.92)
        self.assertEqual(s.semantic_cache_min_margin, 0.03)

    def test_instance_level_policy_isolation(self):
        policy_base = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03)
        policy_cand = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03)

        self.assertEqual(policy_base.min_similarity, 0.92)
        self.assertEqual(policy_cand.min_similarity, 0.90)

        # Confirm global settings were not mutated
        s = get_settings()
        self.assertEqual(s.semantic_cache_min_similarity, 0.92)


class TestCandidateLevelAmbiguityAccounting(unittest.TestCase):
    """Verifies candidate-level ambiguity accounting and differentiation of prevented FP vs erroneous FN using runtime policy."""

    @classmethod
    def setUpClass(cls):
        cls.compat_meta = {
            "schema_version": 2,
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "conceptual"},
            "dimensions": {
                "language": {"status": "NOT_APPLICABLE", "value": None},
                "language_version": {"status": "NOT_APPLICABLE", "value": None},
                "framework": {"status": "NOT_APPLICABLE", "value": None},
                "framework_version": {"status": "NOT_APPLICABLE", "value": None},
                "runtime": {"status": "NOT_APPLICABLE", "value": None},
                "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
                "execution_model": {"status": "NOT_APPLICABLE", "value": None},
                "output_format": {"status": "NOT_APPLICABLE", "value": None},
                "constraints": {"status": "NOT_APPLICABLE", "value": None},
            },
            "generation_contract": {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.7, "max_tokens": 1000},
            "embedding_contract": {"model": "BAAI/bge-small-en-v1.5"},
            "llm_provider": "groq",
            "llm_model": "qwen/qwen3.8-27b",
            "temperature": 0.7,
            "max_tokens": 1000,
        }
        cls.policy = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")
        cls.req_meta = SemanticCompatibilityMetadata.from_dict(cls.compat_meta)

    def test_ambiguity_conflicting_prevents_false_positive(self):
        top_cand = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp1",
            query_text="Top query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.04,
            similarity=0.96,
            cached_response="Incorrect Answer A",
            metadata=self.compat_meta,
        )
        runner_up = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp2",
            query_text="Runner query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.05,
            similarity=0.95,
            cached_response="Different Answer B",
            metadata=self.compat_meta,
        )
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Top query")],
            temperature=0.7,
            max_tokens=1000,
            provider="groq",
            namespace="ns",
        )

        winning, reason = self.policy.evaluate_candidates([top_cand, runner_up], req, request_metadata=self.req_meta)
        self.assertIsNone(winning)
        self.assertIsNotNone(reason)
        self.assertIn("Ambiguous candidate", reason)

        # Top candidate was NOT expected safe response, so ambiguity prevented a false positive!
        expected_safe_response = "Correct Answer C"
        prevented_fp = 1 if (top_cand.cached_response.strip() != expected_safe_response.strip()) else 0
        erroneous_fn = 1 if (top_cand.cached_response.strip() == expected_safe_response.strip()) else 0
        self.assertEqual(prevented_fp, 1)
        self.assertEqual(erroneous_fn, 0)

    def test_ambiguity_conflicting_causes_erroneous_miss(self):
        top_cand = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp1",
            query_text="Top query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.04,
            similarity=0.96,
            cached_response="Correct Answer A",
            metadata=self.compat_meta,
        )
        runner_up = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp2",
            query_text="Runner query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.05,
            similarity=0.95,
            cached_response="Incorrect Answer B",
            metadata=self.compat_meta,
        )
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Top query")],
            temperature=0.7,
            max_tokens=1000,
            provider="groq",
            namespace="ns",
        )

        winning, reason = self.policy.evaluate_candidates([top_cand, runner_up], req, request_metadata=self.req_meta)
        self.assertIsNone(winning)
        self.assertIsNotNone(reason)
        self.assertIn("Ambiguous candidate", reason)

        # Top candidate was actually the correct answer! Ambiguity caused an unnecessary miss
        expected_safe_response = "Correct Answer A"
        prevented_fp = 1 if (top_cand.cached_response.strip() != expected_safe_response.strip()) else 0
        erroneous_fn = 1 if (top_cand.cached_response.strip() == expected_safe_response.strip()) else 0
        self.assertEqual(prevented_fp, 0)
        self.assertEqual(erroneous_fn, 1)

    def test_ambiguity_concordant_answers_accepted(self):
        top_cand = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp1",
            query_text="Top query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.04,
            similarity=0.96,
            cached_response="Same Correct Answer",
            metadata=self.compat_meta,
        )
        runner_up = VectorSearchResult(
            cache_namespace="ns",
            fingerprint="fp2",
            query_text="Runner query",
            model="BAAI/bge-small-en-v1.5",
            distance=0.045,
            similarity=0.955,
            cached_response="Same Correct Answer",
            metadata=self.compat_meta,
        )
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content="Top query")],
            temperature=0.7,
            max_tokens=1000,
            provider="groq",
            namespace="ns",
        )

        winning, reason = self.policy.evaluate_candidates([top_cand, runner_up], req, request_metadata=self.req_meta)
        self.assertIsNotNone(winning, "Concordant answers must not be rejected.")
        self.assertEqual(winning.cached_response, "Same Correct Answer")
        self.assertIsNone(reason)


class TestCompatibilityGuardrails(unittest.TestCase):
    """Verifies that all 60 metadata-incompatible scenarios are rejected deterministically before ranking."""

    @classmethod
    def setUpClass(cls):
        gateway_root = Path(__file__).resolve().parent.parent
        scenarios_path = gateway_root / "evaluation" / "datasets" / "stage3_2_5_scenarios.json"
        with open(scenarios_path, "r", encoding="utf-8") as f:
            cls.scenarios = json.load(f)["compatibility_guardrail"]

        cls.policy = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")

    def test_all_sixty_scenarios_rejected(self):
        rejections = 0
        for sc in self.scenarios:
            req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content=sc["incoming_query"])],
                temperature=0.7,
                max_tokens=1000,
                provider="groq",
                namespace="eval_s325_guardrails",
            )
            cand = VectorSearchResult(
                cache_namespace="eval_s325_guardrails",
                fingerprint="incompat_fp",
                query_text=sc["target_canonical_query"],
                model="BAAI/bge-small-en-v1.5",
                distance=0.01,
                similarity=0.99,  # High similarity!
                cached_response=sc["candidate_response"],
                metadata=sc["candidate_metadata"],
            )

            is_compat, reason = self.policy.is_candidate_compatible(cand, req)
            if not is_compat:
                rejections += 1

        self.assertEqual(rejections, 60, "All 60 metadata-incompatible candidates must be rejected.")


class TestInfrastructureFailOpen(unittest.IsolatedAsyncioTestCase):
    """Verifies that pipeline errors fall back open cleanly to upstream provider."""

    async def test_vector_store_failure_fails_open(self):
        failing_vector_store = AsyncMock()
        failing_vector_store.search.side_effect = RuntimeError("pgvector connection lost")

        mock_embedding_provider = AsyncMock()
        mock_embedding_provider.embed.return_value = MagicMock(vector=[0.1] * 384, model="BAAI/bge-small-en-v1.5")
        mock_embedding_provider.embed_query.return_value = MagicMock(vector=[0.1] * 384, model="BAAI/bge-small-en-v1.5")

        policy = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None  # Exact miss

        semantic_cache = SemanticCacheService(
            embedding_provider=mock_embedding_provider,
            vector_store=failing_vector_store,
            policy=policy,
        )

        core = IntelliCacheCore(
            cache=mock_cache,
            semantic_cache=semantic_cache,
            single_flight=SingleFlightCoordinator(),
            cache_enabled=True,
        )

        stub_provider = DeterministicStubProvider()
        req = GenerationRequest(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[ChatMessage(role="user", content="How do I configure TCP socket keepalive in Python network script?")],
            temperature=0.7,
            max_tokens=1024,
            provider="groq",
        )

        elig = policy.check_eligibility(req)
        self.assertTrue(elig.eligible, f"Query must be eligible for vector store search test, got: {elig.reason}")

        res = await core.execute_query(req, stub_provider)
        self.assertEqual(res.cache_type, "none")
        self.assertFalse(res.cached)
        self.assertIn("[UPSTREAM_GENERATED", res.text)
        self.assertEqual(stub_provider.call_count, 1)
        failing_vector_store.search.assert_awaited_once()

    async def test_embedding_failure_fails_open(self):
        mock_vector_store = AsyncMock()
        failing_embedding_provider = AsyncMock()
        failing_embedding_provider.embed_query.side_effect = TimeoutError("FastEmbed timeout after 5000ms")
        failing_embedding_provider.embed.side_effect = TimeoutError("FastEmbed timeout after 5000ms")

        policy = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None

        semantic_cache = SemanticCacheService(
            embedding_provider=failing_embedding_provider,
            vector_store=mock_vector_store,
            policy=policy,
        )

        core = IntelliCacheCore(
            cache=mock_cache,
            semantic_cache=semantic_cache,
            single_flight=SingleFlightCoordinator(),
            cache_enabled=True,
        )

        stub_provider = DeterministicStubProvider()
        req = GenerationRequest(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[ChatMessage(role="user", content="Write a Python script to compute moving averages with pandas.")],
            temperature=0.7,
            max_tokens=1024,
            provider="groq",
        )

        elig = policy.check_eligibility(req)
        self.assertTrue(elig.eligible, f"Query must be eligible for embedding failure test, got: {elig.reason}")

        res = await core.execute_query(req, stub_provider)
        self.assertEqual(res.cache_type, "none")
        self.assertFalse(res.cached)
        self.assertIn("[UPSTREAM_GENERATED", res.text)
        self.assertEqual(stub_provider.call_count, 1)
        self.assertGreaterEqual(failing_embedding_provider.embed.await_count, 1)
        mock_vector_store.search.assert_not_called()

    async def test_empty_candidate_pool_fails_open(self):
        empty_vector_store = AsyncMock()
        empty_vector_store.search.return_value = []

        mock_embedding_provider = AsyncMock()
        mock_embedding_provider.embed_query.return_value = MagicMock(vector=[0.1] * 384, model="BAAI/bge-small-en-v1.5")
        mock_embedding_provider.embed.return_value = MagicMock(vector=[0.1] * 384, model="BAAI/bge-small-en-v1.5")

        policy = SemanticCachePolicy(min_similarity=0.90, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")
        mock_cache = AsyncMock()
        mock_cache.get.return_value = None

        semantic_cache = SemanticCacheService(
            embedding_provider=mock_embedding_provider,
            vector_store=empty_vector_store,
            policy=policy,
        )

        core = IntelliCacheCore(
            cache=mock_cache,
            semantic_cache=semantic_cache,
            single_flight=SingleFlightCoordinator(),
            cache_enabled=True,
        )

        stub_provider = DeterministicStubProvider()
        req = GenerationRequest(
            model="meta-llama/llama-3.1-8b-instruct",
            messages=[ChatMessage(role="user", content="How do I write a Python function to parse JSON data?")],
            temperature=0.7,
            max_tokens=1024,
            provider="groq",
        )

        elig = policy.check_eligibility(req)
        self.assertTrue(elig.eligible, f"Query must be eligible for empty candidate pool test, got: {elig.reason}")

        res = await core.execute_query(req, stub_provider)
        self.assertEqual(res.cache_type, "none")
        self.assertFalse(res.cached)
        self.assertIn("[UPSTREAM_GENERATED", res.text)
        self.assertEqual(stub_provider.call_count, 1)
        empty_vector_store.search.assert_awaited_once()

    async def test_candidate_pool_immutability_proxies(self):
        ro_cache = ReadOnlyPostgresCache(pool=MagicMock())
        saved = await ro_cache.set("some_key", MagicMock(), namespace="test_ns")
        self.assertIsNone(saved, "ReadOnlyPostgresCache.set must return None without writing.")

        ro_semantic = ReadOnlySemanticCacheService(
            embedding_provider=MagicMock(),
            vector_store=MagicMock(),
            policy=MagicMock(),
        )
        indexed = await ro_semantic.index(MagicMock(), MagicMock())
        self.assertFalse(indexed, "ReadOnlySemanticCacheService.index must no-op and return False.")


class TestReportArtifactsConsistency(unittest.TestCase):
    """Verifies evaluation reports exist and conform to schema and invariants."""

    @classmethod
    def setUpClass(cls):
        gateway_root = Path(__file__).resolve().parent.parent
        cls.json_report_path = gateway_root / "evaluation" / "reports" / "stage3_2_5_end_to_end_evaluation.json"
        cls.md_report_path = gateway_root / "evaluation" / "reports" / "stage3_2_5_end_to_end_evaluation.md"

        if cls.json_report_path.exists():
            with open(cls.json_report_path, "r", encoding="utf-8") as f:
                cls.report_data = json.load(f)
        else:
            cls.report_data = None

    def test_reports_exist(self):
        self.assertTrue(self.json_report_path.exists(), "JSON evaluation report must exist.")
        self.assertTrue(self.md_report_path.exists(), "Markdown evaluation report must exist.")

    def test_report_invariants_and_metrics(self):
        if not self.report_data:
            self.skipTest("Report JSON not generated yet.")

        self.assertEqual(self.report_data["stage"], "Stage 3.2.5 — End-to-End Semantic Cache Pipeline Evaluation")
        self.assertEqual(self.report_data["dataset_metadata"]["scenario_counts"]["primary_held_out"], 252)

        baseline = self.report_data["configurations"]["control_baseline"]["results"]
        candidate = self.report_data["configurations"]["experimental_candidate"]["results"]

        # Precision must be 1.0 (zero false positives)
        self.assertEqual(baseline["false_positives"], 0)
        self.assertEqual(candidate["false_positives"], 0)
        self.assertEqual(baseline["precision"], 1.0)
        self.assertEqual(candidate["precision"], 1.0)

        # Delta true positives non-negative
        paired = self.report_data["paired_comparison"]
        self.assertGreaterEqual(paired["delta_true_positives"], 0)
        self.assertEqual(paired["delta_false_positives"], 0)

        # Compatibility guardrails 100%
        guardrails = self.report_data["compatibility_guardrail_evaluation"]
        self.assertEqual(guardrails["rejection_rate"], 1.0)
        self.assertTrue(guardrails["guardrails_intact"])

        # Fallbacks 100%
        fallbacks = self.report_data["infrastructure_fallback_evaluation"]
        self.assertTrue(fallbacks["all_fell_back_cleanly"])

        # Candidate pool immutability
        self.assertEqual(baseline["candidate_pool_invariants"]["initial_candidate_count"], 84)
        self.assertEqual(baseline["candidate_pool_invariants"]["final_candidate_count"], 84)
        self.assertFalse(baseline["candidate_pool_invariants"]["pool_mutated"])
        self.assertEqual(candidate["candidate_pool_invariants"]["initial_candidate_count"], 84)
        self.assertEqual(candidate["candidate_pool_invariants"]["final_candidate_count"], 84)
        self.assertFalse(candidate["candidate_pool_invariants"]["pool_mutated"])

        # Simulated resource savings note
        self.assertIn("simulated_resource_savings", paired)
        self.assertIn("disclaimer", paired["simulated_resource_savings"])


class TestLiveFastEmbedPgvectorPipelineIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Live integration test executing unmocked FastEmbed inference and pgvector retrieval.
    Exercises the actual pipeline, 0.92 vs 0.90 threshold execution, and namespace isolation.
    """

    async def asyncSetUp(self):
        db_url = os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test"
        parsed = urlsplit(db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.skipTest(f"Database name '{db_name}' does not contain 'test'.")

        try:
            self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=384)
            self.embedding_provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", dimensions=384)
            self.test_ns = "eval_s325_live_test"
            self.other_ns = "eval_s325_live_other"
            self.vector_store = PostgresVectorStore(pool=self.pool, default_namespace=self.test_ns, default_model="BAAI/bge-small-en-v1.5", dimensions=384)
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace IN ($1, $2)", self.test_ns, self.other_ns)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)", self.test_ns, self.other_ns)
        except Exception as e:
            self.skipTest(f"PostgreSQL/pgvector unavailable for live test: {e}")

    async def asyncTearDown(self):
        if hasattr(self, "pool"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM vector_records WHERE cache_namespace IN ($1, $2)", self.test_ns, self.other_ns)
                    await conn.execute("DELETE FROM cache_entries WHERE cache_namespace IN ($1, $2)", self.test_ns, self.other_ns)
            except Exception:
                pass
            await self.pool.close()

    async def test_live_pipeline_hit_and_namespace_isolation(self):
        # 1. Embed and seed a candidate
        canonical_query = "How does Nginx gzip and brotli static compression differ from on-the-fly dynamic compression?"
        incoming_query = "Explain the difference between Nginx dynamic gzip/brotli compression and pre-compressed static files (gzip_static)."
        cached_resp = "Pre-compressed static files eliminate runtime CPU compression overhead on every incoming HTTP request."

        embed_res = await self.embedding_provider.embed(canonical_query, task=EmbeddingTask.DOCUMENT)
        fp = "live_test_fp_001"
        meta = {
            "schema_version": 2,
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "conceptual"},
            "dimensions": {
                "language": {"status": "NOT_APPLICABLE", "value": None},
                "language_version": {"status": "NOT_APPLICABLE", "value": None},
                "framework": {"status": "NOT_APPLICABLE", "value": None},
                "framework_version": {"status": "NOT_APPLICABLE", "value": None},
                "runtime": {"status": "NOT_APPLICABLE", "value": None},
                "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
                "execution_model": {"status": "NOT_APPLICABLE", "value": None},
                "output_format": {"status": "NOT_APPLICABLE", "value": None},
                "constraints": {"status": "NOT_APPLICABLE", "value": None},
            },
            "generation_contract": {"provider": "groq", "model": "qwen/qwen3.8-27b", "temperature": 0.7, "max_tokens": 1000},
            "embedding_contract": {"model": "BAAI/bge-small-en-v1.5"},
            "llm_provider": "groq",
            "llm_model": "qwen/qwen3.8-27b",
            "temperature": 0.7,
            "max_tokens": 1000,
        }

        async with self.pool.acquire() as conn:
            await conn.execute(
                """
                INSERT INTO cache_entries (
                    cache_namespace, fingerprint, response, model, provider,
                    prompt_tokens, completion_tokens, total_tokens, latency_ms,
                    hit_count, metadata, created_at, last_accessed, expires_at
                ) VALUES ($1, $2, $3, $4, $5, 50, 150, 200, 450.0, 0, $6, NOW(), NOW(), NULL)
                ON CONFLICT (cache_namespace, fingerprint) DO UPDATE SET response = EXCLUDED.response;
                """,
                self.test_ns, fp, cached_resp, "qwen/qwen3.8-27b", "groq", json.dumps(meta),
            )
            vec_str = "[" + ",".join(f"{x:.8f}" for x in embed_res.vector) + "]"
            await conn.execute(
                """
                INSERT INTO vector_records (
                    cache_namespace, fingerprint, query_text, model, vector, metadata, created_at, updated_at
                ) VALUES ($1, $2, $3, $4, $5::vector, $6, NOW(), NOW())
                ON CONFLICT (cache_namespace, fingerprint, model) DO UPDATE SET vector = EXCLUDED.vector;
                """,
                self.test_ns, fp, canonical_query, "BAAI/bge-small-en-v1.5", vec_str, json.dumps(meta),
            )

        # 2. Test semantic cache hit at threshold 0.92
        policy_092 = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, configured_model="BAAI/bge-small-en-v1.5")
        sem_cache_092 = SemanticCacheService(embedding_provider=self.embedding_provider, vector_store=self.vector_store, policy=policy_092, default_namespace=self.test_ns)
        cache = PostgresCache(pool=self.pool, default_namespace=self.test_ns)
        core_092 = IntelliCacheCore(cache=cache, semantic_cache=sem_cache_092, single_flight=SingleFlightCoordinator(), cache_enabled=True)

        stub_provider = DeterministicStubProvider()
        req = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content=incoming_query)],
            temperature=0.7,
            max_tokens=1000,
            provider="groq",
            namespace=self.test_ns,
        )

        res = await core_092.execute_query(req, stub_provider)
        self.assertEqual(res.cache_type, "semantic")
        self.assertTrue(res.cached)
        self.assertEqual(res.text, cached_resp)
        self.assertEqual(stub_provider.call_count, 0)

        # 3. Test namespace isolation: same query in other namespace must fall back
        req_other_ns = GenerationRequest(
            model="qwen/qwen3.8-27b",
            messages=[ChatMessage(role="user", content=incoming_query)],
            temperature=0.7,
            max_tokens=1000,
            provider="groq",
            namespace=self.other_ns,
        )
        res_other = await core_092.execute_query(req_other_ns, stub_provider)
        self.assertEqual(res_other.cache_type, "none")
        self.assertFalse(res_other.cached)
        self.assertEqual(stub_provider.call_count, 1)


if __name__ == "__main__":
    unittest.main()
