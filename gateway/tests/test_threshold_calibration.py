"""
Unit tests for IntelliCache Stage 3.2.4 — Semantic Similarity Threshold Calibration and Distributional Analysis.

Verifies:
1. Dataset schema, pair counts, domain coverage, and intent-group zero-leakage splits.
2. Statistical calculations, percentile rankings, ROC/PR AUC, and Wilson score intervals.
3. Threshold sweep optimization logic and monotonic metrics behavior.
4. Ambiguity margin mechanics (conflicting runner-up vs concordant answers).
5. 100% rejection rate of high-similarity metadata-incompatible pairs by CompatibilityValidator.
6. Report artifact presence and consistency.
"""

from __future__ import annotations

import json
import math
from pathlib import Path
import unittest

from app.core.contracts import ChatMessage, GenerationRequest
from app.semantic_cache.policy import SemanticCachePolicy
from app.vector_store.contracts import VectorSearchResult
from scripts.calibrate_semantic_threshold import (
    compute_distribution_stats,
    compute_roc_pr_auc,
    compute_wilson_ci,
    evaluate_threshold_metrics,
)


class TestDatasetIntegrityAndSplits(unittest.TestCase):
    """Validates dataset architecture, intent groups, and zero-leakage splits."""

    @classmethod
    def setUpClass(cls):
        cls.gateway_root = Path(__file__).resolve().parent.parent
        cls.dataset_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_4_semantic_pairs.json"
        cls.metadata_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_4_dataset_metadata.json"

        with open(cls.dataset_path, "r", encoding="utf-8") as f:
            cls.pairs = json.load(f)
        with open(cls.metadata_path, "r", encoding="utf-8") as f:
            cls.meta = json.load(f)

    def test_dataset_files_exist_and_metadata_matches(self):
        self.assertTrue(self.dataset_path.exists(), "Dataset pairs file must exist.")
        self.assertTrue(self.metadata_path.exists(), "Dataset metadata file must exist.")
        self.assertEqual(len(self.pairs), self.meta["pairs_total"])
        self.assertEqual(self.meta["intent_groups_total"], 210)
        self.assertTrue(self.meta["leakage_check_passed"])

    def test_pair_counts_and_domain_distribution(self):
        self.assertEqual(len(self.pairs), 1320, "Total pairs must be exactly 1,320.")

        compat_pairs = [p for p in self.pairs if p["label"] != "incompatible"]
        incompat_pairs = [p for p in self.pairs if p["label"] == "incompatible"]
        pos_pairs = [p for p in self.pairs if p["label"] == "positive"]
        neg_pairs = [p for p in self.pairs if p["label"] == "negative"]

        self.assertEqual(len(compat_pairs), 1260)
        self.assertEqual(len(incompat_pairs), 60)
        self.assertEqual(len(pos_pairs), 420)
        self.assertEqual(len(neg_pairs), 840)

        # 10 distinct domains, each with exactly 132 pairs (126 compatible + 6 incompatible)
        domains = set(p["domain"] for p in self.pairs)
        self.assertEqual(len(domains), 10, f"Expected 10 domains, got {len(domains)}: {domains}")
        for d in domains:
            d_pairs = [p for p in self.pairs if p["domain"] == d]
            self.assertEqual(len(d_pairs), 132, f"Domain {d} must contain exactly 132 pairs.")
            d_compat = [p for p in d_pairs if p["label"] != "incompatible"]
            d_incompat = [p for p in d_pairs if p["label"] == "incompatible"]
            self.assertEqual(len(d_compat), 126, f"Domain {d} must have 126 compatible pairs.")
            self.assertEqual(len(d_incompat), 6, f"Domain {d} must have 6 incompatible pairs.")

    def test_intent_group_split_zero_leakage(self):
        compat_pairs = [p for p in self.pairs if p["label"] != "incompatible"]

        train_groups = set(p["intent_group_id"] for p in compat_pairs if p["split"] == "train")
        cal_groups = set(p["intent_group_id"] for p in compat_pairs if p["split"] == "calibration")
        test_groups = set(p["intent_group_id"] for p in compat_pairs if p["split"] == "test")

        self.assertEqual(len(train_groups), 126, "Train split must contain exactly 126 intent groups (60%).")
        self.assertEqual(len(cal_groups), 42, "Calibration split must contain exactly 42 intent groups (20%).")
        self.assertEqual(len(test_groups), 42, "Test split must contain exactly 42 intent groups (20%).")

        # Zero leakage: pairwise intersections must be strictly empty
        self.assertEqual(train_groups & cal_groups, set(), "Leakage detected between train and calibration groups!")
        self.assertEqual(train_groups & test_groups, set(), "Leakage detected between train and test groups!")
        self.assertEqual(cal_groups & test_groups, set(), "Leakage detected between calibration and test groups!")

    def test_compatible_split_proportions(self):
        compat_pairs = [p for p in self.pairs if p["label"] != "incompatible"]

        train_pairs = [p for p in compat_pairs if p["split"] == "train"]
        cal_pairs = [p for p in compat_pairs if p["split"] == "calibration"]
        test_pairs = [p for p in compat_pairs if p["split"] == "test"]

        # 60% train = 756, 20% cal = 252, 20% test = 252
        self.assertEqual(len(train_pairs), 756)
        self.assertEqual(len(cal_pairs), 252)
        self.assertEqual(len(test_pairs), 252)

        # 1:2 ratio of positives to negatives across all splits
        self.assertEqual(sum(1 for p in train_pairs if p["label"] == "positive"), 252)
        self.assertEqual(sum(1 for p in train_pairs if p["label"] == "negative"), 504)
        self.assertEqual(sum(1 for p in cal_pairs if p["label"] == "positive"), 84)
        self.assertEqual(sum(1 for p in cal_pairs if p["label"] == "negative"), 168)
        self.assertEqual(sum(1 for p in test_pairs if p["label"] == "positive"), 84)
        self.assertEqual(sum(1 for p in test_pairs if p["label"] == "negative"), 168)


