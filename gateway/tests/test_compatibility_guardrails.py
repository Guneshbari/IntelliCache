"""
Unit and adversarial tests for deterministic semantic compatibility guardrails.
Verifies Stage 3.1.0 requirements:
- SemanticCompatibilityMetadata contract validation and lifecycle states (KNOWN, UNKNOWN, NOT_APPLICABLE).
- CompatibilityExtractor deterministic extraction, version parsing, constraint parsing, and disambiguation.
- SemanticCachePolicy.is_candidate_compatible deterministic rejection reason codes.
- SemanticCachePolicy.evaluate_candidates ordering, 1.0 similarity override prevention, and fallback to compatible candidate.
- SemanticCacheService fail-open on validator exceptions with cancellation preservation.
- Rejection of legacy metadata-free candidates.
"""

import asyncio
from dataclasses import asdict
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.core.contracts import ChatMessage, GenerationRequest
from app.metrics.collector import RuntimeMetricsCollector
from app.semantic_cache.compatibility import (
    CompatibilityExtractor,
    compare_compatibility,
)
from app.semantic_cache.contracts import (
    CompatibilityReasonCode,
    ConstraintItem,
    DimensionValue,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    SemanticCompatibilityMetadata,
    TaskKind,
)
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorRecord, VectorSearchResult


