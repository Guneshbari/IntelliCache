"""
Unit and integration tests for semantic cache evaluation, benchmarking,
model-identity validation, and savings accounting.
"""

import asyncio
from dataclasses import dataclass
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.cache.exact_cache import ExactCache
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
from scripts.evaluate_semantic_cache import (
    CandidateData,
    DeterministicMockEmbeddingProvider,
    EvaluationCase,
    calculate_cosine_similarity,
    compute_metrics_from_counts,
    evaluate_dataset,
    evaluate_single_case,
    load_dataset,
    run_threshold_sweep,
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


if __name__ == "__main__":
    unittest.main()