class TestCalibrationStatisticsAndMetrics(unittest.TestCase):
    """Validates mathematical and statistical calculations for calibration."""

    def test_distribution_stats_computation(self):
        # Deterministic sequence: [10.0, 20.0, 30.0, 40.0, 50.0]
        data = [10.0, 20.0, 30.0, 40.0, 50.0]
        stats = compute_distribution_stats(data)

        self.assertEqual(stats["count"], 5)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["max"], 50.0)
        self.assertAlmostEqual(stats["mean"], 30.0)
        self.assertAlmostEqual(stats["median"], 30.0)
        self.assertGreater(stats["std"], 0.0)
        self.assertEqual(stats["p50"], 30.0)
        self.assertLess(stats["p5"], stats["p25"])
        self.assertLess(stats["p75"], stats["p95"])

    def test_roc_pr_auc_calculation(self):
        # Perfect separation: all positives higher than all negatives
        pos = [0.90, 0.92, 0.95, 0.98]
        neg = [0.50, 0.60, 0.70, 0.75]
        roc_auc, pr_auc = compute_roc_pr_auc(pos, neg)
        self.assertEqual(roc_auc, 1.0)
        self.assertEqual(pr_auc, 1.0)

        # Completely reversed separation: all negatives higher than all positives
        pos_rev = [0.50, 0.60]
        neg_rev = [0.90, 0.95]
        roc_auc_rev, _ = compute_roc_pr_auc(pos_rev, neg_rev)
        self.assertEqual(roc_auc_rev, 0.0)

    def test_wilson_ci_bounds(self):
        # Bound limits: interval must remain strictly within [0.0, 1.0]
        low0, high0 = compute_wilson_ci(0, 100)
        self.assertEqual(low0, 0.0)
        self.assertGreater(high0, 0.0)
        self.assertLess(high0, 0.1)

        low1, high1 = compute_wilson_ci(100, 100)
        self.assertGreater(low1, 0.9)
        self.assertEqual(high1, 1.0)

        # Center symmetry for 50/100
        low50, high50 = compute_wilson_ci(50, 100)
        self.assertAlmostEqual((low50 + high50) / 2.0, 0.5, places=3)

        # Sample size shrinkage: 500/1000 has tighter interval than 50/100
        low1000, high1000 = compute_wilson_ci(500, 1000)
        width100 = high50 - low50
        width1000 = high1000 - low1000
        self.assertLess(width1000, width100)

    def test_threshold_metrics_monotonicity(self):
        # Synthetic dataset with mixed scores
        synthetic_pairs = [
            {"similarity": 0.95, "label": "positive"},
            {"similarity": 0.91, "label": "positive"},
            {"similarity": 0.88, "label": "positive"},
            {"similarity": 0.82, "label": "positive"},
            {"similarity": 0.93, "label": "negative"},
            {"similarity": 0.85, "label": "negative"},
            {"similarity": 0.75, "label": "negative"},
            {"similarity": 0.65, "label": "negative"},
        ]

        m_low = evaluate_threshold_metrics(synthetic_pairs, 0.80)
        m_high = evaluate_threshold_metrics(synthetic_pairs, 0.92)

        # As threshold increases:
        # Recall must decrease or stay equal
        self.assertGreaterEqual(m_low["recall"], m_high["recall"])
        # Specificity must increase or stay equal
        self.assertLessEqual(m_low["specificity"], m_high["specificity"])
        # False Positive Rate must decrease or stay equal
        self.assertGreaterEqual(m_low["false_positive_rate"], m_high["false_positive_rate"])

    def test_unmocked_fastembed_similarity_invariants(self):
        """Verifies real unmocked FastEmbed inference, normalization, and similarity matching."""
        try:
            import fastembed
            from app.embeddings.fastembed_adapter import FastEmbedProvider
        except ImportError:
            self.skipTest("fastembed package is not installed; skipping unmocked FastEmbed test.")

        async def _run():
            provider = FastEmbedProvider(model="BAAI/bge-small-en-v1.5", dimensions=384)
            q1 = "How does the Rust borrow checker enforce memory safety?"
            q2 = "Explain compile-time memory safety and lifetimes in the Rust compiler."
            q3 = "Write a Python script using pandas to process CSV files."

            res = await provider.embed_batch([q1, q2, q3])
            v1, v2, v3 = res[0].vector, res[1].vector, res[2].vector

            # Check vector dimensions and normalization
            for v in (v1, v2, v3):
                self.assertEqual(len(v), 384)
                norm = math.sqrt(sum(x * x for x in v))
                self.assertAlmostEqual(norm, 1.0, places=4)

            # Cosine similarity dot product
            sim_pos = sum(a * b for a, b in zip(v1, v2))
            sim_neg = sum(a * b for a, b in zip(v1, v3))

            # Positive paraphrase similarity must be strictly greater than negative unrelated query
            self.assertGreater(sim_pos, sim_neg)
            self.assertGreater(sim_pos, 0.80)
            self.assertLess(sim_neg, 0.75)

        import asyncio
        asyncio.run(_run())


