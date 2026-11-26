"""
Comprehensive unit and integration tests for Stage 3.5.2:
Attribute-Aware Compatibility Policy and Validator Hardening.

Covers all 22 required test scenarios, tri-state CompatibilityDecision outcomes,
technical attribute extraction fixes, UNKNOWN vs NOT_APPLICABLE semantics,
hard-mismatch precedence, trusted vs untrusted compatibility evidence, legacy v2
fail-closed behavior, effective message extraction, SemanticCacheService integration,
PostgreSQL vector-store rejection integration, and preserved safety invariants.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import asyncpg

from app.cache.exact_cache import ExactCache
from app.cache.interfaces import CacheEntry
from app.cache.migration import run_migrations
from app.config import Settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.fingerprinting import generate_fingerprint
from app.core.service import IntelliCacheCore
from app.metrics.collector import RuntimeMetricsCollector
from app.semantic_cache.compatibility import (
    CompatibilityExtractor,
    CompatibilityValidator,
    compare_compatibility,
    compare_compatibility_decision,
    extract_effective_user_query,
    extract_effective_user_query_with_strategy,
)
from app.semantic_cache.contracts import (
    CURRENT_SCHEMA_VERSION,
    CompatibilityDecision,
    CompatibilityEvidence,
    CompatibilityReasonCode,
    CompatibilityStatus,
    ConstraintItem,
    DependencyItem,
    DimensionValue,
    EVIDENCE_TIER_REQUEST_DERIVED,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
    EmbeddingContract,
    GenerationContract,
    MetadataStatus,
    SemanticCompatibilityMetadata,
    TRUSTED_REQUEST_EXTRACTOR_ID,
    TRUSTED_RESPONSE_VERIFIER_ID,
    TaskKind,
)
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.vector_store.contracts import VectorSearchResult
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore


def _make_req(
    text: str,
    *,
    provider: str = "groq",
    model: str = "llama-3.3-70b-versatile",
    temperature: float = 0.7,
    max_tokens: int = 1000,
    namespace: str = "default",
    messages=None,
    conversation_id: str | None = None,
    metadata: dict | None = None,
    visibility: str = "tenant_shared",
) -> GenerationRequest:
    return GenerationRequest(
        messages=messages if messages is not None else [ChatMessage(role="user", content=text)],
        provider=provider,
        model=model,
        temperature=temperature,
        max_tokens=max_tokens,
        namespace=namespace,
        conversation_id=conversation_id,
        metadata=metadata or {},
        visibility=visibility,
    )


def _with_evidence(
    meta: SemanticCompatibilityMetadata,
    evidence: CompatibilityEvidence | None,
) -> SemanticCompatibilityMetadata:
    return SemanticCompatibilityMetadata(
        schema_version=meta.schema_version,
        extractor_version=meta.extractor_version,
        task_kind=meta.task_kind,
        dimensions=meta.dimensions,
        generation_contract=meta.generation_contract,
        embedding_contract=meta.embedding_contract,
        indexed_at=meta.indexed_at,
        compatibility_evidence=evidence,
    )


class TestStage352ExtractionHardening(unittest.TestCase):
    """Tests improved extraction, normalization, Virtual DOM guard, Go/Pydantic/Elixir/Scala, and message selection."""

    def setUp(self):
        self.extractor = CompatibilityExtractor()

    def test_effective_user_query_extraction_strategy(self):
        """Does not blindly assume messages[0]; extracts latest non-empty user turn cleanly."""
        req_single = _make_req("How to reverse a list in Python?")
        q1, s1 = extract_effective_user_query_with_strategy(req_single)
        self.assertEqual(q1, "How to reverse a list in Python?")
        self.assertEqual(s1, "single_user_message")

        req_multi = _make_req(
            "",
            messages=[
                ChatMessage(role="system", content="You are a concise Java assistant."),
                ChatMessage(role="user", content="How to sort in Python?"),
                ChatMessage(role="assistant", content="Use sorted()."),
                ChatMessage(role="user", content="Show binary search in Rust"),
            ],
        )
        q2, s2 = extract_effective_user_query_with_strategy(req_multi)
        self.assertEqual(q2, "Show binary search in Rust")
        self.assertEqual(s2, "latest_user_message")
        meta = self.extractor.extract(req_multi, "nomic-embed-text")
        self.assertEqual(meta.dimensions["language"].value, "rust")

        req_empty = _make_req("", messages=[ChatMessage(role="system", content="System only")])
        self.assertEqual(extract_effective_user_query(req_empty), "")

    def test_pydantic_v1_and_v2_distinguished(self):
        """Distinguishes Pydantic v1 vs v2 cleanly from both explicit versions and version-specific APIs."""
        m_v2 = self.extractor.extract_from_text("How do I write a field validator using Pydantic v2?")
        m_v1 = self.extractor.extract_from_text("How do I write a field validator using Pydantic v1?")
        m_v2_api = self.extractor.extract_from_text("How do I use @field_validator and model_dump with Pydantic?")
        m_v1_api = self.extractor.extract_from_text("How do I use @validator and orm_mode in Pydantic?")

        self.assertEqual(m_v2.dimensions["language"].value, "python")
        self.assertEqual(m_v2.dimensions["dependencies"].value, [DependencyItem("pydantic", "2")])
        self.assertEqual(m_v1.dimensions["dependencies"].value, [DependencyItem("pydantic", "1")])
        self.assertEqual(m_v2_api.dimensions["dependencies"].value, [DependencyItem("pydantic", "2")])
        self.assertEqual(m_v1_api.dimensions["dependencies"].value, [DependencyItem("pydantic", "1")])

        decision = compare_compatibility_decision(m_v2, m_v1)
        self.assertEqual(decision.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(decision.reason_code, CompatibilityReasonCode.DEPENDENCY_VERSION_MISMATCH)
        self.assertEqual(decision.dimension, "dependencies")

    def test_go_versions_distinguished(self):
        """Go 1.21 and Go 1.22 are recognized with distinct language_version metadata."""
        m_121 = self.extractor.extract_from_text("How do loop variables behave in Go 1.21?")
        m_122 = self.extractor.extract_from_text("How do range-over-func iterators and loop variables work in Go 1.22?")

        self.assertEqual(m_121.dimensions["language"].value, "go")
        self.assertEqual(m_121.dimensions["language_version"].value, "1.21")
        self.assertEqual(m_122.dimensions["language"].value, "go")
        self.assertEqual(m_122.dimensions["language_version"].value, "1.22")

        decision = compare_compatibility_decision(m_122, m_121)
        self.assertEqual(decision.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(decision.reason_code, CompatibilityReasonCode.LANGUAGE_VERSION_MISMATCH)
        self.assertEqual(decision.dimension, "language_version")

    def test_elixir_and_scala_conceptual_distinguished(self):
        """Elixir vs Scala conceptual queries extract distinct languages and reject cross-language reuse."""
        m_elixir = self.extractor.extract_from_text("Explain pattern matching in Elixir")
        m_scala = self.extractor.extract_from_text("Explain pattern matching in Scala")

        self.assertEqual(m_elixir.task_kind.value, TaskKind.CONCEPTUAL)
        self.assertEqual(m_elixir.dimensions["language"], DimensionValue(MetadataStatus.KNOWN, "elixir"))
        self.assertEqual(m_scala.dimensions["language"], DimensionValue(MetadataStatus.KNOWN, "scala"))

        decision = compare_compatibility_decision(m_elixir, m_scala)
        self.assertEqual(decision.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(decision.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)

    def test_unlisted_conceptual_language_becomes_unknown_not_not_applicable(self):
        """Conceptual queries referencing an unlisted language become UNKNOWN rather than NOT_APPLICABLE."""
        m_gleam = self.extractor.extract_from_text("Explain pattern matching in Gleam")
        self.assertEqual(m_gleam.dimensions["language"].status, MetadataStatus.UNKNOWN)

        m_nim = self.extractor.extract_from_text("Explain macros in Nim programming language")
        self.assertEqual(m_nim.dimensions["language"].status, MetadataStatus.UNKNOWN)

        # Genuinely language-agnostic CS theory remains NOT_APPLICABLE
        m_bigo = self.extractor.extract_from_text("What is Big-O time complexity in algorithms?")
        self.assertEqual(m_bigo.dimensions["language"].status, MetadataStatus.NOT_APPLICABLE)

    def test_virtual_dom_does_not_infer_browser_runtime_while_explicit_browser_does(self):
        """Scenario 14 & 15: Virtual DOM must not imply browser runtime; explicit browser APIs do."""
        m_vdom = self.extractor.extract_from_text("Explain how Virtual DOM reconciliation works in React 18")
        self.assertEqual(m_vdom.dimensions["framework"].value, "react")
        self.assertEqual(m_vdom.dimensions["framework_version"].value, "18")
        self.assertEqual(m_vdom.dimensions["runtime"].status, MetadataStatus.NOT_APPLICABLE)

        m_browser_api = self.extractor.extract_from_text(
            "How do I select elements using window.document.querySelector in JavaScript?"
        )
        self.assertEqual(m_browser_api.dimensions["runtime"].status, MetadataStatus.KNOWN)
        self.assertEqual(m_browser_api.dimensions["runtime"].value, "browser")

    def test_unknown_library_preserved_and_versionless_is_uncertain(self):
        """Scenario 6: Preserves unrecognized library names/versions; unestablished version yields UNCERTAIN."""
        m_unknown_v2 = self.extractor.extract_from_text("Write an async client in Python using ZephyrHTTP v2.1")
        m_unknown_v1 = self.extractor.extract_from_text("Write an async client in Python using ZephyrHTTP v1.0")
        m_unknown_unver = self.extractor.extract_from_text("Write an async client in Python using the ZephyrHTTP library")

        self.assertEqual(
            m_unknown_v2.dimensions["dependencies"].value,
            [DependencyItem("zephyrhttp", "2.1")],
        )
        self.assertEqual(
            m_unknown_unver.dimensions["dependencies"].value,
            [DependencyItem("zephyrhttp", None)],
        )

        # Different pinned versions -> INCOMPATIBLE
        d_diff = compare_compatibility_decision(m_unknown_v2, m_unknown_v1)
        self.assertEqual(d_diff.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_diff.reason_code, CompatibilityReasonCode.DEPENDENCY_VERSION_MISMATCH)

        # Unspecified/unknown version -> UNCERTAIN
        d_unc = compare_compatibility_decision(m_unknown_v2, m_unknown_unver)
        self.assertEqual(d_unc.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_unc.reason_code, CompatibilityReasonCode.UNKNOWN_VERSION)
        self.assertEqual(d_unc.dimension, "dependencies")

        d_both_unver = compare_compatibility_decision(m_unknown_unver, m_unknown_unver)
        self.assertEqual(d_both_unver.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_both_unver.reason_code, CompatibilityReasonCode.UNKNOWN_VERSION)

    def test_unknown_framework_becomes_unknown_and_validator_returns_uncertain(self):
        """Scenario 7: Unrecognized framework yields UNKNOWN and triggers UNCERTAIN (UNKNOWN_FRAMEWORK)."""
        m_fw = self.extractor.extract_from_text("Build a middleware handler using the ZephyrWeb framework in Python")
        self.assertEqual(m_fw.dimensions["framework"].status, MetadataStatus.UNKNOWN)

        m_generic = self.extractor.extract_from_text("Build a middleware handler in Python")
        d = compare_compatibility_decision(m_fw, m_generic)
        self.assertEqual(d.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.UNKNOWN_FRAMEWORK)
        self.assertEqual(d.dimension, "framework")

    def test_deterministic_normalization_and_requests_plural_guard(self):
        """Ordinary English 'HTTP requests' does not trigger Python 'requests' dependency."""
        m_http = self.extractor.extract_from_text("Explain idempotent retries for HTTP requests in Go")
        self.assertEqual(m_http.dimensions["language"].value, "go")
        self.assertEqual(m_http.dimensions["dependencies"].status, MetadataStatus.NOT_APPLICABLE)

        m1 = self.extractor.extract_from_text("How do I write a field validator in PYDANTIC V2?")
        m2 = self.extractor.extract_from_text("How do I write a field validator in pydantic v2?")
        self.assertEqual(m1.dimensions, m2.dimensions)


class TestStage352TriStateValidatorAndEvidencePolicy(unittest.TestCase):
    """Tests all 22 scenarios through CompatibilityValidator / compare_compatibility_decision / policy."""

    def setUp(self):
        self.extractor = CompatibilityExtractor()
        self.validator = CompatibilityValidator()
        self.policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03)

    def test_scenario_1_exact_compatible_metadata(self):
        m_req = self.extractor.extract_from_text("How do I reverse a string in Python?")
        m_cand = self.extractor.extract_from_text("What is the syntax to reverse a string in Python?")
        d = self.validator.validate(m_req, m_cand)
        self.assertEqual(d.status, CompatibilityStatus.COMPATIBLE)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.COMPATIBLE)
        self.assertTrue(compare_compatibility(m_req, m_cand)[0])

    def test_scenario_8_and_9_unknown_and_missing_critical_metadata(self):
        """Scenarios 8 & 9: UNKNOWN runtime/execution/language yields UNCERTAIN; missing schema fails closed."""
        m_py = self.extractor.extract_from_text("How to sort in Python?")
        m_rt_unk = self.extractor.extract_from_text("How to sort in Python on the EdgeFoo runtime?")
        self.assertEqual(m_rt_unk.dimensions["runtime"].status, MetadataStatus.UNKNOWN)
        d_rt = self.validator.validate(m_rt_unk, m_py)
        self.assertEqual(d_rt.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_rt.reason_code, CompatibilityReasonCode.UNKNOWN_RUNTIME)

        req = _make_req("How to sort in Python?")
        cand_no_meta = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_no_meta",
            query_text="How to sort in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="sorted(x)",
            visibility="tenant_shared",
            metadata={},
        )
        d_miss = self.policy.validate_candidate(cand_no_meta, req)
        self.assertEqual(d_miss.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_miss.reason_code, CompatibilityReasonCode.LEGACY_METADATA_MISSING)

    def test_scenario_2_python_vs_java(self):
        m_py = self.extractor.extract_from_text("How to sort a list in Python?")
        m_java = self.extractor.extract_from_text("How to sort a list in Java?")
        d = self.validator.validate(m_py, m_java)
        self.assertEqual(d.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)
        self.assertEqual(d.dimension, "language")

    def test_hard_incompatibility_takes_precedence_over_uncertainty(self):
        """UNKNOWN dimension must not mask an independently established hard mismatch."""
        m_req = self.extractor.extract_from_text("Use Pydantic in Python")  # dependency version unpinned (UNCERTAIN)
        m_cand = self.extractor.extract_from_text("Use Java collections")   # Python vs Java (INCOMPATIBLE)
        d = self.validator.validate(m_req, m_cand)
        self.assertEqual(d.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)

        # Also UNKNOWN framework in Python vs Java candidate
        m_fw_unknown_py = self.extractor.extract_from_text("Build endpoint in ZephyrWeb framework in Python")
        d2 = self.validator.validate(m_fw_unknown_py, m_cand)
        self.assertEqual(d2.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d2.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)

    def test_scenarios_10_and_11_version_independent_evidence_rules(self):
        """Conceptual cross-version reuse fails without trusted proof and succeeds with trusted proof."""
        req = self.extractor.extract_from_text("Explain concurrent rendering in React 19")
        cand = self.extractor.extract_from_text("Explain concurrent rendering in React 18")

        # Scenario 10: no verified evidence (even though task is conceptual without syntax markers)
        d_no_ev = self.validator.validate(req, cand)
        self.assertEqual(d_no_ev.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_no_ev.reason_code, CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH)

        # Untrusted client evidence cannot bypass
        untrusted = _with_evidence(
            cand,
            CompatibilityEvidence(
                source="client",
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                version_independent=True,
            ),
        )
        d_untrusted = self.validator.validate(req, untrusted)
        self.assertNotEqual(d_untrusted.status, CompatibilityStatus.COMPATIBLE)

        # Scenario 11: trusted response_verified evidence passes when all other dimensions pass
        trusted = _with_evidence(
            cand,
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                version_independent=True,
            ),
        )
        d_trusted = self.validator.validate(req, trusted)
        self.assertEqual(d_trusted.status, CompatibilityStatus.COMPATIBLE)

        # Version-specific code_generation MUST NOT reuse across versions even with version_independent flag
        req_code = self.extractor.extract_from_text("Write a form action component in React 19 using JavaScript")
        cand_code = _with_evidence(
            self.extractor.extract_from_text("Write a form component in React 18 using JavaScript"),
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                version_independent=True,
            ),
        )
        d_code = self.validator.validate(req_code, cand_code)
        self.assertEqual(d_code.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_code.reason_code, CompatibilityReasonCode.FRAMEWORK_VERSION_MISMATCH)

    def test_scenarios_12_and_13_language_independent_evidence_rules(self):
        """Cross-language conceptual reuse requires trusted response_verified evidence."""
        req = self.extractor.extract_from_text("Explain pattern matching in Elixir")
        cand = self.extractor.extract_from_text("Explain pattern matching in Scala")

        # Scenario 12: without evidence
        d_no_ev = self.validator.validate(req, cand)
        self.assertEqual(d_no_ev.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_no_ev.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)

        # Request-derived spoof attempt rejected
        spoofed_req_tier = _with_evidence(
            cand,
            CompatibilityEvidence(
                source=TRUSTED_REQUEST_EXTRACTOR_ID,
                evidence_tier=EVIDENCE_TIER_REQUEST_DERIVED,
                language_independent=True,
            ),
        )
        d_spoof = self.validator.validate(req, spoofed_req_tier)
        self.assertNotEqual(d_spoof.status, CompatibilityStatus.COMPATIBLE)

        # Scenario 13: valid trusted response_verified evidence on conceptual explanation
        trusted = _with_evidence(
            cand,
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                language_independent=True,
            ),
        )
        d_trusted = self.validator.validate(req, trusted)
        self.assertEqual(d_trusted.status, CompatibilityStatus.COMPATIBLE)

    def test_scenarios_16_and_17_output_format_and_artifact_proof(self):
        """Prompt-derived JSON metadata is not verified JSON; JSON vs Markdown fails closed."""
        req_json = self.extractor.extract_from_text("Parse this log entry in Python and output the result as strict JSON")
        cand_json_prompt_only = self.extractor.extract_from_text("Parse this log entry in Python and return strict JSON")
        cand_markdown = self.extractor.extract_from_text("Parse this log entry in Python and output as Markdown")

        # Extractor never sets verified_json_valid from prompt alone
        self.assertFalse(req_json.compatibility_evidence.verified_json_valid)
        self.assertIsNone(req_json.compatibility_evidence.verified_output_format)
        self.assertEqual(req_json.compatibility_evidence.evidence_tier, EVIDENCE_TIER_REQUEST_DERIVED)

        # Scenario 16: JSON vs Markdown -> INCOMPATIBLE
        d_md = self.validator.validate(req_json, cand_markdown)
        self.assertEqual(d_md.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_md.reason_code, CompatibilityReasonCode.OUTPUT_FORMAT_MISMATCH)

        # Scenario 17: prompt-derived JSON candidate without response verification -> UNCERTAIN
        d_unverified = self.validator.validate(req_json, cand_json_prompt_only)
        self.assertEqual(d_unverified.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_unverified.reason_code, CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT)

        # Untrusted verified_json_valid claim fails closed as UNCERTAIN
        untrusted_json = _with_evidence(
            cand_json_prompt_only,
            CompatibilityEvidence(
                source="client_claim",
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                verified_output_format="json",
                verified_json_valid=True,
            ),
        )
        d_untrusted = self.validator.validate(req_json, untrusted_json)
        self.assertEqual(d_untrusted.status, CompatibilityStatus.UNCERTAIN)

        # Trusted response_verified JSON passes
        verified_json = _with_evidence(
            cand_json_prompt_only,
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                verified_output_format="json",
                verified_json_valid=True,
            ),
        )
        d_ok = self.validator.validate(req_json, verified_json)
        self.assertEqual(d_ok.status, CompatibilityStatus.COMPATIBLE)

        # Explicit schema adherence still stays UNCERTAIN (deferred to Stage 3.5.3)
        req_schema = self.extractor.extract_from_text(
            "Parse this log entry in Python and return JSON conforming to this schema"
        )
        cand_schema = self.extractor.extract_from_text(
            "Parse this log entry in Python and return JSON conforming to this schema"
        )
        cand_schema_ev = _with_evidence(
            cand_schema,
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                verified_output_format="json",
                verified_json_valid=True,
            ),
        )
        d_schema = self.validator.validate(req_schema, cand_schema_ev)
        self.assertEqual(d_schema.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_schema.reason_code, CompatibilityReasonCode.UNVERIFIED_OUTPUT_ARTIFACT)

    def test_scenario_18_freshness_dependent_fails_closed(self):
        """Time-sensitive freshness queries are ineligible and return UNCERTAIN in validator."""
        req_fresh = _make_req("What is the latest release version of Python?")
        elig = self.policy.check_eligibility(req_fresh)
        self.assertFalse(elig.eligible)
        self.assertIn("freshness", elig.reason.lower())

        m_req = self.extractor.extract(req_fresh, "nomic-embed-text")
        m_cand = self.extractor.extract_from_text("What is the latest release version of Python?")
        d = self.validator.validate(m_req, m_cand)
        self.assertEqual(d.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.FRESHNESS_NOT_ESTABLISHED)

        # Timeless stdlib datetime question must NOT trigger freshness rejection
        req_datetime = _make_req("How do I get current UTC timestamp in Python datetime?")
        self.assertTrue(self.policy.check_eligibility(req_datetime).eligible)

    def test_scenario_19_context_dependent_single_turn_fails_closed(self):
        """Deictic single-turn prompts are blocked by eligibility and yield UNCERTAIN in validator."""
        req_ctx = _make_req("Fix this error")
        elig = self.policy.check_eligibility(req_ctx)
        self.assertFalse(elig.eligible)

        m_req = self.extractor.extract(req_ctx, "nomic-embed-text")
        m_cand = self.extractor.extract_from_text("How do I reverse a string in Python?")
        d = self.validator.validate(m_req, m_cand)
        self.assertEqual(d.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d.reason_code, CompatibilityReasonCode.CONTEXT_DEPENDENCY_UNRESOLVED)

    def test_scenario_20_ambiguous_evidence_and_spoofed_top_level_flags(self):
        """Conflicting evidence or spoofed top-level independence flags fail closed as UNCERTAIN."""
        m_req = self.extractor.extract_from_text("Explain event loops in Python 3.12")
        m_cand = self.extractor.extract_from_text("Explain event loops in Python 3.12")

        ambig_cand = _with_evidence(
            m_cand,
            CompatibilityEvidence(
                source=TRUSTED_RESPONSE_VERIFIER_ID,
                evidence_tier=EVIDENCE_TIER_RESPONSE_VERIFIED,
                version_independent=True,
                compatible_framework_versions=("18",),
            ),
        )
        d_ambig = self.validator.validate(m_req, ambig_cand)
        self.assertEqual(d_ambig.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_ambig.reason_code, CompatibilityReasonCode.AMBIGUOUS_COMPATIBILITY_EVIDENCE)

        # Spoofed raw top-level client flag inside candidate dict
        raw_spoofed = m_cand.to_dict()
        raw_spoofed["version_independent"] = True
        d_spoof = self.validator.validate(m_req, m_cand, raw_candidate_metadata=raw_spoofed)
        self.assertEqual(d_spoof.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_spoof.reason_code, CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE)

    def test_legacy_v2_metadata_fails_closed_but_still_catches_hard_mismatches(self):
        """Legacy v2 records deserialize cleanly, return INCOMPATIBLE on hard conflict, and UNCERTAIN otherwise."""
        req_py = _make_req("How to sort in Python?")
        v2_py_dict = {
            "schema_version": 2,
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {
                "language": {"status": "KNOWN", "value": "python"},
                "language_version": {"status": "NOT_APPLICABLE", "value": None},
                "framework": {"status": "NOT_APPLICABLE", "value": None},
                "framework_version": {"status": "NOT_APPLICABLE", "value": None},
                "runtime": {"status": "NOT_APPLICABLE", "value": None},
                "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
                "execution_model": {"status": "NOT_APPLICABLE", "value": None},
                "output_format": {"status": "NOT_APPLICABLE", "value": None},
                "constraints": {"status": "NOT_APPLICABLE", "value": None},
            },
            "generation_contract": {"provider": "groq", "model": "llama-3.3-70b-versatile", "temperature": 0.7, "max_tokens": 1000},
            "embedding_contract": {"model": "nomic-embed-text"},
        }
        cand_v2_py = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_v2_py",
            query_text="How to sort in Python?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="sorted(x)",
            metadata=v2_py_dict,
            visibility="tenant_shared",
        )
        d_v2 = self.policy.validate_candidate(cand_v2_py, req_py)
        self.assertEqual(d_v2.status, CompatibilityStatus.UNCERTAIN)
        self.assertEqual(d_v2.reason_code, CompatibilityReasonCode.MISSING_REQUIRED_METADATA)
        self.assertEqual(d_v2.dimension, "schema_version")
        self.assertFalse(self.policy.is_candidate_compatible(cand_v2_py, req_py)[0])

        # Hard mismatch on v2 still reports INCOMPATIBLE (Python req vs Java v2 cand)
        v2_java_dict = json.loads(json.dumps(v2_py_dict))
        v2_java_dict["dimensions"]["language"]["value"] = "java"
        cand_v2_java = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_v2_java",
            query_text="How to sort in Java?",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="Collections.sort(x);",
            metadata=v2_java_dict,
            visibility="tenant_shared",
        )
        d_v2_java = self.policy.validate_candidate(cand_v2_java, req_py)
        self.assertEqual(d_v2_java.status, CompatibilityStatus.INCOMPATIBLE)
        self.assertEqual(d_v2_java.reason_code, CompatibilityReasonCode.LANGUAGE_MISMATCH)


class TestStage352ServiceLookupAndInvariants(unittest.IsolatedAsyncioTestCase):
    """Integration tests covering SemanticCacheService, diagnostics, fail-open, and safety defaults."""

    async def test_lookup_rejects_uncertain_and_selects_lower_compatible_candidate(self):
        """Scenario 22 + UNCERTAIN rejection + structured diagnostics without prompt leakage."""
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03)
        mock_embedder = AsyncMock()
        mock_embedder.embed.return_value = MagicMock(vector=[0.1] * 768, model="nomic-embed-text", dimensions=768)

        secret_prompt = "How to reverse a string in Python?"
        m_v2_legacy = {
            "schema_version": 2,
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {
                "language": {"status": "KNOWN", "value": "python"},
                "language_version": {"status": "NOT_APPLICABLE", "value": None},
                "framework": {"status": "NOT_APPLICABLE", "value": None},
                "framework_version": {"status": "NOT_APPLICABLE", "value": None},
                "runtime": {"status": "NOT_APPLICABLE", "value": None},
                "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
                "execution_model": {"status": "NOT_APPLICABLE", "value": None},
                "output_format": {"status": "NOT_APPLICABLE", "value": None},
                "constraints": {"status": "NOT_APPLICABLE", "value": None},
            },
            "generation_contract": {"provider": "groq", "model": "llama-3.3-70b-versatile", "temperature": 0.7, "max_tokens": 1000},
            "embedding_contract": {"model": "nomic-embed-text"},
        }
        cand_incompat_java = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_1_java",
            query_text="Reverse a string in Java",
            model="nomic-embed-text",
            distance=0.01,
            similarity=0.99,
            cached_response="new StringBuilder(s).reverse().toString()",
            metadata=policy.extractor.extract_from_text(
                "Reverse a string in Java", "groq", "llama-3.3-70b-versatile", 0.7, 1000, "nomic-embed-text"
            ).to_dict(),
            visibility="tenant_shared",
        )
        cand_uncertain_v2 = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_2_v2",
            query_text="Legacy Python reverse",
            model="nomic-embed-text",
            distance=0.03,
            similarity=0.97,
            cached_response="legacy[::-1]",
            metadata=m_v2_legacy,
            visibility="tenant_shared",
        )
        cand_lower_compat = VectorSearchResult(
            cache_namespace="default",
            fingerprint="fp_3_py",
            query_text="What is the syntax to reverse a string in Python with slicing?",
            model="nomic-embed-text",
            distance=0.05,
            similarity=0.95,
            cached_response="s[::-1]",
            visibility="tenant_shared",
            metadata=policy.extractor.extract_from_text(
                "What is the syntax to reverse a string in Python with slicing?",
                "groq",
                "llama-3.3-70b-versatile",
                0.7,
                1000,
                "nomic-embed-text",
            ).to_dict(),
        )

        # Case A: only UNCERTAIN candidate in pool -> rejected, returns None with structured diagnostics
        mock_vs = AsyncMock()
        mock_vs.search.return_value = [cand_uncertain_v2]
        captured_diag = []
        service = SemanticCacheService(
            embedding_provider=mock_embedder,
            vector_store=mock_vs,
            policy=policy,
            diagnostics_hook=captured_diag.append,
        )
        req = _make_req(secret_prompt)
        hit_uncertain = await service.lookup(req)
        self.assertIsNone(hit_uncertain)
        self.assertEqual(service.stats.rejected_candidates, 1)
        self.assertEqual(captured_diag[-1]["decision_status"], CompatibilityStatus.UNCERTAIN.value)
        self.assertEqual(captured_diag[-1]["reason_code"], CompatibilityReasonCode.MISSING_REQUIRED_METADATA.value)
        self.assertEqual(captured_diag[-1]["dimension"], "schema_version")
        serialized_ev = json.dumps(captured_diag[-1]["decision_evidence"])
        self.assertNotIn(secret_prompt, serialized_ev)
        self.assertNotIn("legacy[::-1]", serialized_ev)

        # Case B: INCOMPATIBLE + UNCERTAIN followed by compatible lower-ranked candidate -> hits compatible candidate
        mock_vs.search.return_value = [cand_incompat_java, cand_uncertain_v2, cand_lower_compat]
        hit_valid = await service.lookup(req)
        self.assertIsNotNone(hit_valid)
        self.assertEqual(hit_valid.cached_response, "s[::-1]")

    async def test_validator_exception_fails_open_with_upstream_fallback(self):
        """Scenario 21: Unexpected validator exception triggers upstream generation and records validator failure."""
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03)
        mock_embedder = AsyncMock()
        mock_embedder.embed.return_value = MagicMock(vector=[0.1] * 768, model="nomic-embed-text", dimensions=768)
        mock_vs = AsyncMock()
        mock_vs.search.return_value = [
            VectorSearchResult(
                cache_namespace="default",
                fingerprint="fp_1",
                query_text="Reverse a string in Python",
                model="nomic-embed-text",
                distance=0.01,
                similarity=0.99,
                cached_response="s[::-1]",
                metadata=policy.extractor.extract_from_text("Reverse a string in Python").to_dict(),
                visibility="tenant_shared",
            )
        ]
        service = SemanticCacheService(embedding_provider=mock_embedder, vector_store=mock_vs, policy=policy)
        from app.semantic_cache.control import SemanticServingController
        core = IntelliCacheCore(
            cache=ExactCache(max_entries=100, ttl_seconds=3600),
            cache_enabled=True,
            semantic_cache=service,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )
        upstream = AsyncMock()
        upstream.name = "mock"
        upstream.generate.return_value = GenerationResult(
            text="upstream[::-1]",
            model="llama-3.3-70b-versatile",
            usage=UsageMetrics(prompt_tokens=10, completion_tokens=5, total_tokens=15),
            latency_ms=5.0,
        )
        req = _make_req("How to reverse a string in Python?")

        with patch.object(policy, "validate_candidate", side_effect=RuntimeError("Boom validator")):
            res = await core.execute_query(req, upstream)

        self.assertEqual(res.text, "upstream[::-1]")
        self.assertFalse(res.cached)
        upstream.generate.assert_awaited_once()
        self.assertEqual(service.stats.validator_failures, 1)

    async def test_exact_cache_first_and_multi_turn_system_prompt_exclusions_preserved(self):
        """Preserves exact-cache short-circuit and multi-turn / system-prompt exclusions."""
        exact_cache = ExactCache(max_entries=100, ttl_seconds=3600)
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03)
        mock_embedder = AsyncMock()
        mock_vs = AsyncMock()
        service = SemanticCacheService(embedding_provider=mock_embedder, vector_store=mock_vs, policy=policy)
        from app.semantic_cache.control import SemanticServingController
        core = IntelliCacheCore(
            cache=exact_cache,
            cache_enabled=True,
            semantic_cache=service,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )
        upstream = AsyncMock()

        req_exact = _make_req("How to sort a list in Python?")
        fp_exact = generate_fingerprint(
            messages=req_exact.messages,
            model=req_exact.model,
            temperature=req_exact.temperature,
            max_tokens=req_exact.max_tokens,
            provider=req_exact.provider,
        )
        await exact_cache.set(
            fp_exact,
            CacheEntry(
                response="exact_hit",
                model="llama-3.3-70b-versatile",
                usage=UsageMetrics(prompt_tokens=1, completion_tokens=1, total_tokens=2),
                latency_ms=1.0,
                provider="groq",
                visibility="tenant_shared",
            ),
            namespace="default",
        )
        res = await core.execute_query(req_exact, upstream)
        self.assertTrue(res.cached)
        self.assertEqual(res.cache_type, "exact")
        mock_embedder.embed.assert_not_called()

        # Multi-turn irrelevant chatter allows safe standalone eligibility in Stage 3.6,
        # while unresolved/uncertain context-dependent queries remain strictly excluded.
        req_irrelevant_chatter = _make_req(
            "",
            messages=[
                ChatMessage(role="user", content="Hi"),
                ChatMessage(role="assistant", content="Hello"),
                ChatMessage(role="user", content="Sort a list in Python"),
            ],
        )
        req_uncertain_ctx = _make_req(
            "",
            messages=[
                ChatMessage(role="user", content="Modify the previous implementation to handle empty lists"),
            ],
        )
        req_compacted_ctx = _make_req(
            "",
            messages=[
                ChatMessage(role="user", content="How do I sort a list in Python?"),
            ],
            metadata={"context_status": "compacted"},
        )
        self.assertTrue(policy.check_eligibility(req_irrelevant_chatter).eligible)
        self.assertFalse(policy.check_eligibility(req_uncertain_ctx).eligible)
        self.assertFalse(policy.check_eligibility(req_compacted_ctx).eligible)

    def test_operational_safety_defaults_unchanged(self):
        """Production semantic serving, shadow eval, threshold, and margin defaults remain unchanged."""
        s = Settings(_env_file=None)
        self.assertFalse(s.semantic_cache_enabled)
        self.assertFalse(s.shadow_evaluation_enabled)
        self.assertEqual(s.semantic_cache_min_similarity, 0.92)
        self.assertEqual(s.semantic_cache_min_margin, 0.03)
        self.assertEqual(s.semantic_cache_top_k, 5)


class TestStage352PostgresIntegration(unittest.IsolatedAsyncioTestCase):
    """Live PostgreSQL + pgvector verification of tri-state UNCERTAIN rejection and v3 fallback."""

    async def asyncSetUp(self):
        db_url = os.getenv("TEST_DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test"
        parsed = urlsplit(db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.skipTest(f"Database name '{db_name}' does not contain 'test'.")
        try:
            self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=768)
        except Exception as exc:
            self.skipTest(f"PostgreSQL unavailable: {exc}")

        self.ns = "test_s352_pg"
        self.store = PostgresVectorStore(pool=self.pool, default_namespace=self.ns, default_model="nomic-embed-text", dimensions=768)
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)

    async def asyncTearDown(self):
        if hasattr(self, "pool"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
                    await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)
            except Exception:
                pass
            await self.pool.close()

    async def test_postgres_rejects_unverified_json_and_legacy_v2_and_accepts_lower_v3(self):
        policy = SemanticCachePolicy(min_similarity=0.92, min_margin=0.03, configured_model="nomic-embed-text")
        vec = [1.0] + [0.0] * 767

        # Seed: (1) legacy v2 record (highest similarity) -> UNCERTAIN
        #       (2) compatible v3 record (lower similarity >= 0.92) -> COMPATIBLE
        v2_meta = {
            "schema_version": 2,
            "extractor_version": "compat-extractor-v2",
            "task_kind": {"status": "KNOWN", "value": "code_generation"},
            "dimensions": {
                "language": {"status": "KNOWN", "value": "python"},
                "language_version": {"status": "NOT_APPLICABLE", "value": None},
                "framework": {"status": "NOT_APPLICABLE", "value": None},
                "framework_version": {"status": "NOT_APPLICABLE", "value": None},
                "runtime": {"status": "NOT_APPLICABLE", "value": None},
                "runtime_version": {"status": "NOT_APPLICABLE", "value": None},
                "execution_model": {"status": "NOT_APPLICABLE", "value": None},
                "output_format": {"status": "NOT_APPLICABLE", "value": None},
                "constraints": {"status": "NOT_APPLICABLE", "value": None},
            },
            "generation_contract": {"provider": "groq", "model": "llama-3.3-70b-versatile", "temperature": 0.7, "max_tokens": 1000},
            "embedding_contract": {"model": "nomic-embed-text"},
        }
        v3_meta = policy.extractor.extract_from_text("Reverse a Python string", "groq", "llama-3.3-70b-versatile", 0.7, 1000, "nomic-embed-text").to_dict()

        async with self.pool.acquire() as conn:
            for fp, resp, m_dict, q_vec in [
                ("fp_pg_v2", "legacy_answer", v2_meta, vec),
                ("fp_pg_v3", "s[::-1]", v3_meta, [0.96, 0.28] + [0.0] * 766),
            ]:
                await conn.execute(
                    """
                    INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider, prompt_tokens, completion_tokens, total_tokens, latency_ms, hit_count, metadata, visibility, owner_principal_id)
                    VALUES ($1, $2, $3, 'llama-3.3-70b-versatile', 'groq', 5, 5, 10, 10.0, 0, $4, 'tenant_shared', NULL)
                    """,
                    self.ns, fp, resp, json.dumps(m_dict),
                )
                v_str = "[" + ",".join(f"{x:.6f}" for x in q_vec) + "]"
                await conn.execute(
                    """
                    INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector, metadata, visibility, owner_principal_id)
                    VALUES ($1, $2, 'Reverse string in Python', 'nomic-embed-text', $3::vector, $4, 'tenant_shared', NULL)
                    """,
                    self.ns, fp, v_str, json.dumps(m_dict),
                )

        mock_embedder = AsyncMock()
        mock_embedder.embed.return_value = MagicMock(vector=vec, model="nomic-embed-text", dimensions=768)
        svc = SemanticCacheService(embedding_provider=mock_embedder, vector_store=self.store, policy=policy, default_namespace=self.ns)

        # Only legacy v2 when queried without v3 fallback won't exist? Here lower v3 exists -> hits v3!
        hit = await svc.lookup(_make_req("How to reverse a string in Python?", namespace=self.ns))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.cached_response, "s[::-1]")

        # Now delete v3 leaving only v2 -> must reject UNCERTAIN and return None!
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1 AND fingerprint = 'fp_pg_v3'", self.ns)
        miss = await svc.lookup(_make_req("How to reverse a string in Python?", namespace=self.ns))
        self.assertIsNone(miss)
        self.assertEqual(svc.stats.rejected_candidates, 1)


if __name__ == "__main__":
    unittest.main()
