"""
Unit and adversarial tests for deterministic semantic compatibility guardrails.
Verifies Stage 3.1.1 requirements (FIX-01 to FIX-08):
- FIX-01: Symmetric dimension-state comparison across all material dimensions (no wildcards).
- FIX-02: Strict task-kind and metadata value validation (rejects UNKNOWN candidate task_kind, malformed types, invalid operators).
- FIX-03: Multi-language framework inference safety & word-collision disambiguation guards.
- FIX-04: Deterministic thread-safety constraint extraction and compatibility matching.
- FIX-05: Version fidelity (patch version preservation, runtime_version separation, schema_version 2).
- FIX-06: Problem-group evaluation partition validation and zero partition leakage.
- FIX-07: Single request metadata extraction lifecycle and candidate evaluation determinism.
- FIX-08: End-to-end IntelliCacheCore provider fallback upon validator exception with cancellation propagation.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
import json
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.service import IntelliCacheCore
from app.metrics.collector import RuntimeMetricsCollector
from app.semantic_cache.compatibility import (
    CompatibilityExtractor,
    compare_compatibility,
)
from app.semantic_cache.contracts import (
    CURRENT_SCHEMA_VERSION,
    SUPPORTED_SCHEMA_VERSIONS,
    CompatibilityReasonCode,
    ConstraintItem,
    DimensionValue,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    REQUIRED_DIMENSIONS,
    SemanticCompatibilityMetadata,
    TaskKind,
)
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorSearchResult


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
            "runtime_version": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "dependencies": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "execution_model": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "output_format": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "output_constraints": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "constraints": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "freshness": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
            "context_dependency": DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None),
        }

    def test_valid_metadata_roundtrip(self):
        """Valid metadata serializes to dict and deserializes cleanly without loss."""
        meta = SemanticCompatibilityMetadata(
            schema_version=CURRENT_SCHEMA_VERSION,
            extractor_version="compat-extractor-v2",
            task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
            dimensions=self.valid_dimensions,
            generation_contract=self.valid_gen_contract,
            embedding_contract=self.valid_emb_contract,
        )
        as_dict = meta.to_dict()
        self.assertEqual(as_dict["schema_version"], CURRENT_SCHEMA_VERSION)
        self.assertEqual(as_dict["extractor_version"], "compat-extractor-v2")
        self.assertEqual(as_dict["task_kind"]["value"], TaskKind.CODE_GENERATION)
        self.assertEqual(as_dict["dimensions"]["language"]["value"], "python")
        self.assertIn("runtime_version", as_dict["dimensions"])

        restored = SemanticCompatibilityMetadata.from_dict(as_dict)
        self.assertEqual(restored.schema_version, CURRENT_SCHEMA_VERSION)
        self.assertEqual(restored.dimensions["language"].value, "python")
        self.assertEqual(restored.generation_contract.model, "llama-3.3-70b-versatile")
        self.assertEqual(restored.embedding_contract.model, "nomic-embed-text")

    def test_missing_schema_version_rejected(self):
        """Metadata missing schema_version raises ValueError or fails validation."""
        data = {
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {},
            "generation_contract": asdict(self.valid_gen_contract),
            "embedding_contract": asdict(self.valid_emb_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data)

    def test_unsupported_schema_version_rejected(self):
        """Unsupported schema_version (including legacy v1 and future v999) raises ValueError."""
        for v in (1, 999):
            data = {
                "schema_version": v,
                "extractor_version": "compat-extractor-v2",
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

    def test_unknown_candidate_task_kind_rejected(self):
        """FIX-02: Candidate with UNKNOWN task_kind cannot be accepted."""
        cand_meta = SemanticCompatibilityMetadata(
            schema_version=CURRENT_SCHEMA_VERSION,
            extractor_version="compat-extractor-v2",
            task_kind=DimensionValue(status=MetadataStatus.UNKNOWN, value=None),
            dimensions=self.valid_dimensions,
            generation_contract=self.valid_gen_contract,
            embedding_contract=self.valid_emb_contract,
        )
        req_meta = SemanticCompatibilityMetadata(
            schema_version=CURRENT_SCHEMA_VERSION,
            extractor_version="compat-extractor-v2",
            task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
            dimensions=self.valid_dimensions,
            generation_contract=self.valid_gen_contract,
            embedding_contract=self.valid_emb_contract,
        )
        compat, code, msg = compare_compatibility(req_meta, cand_meta)
        self.assertFalse(compat)
        self.assertEqual(code, CompatibilityReasonCode.INCOMPATIBLE_TASK_KIND)

    def test_unsupported_known_task_kind_rejected(self):
        """FIX-02: Unsupported task_kind value raises ValueError on validate."""
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata(
                schema_version=CURRENT_SCHEMA_VERSION,
                extractor_version="compat-extractor-v2",
                task_kind=DimensionValue(status=MetadataStatus.KNOWN, value="quantum_calculation"),
                dimensions=self.valid_dimensions,
                generation_contract=self.valid_gen_contract,
                embedding_contract=self.valid_emb_contract,
            )

    def test_invalid_dimension_field_types_rejected(self):
        """FIX-02: Non-string dimension values for string dimensions, or non-list for constraints, are rejected."""
        # Non-string value for language
        bad_dims = dict(self.valid_dimensions)
        bad_dims["language"] = DimensionValue(status=MetadataStatus.KNOWN, value=123)
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata(
                schema_version=CURRENT_SCHEMA_VERSION,
                extractor_version="compat-extractor-v2",
                task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
                dimensions=bad_dims,
                generation_contract=self.valid_gen_contract,
                embedding_contract=self.valid_emb_contract,
            )

        # Non-list value for constraints
        bad_dims2 = dict(self.valid_dimensions)
        bad_dims2["constraints"] = DimensionValue(status=MetadataStatus.KNOWN, value="in_place")
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata(
                schema_version=CURRENT_SCHEMA_VERSION,
                extractor_version="compat-extractor-v2",
                task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
                dimensions=bad_dims2,
                generation_contract=self.valid_gen_contract,
                embedding_contract=self.valid_emb_contract,
            )

    def test_invalid_constraint_operator_rejected(self):
        """FIX-02: ConstraintItem with unsupported operator raises ValueError."""
        with self.assertRaises(ValueError):
            ConstraintItem(key="thread_safety", operator="regex", value="thread_safe")

    def test_invalid_generation_parameters_rejected(self):
        """FIX-02: NaN, Inf, negative temperature, or non-positive max_tokens are rejected."""
        with self.assertRaises(ValueError):
            GenerationContract(provider="groq", model="m", temperature=float("nan"), max_tokens=100)
        with self.assertRaises(ValueError):
            GenerationContract(provider="groq", model="m", temperature=float("inf"), max_tokens=100)
        with self.assertRaises(ValueError):
            GenerationContract(provider="groq", model="m", temperature=-0.1, max_tokens=100)
        with self.assertRaises(ValueError):
            GenerationContract(provider="groq", model="m", temperature=0.7, max_tokens=-5)
        with self.assertRaises(ValueError):
            GenerationContract(provider="groq", model="m", temperature=0.7, max_tokens=True)

    def test_schema_version_types_rejected(self):
        """FIX-02: Non-integer schema_version types (strings, bools) are rejected."""
        for bad_ver in ("2", True, None, [2]):
            data = {
                "schema_version": bad_ver,
                "extractor_version": "compat-extractor-v2",
                "task_kind": {"status": "KNOWN", "value": "code_generation"},
                "dimensions": {},
                "generation_contract": asdict(self.valid_gen_contract),
                "embedding_contract": asdict(self.valid_emb_contract),
            }
            with self.assertRaises(ValueError):
                SemanticCompatibilityMetadata.from_dict(data)

    def test_strict_raw_metadata_type_validation_rejected(self):
        """Stage 3.1.2: Validate raw JSON types before coercion to prevent type loopholes."""
        # 1. Boolean temperature rejected
        with self.assertRaises(ValueError):
            GenerationContract.from_dict({
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "temperature": True,
                "max_tokens": 1000,
            })

        # 2. String temperature rejected
        with self.assertRaises(ValueError):
            GenerationContract.from_dict({
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "temperature": "0.7",
                "max_tokens": 1000,
            })

        # 3. Float max_tokens rejected
        with self.assertRaises(ValueError):
            GenerationContract.from_dict({
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.7,
                "max_tokens": 1000.9,
            })

        # 4. String max_tokens rejected
        with self.assertRaises(ValueError):
            GenerationContract.from_dict({
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.7,
                "max_tokens": "1000",
            })

        # 5. Boolean max_tokens rejected
        with self.assertRaises(ValueError):
            GenerationContract.from_dict({
                "provider": "groq",
                "model": "llama-3.3-70b-versatile",
                "temperature": 0.7,
                "max_tokens": True,
            })

        # 6. Non-string ConstraintItem fields rejected
        with self.assertRaises(ValueError):
            ConstraintItem.from_dict({"key": 123, "operator": "equals", "value": "x"})
        with self.assertRaises(ValueError):
            ConstraintItem.from_dict({"key": "thread_safety", "operator": "equals", "value": True})

        # 7. Non-string extractor_version rejected
        data = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "extractor_version": 123,
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {},
            "generation_contract": asdict(self.valid_gen_contract),
            "embedding_contract": asdict(self.valid_emb_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data)

        # 8. Missing or non-dict generation_contract in schema v2 rejected
        data_no_gen = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "extractor_version": "extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {
                k: asdict(DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None))
                for k in REQUIRED_DIMENSIONS
            },
            "embedding_contract": asdict(self.valid_emb_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data_no_gen)

        # 9. Missing or non-dict embedding_contract in schema v2 rejected
        data_no_emb = {
            "schema_version": CURRENT_SCHEMA_VERSION,
            "extractor_version": "extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {
                k: asdict(DimensionValue(status=MetadataStatus.NOT_APPLICABLE, value=None))
                for k in REQUIRED_DIMENSIONS
            },
            "generation_contract": asdict(self.valid_gen_contract),
        }
        with self.assertRaises(ValueError):
            SemanticCompatibilityMetadata.from_dict(data_no_emb)


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

        meta_eng = self.extractor.extract_from_text("How to go through a list of items?")
        self.assertNotEqual(meta_eng.dimensions["language"].value, "go")

    def test_r_disambiguation_guards(self):
        """Disambiguates R language from single letter 'r'."""
        meta_r = self.extractor.extract_from_text("How to create a data.frame in R?")
        self.assertEqual(meta_r.dimensions["language"].value, "r")

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

    def test_framework_unambiguous_inference(self):
        """FIX-03: Unambiguous frameworks infer language safely (FastAPI -> Python, Spring -> Java)."""
        meta_fastapi = self.extractor.extract_from_text("How to declare path parameters in FastAPI?")
        self.assertEqual(meta_fastapi.dimensions["framework"].value, "fastapi")
        self.assertEqual(meta_fastapi.dimensions["language"].value, "python")

        meta_spring = self.extractor.extract_from_text("How to configure Spring Boot bean dependencies?")
        self.assertEqual(meta_spring.dimensions["framework"].value, "spring")
        self.assertEqual(meta_spring.dimensions["language"].value, "java")

    def test_multi_language_framework_requires_explicit_evidence(self):
        """FIX-03: Multi-language frameworks (React, Vue, Next.js, Express) do NOT assume JS for code gen."""
        # Unspecified language yields UNKNOWN for code generation
        meta_react_plain = self.extractor.extract_from_text("Create a React component with conditional rendering")
        self.assertEqual(meta_react_plain.dimensions["framework"].value, "react")
        self.assertEqual(meta_react_plain.dimensions["language"].status, MetadataStatus.UNKNOWN)

        # Explicit TypeScript
        meta_react_ts = self.extractor.extract_from_text("Create a React component with TypeScript")
        self.assertEqual(meta_react_ts.dimensions["framework"].value, "react")
        self.assertEqual(meta_react_ts.dimensions["language"].value, "typescript")

        # Explicit JavaScript
        meta_react_js = self.extractor.extract_from_text("Create a React component in JavaScript")
        self.assertEqual(meta_react_js.dimensions["framework"].value, "react")
        self.assertEqual(meta_react_js.dimensions["language"].value, "javascript")

    def test_framework_word_collision_guards(self):
        """FIX-03: Common English words 'react', 'next', 'express', 'spring' are not misclassified as frameworks."""
        meta_react = self.extractor.extract_from_text("How do I react to window resize events in JavaScript?")
        self.assertEqual(meta_react.dimensions["framework"].status, MetadataStatus.NOT_APPLICABLE)
        self.assertEqual(meta_react.dimensions["language"].value, "javascript")

        meta_next = self.extractor.extract_from_text("What is the next step after git commit in command line?")
        self.assertEqual(meta_next.dimensions["framework"].status, MetadataStatus.NOT_APPLICABLE)

        meta_express = self.extractor.extract_from_text("How to express algorithmic complexity in Python docstring?")
        self.assertEqual(meta_express.dimensions["framework"].status, MetadataStatus.NOT_APPLICABLE)

        meta_spring = self.extractor.extract_from_text("How does spring tension affect mechanical oscillations?")
        self.assertEqual(meta_spring.dimensions["framework"].status, MetadataStatus.NOT_APPLICABLE)

    def test_version_extraction_patch_fidelity(self):
        """FIX-05: Complete patch versions (3.12.1, 18.2.4) are preserved and distinguishable."""
        meta_v1 = self.extractor.extract_from_text("How to configure typing in Python 3.12.1?")
        meta_v2 = self.extractor.extract_from_text("How to configure typing in Python 3.12.9?")
        self.assertEqual(meta_v1.dimensions["language_version"].value, "3.12.1")
        self.assertEqual(meta_v2.dimensions["language_version"].value, "3.12.9")
        self.assertNotEqual(meta_v1.dimensions["language_version"].value, meta_v2.dimensions["language_version"].value)

        meta_react_v1 = self.extractor.extract_from_text("How to use useId in React 18.2.0?")
        meta_react_v2 = self.extractor.extract_from_text("How to use useId in React 18.2.4?")
        self.assertEqual(meta_react_v1.dimensions["framework_version"].value, "18.2.0")
        self.assertEqual(meta_react_v2.dimensions["framework_version"].value, "18.2.4")
        self.assertNotEqual(meta_react_v1.dimensions["framework_version"].value, meta_react_v2.dimensions["framework_version"].value)

    def test_runtime_version_separation(self):
        """FIX-05: Runtime version is stored in runtime_version dimension, separate from language_version."""
        meta_node = self.extractor.extract_from_text("How to run an HTTP server in Node.js 20?")
        self.assertEqual(meta_node.dimensions["runtime"].value, "node")
        self.assertEqual(meta_node.dimensions["runtime_version"].value, "20")
        self.assertEqual(meta_node.dimensions["language_version"].status, MetadataStatus.NOT_APPLICABLE)

    def test_thread_safety_constraint_extraction(self):
        """FIX-04: Extracts thread-safety constraints accurately across explicit patterns."""
        # Positive thread-safe patterns
        for q in (
            "How to implement a thread-safe singleton in Python?",
            "How to implement a singleton using locks in Python?",
            "How to implement a singleton with a mutex in Python?",
            "How to design a thread-safe cache with double-checked locking?",
        ):
            meta = self.extractor.extract_from_text(q)
            const_dim = meta.dimensions["constraints"]
            self.assertEqual(const_dim.status, MetadataStatus.KNOWN, f"Failed for {q}")
            self.assertTrue(
                any(c.key == "thread_safety" and c.value == "thread_safe" for c in const_dim.value),
                f"Missing thread_safe in {q}",
            )

        # Negative non-thread-safe patterns
        for q in (
            "How to implement a basic singleton class in Python?",
            "How to implement a naive singleton in Python?",
            "How to write a simple singleton without locks in Python?",
            "How to implement a not thread-safe singleton in Python?",
            "How to implement a not-thread-safe singleton in Python?",
            "How to implement a non-thread-safe singleton in Python?",
            "How to implement a non thread safe singleton in Python?",
        ):
            meta = self.extractor.extract_from_text(q)
            const_dim = meta.dimensions["constraints"]
            self.assertEqual(const_dim.status, MetadataStatus.KNOWN, f"Failed for {q}")
            self.assertTrue(
                any(c.key == "thread_safety" and c.value == "not_thread_safe" for c in const_dim.value),
                f"Missing not_thread_safe in {q}",
            )

        # Contradictory patterns fail closed to UNKNOWN
        meta_contra = self.extractor.extract_from_text("Implement a thread-safe singleton without locks in Python")
        self.assertEqual(meta_contra.dimensions["constraints"].status, MetadataStatus.UNKNOWN)

        # Non-concurrency query has NOT_APPLICABLE
        meta_plain = self.extractor.extract_from_text("How to reverse a string in Python?")
        self.assertEqual(meta_plain.dimensions["constraints"].status, MetadataStatus.NOT_APPLICABLE)

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
            "visibility": "tenant_shared",
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
            visibility=kwargs.get("visibility", "tenant_shared"),
        )

    def test_language_mismatch_rejected(self):
        """Python request cannot reuse JavaScript candidate."""
        req = self._make_req("How do I filter an array of numbers in Python?")
        cand = self._make_cand("How do I filter an array of numbers in JavaScript?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.LANGUAGE_MISMATCH, reason)

    def test_framework_mismatch_rejected(self):
        """React request with explicit JS cannot reuse Vue candidate."""
        req = self._make_req("How to implement conditional rendering in React with JavaScript?")
        cand = self._make_cand("How to implement conditional rendering in Vue with JavaScript?")
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

    def test_legacy_v1_metadata_rejected(self):
        """FIX-05: Candidate with legacy schema_version=1 is deterministically rejected."""
        req = self._make_req("How do I reverse a string in Python?")
        cand = self._make_cand(
            "How do I reverse a string in Python?",
            metadata={"schema_version": 1, "extractor_version": "compat-extractor-v1"},
        )
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.UNSUPPORTED_SCHEMA_VERSION, reason)

    def test_symmetric_dimension_comparison_api_vs_fastapi(self):
        """FIX-01: Generic Python API request versus FastAPI-specific candidate is rejected."""
        req = self._make_req("How to build an HTTP REST endpoint in Python?")
        cand = self._make_cand("How to build an HTTP REST endpoint in FastAPI with Python?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.FRAMEWORK_MISMATCH, reason)

    def test_symmetric_dimension_comparison_browser_vs_node(self):
        """FIX-01: Browser JavaScript request versus Node.js-specific candidate is rejected."""
        req = self._make_req("How to read URL query parameters in browser JavaScript?")
        cand = self._make_cand("How to read URL query parameters in Node.js JavaScript?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.RUNTIME_MISMATCH, reason)

    def test_symmetric_dimension_comparison_unversioned_vs_versioned(self):
        """FIX-01: Generic unversioned Python request versus Python 3.12-specific candidate is rejected."""
        req = self._make_req("How to define generic classes in Python?")
        cand = self._make_cand("How to define generic classes in Python 3.12?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH, reason)

    def test_symmetric_dimension_comparison_generic_framework_vs_versioned(self):
        """FIX-01: Generic React request versus React 19-specific candidate is rejected."""
        req = self._make_req("How to use actions in React with JavaScript?")
        cand = self._make_cand("How to use actions in React 19 with JavaScript?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH, reason)

    def test_thread_safety_singleton_elimination(self):
        """FIX-04: Thread-safe singleton request versus basic singleton candidate is rejected (neg-con-test-001)."""
        req = self._make_req("How to implement a thread-safe singleton in Python?")
        cand = self._make_cand("How to implement a basic singleton class in Python?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertFalse(is_compat)
        self.assertIn(CompatibilityReasonCode.CONSTRAINT_MISMATCH, reason)

    def test_thread_safety_singleton_compatible(self):
        """FIX-04: Thread-safe singleton request versus thread-safe candidate is compatible."""
        req = self._make_req("How to implement a thread-safe singleton in Python?")
        cand = self._make_cand("How to implement a thread-safe singleton class in Python with locks?")
        is_compat, reason = self.policy.is_candidate_compatible(cand, req)
        self.assertTrue(is_compat)
        self.assertIn(CompatibilityReasonCode.COMPATIBLE, reason)

    def test_constraint_operators_logical_evaluation(self):
        """Stage 3.1.3: Complete 4x4 matrix and empty set tests for constraint operator evaluation."""
        def _make_meta_with_constraints(items: list[ConstraintItem]) -> SemanticCompatibilityMetadata:
            dims = dict(self.extractor.extract_from_text("How to sort in Python?").dimensions)
            dims["constraints"] = DimensionValue(status=MetadataStatus.KNOWN, value=items)
            return SemanticCompatibilityMetadata(
                schema_version=CURRENT_SCHEMA_VERSION,
                extractor_version="compat-extractor-v2",
                task_kind=DimensionValue(status=MetadataStatus.KNOWN, value=TaskKind.CODE_GENERATION),
                dimensions=dims,
                generation_contract=GenerationContract(provider="groq", model="llama-3.3-70b-versatile", temperature=0.7),
                embedding_contract=EmbeddingContract(model="nomic-embed-text"),
            )

        # -------------------------------------------------------------
        # 1. Request operator == 'equals' (4 candidate operator cases)
        # -------------------------------------------------------------
        req_eq = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="httpx")])

        # 1a. vs candidate equals
        cand_eq_match = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="httpx")])
        cand_eq_diff = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="aiohttp")])
        self.assertTrue(compare_compatibility(req_eq, cand_eq_match)[0])
        self.assertFalse(compare_compatibility(req_eq, cand_eq_diff)[0])

        # 1b. vs candidate in: multi-value MUST be rejected (P1 fix), exact single-value accepted
        cand_in_multi = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,aiohttp")])
        cand_in_single = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx")])
        self.assertFalse(compare_compatibility(req_eq, cand_in_multi)[0])
        self.assertTrue(compare_compatibility(req_eq, cand_in_single)[0])

        # 1c. vs candidate not_equals: negative cannot guarantee positive exact
        cand_neq = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_equals", value="requests")])
        self.assertFalse(compare_compatibility(req_eq, cand_neq)[0])

        # 1d. vs candidate not_in: negative cannot guarantee positive exact
        cand_nin = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests,urllib3")])
        self.assertFalse(compare_compatibility(req_eq, cand_nin)[0])

        # -------------------------------------------------------------
        # 2. Request operator == 'in' (4 candidate operator cases)
        # -------------------------------------------------------------
        req_in = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,aiohttp,requests")])

        # 2a. vs candidate equals: candidate value in request set
        cand_eq_in_set = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="httpx")])
        cand_eq_not_in_set = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="urllib3")])
        self.assertTrue(compare_compatibility(req_in, cand_eq_in_set)[0])
        self.assertFalse(compare_compatibility(req_in, cand_eq_not_in_set)[0])

        # 2b. vs candidate in: candidate set is subset of request set
        cand_in_subset = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,aiohttp")])
        cand_in_not_subset = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,urllib3")])
        self.assertTrue(compare_compatibility(req_in, cand_in_subset)[0])
        self.assertFalse(compare_compatibility(req_in, cand_in_not_subset)[0])

        # 2c. vs candidate not_equals
        self.assertFalse(compare_compatibility(req_in, cand_neq)[0])

        # 2d. vs candidate not_in
        self.assertFalse(compare_compatibility(req_in, cand_nin)[0])

        # -------------------------------------------------------------
        # 3. Request operator == 'not_equals' (4 candidate operator cases)
        # -------------------------------------------------------------
        req_neq = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_equals", value="requests")])

        # 3a. vs candidate equals
        cand_eq_diff2 = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="httpx")])
        cand_eq_same = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="requests")])
        self.assertTrue(compare_compatibility(req_neq, cand_eq_diff2)[0])
        self.assertFalse(compare_compatibility(req_neq, cand_eq_same)[0])

        # 3b. vs candidate in: candidate allowed set does not contain forbidden requests
        cand_in_clean = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,aiohttp")])
        cand_in_tainted = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,requests")])
        self.assertTrue(compare_compatibility(req_neq, cand_in_clean)[0])
        self.assertFalse(compare_compatibility(req_neq, cand_in_tainted)[0])

        # 3c. vs candidate not_equals
        cand_neq_same = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_equals", value="requests")])
        cand_neq_diff = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_equals", value="urllib3")])
        self.assertTrue(compare_compatibility(req_neq, cand_neq_same)[0])
        self.assertFalse(compare_compatibility(req_neq, cand_neq_diff)[0])

        # 3d. vs candidate not_in: candidate forbids requests
        cand_nin_includes = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests,urllib3")])
        cand_nin_missing = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="urllib3,aiohttp")])
        self.assertTrue(compare_compatibility(req_neq, cand_nin_includes)[0])
        self.assertFalse(compare_compatibility(req_neq, cand_nin_missing)[0])

        # -------------------------------------------------------------
        # 4. Request operator == 'not_in' (4 candidate operator cases)
        # -------------------------------------------------------------
        req_nin = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests,urllib3")])

        # 4a. vs candidate equals
        cand_eq_safe = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="httpx")])
        cand_eq_forbidden = _make_meta_with_constraints([ConstraintItem(key="lib", operator="equals", value="requests")])
        self.assertTrue(compare_compatibility(req_nin, cand_eq_safe)[0])
        self.assertFalse(compare_compatibility(req_nin, cand_eq_forbidden)[0])

        # 4b. vs candidate in
        cand_in_disjoint = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,aiohttp")])
        cand_in_overlap = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx,requests")])
        self.assertTrue(compare_compatibility(req_nin, cand_in_disjoint)[0])
        self.assertFalse(compare_compatibility(req_nin, cand_in_overlap)[0])

        # 4c. vs candidate not_equals
        req_nin_single = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests")])
        cand_neq_match2 = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_equals", value="requests")])
        self.assertTrue(compare_compatibility(req_nin_single, cand_neq_match2)[0])
        self.assertFalse(compare_compatibility(req_nin, cand_neq_match2)[0])  # req has urllib3 too

        # 4d. vs candidate not_in
        cand_nin_superset = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests,urllib3,aiohttp")])
        cand_nin_subset = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value="requests")])
        self.assertTrue(compare_compatibility(req_nin, cand_nin_superset)[0])
        self.assertFalse(compare_compatibility(req_nin, cand_nin_subset)[0])

        # -------------------------------------------------------------
        # 5. Empty set handling
        # -------------------------------------------------------------
        req_empty_in = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value=",  , ")])
        cand_valid_in = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx")])
        self.assertFalse(compare_compatibility(req_empty_in, cand_valid_in)[0])

        req_valid_in = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value="httpx")])
        cand_empty_in = _make_meta_with_constraints([ConstraintItem(key="lib", operator="in", value=", ,")])
        self.assertFalse(compare_compatibility(req_valid_in, cand_empty_in)[0])

        req_empty_nin = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value=",")])
        self.assertFalse(compare_compatibility(req_empty_nin, cand_valid_in)[0])

        cand_empty_nin = _make_meta_with_constraints([ConstraintItem(key="lib", operator="not_in", value=",,")])
        self.assertFalse(compare_compatibility(req_valid_in, cand_empty_nin)[0])

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
    """Verifies that 1.0 similarity cannot override incompatibility, and request metadata is reused."""

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
            visibility="tenant_shared",
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
            visibility="tenant_shared",
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

    def test_request_metadata_extracted_once_and_reused(self):
        """FIX-07: In evaluate_candidates, incoming request metadata is extracted once, not per-candidate."""
        req = self._make_req("How do I reverse a string in Python?")
        cands = [
            self._make_cand(f"Reverse a string in Python variant {i}", f"s[::-1]_{i}", 0.90 + i * 0.01)
            for i in range(5)
        ]

        with patch.object(self.policy.extractor, "extract", wraps=self.policy.extractor.extract) as spy_extract:
            winning, reason = self.policy.evaluate_candidates(cands, req)
            # Extractor must be called at most ONCE for the request during evaluation
            self.assertEqual(spy_extract.call_count, 1)

    def test_determinism_top_k_1_and_top_k_5(self):
        """FIX-07: Candidate evaluation results are deterministic across top_k=1 and top_k=5."""
        req = self._make_req("How do I reverse a string in Python?")
        c1 = self._make_cand("How to reverse a string in Python?", "s[::-1]", 0.96)
        c2 = self._make_cand("Reversing a string in Python slicing", "s[::-1]", 0.94)
        c3 = self._make_cand("Python string reverse idiom", "s[::-1]", 0.92)

        win1, r1 = self.policy.evaluate_candidates([c1], req)
        win5, r5 = self.policy.evaluate_candidates([c1, c2, c3], req)

        self.assertIsNotNone(win1)
        self.assertIsNotNone(win5)
        self.assertEqual(win1.cached_response, win5.cached_response)


class TestProblemGroupPartitionValidation(unittest.TestCase):
    """FIX-06: Verifies load_dataset group-aware evaluation partition validation."""

    def test_load_dataset_rejects_partition_leakage(self):
        """FIX-06: load_dataset raises ValueError when a problem group appears in both dev and test."""
        from scripts.evaluate_semantic_cache import load_dataset
        import tempfile

        leaked_dataset = [
            {
                "id": "case-dev-01",
                "problem_group_id": "grp-shared-topic",
                "query": "How to reverse string in Python?",
                "category": "positive_paraphrase",
                "split": "development",
                "expected_answer_label": "ans_1",
                "candidates": [
                    {
                        "id": "c1",
                        "query_text": "How to reverse string in Python?",
                        "cached_response": "s[::-1]",
                        "answer_label": "ans_1",
                    }
                ],
            },
            {
                "id": "case-test-01",
                "problem_group_id": "grp-shared-topic",
                "query": "How to reverse string in Python with slicing?",
                "category": "positive_paraphrase",
                "split": "test",
                "expected_answer_label": "ans_1",
                "candidates": [
                    {
                        "id": "c2",
                        "query_text": "How to reverse string in Python with slicing?",
                        "cached_response": "s[::-1]",
                        "answer_label": "ans_1",
                    }
                ],
            },
        ]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(leaked_dataset, f)
            tpath = f.name

        try:
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tpath)
            self.assertIn("Partition leakage detected", str(ctx.exception))
        finally:
            Path(tpath).unlink(missing_ok=True)

    def test_load_dataset_rejects_missing_problem_group_id(self):
        """FIX-06: When problem groups are present, missing problem_group_id raises ValueError."""
        from scripts.evaluate_semantic_cache import load_dataset
        import tempfile

        invalid_dataset = [
            {
                "id": "case-dev-01",
                "problem_group_id": "grp-topic-1",
                "query": "How to reverse string in Python?",
                "category": "positive_paraphrase",
                "split": "development",
                "expected_answer_label": "ans_1",
                "candidates": [
                    {
                        "id": "c1",
                        "query_text": "How to reverse string in Python?",
                        "cached_response": "s[::-1]",
                        "answer_label": "ans_1",
                    }
                ],
            },
            {
                "id": "case-dev-02",
                # missing problem_group_id
                "query": "How to merge dicts in Python?",
                "category": "positive_paraphrase",
                "split": "development",
                "expected_answer_label": "ans_2",
                "candidates": [
                    {
                        "id": "c2",
                        "query_text": "How to merge dicts in Python?",
                        "cached_response": "a | b",
                        "answer_label": "ans_2",
                    }
                ],
            },
        ]

        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump(invalid_dataset, f)
            tpath = f.name

        try:
            with self.assertRaises(ValueError) as ctx:
                load_dataset(tpath)
            self.assertIn("missing a non-empty string 'problem_group_id'", str(ctx.exception))
        finally:
            Path(tpath).unlink(missing_ok=True)

    def test_dataset_stage3_1_1_has_zero_partition_leakage(self):
        """FIX-06: Verified on real dataset_stage3_1_1.json."""
        from scripts.evaluate_semantic_cache import load_dataset
        dataset_path = Path(__file__).resolve().parent.parent / "evaluation" / "dataset_stage3_1_1.json"
        cases = load_dataset(dataset_path)
        self.assertEqual(len(cases), 130)

        dev_cases = [c for c in cases if c.split == "development"]
        test_cases = [c for c in cases if c.split == "test"]
        self.assertEqual(len(dev_cases), 65)
        self.assertEqual(len(test_cases), 65)

        dev_groups = {c.problem_group_id for c in dev_cases}
        test_groups = {c.problem_group_id for c in test_cases}
        # Clustered intra-partition problem groups verify that family grouping works while disjoint
        self.assertEqual(len(dev_groups), 59)
        self.assertEqual(len(test_groups), 62)
        self.assertEqual(dev_groups.intersection(test_groups), set())


class TestValidatorFallbackE2E(unittest.IsolatedAsyncioTestCase):
    """FIX-08: Verifies end-to-end validator exception fallback in IntelliCacheCore."""

    async def test_validator_exception_core_pipeline_fallback(self):
        """FIX-08: Validator exception in semantic cache causes IntelliCacheCore to fall back
        to calling the upstream LLM provider exactly once, returning cached=False.
        """
        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)

        mock_embedder = AsyncMock()
        mock_embedder.model_name = "nomic-embed-text"
        mock_embedder.dimensions = 768
        mock_embedder.embed.return_value = MagicMock(vector=tuple([0.1] * 768), model="nomic-embed-text", dimensions=768)

        mock_vector_store = AsyncMock()
        mock_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_123",
            query_text="How to reverse a string in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="s[::-1]",
            metadata={"schema_version": CURRENT_SCHEMA_VERSION},
            visibility="tenant_shared",
        )
        mock_vector_store.search.return_value = [mock_cand]

        policy = SemanticCachePolicy()
        metrics = RuntimeMetricsCollector()
        semantic_service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=metrics,
        )

        from app.semantic_cache.control import SemanticServingController
        core = IntelliCacheCore(
            cache=exact_cache,
            cache_enabled=True,
            semantic_cache=semantic_service,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )

        upstream_provider = AsyncMock()
        upstream_provider.generate.return_value = GenerationResult(
            text="Freshly generated text by upstream provider",
            model="llama-3.3-70b-versatile",
            cached=False,
            usage=UsageMetrics(prompt_tokens=50, completion_tokens=50, total_tokens=100),
            latency_ms=120.0,
        )

        req = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            visibility="tenant_shared",
        )

        # Inject validator exception during candidate evaluation
        with patch.object(policy, "evaluate_candidates", side_effect=RuntimeError("Simulated validator internal error")):
            result = await core.execute_query(req, upstream_provider)

        # Assert result is from provider, NOT cached
        self.assertFalse(result.cached)
        self.assertEqual(result.text, "Freshly generated text by upstream provider")
        # Assert upstream provider was called exactly once
        upstream_provider.generate.assert_called_once()
        # Assert validator failure metric was recorded
        snap = metrics.get_namespace_snapshot("default")
        self.assertEqual(snap.semantic.validator_failures, 1)

    async def test_candidate_evaluation_cancellation_propagates_in_lookup(self):
        """Stage 3.1.2: asyncio.CancelledError during candidate evaluation propagates out of lookup without recording validator failure."""
        mock_embedder = AsyncMock()
        mock_embedder.model_name = "nomic-embed-text"
        mock_embedder.dimensions = 768
        mock_embedder.embed.return_value = MagicMock(vector=tuple([0.1] * 768), model="nomic-embed-text", dimensions=768)

        mock_vector_store = AsyncMock()
        mock_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_123",
            query_text="How to reverse a string in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="s[::-1]",
            metadata={"schema_version": CURRENT_SCHEMA_VERSION},
            visibility="tenant_shared",
        )
        mock_vector_store.search.return_value = [mock_cand]

        policy = SemanticCachePolicy()
        metrics = RuntimeMetricsCollector()
        semantic_service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=metrics,
        )

        req = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            visibility="tenant_shared",
        )

        with patch.object(policy, "evaluate_candidates", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await semantic_service.lookup(req)

        # Cancellation must NOT count as a validator failure
        snap = metrics.get_namespace_snapshot("default")
        self.assertEqual(snap.semantic.validator_failures, 0)

    async def test_candidate_evaluation_cancellation_propagates_in_core(self):
        """Stage 3.1.2: asyncio.CancelledError during candidate evaluation propagates through core and skips provider."""
        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        mock_embedder = AsyncMock()
        mock_embedder.model_name = "nomic-embed-text"
        mock_embedder.dimensions = 768
        mock_embedder.embed.return_value = MagicMock(vector=tuple([0.1] * 768), model="nomic-embed-text", dimensions=768)

        mock_vector_store = AsyncMock()
        mock_cand = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_123",
            query_text="How to reverse a string in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="s[::-1]",
            metadata={"schema_version": CURRENT_SCHEMA_VERSION},
        )
        mock_vector_store.search.return_value = [mock_cand]

        policy = SemanticCachePolicy()
        metrics = RuntimeMetricsCollector()
        semantic_service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vector_store,
            policy=policy,
            metrics_collector=metrics,
        )

        from app.semantic_cache.control import SemanticServingController
        core = IntelliCacheCore(
            cache=exact_cache,
            cache_enabled=True,
            semantic_cache=semantic_service,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )

        upstream_provider = AsyncMock()
        req = GenerationRequest(
            messages=[ChatMessage(role="user", content="How do I reverse a string in Python?")],
            provider="groq",
            model="llama-3.3-70b-versatile",
            temperature=0.7,
            max_tokens=1000,
            visibility="tenant_shared",
        )

        with patch.object(policy, "evaluate_candidates", side_effect=asyncio.CancelledError()):
            with self.assertRaises(asyncio.CancelledError):
                await core.execute_query(req, upstream_provider)

        upstream_provider.generate.assert_not_called()
        snap = metrics.get_namespace_snapshot("default")
        self.assertEqual(snap.semantic.validator_failures, 0)


if __name__ == "__main__":
    unittest.main()