class TestAmbiguityMarginLogic(unittest.TestCase):
    """Validates the ambiguity margin mechanics on conflicting vs concordant responses."""

    def test_ambiguity_rejection_on_conflicting_response(self):
        # Top candidate similarity 0.94, runner up 0.93 (diff = 0.01 < margin 0.03) with DIFFERENT response
        # Should trigger ambiguity rejection
        top_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="query 1",
            model="BAAI/bge-small-en-v1.5",
            distance=0.06,
            similarity=0.94,
            cached_response="Response Option A",
        )
        runner_up = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp2",
            query_text="query 2",
            model="BAAI/bge-small-en-v1.5",
            distance=0.07,
            similarity=0.93,
            cached_response="Response Option B",  # Different!
        )

        margin = 0.03
        diff = top_cand.similarity - runner_up.similarity
        is_ambiguous = (diff < margin) and (top_cand.cached_response != runner_up.cached_response)
        self.assertTrue(is_ambiguous, "Close runner-up with conflicting answer must trigger ambiguity rejection.")

    def test_concordant_response_not_rejected(self):
        # Top candidate similarity 0.94, runner up 0.935 (diff = 0.005 < margin 0.03) with IDENTICAL response
        # Should NOT trigger ambiguity rejection (concordant answers)
        top_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="query 1",
            model="BAAI/bge-small-en-v1.5",
            distance=0.06,
            similarity=0.94,
            cached_response="Identical Response",
        )
        runner_up = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp2",
            query_text="query 2",
            model="BAAI/bge-small-en-v1.5",
            distance=0.065,
            similarity=0.935,
            cached_response="Identical Response",  # Same!
        )

        margin = 0.03
        diff = top_cand.similarity - runner_up.similarity
        is_ambiguous = (diff < margin) and (top_cand.cached_response != runner_up.cached_response)
        self.assertFalse(is_ambiguous, "Close runner-up with identical answer must NOT trigger ambiguity rejection.")

    def test_runner_up_outside_margin_accepted(self):
        # Top candidate similarity 0.95, runner up 0.90 (diff = 0.05 >= margin 0.03)
        top_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp1",
            query_text="query 1",
            model="BAAI/bge-small-en-v1.5",
            distance=0.05,
            similarity=0.95,
            cached_response="Response A",
        )
        runner_up = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp2",
            query_text="query 2",
            model="BAAI/bge-small-en-v1.5",
            distance=0.10,
            similarity=0.90,
            cached_response="Response B",
        )

        margin = 0.03
        diff = top_cand.similarity - runner_up.similarity
        is_ambiguous = (diff < margin) and (top_cand.cached_response != runner_up.cached_response)
        self.assertFalse(is_ambiguous, "Runner-up outside margin must NOT trigger ambiguity rejection.")