class TestMetadataContractValidation(unittest.TestCase):
    """Verifies schema versioning, dimension lifecycle states, and contract validation."""

    def setUp(self):
        self.valid_gen_contract = GenerationContract(
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
        )
        self.valid_emb_contract = EmbeddingContract(model="nomic-embed-text")
        self.valid_dimensions = {
            "language": DimensionValue(status=MetadataStatus.KNOWN, value="python"),
            "language_version": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "framework": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "framework_version": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "runtime": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "execution_model": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "output_format": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "constraints": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
        }

    def test_valid_metadata_roundtrip(self):
        """Valid metadata serializes to dict and deserializes cleanly without loss."""
        meta = SemanticCompatibilityMetadata(
            schema_version=1,
            extractor_version="1.0",
            task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
            dimensions=self.valid_dimensions,
            generation_contract=self.valid_gen_contract,
            embedding_contract=self.valid_emb_contract,
        )
        as_dict = meta.to_dict()
        self.assertEqual(as_dict["schema_version"], 1)
        self.assertEqual(as_dict["extractor_version"], "1.0")
        self.assertEqual(as_dict["task_kind"]["value"], TaskKind.CODE_GENERATION)
        self.assertEqual(as_dict["dimensions"]["language"]["value"], "python")

        restored = SemanticCompatibilityMetadata.from_dict(as_dict)
        self.assertEqual(restored.schema_version, 1)
        self.assertEqual(restored.dimensions["language"].value, "python")
        self.assertEqual(restored.generation_contract.model, "llama-3.3-70b-versatile")
        self.assertEqual(restored.embedding_contract.model, "nomic-embed-text")

    def test_missing_schema_version_rejected(self):
        """Metadata missing schema_version raises ValueError or fails validation."""
        data = {
            "extractor_version": "1.0",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {},
            "generation_contract": asdict(self.valid_gen_contract),
            "embedding_contract": asdict(self.valid_emb_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data)

    def test_unsupported_schema_version_rejected(self):
        """Unsupported schema_version raises ValueError."""
        data = {
            "schema_version": 999,
            "extractor_version": "1.0",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {},
            "generation_contract": asdict(self.valid_gen_contract),
            "embedding_contract": asdict(self.valid_emb_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data)

    def test_invalid_dimension_status_rejected(self):
        """Invalid lifecycle status raises ValueError."""
        with self.assertRaises(ValueError):
            DimensionValue(status="INVALID_STATUS", value="python")

    def test_known_status_with_none_value_rejected(self):
        """KNOWN status must have a non-None value."""
        with self.assertRaises(ValueError):
            DimensionValue(status=MetadataStatus.KNOWN, value=None)

    def test_not_applicable_with_value_rejected(self):
        """NOT_APPLICABLE status must have value=None."""
        with self.assertRaises(ValueError):
            DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value="something")


class TestCompatibilityExtractor(unittest.TestCase):
    """Verifies deterministic extraction of language, versions, frameworks, constraints, and disambiguation."""

    def setUp(self):
        self.extractor = CompatibilityExtractor()

    def test_extract_python_canonical_and_syntax(self):
        """Extracts python from keyword and code markers."""
        meta1 = self.extractor.extract_from_text("How do I sort a list in Python 3.12?")
        self.assertEqual(meta1.dimensions["language"].value, "python")
        self.assertEqual(meta1.dimensions["language_version"].value, "3.12")

        meta2 = self.extractor.extract_from_text("def process_items(items):\n    return [x * 2 for x in items]")
        self.assertEqual(meta2.dimensions["language"].value, "python")

    def test_extract_javascript_and_typescript(self):
        """Extracts javascript and typescript distinctly."""
        meta_js = self.extractor.extract_from_text("How do I filter an array in JavaScript?")
        self.assertEqual(meta_js.dimensions["language"].value, "javascript")

        meta_ts = self.extractor.extract_from_text("How do I define a generic interface in TypeScript?")
        self.assertEqual(meta_ts.dimensions["language"].value, "typescript")

    def test_go_disambiguation_guards(self):
        """Correctly identifies Go programming vs ordinary English usage."""
        meta_go1 = self.extractor.extract_from_text("How to write a goroutine in Golang?")
        self.assertEqual(meta_go1.dimensions["language"].value, "go")

        meta_go2 = self.extractor.extract_from_text("How to use channels with Go?")
        self.assertEqual(meta_go2.dimensions["language"].value, "go")

        # English prose should NOT be tagged as Go
        meta_eng = self.extractor.extract_from_text("How to go through a list of items?")
        self.assertNotEqual(meta_eng.dimensions["language"].value, "go")

    def test_r_disambiguation_guards(self):
        """Disambiguates R language from single letter 'r'."""
        meta_r = self.extractor.extract_from_text("How to create a data.frame in R?")
        self.assertEqual(meta_r.dimensions["language"].value, "r")

        # Letter r in ordinary English
        meta_eng = self.extractor.extract_from_text("Replace character 'r' with 's' in a string")
        self.assertNotEqual(meta_eng.dimensions["language"].value, "r")

    def test_c_disambiguation_guards(self):
        """Disambiguates C from C++ and C#."""
        meta_c = self.extractor.extract_from_text("How to allocate memory with malloc in C?")
        self.assertEqual(meta_c.dimensions["language"].value, "c")

        meta_cpp = self.extractor.extract_from_text("How to use std::vector in C++?")
        self.assertEqual(meta_cpp.dimensions["language"].value, "c++")

        meta_cs = self.extractor.extract_from_text("How to use LINQ queries in C#?")
        self.assertEqual(meta_cs.dimensions["language"].value, "c#")

    def test_framework_and_ecosystem_extraction(self):
        """Extracts frameworks and associates canonical ecosystem language."""
        meta_react = self.extractor.extract_from_text("How to use useEffect in React 18?")
        self.assertEqual(meta_react.dimensions["framework"].value, "react")
        self.assertEqual(meta_react.dimensions["framework_version"].value, "18")
        self.assertEqual(meta_react.dimensions["language"].value, "javascript")

        meta_fastapi = self.extractor.extract_from_text("How to declare path parameters in FastAPI?")
        self.assertEqual(meta_fastapi.dimensions["framework"].value, "fastapi")
        self.assertEqual(meta_fastapi.dimensions["language"].value, "python")

    def test_execution_model_extraction(self):
        """Extracts synchronous vs asynchronous execution model."""
        meta_async = self.extractor.extract_from_text("How to fetch data asynchronously with asyncio in Python?")
        self.assertEqual(meta_async.dimensions["execution_model"].value, "async")

        meta_sync = self.extractor.extract_from_text("How to write a synchronous file reader in Python?")
        self.assertEqual(meta_sync.dimensions["execution_model"].value, "sync")

    def test_constraints_extraction(self):
        """Extracts case sensitivity, hidden items, traversal, and mutation constraints."""
        meta_mut = self.extractor.extract_from_text("Sort a list in Python in-place without allocating a new list")
        const_dim = meta_mut.dimensions["constraints"]
        self.assertEqual(const_dim.status, MetadataStatus.KNOWN)
        self.assertTrue(any(c.key == "mutation" and c.value == "in_place" for c in const_dim.value))

        meta_copy = self.extractor.extract_from_text("Sort a list in Python and return a new list copy")
        const_dim2 = meta_copy.dimensions["constraints"]
        self.assertEqual(const_dim2.status, MetadataStatus.KNOWN)
        self.assertTrue(any(c.key == "mutation" and c.value == "immutable" for c in const_dim2.value))

    def test_contradictory_constraints_yield_unknown(self):
        """Contradictory constraints within query fail closed to UNKNOWN."""
        meta_contra = self.extractor.extract_from_text("Sort this in-place and return a new immutable list copy in Python")
        self.assertEqual(meta_contra.dimensions["constraints"].status, MetadataStatus.UNKNOWN)

    def test_output_format_extraction(self):
        """Extracts explicit JSON output requirement."""
        meta_json = self.extractor.extract_from_text("Generate the response formatted as strict JSON")
        self.assertEqual(meta_json.dimensions["output_format"].value, "json")


class TestCandidateCompatibilityRules(unittest.TestCase):
    """Table-driven unit tests for deterministic compatibility rules in compare_compatibility."""

    def setUp(self):
        self.policy = SemanticCachePolicy()
        self.extractor = self.policy.extractor

    def _make_req(self, text: str, **kwargs) -> GenerationRequest:
        defaults = {
            "provider": "groq",
            "model": "llama-3.3-70b-versatile",
            "temperature": 0.7,
            "max_tokens": 1000,
            "stream": False,
            "bypass_cache": False,
            "namespace": "default",
        }
        defaults.update(kwargs)
        return GenerationRequest(
            messages=[ChatMessage(role="user", content=text)],
            **defaults,
        )

    def _make_cand(self, text: str, cached_response: str = "ok", **kwargs) -> VectorSearchResult:
        meta_dict = kwargs.get("metadata")
        if meta_dict is None:
            meta_dict = self.extractor.extract_from_text(
                text=text,
                provider=kwargs.get("provider", "groq"),
                model=kwargs.get("model", "llama-3.3-70b-versatile"),
                temperature=float(kwargs.get("temperature", 0.7)),
                max_tokens=int(kwargs.get("max_tokens", 1000)),
                configured_model=self.policy.configured_model,
            ).to_dict()

        return VectorSearchResult(
            cache_namespace=kwargs.get("namespace", "default"),
            fingerprint=f"fp_{abs(hash(text))}",
            query_text=text,
            model=kwargs.get("model_name", self.policy.configured_model),
            distance=kwargs.get("distance", 0.05),
            similarity=kwargs.get("similarity", 0.95),
            cached_response=cached_response,
            metadata=meta_dict,
        )

    def test_language_mismatch_rejected(self):
        """Python request cannot reuse JavaScript candidate."""
        req = self._make_req("How do I filter an array of numbers in Python?")
        cand = self._make_cand("How do I filter an array of numbers in JavaScript?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.LANGUAGE_MISMATCH, reason)

    def test_framework_mismatch_rejected(self):
        """React request cannot reuse Vue candidate."""
        req = self._make_req("How to implement conditional rendering in React?")
        cand = self._make_cand("How to implement conditional rendering in Vue?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.FRAMEWORK_MISMATCH, reason)

    def test_language_version_mismatch_rejected(self):
        """Python 3.12 request cannot reuse Python 3.10 candidate."""
        req = self._make_req("How to use type parameter syntax in Python 3.12?")
        cand = self._make_cand("How to use type annotations in Python 3.10?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH, reason)

    def test_execution_model_mismatch_rejected(self):
        """Async request cannot reuse sync candidate."""
        req = self._make_req("How to read files asynchronously with asyncio in Python?")
        cand = self._make_cand("How to read files synchronously in Python?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.EXECUTION_MODEL_MISMATCH, reason)

    def test_constraint_mismatch_in_place_vs_copy_rejected(self):
        """In-place sort cannot reuse new list copy candidate."""
        req = self._make_req("How do I sort a list in Python in-place without allocating a new list?")
        cand = self._make_cand("How do I sort a list in Python and return a new sorted copy?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.CONSTRAINT_MISMATCH, reason)

    def test_output_format_mismatch_rejected(self):
        """Strict JSON output cannot reuse plain text candidate."""
        req = self._make_req("Parse this log entry in Python and output the result as strict JSON")
        cand = self._make_cand("Parse this log entry in Python and output the result as plain text")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.OUTPUT_FORMAT_MISMATCH, reason)

    def test_legacy_metadata_missing_rejected(self):
        """Candidate with legacy metadata missing schema_version is deterministically rejected."""
        req = self._make_req("How do I reverse a string in Python?")
        cand = self._make_cand(
            "How do I reverse a string in Python?",
            metadata={"llm_provider": "groq", "llm_model": "llama-3.3-70b-versatile"},
        )
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.LEGACY_METADATA_MISSING, reason)

    def test_case_sensitivity_mismatch_rejected(self):
        """Case-sensitive query cannot reuse case-insensitive candidate."""
        req = self._make_req("How do I search for a pattern in Python with case-sensitive matching?")
        cand = self._make_cand("How do I search for a pattern in Python with case-insensitive matching?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.CONSTRAINT_MISMATCH, reason)

    def test_hidden_items_mismatch_rejected(self):
        """Including hidden files cannot reuse excluding hidden files candidate."""
        req = self._make_req("How do I list files in Python including hidden files?")
        cand = self._make_cand("How do I list files in Python excluding hidden files?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.CONSTRAINT_MISMATCH, reason)

    def test_code_generation_without_language_ineligible(self):
        """Code generation request without specified language fails closed during eligibility check."""
        req = self._make_req("Write a function to implement binary search.")
        elig = self.policy.check_eligibility(req)
        self.assertFalse(elig.eligible)
        self.assertIn("programming language", elig.reason.lower())

    def test_conceptual_query_without_language_eligible(self):
        """Conceptual question without language is eligible (language is NOT_APPLICABLE)."""
        req = self._make_req("What is recursion and how does the call stack work?")
        elig = self.policy.check_eligibility(req)
        self.assertTrue(elig.eligible)


class TestCandidateEvaluationOrderingAndAdversarial(unittest.TestCase):
    """Verifies that 1.0 similarity cannot override incompatibility, and lower compatible candidates are chosen."""

    def setUp(self):
        self.policy = SemanticCachePolicy(min_similarity=0.82, min_margin=0.03)
        self.extractor = self.policy.extractor

    def _make_req(self, text: str) -> GenerationRequest:
        return GenerationRequest(
            messages=[ChatMessage(role="user", content=text)],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            stream=False,
            bypass_cache=False,
            namespace="default",
        )

    def _make_cand(self, text: str, resp: str, sim: float) -> VectorSearchResult:
        meta_dict = self.extractor.extract_from_text(
            text=text,
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            configured_model=self.policy.configured_model,
        ).to_dict()

        return VectorSearchResult(
            cache_namespace="default",
            fingerprint=f"fp_{abs(hash(text))}",
            query_text=text,
            model=self.policy.configured_model,
            distance=1.0 - sim,
            similarity=sim,
            cached_response=resp,
            metadata=meta_dict,
        )

    def test_similarity_1_0_incompatible_is_rejected(self):
        """A candidate with similarity 1.0 but mismatched language must NOT be accepted."""
        req = self._make_req("How do I filter an array of numbers in Python?")
        cand_incompat = self._make_cand("How do I filter an array of numbers in JavaScript?", "arr.filter()", 1.0)

        winning, reason = self.policy.evaluate_candidates([cand_incompat], req)
        self.assertIsNone(winning)
        self.assertIn("No compatible candidates", reason)

    def test_incompatible_top_candidate_falls_through_to_lower_compatible_candidate(self):
        """Top candidate (0.99) is incompatible JS, lower candidate (0.92) is compatible Python.

        Lower candidate must be safely selected!
        """
        req = self._make_req("How do I reverse a string in Python?")
        cand_top_incompat = self._make_cand(
            "How do I reverse a string in JavaScript?",
            "s.split('').reverse().join('')",
            0.99,
        )
        cand_lower_compat = self._make_cand(
            "What is the syntax to reverse a string in Python with slicing?",
            "s[::-1]",
            0.92,
        )

        winning, reason = self.policy.evaluate_candidates([cand_top_incompat, cand_lower_compat], req)
        self.assertIsNotNone(winning)
        self.assertIsNone(reason)
        self.assertEqual(winning.cached_response, "s[::-1]")


class TestValidatorFailureFailOpen(unittest.IsolatedAsyncioTestCase):
    """Verifies that an unexpected validator exception in SemanticCacheService.lookup()

    fails open to normal LLM generation and does not swallow asyncio.CancelledError.
    """

    async def test_validator_exception_fails_open_and_increments_metric(self):
        """Injected validator exception records failure metric and returns None to proceed with provider."""
        metrics_collector = RuntimeMetricsCollector()
        mock_provider = AsyncMock()
        mock_provider.model_name = "nomic-embed-text"
        mock_provider.dimensions = 768
        mock_provider.embed.return_value = MagicMock(vector=tuple([0.1] * 768), model="nomic-embed-text", dimensions=768)

        mock_vector_store = AsyncMock()
        mock_vector_store.model = "nomic-embed-text"
        mock_vector_store.dimensions = 768
        mock_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_err",
            query_text="How to sort in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="sorted(x)",
            metadata={"schema_version": 1},
        )
        mock_vector_store.search.return_value = [mock_cand]

        policy = SemanticCachePolicy()
        service = SemanticCacheService(
            embedding_provider=mock_provider,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=metrics_collector,
        )

        req = GenerationRequest(
            messages=[ChatMessage(role="user", content="How to sort in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
        )

        # Inject exception into evaluate_candidates
        with patch.object(policy, "evaluate_candidates", side_effect=RuntimeError("Simulated validator internal error")):
            result = await service.lookup(req)
            self.assertIsNone(result)

        # Verify stats and metrics recorded validator failure
        self.assertEqual(service.stats.validator_failures, 1)
        snap = metrics_collector.get_namespace_snapshot("default")
        self.assertEqual(snap.semantic.validator_failures, 1)
        await service.close()

    async def test_cancelled_error_is_not_swallowed(self):
        """asyncio.CancelledError in lookup() is never swallowed and re-raises."""
        mock_provider = AsyncMock()
        mock_provider.model_name = "nomic-embed-text"
        mock_provider.dimensions = 768
        mock_vector_store = AsyncMock()
        policy = SemanticCachePolicy()

        service = SemanticCacheService(
            embedding_provider=mock_provider,
            vector_store=mock_vector_store,
            policy=policy,
        )

        req = GenerationRequest(
            messages=[ChatMessage(role="user", content="How to sort in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
        )

        with patch.object(mock_provider, "embed", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await service.lookup(req)

        await service.close()


if __name__ == "__main__":
    unittest.main()
