"""
Unit and integration tests for semantic cache evaluation, benchmarking,
model-identity validation, and savings accounting.
"""

import asyncio
from dataclasses import dataclass
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import asyncpg

from app.cache.exact_cache import ExactCache
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.service import IntelliCacheCore
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.embeddings.errors import EmbeddingTimeoutError
from app.embeddings.interfaces import EmbeddingProvider
from app.semantic_cache.contracts import SemanticHit
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import (
    VectorInvalidError,
    VectorRecord,
    VectorSearchResult,
    cosine_distance_to_similarity,
)
from app.vector_store.migration import check_pgvector_available, run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore
from scripts.evaluate_semantic_cache import (
    CandidateData,
    DeterministicMockEmbeddingProvider,
    EvaluationCase,
    EvaluationMetrics,
    EvaluationOutcome,
    calculate_cosine_similarity,
    compute_candidate_fingerprint,
    compute_latency_stats,
    compute_metrics_from_counts,
    evaluate_dataset,
    evaluate_single_case,
    load_dataset,
    redact_database_url,
    run_benchmark,
    run_threshold_sweep,
    save_reports,
    validate_test_database_url,
)


class TestSemanticEvaluation(unittest.IsolatedAsyncioTestCase):
    """Test suite covering offline semantic evaluation correctness and safety."""

    def setUp(self) -> None:
        self.dataset_path = Path(__file__).resolve().parent.parent / "evaluation" / "dataset.json"

    def test_compute_metrics_from_counts(self) -> None:
        """Verifies confusion matrix metric computations with known predictions."""
        # 1. Perfect predictions
        p, r, f1, fpr, rej, acc = compute_metrics_from_counts(tp=10, fp=0, fn=0, tn=10)
        self.assertEqual(p, 1.0)
        self.assertEqual(r, 1.0)
        self.assertEqual(f1, 1.0)
        self.assertEqual(fpr, 0.0)
        self.assertEqual(rej, 0.5)
        self.assertEqual(acc, 1.0)

        # 2. Known mixed outcomes
        # TP=8, FP=2, FN=4, TN=16 (Total=30)
        # Precision = 8 / 10 = 0.80
        # Recall = 8 / 12 = 0.6667
        # FPR = 2 / 18 = 0.1111
        # Rejection = 20 / 30 = 0.6667
        # Accuracy = 24 / 30 = 0.80
        p, r, f1, fpr, rej, acc = compute_metrics_from_counts(tp=8, fp=2, fn=4, tn=16)
        self.assertAlmostEqual(p, 0.80, places=4)
        self.assertAlmostEqual(r, 8 / 12, places=4)
        self.assertAlmostEqual(fpr, 2 / 18, places=4)
        self.assertAlmostEqual(rej, 20 / 30, places=4)
        self.assertAlmostEqual(acc, 0.80, places=4)

        # 3. Safe against division by zero
        p, r, f1, fpr, rej, acc = compute_metrics_from_counts(tp=0, fp=0, fn=0, tn=0)
        self.assertEqual(p, 0.0)
        self.assertEqual(r, 0.0)
        self.assertEqual(f1, 0.0)
        self.assertEqual(fpr, 0.0)
        self.assertEqual(rej, 0.0)
        self.assertEqual(acc, 0.0)

    def test_dataset_schema_and_split_separation(self) -> None:
        """Verifies dataset structure, ID uniqueness, split balance, and category counts."""
        self.assertTrue(self.dataset_path.is_file(), f"Dataset file missing at {self.dataset_path}")
        cases = load_dataset(self.dataset_path)

        # Total counts
        self.assertGreaterEqual(len(cases), 100)

        dev_cases = [c for c in cases if c.split == "development"]
        test_cases = [c for c in cases if c.split == "test"]
        self.assertGreaterEqual(len(dev_cases), 50)
        self.assertGreaterEqual(len(test_cases), 50)

        # Strict ID disjointness between development and test
        dev_ids = {c.id for c in dev_cases}
        test_ids = {c.id for c in test_cases}
        self.assertEqual(len(dev_ids.intersection(test_ids)), 0)

        # Check positive paraphrases count >= 30
        pos_cases = [c for c in cases if c.category == "positive_paraphrase"]
        self.assertGreaterEqual(len(pos_cases), 30)

        # Check hard negatives count >= 50
        neg_categories = {
            "different_language", "version_difference", "error_difference",
            "constraint_difference", "runtime_difference", "output_difference"
        }
        hard_neg_cases = [c for c in cases if c.category in neg_categories]
        self.assertGreaterEqual(len(hard_neg_cases), 50)

        # Check ambiguous cases
        amb_cases = [c for c in cases if c.category == "ambiguous_candidates"]
        self.assertGreaterEqual(len(amb_cases), 10)

        # Check unrelated cases
        unr_cases = [c for c in cases if c.category == "unrelated_technical"]
        self.assertGreaterEqual(len(unr_cases), 10)

        # Validate candidates structure
        for c in cases:
            self.assertTrue(c.query.strip())
            self.assertGreaterEqual(len(c.candidates), 1)
            for cand in c.candidates:
                self.assertTrue(cand.id.strip())
                self.assertTrue(cand.query_text.strip())
                self.assertTrue(cand.cached_response.strip())
                self.assertTrue(cand.answer_label.strip())

    async def test_positive_paraphrase_evaluation(self) -> None:
        """Verifies safe reuse on positive paraphrase case."""
        case = EvaluationCase(
            id="test-pos",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="py_reverse",
            rationale="Identical intent",
            candidates=[
                CandidateData(
                    id="cand-1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="Use s[::-1]",
                    answer_label="py_reverse",
                    prompt_tokens=100,
                    completion_tokens=200,
                    total_tokens=300,
                    original_latency_ms=1000.0,
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.75, min_margin=0.03)

        outcome = await evaluate_single_case(case, policy, provider)
        self.assertEqual(outcome.classification, "TP")
        self.assertEqual(outcome.decision, "safe_reuse")
        self.assertEqual(outcome.accepted_answer_label, "py_reverse")
        self.assertGreaterEqual(outcome.top_similarity, 0.75)
        self.assertEqual(outcome.total_tokens, 300)

    async def test_hard_negative_rejection_on_changed_language(self) -> None:
        """Verifies rejection when candidate language is different (Python vs JS)."""
        case = EvaluationCase(
            id="test-neg-lang",
            query="How do I filter an array of numbers in Python?",
            category="different_language",
            split="development",
            expected_answer_label=None,
            rationale="Python query cannot reuse JavaScript answer",
            candidates=[
                CandidateData(
                    id="cand-js",
                    query_text="How do I filter an array of numbers in JavaScript?",
                    cached_response="Use arr.filter(x => x > 0)",
                    answer_label="js_array_filter",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.82, min_margin=0.03)

        outcome = await evaluate_single_case(case, policy, provider)
        self.assertEqual(outcome.classification, "TN")
        self.assertEqual(outcome.decision, "safe_rejection")
        self.assertIsNone(outcome.accepted_candidate_id)
        self.assertIn("below threshold", outcome.rejection_reason.lower())

    async def test_hard_negative_rejection_on_changed_constraints(self) -> None:
        """Verifies rejection when candidate has opposite in-place vs copy constraint."""
        case = EvaluationCase(
            id="test-neg-con",
            query="How do I sort a list in Python in-place without allocating a new list?",
            category="constraint_difference",
            split="development",
            expected_answer_label=None,
            rationale="Candidate allocates a new sorted copy instead of in-place mutation",
            candidates=[
                CandidateData(
                    id="cand-copy",
                    query_text="How do I sort a list in Python and return a new sorted copy?",
                    cached_response="Use sorted(items)",
                    answer_label="py_sorted_copy",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.82, min_margin=0.03)

        outcome = await evaluate_single_case(case, policy, provider)
        self.assertEqual(outcome.classification, "TN")
        self.assertEqual(outcome.decision, "safe_rejection")
        self.assertIsNone(outcome.accepted_candidate_id)

    async def test_ambiguity_rejection(self) -> None:
        """Verifies ambiguity rejection when two candidates have close similarities with different answers."""
        case = EvaluationCase(
            id="test-amb",
            query="How do I clear the terminal screen?",
            category="ambiguous_candidates",
            split="development",
            expected_answer_label=None,
            rationale="Ambiguous between Windows cls and Linux clear",
            candidates=[
                CandidateData(
                    id="cand-win",
                    query_text="How do I clear the terminal screen in Windows command prompt?",
                    cached_response="Run cls",
                    answer_label="win_cls",
                ),
                CandidateData(
                    id="cand-linux",
                    query_text="How do I clear the terminal screen on Linux bash?",
                    cached_response="Run clear",
                    answer_label="linux_clear",
                ),
            ],
        )

        class AmbiguousMockProvider(EmbeddingProvider):
            @property
            def model_name(self) -> str:
                return "nomic-embed-text"

            @property
            def dimensions(self) -> int:
                return 768

            async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
                # Return vectors that produce very close similarities (> 0.85, diff < 0.01)
                if "Windows" in text:
                    vec = [0.99] + [0.0] * 767
                elif "Linux" in text:
                    vec = [0.985] + [0.0] * 767
                else:
                    vec = [1.0] + [0.0] * 767
                return EmbeddingResult(vector=tuple(vec), model="nomic-embed-text", dimensions=768)

            async def close(self) -> None:
                pass

        provider = AmbiguousMockProvider()
        policy = SemanticCachePolicy(min_similarity=0.82, min_margin=0.03)

        outcome = await evaluate_single_case(case, policy, provider)
        self.assertEqual(outcome.classification, "TN")
        self.assertEqual(outcome.decision, "safe_rejection")
        self.assertIsNotNone(outcome.rejection_reason)
        self.assertIn("ambiguous", outcome.rejection_reason.lower())

    async def test_threshold_sweep_execution(self) -> None:
        """Verifies that parameter grid sweep computes metrics across thresholds and margins."""
        cases = [
            EvaluationCase(
                id="pos-1",
                query="How to reverse a string in Python using slicing?",
                category="positive_paraphrase",
                split="development",
                expected_answer_label="py_reverse",
                rationale="",
                candidates=[
                    CandidateData(
                        id="c1",
                        query_text="What is the slice syntax to reverse a string in Python?",
                        cached_response="s[::-1]",
                        answer_label="py_reverse",
                    )
                ],
            ),
            EvaluationCase(
                id="neg-1",
                query="How do I filter an array of numbers in Python?",
                category="different_language",
                split="development",
                expected_answer_label=None,
                rationale="",
                candidates=[
                    CandidateData(
                        id="c2",
                        query_text="How do I filter an array of numbers in JavaScript?",
                        cached_response="arr.filter()",
                        answer_label="js_array_filter",
                    )
                ],
            ),
        ]
        provider = DeterministicMockEmbeddingProvider()
        thresholds = [0.70, 0.85, 0.95]
        margins = [0.02, 0.05]

        sweep_res = await run_threshold_sweep(cases, provider, thresholds, margins, split="development")
        self.assertEqual(len(sweep_res), len(thresholds) * len(margins))
        for row in sweep_res:
            self.assertIn("threshold", row)
            self.assertIn("margin", row)
            self.assertIn("f1_score", row)
            self.assertIn("precision", row)
            self.assertIn("recall", row)

    async def test_model_identity_validation_lookup(self) -> None:
        """Verifies that SemanticCacheService fails open when embedding model differs during lookup."""
        mock_provider = AsyncMock(spec=EmbeddingProvider)
        mock_provider.embed.return_value = EmbeddingResult(
            vector=(0.1,) * 768,
            model="incompatible-model-v2",  # Mismatched model!
            dimensions=768,
        )
        mock_store = AsyncMock()

        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        service = SemanticCacheService(
            embedding_provider=mock_provider,
            vector_store=mock_store,
            policy=policy,
        )

        request = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
        )

        hit = await service.lookup(request)
        # Fail open: returns None and vector_store is never queried
        self.assertIsNone(hit)
        mock_store.search.assert_not_called()
        self.assertEqual(service.stats.embedding_failures, 1)

    async def test_model_identity_validation_indexing(self) -> None:
        """Verifies that SemanticCacheService rejects indexing when embedding model differs."""
        mock_provider = AsyncMock(spec=EmbeddingProvider)
        mock_provider.embed.return_value = EmbeddingResult(
            vector=(0.1,) * 768,
            model="wrong-model",  # Mismatched model!
            dimensions=768,
        )
        mock_store = AsyncMock()

        policy = SemanticCachePolicy(configured_model="nomic-embed-text")
        service = SemanticCacheService(
            embedding_provider=mock_provider,
            vector_store=mock_store,
            policy=policy,
        )

        request = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
        )
        result = GenerationResult(
            text="Use s[::-1]",
            model="llama-3.3-70b-versatile",
            usage=UsageMetrics(),
            latency_ms=500.0,
            cached=False,
            fingerprint="fp123",
        )

        success = await service.index(request, result)
        self.assertFalse(success)
        mock_store.upsert.assert_not_called()
        self.assertEqual(service.stats.indexing_failures, 1)

    async def test_embedding_failure_handling(self) -> None:
        """Verifies safe error handling when embedding provider fails."""
        mock_provider = AsyncMock(spec=EmbeddingProvider)
        mock_provider.embed.side_effect = EmbeddingTimeoutError("Timeout contacting Ollama", provider="ollama")
        mock_store = AsyncMock()

        service = SemanticCacheService(
            embedding_provider=mock_provider,
            vector_store=mock_store,
            policy=SemanticCachePolicy(),
        )

        request = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
        )

        hit = await service.lookup(request)
        self.assertIsNone(hit)
        self.assertEqual(service.stats.embedding_failures, 1)

    def test_rejection_of_invalid_vectors(self) -> None:
        """Verifies rejection of non-numeric, NaN, Infinity, and empty vectors in contracts."""
        with self.assertRaises(VectorInvalidError):
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp1",
                query_text="query",
                vector=[float("nan")] * 768,
                model="nomic-embed-text",
            )

        with self.assertRaises(VectorInvalidError):
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp2",
                query_text="query",
                vector=[float("inf")] * 768,
                model="nomic-embed-text",
            )

        with self.assertRaises(VectorInvalidError):
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp3",
                query_text="query",
                vector=[],
                model="nomic-embed-text",
            )

    async def test_no_accidental_llm_inference_during_offline_evaluation(self) -> None:
        """Verifies that offline evaluation runs with zero LLM API provider calls."""
        cases = [
            EvaluationCase(
                id="pos-check-no-llm",
                query="How to reverse a string in Python?",
                category="positive_paraphrase",
                split="development",
                expected_answer_label="py_reverse",
                rationale="",
                candidates=[
                    CandidateData(
                        id="c1",
                        query_text="What is the slice syntax to reverse a string in Python?",
                        cached_response="s[::-1]",
                        answer_label="py_reverse",
                    )
                ],
            )
        ]
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.82)

        with patch("app.core.service.IntelliCacheCore") as mock_core:
            metrics, outcomes = await evaluate_dataset(cases, policy, provider, split="development")
            mock_core.assert_not_called()
            self.assertEqual(len(outcomes), 1)

    async def test_semantic_hit_usage_and_savings_accounting(self) -> None:
        """Verifies that semantic cache hits preserve original cached usage metrics and record savings."""
        cache = ExactCache(max_entries=100)
        mock_provider = AsyncMock()
        mock_provider.name = "mock_provider"
        mock_semantic = AsyncMock()
        mock_semantic.lookup.return_value = None

        # Semantic hit returning original generation token usage and latency
        mock_semantic.lookup.return_value = SemanticHit(
            cached_response="Cached answer text",
            fingerprint="candidate_fp_999",
            distance=0.05,
            similarity=0.95,
            model="nomic-embed-text",
            query_text="Paraphrased query",
            prompt_tokens=150,
            completion_tokens=250,
            total_tokens=400,
            original_latency_ms=1500.0,
        )

        core = IntelliCacheCore(
            cache=cache,
            semantic_cache=mock_semantic,
            cache_enabled=True,
        )

        request = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
        )

        result = await core.execute_query(request, mock_provider)

        # 1. Verification of returned result
        self.assertTrue(result.cached)
        self.assertEqual(result.cache_type, "semantic")
        self.assertEqual(result.text, "Cached answer text")
        # Usage must contain original tokens, NOT zeros!
        self.assertEqual(result.usage.prompt_tokens, 150)
        self.assertEqual(result.usage.completion_tokens, 250)
        self.assertEqual(result.usage.total_tokens, 400)

        # 2. Upstream provider must NOT have been called
        mock_provider.generate.assert_not_called()

        # 3. Cache stats must reflect recorded savings without double counting
        stats = await cache.stats()
        self.assertEqual(stats.hits, 1)
        self.assertEqual(stats.total_tokens_saved, 400)
        self.assertGreater(stats.total_latency_saved_ms, 0.0)

    async def test_exact_cache_behavior_unchanged(self) -> None:
        """Verifies that exact cache hit takes precedence before semantic lookup."""
        cache = ExactCache(max_entries=100)
        mock_provider = AsyncMock()
        mock_provider.name = "groq"
        mock_semantic = AsyncMock()
        mock_semantic.lookup.return_value = None

        core = IntelliCacheCore(
            cache=cache,
            semantic_cache=mock_semantic,
            cache_enabled=True,
        )

        request = GenerationRequest(
            messages=[ChatMessage(role="user", content="Exact query text")],
            provider="groq",
            model="llama-3.3-70b-versatile",
        )

        # 1. Miss -> provider called
        mock_provider.generate.return_value = GenerationResult(
            text="Initial generation",
            model="llama-3.3-70b-versatile",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            latency_ms=500.0,
            cached=False,
        )
        res1 = await core.execute_query(request, mock_provider)
        self.assertFalse(res1.cached)
        mock_provider.generate.assert_called_once()

        # Reset mock before second identical request
        mock_semantic.reset_mock()
        mock_provider.reset_mock()

        # 2. Identical second request -> Exact hit
        res2 = await core.execute_query(request, mock_provider)
        self.assertTrue(res2.cached)
        self.assertEqual(res2.cache_type, "exact")
        self.assertEqual(res2.text, "Initial generation")
        # Exact cache hit: neither provider nor semantic lookup should be invoked
        mock_semantic.lookup.assert_not_called()
        mock_provider.generate.assert_not_called()

    def test_load_dataset_rejects_duplicate_ids(self) -> None:
        """Verifies load_dataset rejects datasets containing duplicate case IDs."""
        bad_data = [
            {"id": "dup-1", "split": "development", "category": "positive_paraphrase", "query": "test query 1", "expected_answer_label": "l1", "candidates": [{"id": "c1", "query_text": "cand1", "cached_response": "resp1", "answer_label": "l1"}]},
            {"id": "dup-1", "split": "development", "category": "positive_paraphrase", "query": "test query 2", "expected_answer_label": "l1", "candidates": [{"id": "c2", "query_text": "cand2", "cached_response": "resp2", "answer_label": "l1"}]},
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bad_data, f)
            tpath = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tpath)
            self.assertIn("Duplicate case id", str(ctx.exception))
        finally:
            Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_invalid_split_or_category(self) -> None:
        """Verifies load_dataset rejects unknown splits or categories."""
        bad_split = [
            {"id": "c-1", "split": "staging", "category": "positive_paraphrase", "query": "test query 1", "candidates": [{"id": "c1", "query_text": "cand1", "cached_response": "resp1", "answer_label": "l1"}]}
        ]
        bad_cat = [
            {"id": "c-2", "split": "development", "category": "unsupported_chat", "query": "test query 1", "candidates": [{"id": "c1", "query_text": "cand1", "cached_response": "resp1", "answer_label": "l1"}]}
        ]
        import tempfile
        for data, expected_err in [(bad_split, "invalid split"), (bad_cat, "invalid category")]:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(data, f)
                tpath = f.name
            try:
                with self.assertRaises(ValueError) as ctx:
                    load_dataset(tpath)
                self.assertIn(expected_err, str(ctx.exception).lower())
            finally:
                Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_empty_query_or_candidates(self) -> None:
        """Verifies load_dataset rejects empty queries or empty candidates lists."""
        bad_q = [
            {"id": "c-1", "split": "development", "category": "positive_paraphrase", "query": "  ", "candidates": [{"id": "c1", "query_text": "cand1", "cached_response": "resp1", "answer_label": "l1"}]}
        ]
        bad_cands = [
            {"id": "c-2", "split": "development", "category": "positive_paraphrase", "query": "valid query", "candidates": []}
        ]
        import tempfile
        for data in [bad_q, bad_cands]:
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                json.dump(data, f)
                tpath = f.name
            try:
                with self.assertRaises(ValueError):
                    load_dataset(tpath)
            finally:
                Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_missing_candidate_fields(self) -> None:
        """Verifies load_dataset rejects candidates with missing required fields."""
        bad_c = [
            {"id": "c-1", "split": "development", "category": "positive_paraphrase", "query": "valid query", "candidates": [{"id": "c1", "query_text": "cand1", "cached_response": ""}]}
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bad_c, f)
            tpath = f.name
        try:
            with self.assertRaises(ValueError):
                load_dataset(tpath)
        finally:
            Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_missing_positive_answer_label(self) -> None:
        """Verifies positive case fails validation if expected_answer_label is not in candidates."""
        bad_positive = [
            {
                "id": "c-pos-bad",
                "split": "development",
                "category": "positive_paraphrase",
                "query": "valid query text",
                "expected_answer_label": "expected_class_a",
                "candidates": [
                    {"id": "cand-1", "query_text": "cand query", "cached_response": "response", "answer_label": "mismatched_class_b"}
                ],
            }
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(bad_positive, f)
            tpath = f.name
        try:
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tpath)
            self.assertIn("does not appear in any candidate", str(ctx.exception))
        finally:
            Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_overlapping_dev_and_test_ids(self) -> None:
        """Verifies load_dataset rejects datasets where development and test IDs overlap."""
        overlapping = [
            {"id": "shared-id-01", "split": "development", "category": "positive_paraphrase", "query": "valid query text 1", "expected_answer_label": "l1", "candidates": [{"id": "c1", "query_text": "q1", "cached_response": "r1", "answer_label": "l1"}]},
            {"id": "shared-id-01", "split": "test", "category": "positive_paraphrase", "query": "valid query text 2", "expected_answer_label": "l2", "candidates": [{"id": "c2", "query_text": "q2", "cached_response": "r2", "answer_label": "l2"}]},
        ]
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(overlapping, f)
            tpath = f.name
        try:
            with self.assertRaises(ValueError):
                load_dataset(tpath)
        finally:
            Path(tpath).unlink(missing_ok=True)

    async def test_threshold_sweep_strictly_restricted_to_development(self) -> None:
        """Verifies that run_threshold_sweep ignores test cases even if requested."""
        cases = [
            EvaluationCase(
                id="dev-c1", query="query text", category="positive_paraphrase", split="development",
                expected_answer_label="l1", rationale="",
                candidates=[CandidateData(id="c1", query_text="cand query", cached_response="resp", answer_label="l1")],
            ),
            EvaluationCase(
                id="test-c1", query="query text", category="positive_paraphrase", split="test",
                expected_answer_label="l1", rationale="",
                candidates=[CandidateData(id="c2", query_text="cand query", cached_response="resp", answer_label="l1")],
            ),
        ]
        provider = DeterministicMockEmbeddingProvider()
        # Even if caller passes split="test", the function must force development
        results = await run_threshold_sweep(cases, provider, thresholds=[0.82], margins=[0.03], split="test")
        self.assertEqual(len(results), 1)
        # Total cases evaluated should be 1 (the dev case only), not 2
        total_eval = results[0]["tp"] + results[0]["fp"] + results[0]["fn"] + results[0]["tn"]
        self.assertEqual(total_eval, 1)

    async def test_savings_accounting_distinguishes_safe_and_hazardous_reuse(self) -> None:
        """Verifies that safe reuse (TP) and incorrect reuse (FP) are accounted separately."""
        cases = [
            # 1. Positive case that will match (TP)
            EvaluationCase(
                id="pos-safe", query="How to reverse a string in Python?", category="positive_paraphrase", split="development",
                expected_answer_label="py_rev", rationale="",
                candidates=[CandidateData(id="c1", query_text="What is the slice syntax to reverse a string in Python?", cached_response="s[::-1]", answer_label="py_rev", total_tokens=300)],
            ),
            # 2. Negative case forced to match a candidate (FP)
            EvaluationCase(
                id="neg-unsafe", query="How to reverse a string in Python?", category="different_language", split="development",
                expected_answer_label=None, rationale="",
                candidates=[CandidateData(id="c2", query_text="What is the slice syntax to reverse a string in Python?", cached_response="s[::-1]", answer_label="py_rev", total_tokens=200)],
            ),
        ]
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(min_similarity=0.70)  # low threshold so both match

        metrics, outcomes = await evaluate_dataset(cases, policy, provider, split="development")
        self.assertEqual(metrics.tp, 1)
        self.assertEqual(metrics.fp, 1)
        self.assertEqual(metrics.safe_reuse_count, 1)
        self.assertEqual(metrics.incorrect_reuse_count, 1)
        self.assertEqual(metrics.safe_provider_calls_avoided, 1)
        self.assertEqual(metrics.hazardous_provider_calls_avoided, 1)
        self.assertEqual(metrics.total_accepted_hits, 2)
        # Tokens saved should ONLY contain the TP tokens (300), NOT 300+200
        self.assertEqual(metrics.safe_tokens_saved, 300)
        # Hazardous tokens must track the FP tokens separately
        self.assertEqual(metrics.hazardous_tokens_involved, 200)

    async def test_benchmark_offline_component_computes_actual_similarities(self) -> None:
        """Verifies that run_benchmark computes real cosine similarity rather than hardcoded 0.9."""
        from scripts.evaluate_semantic_cache import run_benchmark
        case = EvaluationCase(
            id="bench-case", query="How to reverse string?", category="positive_paraphrase", split="development",
            expected_answer_label="l1", rationale="",
            candidates=[CandidateData(id="c1", query_text="How to reverse string?", cached_response="resp", answer_label="l1")],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy()

        bench_res = await run_benchmark([case], policy, provider, split="development", iterations=2, benchmark_mode="component")
        self.assertEqual(bench_res["benchmark_mode"], "offline_component")
        self.assertIn("Synthetic/mock", bench_res["benchmark_label"])
        self.assertGreater(bench_res["sample_count"], 0)
        self.assertIn("query_embedding_latency_ms", bench_res)
        self.assertIn("candidate_sim_and_ranking_latency_ms", bench_res)
        self.assertIn("total_measured_latency_ms", bench_res)

    def test_get_dataset_hash(self) -> None:
        """Verifies SHA-256 hash computation for dataset files."""
        from scripts.evaluate_semantic_cache import get_dataset_hash
        h = get_dataset_hash(self.dataset_path)
        self.assertEqual(len(h), 64)

    def test_validate_test_database_url(self) -> None:
        """Verifies TEST_DATABASE_URL validation and safety enforcement."""
        # Valid URL containing 'test' in db name
        valid = "postgresql://user:secret@localhost:5432/intellicache_test"
        self.assertEqual(validate_test_database_url(valid), valid)

        # Missing or empty URL
        with self.assertRaises(ValueError) as ctx:
            validate_test_database_url(None)
        self.assertIn("TEST_DATABASE_URL must be explicitly configured", str(ctx.exception))

        with self.assertRaises(ValueError) as ctx:
            validate_test_database_url("   ")
        self.assertIn("TEST_DATABASE_URL must be explicitly configured", str(ctx.exception))

        # Rejects non-test database name
        non_test_urls = [
            "postgresql://user:secret@localhost:5432/production_db",
            "postgresql://user:secret@localhost:5432/intellicache",
            "postgresql://user:secret@localhost:5432/postgres",
        ]
        for bad_url in non_test_urls:
            with self.assertRaises(ValueError) as ctx:
                validate_test_database_url(bad_url)
            self.assertIn("must contain 'test'", str(ctx.exception))

    def test_redact_database_url(self) -> None:
        """Verifies redaction of sensitive credentials in database URLs."""
        url = "postgresql://cache_admin:super_secret_pw@db.internal:5432/intellicache_test"
        redacted = redact_database_url(url)
        self.assertEqual(redacted, "postgresql://***:***@db.internal:5432/intellicache_test")
        self.assertNotIn("cache_admin", redacted)
        self.assertNotIn("super_secret_pw", redacted)

        # Empty string
        self.assertEqual(redact_database_url(""), "")

    def test_compute_candidate_fingerprint_deterministic_and_unique(self) -> None:
        """Verifies deterministic and collision-resistant fingerprint hashing."""
        fp1 = compute_candidate_fingerprint("hash_abc", "case_1", "cand_1")
        fp2 = compute_candidate_fingerprint("hash_abc", "case_1", "cand_1")
        self.assertEqual(fp1, fp2)
        self.assertEqual(len(fp1), 64)

        fp_diff = compute_candidate_fingerprint("hash_abc", "case_1", "cand_2")
        self.assertNotEqual(fp1, fp_diff)

    def test_compute_latency_stats(self) -> None:
        """Verifies latency distribution statistics, sample counts, and small-sample warnings."""
        data = [10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0]
        stats = compute_latency_stats(data)
        self.assertEqual(stats["sample_count"], 10)
        self.assertEqual(stats["min"], 10.0)
        self.assertEqual(stats["mean"], 55.0)
        self.assertEqual(stats["p50"], 60.0)
        self.assertGreaterEqual(stats["p95"], 90.0)
        self.assertGreaterEqual(stats["p99"], 90.0)
        self.assertNotIn("warning", stats)

        # Small sample size triggers warning
        small_data = [12.0, 15.0, 18.0]
        small_stats = compute_latency_stats(small_data)
        self.assertEqual(small_stats["sample_count"], 3)
        self.assertIn("warning", small_stats)
        self.assertIn("Sample size < 10", small_stats["warning"])

        # Empty data safety
        empty_stats = compute_latency_stats([])
        self.assertEqual(empty_stats["sample_count"], 0)
        self.assertEqual(empty_stats["min"], 0.0)
        self.assertEqual(empty_stats["mean"], 0.0)
        self.assertEqual(empty_stats["p50"], 0.0)
        self.assertNotIn("warning", empty_stats)

    async def test_indexing_latency_separated_from_lookup_and_does_not_reduce_savings(self) -> None:
        """
        Verifies Stage A (candidate indexing) is timed separately from Stage B (lookup),
        and candidate indexing time does not penalize safe latency saved calculation.
        """
        case = EvaluationCase(
            id="latency-sep-test",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="py_reverse",
            rationale="Test latency separation",
            candidates=[
                CandidateData(
                    id="cand-sep-1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="s[::-1]",
                    answer_label="py_reverse",
                    original_latency_ms=1000.0,
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider(model="nomic-embed-text", dimensions=768)
        policy = SemanticCachePolicy(min_similarity=0.75, configured_model="nomic-embed-text")

        outcome = await evaluate_single_case(case, policy, provider)
        self.assertGreater(outcome.indexing_latency_ms, 0.0)
        self.assertGreater(outcome.lookup_latency_ms, 0.0)
        self.assertEqual(outcome.classification, "TP")

        metrics, outcomes = await evaluate_dataset([case], policy, provider, split="development")
        expected_saved = round(max(0.0, 1000.0 - outcomes[0].lookup_latency_ms), 2)
        self.assertEqual(metrics.safe_latency_saved_ms, expected_saved)
        # Latency saved must NOT be reduced by indexing_latency_ms
        self.assertGreater(metrics.safe_latency_saved_ms, 1000.0 - outcomes[0].lookup_latency_ms - outcomes[0].indexing_latency_ms)

    async def test_pgvector_insertion_order_exact_cache_before_vector(self) -> None:
        """Verifies strict foreign-key insertion order: exact cache entry before vector upsert."""
        call_order = []

        mock_cache = AsyncMock()
        async def fake_cache_set(*args, **kwargs):
            call_order.append("cache.set")
        mock_cache.set.side_effect = fake_cache_set
        mock_cache.clear = AsyncMock()

        mock_vector_store = AsyncMock()
        async def fake_vector_upsert(*args, **kwargs):
            call_order.append("vector_store.upsert")
        mock_vector_store.upsert.side_effect = fake_vector_upsert
        mock_vector_store.clear = AsyncMock()
        mock_vector_store.search.return_value = []

        case = EvaluationCase(
            id="test-order",
            query="Test query",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="l1",
            rationale="",
            candidates=[
                CandidateData(
                    id="c1",
                    query_text="Cand query",
                    cached_response="Resp",
                    answer_label="l1",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy()

        await evaluate_single_case(
            case,
            policy,
            provider,
            cache=mock_cache,
            vector_store=mock_vector_store,
            dataset_hash="hash123",
            run_id="run456",
        )

        self.assertEqual(call_order, ["cache.set", "vector_store.upsert"])
        mock_cache.set.assert_called_once()
        mock_vector_store.upsert.assert_called_once()

    async def test_pgvector_namespace_isolation_and_search_invocation(self) -> None:
        """Verifies that vector search is scoped to the run-specific case namespace."""
        mock_cache = AsyncMock()
        mock_vector_store = AsyncMock()
        mock_vector_store.search.return_value = []

        case = EvaluationCase(
            id="test-ns-case",
            query="Isolation test query",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="l1",
            rationale="",
            candidates=[
                CandidateData(
                    id="c1",
                    query_text="Cand query",
                    cached_response="Resp",
                    answer_label="l1",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")

        await evaluate_single_case(
            case,
            policy,
            provider,
            cache=mock_cache,
            vector_store=mock_vector_store,
            dataset_hash="hash123",
            run_id="run789",
        )

        expected_ns = "eval_run789_test-ns-case"
        search_kwargs = mock_vector_store.search.call_args.kwargs
        self.assertEqual(search_kwargs.get("namespace"), expected_ns)
        self.assertEqual(search_kwargs.get("top_k"), policy.top_k)
        self.assertEqual(search_kwargs.get("model"), "nomic-embed-text")

    async def test_pgvector_cleanup_on_success_and_exception(self) -> None:
        """Verifies that namespace-scoped cleanup is executed in finally block."""
        mock_cache = AsyncMock()
        mock_vector_store = AsyncMock()
        mock_vector_store.search.return_value = []

        case = EvaluationCase(
            id="test-clean",
            query="Clean test query",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="l1",
            rationale="",
            candidates=[
                CandidateData(id="c1", query_text="Cand query", cached_response="Resp", answer_label="l1")
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy()

        # 1. Successful run triggers cleanup
        await evaluate_single_case(
            case, policy, provider, cache=mock_cache, vector_store=mock_vector_store,
            dataset_hash="h1", run_id="r1",
        )
        expected_ns = "eval_r1_test-clean"
        mock_vector_store.clear.assert_called_with(expected_ns)
        mock_cache.clear.assert_called_with(expected_ns)

        # 2. Exception during search still triggers cleanup
        mock_vector_store.clear.reset_mock()
        mock_cache.clear.reset_mock()
        mock_vector_store.search.side_effect = RuntimeError("Simulated search failure")

        with self.assertRaises(RuntimeError):
            await evaluate_single_case(
                case, policy, provider, cache=mock_cache, vector_store=mock_vector_store,
                dataset_hash="h1", run_id="r2",
            )
        expected_ns2 = "eval_r2_test-clean"
        mock_vector_store.clear.assert_called_with(expected_ns2)
        mock_cache.clear.assert_called_with(expected_ns2)

    async def test_pgvector_benchmark_measures_all_five_latencies(self) -> None:
        """Verifies that pgvector benchmark measures and returns all required latency breakdown metrics."""
        mock_cache = AsyncMock()
        mock_vector_store = AsyncMock()
        mock_vector_store.search.return_value = []

        case = EvaluationCase(
            id="bench-pgv",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="l1",
            rationale="",
            candidates=[
                CandidateData(
                    id="c1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="s[::-1]",
                    answer_label="l1",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider()
        policy = SemanticCachePolicy()

        db_info = {
            "postgres_version": "17.0",
            "pgvector_version": "0.8.7",
            "database_url": "postgresql://***:***@127.0.0.1:5433/intellicache_test",
        }

        bench_res = await run_benchmark(
            [case],
            policy,
            provider,
            split="development",
            iterations=2,
            benchmark_mode="pgvector",
            cache=mock_cache,
            vector_store=mock_vector_store,
            dataset_hash="hash999",
            db_info=db_info,
        )

        self.assertEqual(bench_res["benchmark_mode"], "pgvector")
        self.assertIn("PostgreSQL/pgvector", bench_res["benchmark_label"])
        self.assertEqual(bench_res["iterations"], 2)
        self.assertEqual(bench_res["sample_count"], 2)
        self.assertIn("per_metric_sample_counts", bench_res)
        self.assertEqual(bench_res["per_metric_sample_counts"]["candidate_indexing"], 1)
        self.assertEqual(bench_res["per_metric_sample_counts"]["query_embedding"], 2)
        self.assertEqual(bench_res["per_metric_sample_counts"]["postgres_retrieval"], 2)
        self.assertEqual(bench_res["per_metric_sample_counts"]["policy_evaluation"], 2)
        self.assertEqual(bench_res["per_metric_sample_counts"]["semantic_lookup"], 2)

        # Verify all 5 separated latency measurement dictionaries
        for key in [
            "candidate_indexing_latency_ms",
            "query_embedding_latency_ms",
            "postgres_retrieval_latency_ms",
            "policy_evaluation_latency_ms",
            "semantic_lookup_latency_ms",
        ]:
            self.assertIn(key, bench_res)
            stats = bench_res[key]
            self.assertIn("min", stats)
            self.assertIn("mean", stats)
            self.assertIn("p50", stats)
            self.assertIn("p95", stats)
            self.assertIn("p99", stats)
            self.assertGreater(stats["sample_count"], 0)
            self.assertIn("warning", stats)

        self.assertEqual(bench_res["database_info"], db_info)

    def test_save_reports_serializes_database_info_and_benchmark_mode(self) -> None:
        """Verifies report serialization records pgvector provenance, labels, and safety status."""
        metrics = EvaluationMetrics(
            total_cases=2,
            tp=2,
            fp=0,
            fn=0,
            tn=0,
            precision=1.0,
            recall=1.0,
            f1_score=1.0,
            safe_reuse_count=2,
            safe_tokens_saved=740,
        )
        outcomes = [
            EvaluationOutcome(
                case_id="c1",
                category="positive_paraphrase",
                split="development",
                expected_answer_label="l1",
                safe_reuse_expected=True,
                accepted_candidate_id="cand1",
                accepted_answer_label="l1",
                top_similarity=0.95,
                margin=None,
                decision="safe_reuse",
                classification="TP",
                rejection_reason=None,
                indexing_latency_ms=15.2,
                lookup_latency_ms=6.8,
            )
        ]
        meta = {
            "dataset_hash": "a" * 64,
            "dataset_split": "development",
            "benchmark_mode": "pgvector",
            "embedding_provider": "OllamaEmbeddingProvider",
            "embedding_model": "nomic-embed-text",
            "vector_index_type": "hnsw",
            "vector_index_available": True,
            "vector_index_used": True,
            "retrieval_method": "HNSW approximate nearest-neighbour search",
            "database_info": {
                "postgres_version": "17.0",
                "pgvector_version": "0.8.7",
                "database_url": "postgresql://***:***@localhost:5433/intellicache_test",
            },
        }

        with tempfile.TemporaryDirectory() as tmpdir:
            json_p, csv_p = save_reports(metrics, outcomes, None, None, tmpdir, meta)
            self.assertTrue(json_p.is_file())
            self.assertTrue(csv_p.is_file())

            with open(json_p, "r", encoding="utf-8") as f:
                data = json.load(f)
            rep_meta = data["report_metadata"]
            self.assertEqual(rep_meta["benchmark_mode"], "pgvector")
            self.assertEqual(rep_meta["vector_index_type"], "hnsw")
            self.assertTrue(rep_meta["vector_index_available"])
            self.assertTrue(rep_meta["vector_index_used"])
            self.assertEqual(rep_meta["retrieval_method"], "HNSW approximate nearest-neighbour search")
            self.assertEqual(rep_meta["database_info"]["pgvector_version"], "0.8.7")
            self.assertIn("Safe: Zero false positives", rep_meta["safety_status"])
            self.assertEqual(data["outcomes"][0]["indexing_latency_ms"], 15.2)
            self.assertEqual(data["outcomes"][0]["lookup_latency_ms"], 6.8)

            with open(csv_p, "r", encoding="utf-8") as f:
                csv_lines = f.readlines()
            self.assertTrue(any("VectorIndex: hnsw" in line for line in csv_lines))
            self.assertTrue(any("indexing_latency_ms" in line for line in csv_lines))


class TestMockPgvectorSemanticEvaluationIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Integration tests verifying PostgreSQL vector persistence, pgvector ANN retrieval,
    exact-cache JOIN, and policy evaluation using deterministic mock embeddings.
    Only runs when TEST_DATABASE_URL is explicitly set and points to an active
    PostgreSQL instance with the pgvector extension installed.
    """

    async def asyncSetUp(self) -> None:
        raw_url = os.environ.get("TEST_DATABASE_URL")
        self.is_explicit = raw_url is not None
        if not self.is_explicit:
            self.skipTest("TEST_DATABASE_URL not set; skipping live PostgreSQL pgvector integration tests.")

        try:
            self.db_url = validate_test_database_url(raw_url)
        except ValueError as exc:
            self.fail(f"Invalid TEST_DATABASE_URL: {exc}")

        safe_url = redact_database_url(self.db_url)
        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                has_vector = await check_pgvector_available(conn)
                if not has_vector:
                    self.fail(f"pgvector extension 'vector' is not available at {safe_url}")

            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=768)

            self.cache = PostgresCache(pool=self.pool, default_namespace="test_eval_ns")
            self.store = PostgresVectorStore(
                pool=self.pool,
                default_namespace="test_eval_ns",
                default_model="nomic-embed-text",
                dimensions=768,
            )
        except Exception as exc:
            self.fail(f"Live pgvector setup failed for TEST_DATABASE_URL ({safe_url}): {exc}")

    async def asyncTearDown(self) -> None:
        if hasattr(self, "pool") and self.pool is not None:
            await self.pool.close()

    async def test_live_pgvector_evaluation_positive_case(self) -> None:
        """Verifies genuine PostgreSQL vector upsert, search, policy evaluation, and cleanup."""
        case = EvaluationCase(
            id="pgv-pos-test",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="py_reverse",
            rationale="Identical semantic intent",
            candidates=[
                CandidateData(
                    id="cand-1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="Use s[::-1]",
                    answer_label="py_reverse",
                    prompt_tokens=100,
                    completion_tokens=200,
                    total_tokens=300,
                    original_latency_ms=1000.0,
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider(model="nomic-embed-text", dimensions=768)
        policy = SemanticCachePolicy(min_similarity=0.75, configured_model="nomic-embed-text")

        outcome = await evaluate_single_case(
            case,
            policy,
            provider,
            cache=self.cache,
            vector_store=self.store,
            dataset_hash="integration_hash",
            run_id="test_run",
        )

        self.assertEqual(outcome.classification, "TP")
        self.assertEqual(outcome.decision, "safe_reuse")
        self.assertEqual(outcome.accepted_answer_label, "py_reverse")
        self.assertGreaterEqual(outcome.top_similarity, 0.75)

        # Verify namespace cleanup: case namespace must be completely empty
        case_ns = "eval_test_run_pgv-pos-test"
        async with self.pool.acquire() as conn:
            vec_count = await conn.fetchval(
                "SELECT COUNT(*) FROM vector_records WHERE cache_namespace = $1", case_ns
            )
            cache_count = await conn.fetchval(
                "SELECT COUNT(*) FROM cache_entries WHERE cache_namespace = $1", case_ns
            )
        self.assertEqual(vec_count, 0)
        self.assertEqual(cache_count, 0)

    async def test_live_pgvector_evaluation_negative_case(self) -> None:
        """Verifies genuine PostgreSQL vector search rejects hard negative below threshold."""
        case = EvaluationCase(
            id="pgv-neg-test",
            query="How do I filter an array in Python?",
            category="different_language",
            split="development",
            expected_answer_label=None,
            rationale="Cannot reuse JS answer for Python query",
            candidates=[
                CandidateData(
                    id="cand-js",
                    query_text="How do I filter an array in JavaScript?",
                    cached_response="arr.filter(...)",
                    answer_label="js_array_filter",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider(model="nomic-embed-text", dimensions=768)
        policy = SemanticCachePolicy(min_similarity=0.82, configured_model="nomic-embed-text")

        outcome = await evaluate_single_case(
            case,
            policy,
            provider,
            cache=self.cache,
            vector_store=self.store,
            dataset_hash="integration_hash",
            run_id="test_run",
        )

        self.assertEqual(outcome.classification, "TN")
        self.assertEqual(outcome.decision, "safe_rejection")
        self.assertIsNone(outcome.accepted_candidate_id)

        # Verify namespace cleanup
        case_ns = "eval_test_run_pgv-neg-test"
        async with self.pool.acquire() as conn:
            vec_count = await conn.fetchval(
                "SELECT COUNT(*) FROM vector_records WHERE cache_namespace = $1", case_ns
            )
        self.assertEqual(vec_count, 0)

    async def test_live_pgvector_benchmark_execution(self) -> None:
        """Verifies running genuine pgvector benchmarking across multiple iterations."""
        case = EvaluationCase(
            id="pgv-bench-case",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="py_reverse",
            rationale="",
            candidates=[
                CandidateData(
                    id="cand-bench-1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="s[::-1]",
                    answer_label="py_reverse",
                )
            ],
        )
        provider = DeterministicMockEmbeddingProvider(model="nomic-embed-text", dimensions=768)
        policy = SemanticCachePolicy(configured_model="nomic-embed-text")

        db_info = {
            "postgres_version": "17.0",
            "pgvector_version": "0.8.7",
            "database_url": redact_database_url(self.db_url),
        }

        bench_res = await run_benchmark(
            [case],
            policy,
            provider,
            split="development",
            iterations=3,
            benchmark_mode="pgvector",
            cache=self.cache,
            vector_store=self.store,
            dataset_hash="integration_hash",
            db_info=db_info,
        )

        self.assertEqual(bench_res["benchmark_mode"], "pgvector")
        self.assertEqual(bench_res["sample_count"], 3)
        self.assertGreater(bench_res["postgres_retrieval_latency_ms"]["mean"], 0.0)
        self.assertGreater(bench_res["candidate_indexing_latency_ms"]["mean"], 0.0)
        self.assertGreater(bench_res["semantic_lookup_latency_ms"]["mean"], 0.0)


class TestRealOllamaPgvectorIntegration(unittest.IsolatedAsyncioTestCase):
    """
    Opt-in end-to-end integration tests using real OllamaEmbeddingProvider with nomic-embed-text
    against live PostgreSQL/pgvector database.
    Guarded by environment variable: OLLAMA_INTEGRATION_TEST=true.
    Fails fast with clear error message if enabled and Ollama or PostgreSQL is unreachable.
    """

    async def asyncSetUp(self) -> None:
        enabled = os.environ.get("OLLAMA_INTEGRATION_TEST", "").strip().lower() in {"true", "1", "yes"}
        if not enabled:
            self.skipTest("Opt-in test: set OLLAMA_INTEGRATION_TEST=true to run real Ollama model integration.")

        raw_url = os.environ.get("TEST_DATABASE_URL")
        if not raw_url:
            self.fail("OLLAMA_INTEGRATION_TEST=true requires TEST_DATABASE_URL to be set.")

        try:
            self.db_url = validate_test_database_url(raw_url)
        except ValueError as exc:
            self.fail(f"Invalid TEST_DATABASE_URL: {exc}")

        ollama_url = os.environ.get("EMBEDDING_BASE_URL", "http://127.0.0.1:11434")
        from app.embeddings.ollama_adapter import OllamaEmbeddingProvider
        self.provider = OllamaEmbeddingProvider(model="nomic-embed-text", base_url=ollama_url)
        try:
            await self.provider.embed("ping", task=EmbeddingTask.QUERY)
        except Exception as exc:
            await self.provider.close()
            self.fail(f"Ollama integration test enabled, but Ollama provider at {ollama_url} failed: {exc}")

        safe_url = redact_database_url(self.db_url)
        try:
            self.pool = await asyncpg.create_pool(self.db_url, min_size=1, max_size=5)
            async with self.pool.acquire() as conn:
                has_vector = await check_pgvector_available(conn)
                if not has_vector:
                    self.fail(f"pgvector extension 'vector' is not available at {safe_url}")

            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=self.provider.dimensions)

            self.cache = PostgresCache(pool=self.pool, default_namespace="test_real_eval_ns")
            self.store = PostgresVectorStore(
                pool=self.pool,
                default_namespace="test_real_eval_ns",
                default_model="nomic-embed-text",
                dimensions=self.provider.dimensions,
            )
        except Exception as exc:
            self.fail(f"Real Ollama pgvector setup failed for TEST_DATABASE_URL ({safe_url}): {exc}")

    async def asyncTearDown(self) -> None:
        if hasattr(self, "provider") and self.provider is not None:
            await self.provider.close()
        if hasattr(self, "pool") and self.pool is not None:
            await self.pool.close()

    async def test_real_ollama_semantic_retrieval_and_decision(self) -> None:
        """Verifies genuine Ollama embeddings against PostgreSQL vector store."""
        case = EvaluationCase(
            id="real-pos-test",
            query="How to reverse a string in Python?",
            category="positive_paraphrase",
            split="development",
            expected_answer_label="py_reverse",
            rationale="Identical semantic intent with real model embeddings",
            candidates=[
                CandidateData(
                    id="cand-real-1",
                    query_text="What is the slice syntax to reverse a string in Python?",
                    cached_response="Use s[::-1]",
                    answer_label="py_reverse",
                    prompt_tokens=100,
                    completion_tokens=200,
                    total_tokens=300,
                    original_latency_ms=1200.0,
                )
            ],
        )
        policy = SemanticCachePolicy(min_similarity=0.75, configured_model="nomic-embed-text")

        outcome = await evaluate_single_case(
            case,
            policy,
            self.provider,
            cache=self.cache,
            vector_store=self.store,
            dataset_hash="real_model_hash",
            run_id="real_test_run",
        )

        self.assertEqual(outcome.classification, "TP")
        self.assertEqual(outcome.decision, "safe_reuse")
        self.assertEqual(outcome.accepted_answer_label, "py_reverse")
        self.assertGreaterEqual(outcome.top_similarity, 0.75)
        self.assertGreater(outcome.indexing_latency_ms, 0.0)
        self.assertGreater(outcome.lookup_latency_ms, 0.0)


if __name__ == "__main__":
    unittest.main()