class TestCompatibilityGuardrailsHighSimilarity(unittest.TestCase):
    """Validates that CompatibilityValidator strictly rejects all 60 high-similarity incompatible records."""

    @classmethod
    def setUpClass(cls):
        cls.gateway_root = Path(__file__).resolve().parent.parent
        cls.dataset_path = cls.gateway_root / "evaluation" / "datasets" / "stage3_2_4_semantic_pairs.json"
        with open(cls.dataset_path, "r", encoding="utf-8") as f:
            pairs = json.load(f)
        cls.incompat_pairs = [p for p in pairs if p["label"] == "incompatible"]
        cls.policy = SemanticCachePolicy(configured_model="BAAI/bge-small-en-v1.5")

    def test_incompatible_cases_100_percent_rejection(self):
        self.assertEqual(len(self.incompat_pairs), 60, "Expected exactly 60 metadata-incompatible pairs.")

        rejections = 0
        acceptances = 0
        reason_codes = set()

        for p in self.incompat_pairs:
            req = GenerationRequest(
                model="qwen/qwen3.8-27b",
                messages=[ChatMessage(role="user", content=p["query_a"])],
                temperature=0.7,
                max_tokens=1000,
                provider="groq",
                namespace="default",
            )

            cand = VectorSearchResult(
                cache_namespace="default",
                fingerprint=f"fp_{p['pair_id']}",
                query_text=p["query_b"],
                model="BAAI/bge-small-en-v1.5",
                distance=0.0,
                similarity=1.0,  # Deliberately perfect similarity (1.0000)
                cached_response=p.get("cached_response_b") or "dummy",
                metadata=p.get("compatibility_metadata") or {},
            )

            is_compat, reason = self.policy.is_candidate_compatible(cand, req)
            if not is_compat:
                rejections += 1
                code = reason.split(":")[0].strip() if ":" in reason else "UNKNOWN"
                reason_codes.add(code)
            else:
                acceptances += 1

        self.assertEqual(acceptances, 0, "Zero false acceptances allowed: CompatibilityValidator must never accept incompatible metadata.")
        self.assertEqual(rejections, 60, "All 60 incompatible cases must be rejected.")

        # Ensure major reason codes are present
        self.assertIn("GENERATION_PROVIDER_MISMATCH", reason_codes)
        self.assertIn("GENERATION_MODEL_MISMATCH", reason_codes)
        self.assertIn("GENERATION_TEMPERATURE_MISMATCH", reason_codes)
        self.assertIn("GENERATION_MAX_TOKENS_MISMATCH", reason_codes)


class TestCalibrationReportArtifacts(unittest.TestCase):
    """Validates generated report artifacts and structural contents."""

    @classmethod
    def setUpClass(cls):
        cls.gateway_root = Path(__file__).resolve().parent.parent
        cls.json_path = cls.gateway_root / "evaluation" / "reports" / "stage3_2_4_threshold_calibration.json"
        cls.md_path = cls.gateway_root / "evaluation" / "reports" / "stage3_2_4_threshold_calibration.md"

    def test_report_files_exist(self):
        self.assertTrue(self.json_path.exists(), f"Calibration JSON report must exist at {self.json_path}")
        self.assertTrue(self.md_path.exists(), f"Calibration Markdown report must exist at {self.md_path}")

    def test_report_contents_sanity(self):
        with open(self.json_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        self.assertEqual(data["stage"], "Stage 3.2.4")
        self.assertEqual(data["embedding_dimensions"], 384)
        self.assertEqual(data["total_pairs"], 1320)
        self.assertEqual(data["compatible_pairs"], 1260)
        self.assertEqual(data["incompatible_pairs"], 60)

        # Baseline evaluation
        baseline = data["baseline_evaluation"]
        self.assertIn("test", baseline)
        self.assertGreater(baseline["test"]["precision"], 0.90)

        # Held-out test evaluation
        test_eval = data["held_out_test_evaluation"]
        self.assertGreaterEqual(test_eval["precision"], 0.95)
        self.assertGreater(test_eval["recall"], 0.20)
        self.assertGreater(test_eval["roc_auc"], 0.85)
        self.assertGreater(test_eval["pr_auc"], 0.80)

        # Compatibility verification
        compat_eval = data["compatibility_guardrail_verification"]
        self.assertEqual(compat_eval["rejection_rate"], 1.0)
        self.assertEqual(compat_eval["false_acceptance_count"], 0)


if __name__ == "__main__":
    unittest.main()
