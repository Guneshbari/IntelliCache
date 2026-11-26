"""
Stage 3.5.3 Comprehensive Test Suite:
- ResponseArtifactVerifier (formats, JSON/YAML/XML/CSV syntax, JSON Schema subset, constraints, truncation)
- EvidenceAuthority (cryptographic HMAC-SHA256 provenance binding, tamper/copy/forgery rejection)
- Response-Aware Cache Admission (ADMIT / REJECT / UNCERTAIN)
- Variant Coexistence & Collision Protection (compute_variant_key)
- Retrieval Starvation Prevention (bounded retrieval_budget=25, top_k=5 preserved)
- Legacy Schema v2 Inspection, Migration, Reversible Exclusion & Rollback
- Full End-to-End Scenarios 1..14 + HTTP /query guardrails
- PostgreSQL + pgvector (+ FastEmbed) Lifecycle, Latency Measurement & EXPLAIN ANALYZE
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import time
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from urllib.parse import urlsplit

import asyncpg
from fastapi.testclient import TestClient

from app.cache.exact_cache import CacheEntry, ExactCache
from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.config import Settings, reload_settings
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.fingerprinting import (
    RESPONSE_AFFECTING_METADATA_KEYS,
    derive_variant_exact_fingerprint,
    generate_fingerprint,
)
from app.core.service import IntelliCacheCore
from app.embeddings.contracts import EmbeddingResult, EmbeddingTask
from app.main import app
from app.metrics import get_metrics_collector
from app.semantic_cache.compatibility import CompatibilityExtractor, CompatibilityValidator
from app.semantic_cache.contracts import (
    AdmissionStatus,
    CompatibilityEvidence,
    CompatibilityReasonCode,
    CompatibilityStatus,
    ConstraintItem,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
    MetadataStatus,
    SemanticCompatibilityMetadata,
    TRUSTED_RESPONSE_VERIFIER_ID,
    TruncationStatus,
    VerificationStatus,
    compute_variant_key,
)
from app.semantic_cache.evidence import (
    EvidenceAuthority,
    compute_canonical_schema_digest,
    get_default_evidence_authority,
)
from app.semantic_cache.legacy import (
    PostgresLegacyLifecycleManager,
    build_migrated_record_metadata,
    classify_legacy_record_metadata,
)
from app.semantic_cache.control import SemanticServingController
from app.semantic_cache.policy import SemanticCachePolicy
from app.semantic_cache.service import SemanticCacheService
from app.semantic_cache.verification import ResponseArtifactVerifier
from app.vector_store.contracts import VectorRecord, VectorSearchResult
from app.vector_store.in_memory_store import InMemoryVectorStore
from app.vector_store.migration import run_vector_migrations
from app.vector_store.postgres_store import PostgresVectorStore


def _req(
    prompt: str,
    *,
    namespace: str = "default",
    model: str = "llama-3.3-70b-versatile",
    provider: str = "groq",
    temperature: float = 0.7,
    max_tokens: int | None = 1000,
    metadata: dict | None = None,
    visibility: str = "tenant_shared",
) -> GenerationRequest:
    return GenerationRequest(
        messages=[ChatMessage(role="user", content=prompt)],
        model=model,
        provider=provider,
        temperature=temperature,
        max_tokens=max_tokens,
        namespace=namespace,
        metadata=metadata or {},
        visibility=visibility,
    )


def _topic_vec(dim: int, angle_deg: float) -> list[float]:
    rad = math.radians(angle_deg)
    vec = [0.0] * dim
    vec[0] = math.cos(rad)
    vec[1] = math.sin(rad)
    return vec


class TopicRoutingEmbedder:
    """Deterministic local embedder assigning controlled cosine similarities by substring topic."""

    def __init__(self, rules: list[tuple[str, float]], dim: int = 768, model: str = "nomic-embed-text"):
        self.rules = rules
        self.dim = dim
        self.model = model
        self.calls: list[tuple[str, EmbeddingTask]] = []

    async def embed(self, text: str, task: EmbeddingTask = EmbeddingTask.QUERY) -> EmbeddingResult:
        self.calls.append((text, task))
        lower = text.lower()
        angle = 45.0
        for substr, deg in self.rules:
            if substr.lower() in lower:
                angle = deg
                break
        vec = _topic_vec(self.dim, angle)
        return EmbeddingResult(vector=vec, model=self.model, dimensions=self.dim)

    async def close(self) -> None:
        return None


class MockUpstream:
    """Controllable async LLM provider returning per-prompt or queued GenerationResult."""

    def __init__(self, responder):
        self.name = "groq"
        self._responder = responder
        self.calls: list[GenerationRequest] = []

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.calls.append(request)
        return self._responder(request)


class TestStage353VerifierAndEvidence(unittest.TestCase):
    """Unit tests covering ResponseArtifactVerifier, EvidenceAuthority, and variant_key."""

    def setUp(self):
        self.extractor = CompatibilityExtractor()
        self.verifier = ResponseArtifactVerifier()
        self.authority = EvidenceAuthority(secret_key="unit-test-evidence-secret")

    def test_json_yaml_xml_csv_plain_text_verification(self):
        # JSON valid vs broken vs markdown-wrapped
        req_json = self.extractor.extract_from_text("Parse log entry in Python and output strict JSON")
        v_ok = self.verifier.verify(
            response_text='{"status":"ok","count":3}',
            request_metadata=req_json,
            finish_reason="stop",
        )
        self.assertEqual(v_ok.status, VerificationStatus.VERIFIED)
        self.assertEqual(v_ok.verified_output_format, "json")
        self.assertTrue(v_ok.verified_json_valid)

        v_bad = self.verifier.verify(response_text='{"status": "ok",', request_metadata=req_json, finish_reason="stop")
        self.assertEqual(v_bad.status, VerificationStatus.FAILED)

        v_md = self.verifier.verify(
            response_text='Here is JSON:\n```json\n{"status":"ok"}\n```',
            request_metadata=req_json,
            finish_reason="stop",
        )
        self.assertEqual(v_md.status, VerificationStatus.FAILED)
        self.assertEqual(v_md.reason_code, CompatibilityReasonCode.VERIFICATION_FAILED_FORMAT)

        # YAML valid vs plain prose sentence
        req_yaml = self.extractor.extract_from_text("Serialize config in Python and output as YAML")
        self.assertEqual(
            self.verifier.verify(
                response_text="host: localhost\nport: 5432\n",
                request_metadata=req_yaml,
                finish_reason="stop",
            ).status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            self.verifier.verify(
                response_text="Here is a YAML explanation in plain prose.",
                request_metadata=req_yaml,
                finish_reason="stop",
            ).status,
            VerificationStatus.FAILED,
        )

        # XML valid vs XXE doctype vs malformed
        req_xml = self.extractor.extract_from_text("Export records in Python and output as XML")
        self.assertEqual(
            self.verifier.verify(
                response_text="<root><item id='1'>ok</item></root>",
                request_metadata=req_xml,
                finish_reason="stop",
            ).status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            self.verifier.verify(
                response_text="<!DOCTYPE foo><root/>",
                request_metadata=req_xml,
                finish_reason="stop",
            ).status,
            VerificationStatus.FAILED,
        )

        # CSV valid vs ragged rows
        req_csv = self.extractor.extract_from_text("Export records in Python and output as CSV")
        self.assertEqual(
            self.verifier.verify(
                response_text="id,name\n1,alice\n2,bob\n",
                request_metadata=req_csv,
                finish_reason="stop",
            ).status,
            VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            self.verifier.verify(
                response_text="id,name\n1,alice,extra\n",
                request_metadata=req_csv,
                finish_reason="stop",
            ).status,
            VerificationStatus.FAILED,
        )

    def test_json_schema_subset_and_fail_closed_unsupported(self):
        schema = {
            "type": "object",
            "properties": {
                "user_id": {"type": "integer", "minimum": 1},
                "role": {"type": "string", "enum": ["admin", "user"]},
            },
            "required": ["user_id", "role"],
            "additionalProperties": False,
        }
        req = _req(
            "Parse input in Python and return JSON conforming to schema",
            metadata={"json_schema": schema},
        )
        req_meta = self.extractor.extract(req, "nomic-embed-text")

        v_pass = self.verifier.verify(
            response_text='{"user_id": 7, "role": "admin"}',
            request_metadata=req_meta,
            query_text=req.messages[0].content,
            finish_reason="stop",
            request_extra_metadata=req.metadata,
        )
        self.assertEqual(v_pass.status, VerificationStatus.VERIFIED)
        self.assertEqual(v_pass.verified_schema_version, 1)
        self.assertEqual(v_pass.verified_schema_digest, compute_canonical_schema_digest(schema))

        v_fail = self.verifier.verify(
            response_text='{"user_id": 0, "role": "guest", "extra": true}',
            request_metadata=req_meta,
            query_text=req.messages[0].content,
            finish_reason="stop",
            request_extra_metadata=req.metadata,
        )
        self.assertEqual(v_fail.status, VerificationStatus.FAILED)
        self.assertEqual(v_fail.reason_code, CompatibilityReasonCode.VERIFICATION_FAILED_SCHEMA)

        # Unsupported JSON Schema keyword ($ref / oneOf) MUST return UNCERTAIN (never approximate)
        unsupported_schema = {"type": "object", "oneOf": [{"required": ["a"]}, {"required": ["b"]}]}
        req_unsup = _req(
            "Return JSON in Python following schema",
            metadata={"json_schema": unsupported_schema},
        )
        meta_unsup = self.extractor.extract(req_unsup, "nomic-embed-text")
        v_unsup = self.verifier.verify(
            response_text='{"a": 1}',
            request_metadata=meta_unsup,
            query_text=req_unsup.messages[0].content,
            finish_reason="stop",
            request_extra_metadata=req_unsup.metadata,
        )
        self.assertEqual(v_unsup.status, VerificationStatus.UNCERTAIN)
        self.assertEqual(v_unsup.reason_code, CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT)
        self.assertIn("schema_adherence", v_unsup.unverified_constraints)

        # Schema hint without any concrete schema fails closed UNCERTAIN
        meta_no_schema = self.extractor.extract_from_text("Return JSON in Python conforming to this schema")
        v_missing_schema = self.verifier.verify(
            response_text='{"a": 1}',
            request_metadata=meta_no_schema,
            query_text="Return JSON in Python conforming to this schema",
            finish_reason="stop",
        )
        self.assertEqual(v_missing_schema.status, VerificationStatus.UNCERTAIN)
        self.assertEqual(v_missing_schema.reason_code, CompatibilityReasonCode.VERIFICATION_UNSUPPORTED_CONSTRAINT)

    def test_truncation_and_max_words_required_keys_verification(self):
        meta = self.extractor.extract_from_text(
            "Summarize auth in Python with required keys: token, expires_in and at most 12 words as strict JSON"
        )
        # Valid response satisfying required_keys and max_words
        v_ok = self.verifier.verify(
            response_text='{"token": "abc", "expires_in": 300}',
            request_metadata=meta,
            finish_reason="stop",
            completion_tokens=15,
            request_max_tokens=1000,
        )
        self.assertEqual(v_ok.status, VerificationStatus.VERIFIED)

        # Missing required key -> FAILED
        v_missing_key = self.verifier.verify(
            response_text='{"token": "abc"}',
            request_metadata=meta,
            finish_reason="stop",
        )
        self.assertEqual(v_missing_key.status, VerificationStatus.FAILED)
        self.assertEqual(v_missing_key.reason_code, CompatibilityReasonCode.VERIFICATION_FAILED_CONSTRAINT)

        # Truncated finish_reason -> FAILED
        v_trunc = self.verifier.verify(
            response_text='{"token": "abc", "expires_in": 300}',
            request_metadata=meta,
            finish_reason="length",
        )
        self.assertEqual(v_trunc.status, VerificationStatus.FAILED)
        self.assertEqual(v_trunc.truncation_status, TruncationStatus.TRUNCATED)

        # Hit max_tokens boundary without stop finish_reason -> FAILED
        v_ceiling = self.verifier.verify(
            response_text='{"token": "abc", "expires_in": 300}',
            request_metadata=meta,
            finish_reason=None,
            completion_tokens=100,
            request_max_tokens=100,
        )
        self.assertEqual(v_ceiling.status, VerificationStatus.FAILED)
        self.assertEqual(v_ceiling.truncation_status, TruncationStatus.TRUNCATED)

        # Unrecognized finish_reason -> UNCERTAIN
        v_unknown_fr = self.verifier.verify(
            response_text='{"token": "abc", "expires_in": 300}',
            request_metadata=meta,
            finish_reason="unexpected_vendor_reason",
        )
        self.assertEqual(v_unknown_fr.status, VerificationStatus.UNCERTAIN)
        self.assertEqual(v_unknown_fr.reason_code, CompatibilityReasonCode.VERIFICATION_UNCERTAIN_TRUNCATION)

    def test_evidence_authority_binding_and_tamper_detection(self):
        meta = self.extractor.extract_from_text("Serialize payload in Python and return strict JSON")
        resp_text = '{"ok": true}'
        ver = self.verifier.verify(response_text=resp_text, request_metadata=meta, finish_reason="stop")
        ev = self.authority.mint_response_verified_evidence(
            cache_namespace="tenant_a",
            fingerprint="fp_original",
            response_text=resp_text,
            metadata=meta,
            verification=ver,
        )
        self.assertTrue(ev.has_bound_signature())

        # Authentic verification passes
        ok, code, _ = self.authority.verify_evidence_binding(
            ev,
            cache_namespace="tenant_a",
            fingerprint="fp_original",
            response_text=resp_text,
            metadata=meta,
        )
        self.assertTrue(ok)
        self.assertEqual(code, CompatibilityReasonCode.COMPATIBLE)

        # Copied to different fingerprint -> EVIDENCE_BINDING_MISMATCH
        ok_fp, code_fp, _ = self.authority.verify_evidence_binding(
            ev,
            cache_namespace="tenant_a",
            fingerprint="fp_other",
            response_text=resp_text,
            metadata=meta,
        )
        self.assertFalse(ok_fp)
        self.assertEqual(code_fp, CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH)

        # Copied to different namespace -> EVIDENCE_BINDING_MISMATCH
        ok_ns, code_ns, _ = self.authority.verify_evidence_binding(
            ev,
            cache_namespace="tenant_b",
            fingerprint="fp_original",
            response_text=resp_text,
            metadata=meta,
        )
        self.assertFalse(ok_ns)
        self.assertEqual(code_ns, CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH)

        # Tampered cached response text -> EVIDENCE_BINDING_MISMATCH
        ok_resp, code_resp, _ = self.authority.verify_evidence_binding(
            ev,
            cache_namespace="tenant_a",
            fingerprint="fp_original",
            response_text='{"ok": false}',
            metadata=meta,
        )
        self.assertFalse(ok_resp)
        self.assertEqual(code_resp, CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH)

        # Forged signature -> EVIDENCE_SIGNATURE_INVALID
        forged = CompatibilityEvidence(
            source=ev.source,
            evidence_tier=ev.evidence_tier,
            verified_output_format=ev.verified_output_format,
            verified_json_valid=ev.verified_json_valid,
            artifact_schema_version=ev.artifact_schema_version,
            verifier_version=ev.verifier_version,
            response_digest=ev.response_digest,
            binding_digest=ev.binding_digest,
            evidence_signature="0" * 64,
            verified_constraints=ev.verified_constraints,
            verified_schema_digest=ev.verified_schema_digest,
        )
        ok_sig, code_sig, _ = self.authority.verify_evidence_binding(
            forged,
            cache_namespace="tenant_a",
            fingerprint="fp_original",
            response_text=resp_text,
            metadata=meta,
        )
        self.assertFalse(ok_sig)
        self.assertEqual(code_sig, CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID)

        # Cannot mint from FAILED or UNCERTAIN
        failed_ver = self.verifier.verify(response_text="not json", request_metadata=meta, finish_reason="stop")
        with self.assertRaises(ValueError):
            self.authority.mint_response_verified_evidence(
                cache_namespace="tenant_a",
                fingerprint="fp_x",
                response_text="not json",
                metadata=meta,
                verification=failed_ver,
            )

    def test_variant_key_isolation_and_config_defaults(self):
        m_go21 = self.extractor.extract_from_text("Write an HTTP handler in Go 1.21")
        m_go22 = self.extractor.extract_from_text("Write an HTTP handler in Go 1.22")
        m_pyd1 = self.extractor.extract_from_text("Define model in Python using pydantic v1")
        m_pyd2 = self.extractor.extract_from_text("Define model in Python using pydantic v2")
        m_pyd1_dup = self.extractor.extract_from_text("Define model in Python using pydantic v1")

        self.assertNotEqual(compute_variant_key(m_go21), compute_variant_key(m_go22))
        self.assertNotEqual(compute_variant_key(m_pyd1), compute_variant_key(m_pyd2))
        self.assertEqual(compute_variant_key(m_pyd1), compute_variant_key(m_pyd1_dup))

        # Safe default preservation check
        s = Settings(_env_file=None)
        self.assertFalse(s.semantic_cache_enabled)
        self.assertFalse(s.shadow_evaluation_enabled)
        self.assertEqual(s.semantic_cache_min_similarity, 0.92)
        self.assertEqual(s.semantic_cache_min_margin, 0.03)
        self.assertEqual(s.semantic_cache_top_k, 5)
        self.assertEqual(s.semantic_cache_max_retrieval_candidates, 25)

    def test_persistent_evidence_secret_validation_restart_multiworker_and_rotation(self):
        # 1. Persistent PostgreSQL semantic cache MUST reject missing secret at config load
        with self.assertRaises(ValueError) as ctx_missing:
            Settings(
                _env_file=None,
                groq_api_key="gsk_test",
                cache_backend="postgres",
                vector_store_backend="postgres",
                database_url="postgresql://user:pass@localhost:5432/intellicache_test",
                semantic_cache_enabled=True,
            )
        self.assertIn("SEMANTIC_CACHE_EVIDENCE_SECRET is required", str(ctx_missing.exception))

        # 2. Rejects weak/placeholder/short/repeating/low-diversity secrets (minimum-format sanity checks)
        for weak in (
            "changeme",
            "short-secret-123",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            "abcd1234abcd1234abcd1234abcd1234",
            "01234567890123456789012345678901",
            "aaaaaaaaaaaaaaaaaaaaaaaaaaaa1234",
        ):
            with self.assertRaises(ValueError):
                Settings(
                    _env_file=None,
                    groq_api_key="gsk_test",
                    cache_backend="postgres",
                    vector_store_backend="postgres",
                    database_url="postgresql://user:pass@localhost:5432/intellicache_test",
                    semantic_cache_enabled=True,
                    semantic_cache_evidence_secret=weak,
                )

        # Valid 256-bit hex secret format (equivalent to `openssl rand -hex 32`) passes
        hex_secret = "9f86d081884c7d659a2feaa0c55ad015a3bf4f1b2b0b822cd15d6c15b0f00a08"
        valid_cfg = Settings(
            _env_file=None,
            groq_api_key="gsk_test",
            cache_backend="postgres",
            vector_store_backend="postgres",
            database_url="postgresql://user:pass@localhost:5432/intellicache_test",
            semantic_cache_enabled=True,
            semantic_cache_evidence_secret=hex_secret,
        )
        self.assertEqual(valid_cfg.semantic_cache_evidence_secret, hex_secret)

        # 3. Persistent EvidenceAuthority refuses ephemeral runtime fallback
        with patch.dict(os.environ, {"SEMANTIC_CACHE_EVIDENCE_SECRET": ""}, clear=False):
            with self.assertRaises(ValueError):
                EvidenceAuthority(allow_ephemeral=False)

        # 4. Restart & multi-worker sharing + zero-downtime key rotation
        k1 = "primary-evidence-secret-k1-0123456789abcdef"
        k2 = "rotated-evidence-secret-k2-fedcba9876543210"
        worker_1 = EvidenceAuthority(secret_key=k1, allow_ephemeral=False)
        worker_2_restart = EvidenceAuthority(secret_key=k1, allow_ephemeral=False)

        meta = self.extractor.extract_from_text("Serialize payload in Python and output strict JSON")
        resp = '{"status": "ready"}'
        ver = self.verifier.verify(response_text=resp, request_metadata=meta, finish_reason="stop")
        ev_k1 = worker_1.mint_response_verified_evidence(
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
            verification=ver,
        )
        self.assertIsNotNone(ev_k1.signing_key_id)

        # Worker 2 / Restarted process with same key K1 verifies cleanly
        ok_w2, _, _ = worker_2_restart.verify_evidence_binding(
            ev_k1,
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
        )
        self.assertTrue(ok_w2)

        # Rotated worker with primary=K2 and previous=[K1] still verifies K1 AND mints with K2
        worker_rotated = EvidenceAuthority(secret_key=k2, previous_secret_keys=[k1], allow_ephemeral=False)
        ok_rot, _, _ = worker_rotated.verify_evidence_binding(
            ev_k1,
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
        )
        self.assertTrue(ok_rot)
        ev_k2 = worker_rotated.mint_response_verified_evidence(
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
            verification=ver,
        )
        self.assertNotEqual(ev_k2.signing_key_id, ev_k1.signing_key_id)

        # After draining old K1 secret, K1 evidence fails closed while K2 succeeds
        worker_k2_only = EvidenceAuthority(secret_key=k2, allow_ephemeral=False)
        ok_old_dropped, code_dropped, _ = worker_k2_only.verify_evidence_binding(
            ev_k1,
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
        )
        self.assertFalse(ok_old_dropped)
        self.assertEqual(code_dropped, CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID)
        ok_new_only, _, _ = worker_k2_only.verify_evidence_binding(
            ev_k2,
            cache_namespace="prod_ns",
            fingerprint="fp_persistent",
            response_text=resp,
            metadata=meta,
        )
        self.assertTrue(ok_new_only)


class TestStage353EndToEndInMemory(unittest.IsolatedAsyncioTestCase):
    """End-to-end IntelliCacheCore + InMemoryVectorStore lifecycle scenarios (Scenarios 1..14)."""

    async def asyncSetUp(self):
        get_metrics_collector().reset()
        self.authority = EvidenceAuthority(secret_key="e2e-inmemory-secret")
        self.exact_cache = ExactCache(max_entries=200, ttl_seconds=3600)
        self.vector_store = InMemoryVectorStore(
            dimensions=768,
            default_namespace="default",
            default_model="nomic-embed-text",
            exact_cache=self.exact_cache,
        )
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            retrieval_budget=25,
            evidence_authority=self.authority,
        )

    def _build_core(self, embedder, responder):
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.vector_store,
            policy=self.policy,
            evidence_authority=self.authority,
        )
        upstream = MockUpstream(responder)
        core = IntelliCacheCore(
            cache=self.exact_cache,
            cache_enabled=True,
            semantic_cache=svc,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )
        return svc, upstream, core

    async def test_scenarios_1_to_3_and_11_variant_coexistence(self):
        # All prompts share high semantic vector angle (sim ~0.996 between 0 deg and 5 deg)
        embedder = TopicRoutingEmbedder(
            [
                ("go 1.21", 0.0),
                ("go 1.22", 2.0),
                ("pydantic v1", 3.0),
                ("pydantic v2", 4.0),
                ("python", 1.0),
                ("java", 1.5),
            ]
        )

        def responder(req: GenerationRequest) -> GenerationResult:
            p = req.messages[0].content.lower()
            if "java" in p:
                txt = "public class Sort {}"
            elif "go 1.21" in p:
                txt = "func main() { /* go1.21 */ }"
            elif "go 1.22" in p:
                txt = "func main() { /* go1.22 ServeMux */ }"
            elif "pydantic v1" in p:
                txt = "class User(BaseModel): class Config: orm_mode = True"
            elif "pydantic v2" in p:
                txt = "class User(BaseModel): model_config = ConfigDict(from_attributes=True)"
            else:
                txt = "def sort_list(x): return sorted(x)"
            return GenerationResult(
                text=txt,
                model=req.model,
                usage=UsageMetrics(prompt_tokens=10, completion_tokens=10, total_tokens=20),
                latency_ms=10.0,
                finish_reason="stop",
            )

        svc, upstream, core = self._build_core(embedder, responder)

        # Scenario 1: Python vs Java
        r_py = await core.execute_query(_req("Write a sort helper function in Python"), upstream)
        self.assertFalse(r_py.cached)
        r_java = await core.execute_query(_req("Write a sort helper function in Java"), upstream)
        self.assertFalse(r_java.cached)
        self.assertEqual(r_java.text, "public class Sort {}")

        # Scenario 2: Go 1.21 vs Go 1.22
        r_go21 = await core.execute_query(_req("Write HTTP handler code in Go 1.21"), upstream)
        r_go22 = await core.execute_query(_req("Write HTTP handler code in Go 1.22"), upstream)
        self.assertFalse(r_go21.cached)
        self.assertFalse(r_go22.cached)
        self.assertIn("go1.22", r_go22.text)

        # Scenario 3: Pydantic v1 vs Pydantic v2
        r_p1 = await core.execute_query(_req("Write model config in Python using pydantic v1"), upstream)
        r_p2 = await core.execute_query(_req("Write model config in Python using pydantic v2"), upstream)
        self.assertFalse(r_p1.cached)
        self.assertFalse(r_p2.cached)
        self.assertIn("ConfigDict", r_p2.text)

        # Confirm all 6 variants coexist in vector store and subsequent paraphrased Go 1.22 query hits ONLY Go 1.22
        self.assertEqual(await self.vector_store.get_record_count("default"), 6)
        r_go22_hit = await core.execute_query(_req("Implement HTTP handler example in Go 1.22"), upstream)
        self.assertTrue(r_go22_hit.cached)
        self.assertEqual(r_go22_hit.cache_type, "semantic")
        self.assertEqual(r_go22_hit.text, "func main() { /* go1.22 ServeMux */ }")

        # Paraphrased Pydantic v1 query hits ONLY Pydantic v1
        r_p1_hit = await core.execute_query(_req("Show model config in Python with pydantic v1"), upstream)
        self.assertTrue(r_p1_hit.cached)
        self.assertEqual(r_p1_hit.cache_type, "semantic")
        self.assertIn("orm_mode", r_p1_hit.text)

    async def test_scenarios_4_5_6_7_8_response_aware_admission_gates(self):
        embedder = TopicRoutingEmbedder(
            [
                ("event payload", 0.0),
                ("config payload", 35.0),
                ("metrics json", 65.0),
                ("summary json", 95.0),
                ("user entity", 125.0),
                ("xsd", 155.0),
            ]
        )
        responses = {}

        def responder(req: GenerationRequest) -> GenerationResult:
            p = req.messages[0].content
            val = responses[p]
            return GenerationResult(
                text=val["text"],
                model=req.model,
                usage=UsageMetrics(
                    prompt_tokens=10,
                    completion_tokens=val.get("completion_tokens", 12),
                    total_tokens=22,
                ),
                latency_ms=8.0,
                finish_reason=val.get("finish_reason", "stop"),
            )

        svc, upstream, core = self._build_core(embedder, responder)

        # Scenario 4a: Broken JSON blocked from admission; exact cache & response preserved
        p_bad_json = "Serialize event payload in Python and output strict JSON"
        responses[p_bad_json] = {"text": "{bad json", "finish_reason": "stop"}
        res_bad = await core.execute_query(_req(p_bad_json), upstream)
        self.assertEqual(res_bad.text, "{bad json")
        self.assertFalse(res_bad.cached)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.REJECT)
        self.assertEqual(await self.vector_store.get_record_count("default"), 0)
        # Paraphrase misses semantic cache because invalid JSON was never admitted
        p_valid_json = "Format event payload in Python and output strict JSON"
        responses[p_valid_json] = {"text": '{"event":"login","ok":true}', "finish_reason": "stop"}
        res_valid = await core.execute_query(_req(p_valid_json), upstream)
        self.assertFalse(res_valid.cached)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.ADMIT)
        self.assertEqual(await self.vector_store.get_record_count("default"), 1)

        # Paraphrase NOW hits the admitted valid JSON
        res_hit = await core.execute_query(_req("Return event payload in Python and output strict JSON"), upstream)
        self.assertTrue(res_hit.cached)
        self.assertEqual(res_hit.cache_type, "semantic")
        self.assertEqual(res_hit.text, '{"event":"login","ok":true}')

        # Scenario 6: Markdown returned when strict JSON expected -> REJECT
        p_md_for_json = "Build config payload in Python and return JSON only"
        responses[p_md_for_json] = {"text": "```json\n{\"k\":1}\n```", "finish_reason": "stop"}
        await core.execute_query(_req(p_md_for_json), upstream)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.REJECT)

        # Scenario 7: Truncated JSON response (finish_reason="length" & max_tokens hit) -> REJECT
        p_trunc = "Produce metrics JSON object in Python as strict JSON"
        responses[p_trunc] = {"text": '{"ok": true}', "finish_reason": "length"}
        await core.execute_query(_req(p_trunc), upstream)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.REJECT)
        self.assertEqual(
            svc.last_admission_decision.verification.reason_code,
            CompatibilityReasonCode.VERIFICATION_FAILED_TRUNCATED,
        )

        p_trunc_max = "Produce summary JSON object in Python as strict JSON"
        responses[p_trunc_max] = {"text": '{"ok": true}', "finish_reason": None, "completion_tokens": 50}
        await core.execute_query(_req(p_trunc_max, max_tokens=50), upstream)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.REJECT)

        # Scenario 5: Schema-constrained JSON failing concrete schema -> REJECT; conforming -> ADMIT & HIT
        schema = {
            "type": "object",
            "properties": {"id": {"type": "integer"}, "active": {"type": "boolean"}},
            "required": ["id", "active"],
            "additionalProperties": False,
        }
        p_schema_bad = "Parse user entity in Python as JSON conforming to schema"
        responses[p_schema_bad] = {"text": '{"id": "wrong_type", "active": true}', "finish_reason": "stop"}
        await core.execute_query(_req(p_schema_bad, metadata={"json_schema": schema}), upstream)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.REJECT)
        self.assertEqual(
            svc.last_admission_decision.verification.reason_code,
            CompatibilityReasonCode.VERIFICATION_FAILED_SCHEMA,
        )

        p_schema_ok = "Build user entity in Python as JSON conforming to schema"
        responses[p_schema_ok] = {"text": '{"id": 99, "active": true}', "finish_reason": "stop"}
        await core.execute_query(_req(p_schema_ok, metadata={"json_schema": schema}), upstream)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.ADMIT)

        # Semantic query with same schema hits, query with DIFFERENT schema misses
        res_schema_hit = await core.execute_query(
            _req("Serialize user entity in Python as JSON conforming to schema", metadata={"json_schema": schema}),
            upstream,
        )
        self.assertTrue(res_schema_hit.cached)
        self.assertEqual(res_schema_hit.text, '{"id": 99, "active": true}')

        other_schema = {"type": "object", "properties": {"uuid": {"type": "string"}}, "required": ["uuid"]}
        p_other = "Serialize user entity in Python as JSON conforming to schema"
        responses[p_other] = {"text": '{"uuid": "abc"}', "finish_reason": "stop"}
        res_other = await core.execute_query(_req(p_other, metadata={"json_schema": other_schema}), upstream)
        self.assertFalse(res_other.cached)

        # Scenario 8: Unsupported/unverifiable constraint -> UNCERTAIN, not admitted
        p_unsup = "Export report in Python conforming to XSD"
        responses[p_unsup] = {"text": "<report/>", "finish_reason": "stop"}
        res_unsup = await core.execute_query(_req(p_unsup), upstream)
        self.assertFalse(res_unsup.cached)
        self.assertEqual(svc.last_admission_decision.status, AdmissionStatus.UNCERTAIN)
        self.assertIn(
            "unverified_execution_constraint",
            svc.last_admission_decision.verification.unverified_constraints,
        )

    async def test_scenario_11_same_fingerprint_concurrent_and_sequential_variant_coexistence(self):
        schema_a = {"type": "object", "properties": {"x": {"type": "integer"}}, "required": ["x"]}
        schema_b = {"type": "object", "properties": {"y": {"type": "string"}}, "required": ["y"]}
        embedder = TopicRoutingEmbedder([("encode", 0.0)])
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.vector_store,
            policy=self.policy,
            evidence_authority=self.authority,
        )

        # Identical prompt, model, temperature, max_tokens, provider & exact shared fingerprint
        prompt_text = "Encode item in Python conforming to schema"
        req_a = _req(prompt_text, metadata={"json_schema": schema_a})
        req_b = _req(prompt_text, metadata={"json_schema": schema_b})
        shared_fp = generate_fingerprint(
            messages=req_a.messages,
            model=req_a.model,
            temperature=req_a.temperature,
            max_tokens=req_a.max_tokens,
            provider=req_a.provider,
        )
        await self.exact_cache.set(
            shared_fp,
            CacheEntry(
                response='{"x": 1}',
                model=req_a.model,
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=4.0,
                provider=req_a.provider,
                visibility="tenant_shared",
            ),
            namespace="default",
        )
        res_a = GenerationResult(
            text='{"x": 1}',
            model=req_a.model,
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=4.0,
            fingerprint=shared_fp,
            finish_reason="stop",
        )
        res_b = GenerationResult(
            text='{"y": "hi"}',
            model=req_b.model,
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=4.0,
            fingerprint=shared_fp,
            finish_reason="stop",
        )

        # Concurrent admission sharing identical fingerprint stores BOTH variants atomically
        results = await asyncio.gather(svc.index(req_a, res_a), svc.index(req_b, res_b))
        self.assertEqual(results, [True, True])
        self.assertEqual(await self.vector_store.get_record_count("default"), 2)

        hit_a = await svc.lookup(req_a)
        hit_b = await svc.lookup(req_b)
        self.assertIsNotNone(hit_a)
        self.assertIsNotNone(hit_b)
        self.assertEqual(hit_a.cached_response, '{"x": 1}')
        self.assertEqual(hit_b.cached_response, '{"y": "hi"}')

        # Updating SAME variant key updates in-place without duplicating count
        res_a_new = GenerationResult(
            text='{"x": 2}',
            model=req_a.model,
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=4.0,
            fingerprint=shared_fp,
            finish_reason="stop",
        )
        self.assertTrue(await svc.index(req_a, res_a_new))
        self.assertEqual(await self.vector_store.get_record_count("default"), 2)
        hit_a_new = await svc.lookup(req_a)
        self.assertEqual(hit_a_new.cached_response, '{"x": 2}')

        # Also verify full IntelliCacheCore execution isolates exact-cache & semantic cache per schema
        calls = []

        def core_responder(req: GenerationRequest) -> GenerationResult:
            calls.append(req)
            if req.metadata.get("json_schema") == schema_a:
                body = '{"x": 77}'
            else:
                body = '{"y": "core_b"}'
            return GenerationResult(
                text=body,
                model=req.model,
                usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12),
                latency_ms=5.0,
                finish_reason="stop",
            )

        await self.exact_cache.clear(namespace="default")
        await self.vector_store.clear(namespace="default")
        core_svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.vector_store,
            policy=self.policy,
            evidence_authority=self.authority,
        )
        core = IntelliCacheCore(
            cache=self.exact_cache,
            cache_enabled=True,
            semantic_cache=core_svc,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )
        out_a = await core.execute_query(_req("Serialize node in Python conforming to schema", metadata={"json_schema": schema_a}), MockUpstream(core_responder))
        out_b = await core.execute_query(_req("Serialize node in Python conforming to schema", metadata={"json_schema": schema_b}), MockUpstream(core_responder))
        self.assertFalse(out_a.cached)
        self.assertFalse(out_b.cached)
        self.assertEqual(out_a.text, '{"x": 77}')
        self.assertEqual(out_b.text, '{"y": "core_b"}')
        self.assertEqual(len(calls), 2)

        # Second identical Schema B request hits exact cache for Schema B (not Schema A)
        out_b_exact = await core.execute_query(_req("Serialize node in Python conforming to schema", metadata={"json_schema": schema_b}), MockUpstream(core_responder))
        self.assertTrue(out_b_exact.cached)
        self.assertEqual(out_b_exact.text, '{"y": "core_b"}')

    async def test_exact_fingerprint_allowlist_and_constraint_isolation(self):
        prompt = "Summarize sorting algorithms in Python"
        msgs = [ChatMessage(role="user", content=prompt)]
        base_fp = generate_fingerprint(msgs, "llama-3.3-70b-versatile", 0.0, 512, "groq")

        # 1. Operational metadata MUST NOT fragment exact-cache fingerprint
        op_fp = derive_variant_exact_fingerprint(
            base_fp,
            {"turn_id": "turn_123", "context_status": "fresh", "request_id": "req_9", "trace_id": "abc"},
        )
        self.assertEqual(op_fp, base_fp)

        # 2. Every allowlisted response-affecting key MUST isolate exact fingerprints when differing
        sample_values = {
            "json_schema": {"type": "object", "properties": {"a": {"type": "integer"}}},
            "output_schema": {"type": "object", "properties": {"b": {"type": "string"}}},
            "response_schema": {"type": "object", "properties": {"c": {"type": "boolean"}}},
            "output_format": "json",
            "output_constraints": {"mode": "strict"},
            "constraints": ["no_prose"],
            "strict_format": "json_only",
            "markup": "no_markdown",
            "code_only": True,
            "required_keys": ["id", "name"],
            "max_words": 20,
            "max_chars": 120,
            "schema_adherence": "sha256:abc",
        }
        self.assertEqual(set(sample_values.keys()), set(RESPONSE_AFFECTING_METADATA_KEYS))
        for key, val in sample_values.items():
            derived = derive_variant_exact_fingerprint(base_fp, {key: val, "turn_id": "ignored"})
            self.assertNotEqual(derived, base_fp, f"Expected allowlist key '{key}' to isolate exact fingerprint")

        # 3. Canonical equivalence across ordering/casing/type differences
        self.assertEqual(
            derive_variant_exact_fingerprint(base_fp, {"required_keys": ["name", "id"], "max_words": "20"}),
            derive_variant_exact_fingerprint(base_fp, {"required_keys": " id , name ", "max_words": 20}),
        )
        self.assertEqual(
            derive_variant_exact_fingerprint(base_fp, {"output_format": "JSON", "strict_format": " JSON_ONLY "}),
            derive_variant_exact_fingerprint(base_fp, {"output_format": "json", "strict_format": "json_only"}),
        )

        # 4. End-to-end IntelliCacheCore exact + semantic isolation across non-schema verifier constraints
        calls = []

        def constraint_responder(req: GenerationRequest) -> GenerationResult:
            calls.append(req.metadata)
            if req.metadata.get("max_words") == 3:
                txt = "Two words"
            elif req.metadata.get("markup") == "no_markdown":
                txt = "Quicksort partitions arrays in place without markdown."
            else:
                txt = '{"id": 1, "name": "ok"}'
            return GenerationResult(
                text=txt,
                model=req.model,
                usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12),
                latency_ms=4.0,
                finish_reason="stop",
            )

        embedder = TopicRoutingEmbedder([("sorting", 0.0)])
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.vector_store,
            policy=self.policy,
            evidence_authority=self.authority,
        )
        core = IntelliCacheCore(
            cache=self.exact_cache,
            cache_enabled=True,
            semantic_cache=svc,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )

        r1 = await core.execute_query(
            _req(prompt, metadata={"max_words": 3}),
            MockUpstream(constraint_responder),
        )
        r2 = await core.execute_query(
            _req(prompt, metadata={"max_words": 50, "markup": "no_markdown"}),
            MockUpstream(constraint_responder),
        )
        r3 = await core.execute_query(
            _req(prompt, metadata={"output_format": "json", "strict_format": "json_only", "required_keys": ["id", "name"]}),
            MockUpstream(constraint_responder),
        )
        self.assertFalse(r1.cached)
        self.assertFalse(r2.cached)
        self.assertFalse(r3.cached)
        self.assertEqual(len(calls), 3)
        self.assertEqual(r1.text, "Two words")
        self.assertEqual(r2.text, "Quicksort partitions arrays in place without markdown.")
        self.assertEqual(r3.text, '{"id": 1, "name": "ok"}')

        # Repeated call for each constraint profile hits exact cache cleanly with its own variant
        r2_hit = await core.execute_query(
            _req(prompt, metadata={"max_words": 50, "markup": "no_markdown"}),
            MockUpstream(constraint_responder),
        )
        self.assertTrue(r2_hit.cached)
        self.assertEqual(r2_hit.cache_type, "exact")
        self.assertEqual(r2_hit.text, r2.text)

    async def test_bounded_retrieval_budget_exhaustion_telemetry(self):
        ext = CompatibilityExtractor()
        embedder = TopicRoutingEmbedder([("reverse", 0.0)])
        from app.cache.exact_cache import CacheEntry

        # Seed 25 incompatible Java records at higher similarity + 1 compatible Python record at rank 26
        for i in range(25):
            fp = f"fp_incompat_budget_{i}"
            qp = f"Reverse list {i} in Java"
            await self.exact_cache.set(
                fp,
                CacheEntry(
                    response=f"java_{i}",
                    model="llama-3.3-70b-versatile",
                    usage=UsageMetrics(),
                    latency_ms=2.0,
                    provider="groq",
                    visibility="tenant_shared",
                ),
                namespace="default",
            )
            m = ext.extract_from_text(qp, configured_model="nomic-embed-text").to_dict()
            await self.vector_store.upsert(
                VectorRecord(
                    cache_namespace="default",
                    fingerprint=fp,
                    query_text=qp,
                    vector=_topic_vec(768, float(i) * 0.2),
                    model="nomic-embed-text",
                    visibility="tenant_shared",
                    metadata=m,
                    response_text=f"java_{i}",
                )
            )

        target_req = _req("Reverse list in Python")
        target_meta = ext.extract(target_req, "nomic-embed-text")
        ver = ResponseArtifactVerifier().verify(response_text="list(reversed(x))", request_metadata=target_meta, finish_reason="stop")
        ev = self.authority.mint_response_verified_evidence(
            cache_namespace="default",
            fingerprint="fp_compat_rank26",
            response_text="list(reversed(x))",
            metadata=target_meta,
            verification=ver,
            visibility="tenant_shared",
        )
        m_target = SemanticCompatibilityMetadata(
            schema_version=target_meta.schema_version,
            extractor_version=target_meta.extractor_version,
            task_kind=target_meta.task_kind,
            dimensions=target_meta.dimensions,
            generation_contract=target_meta.generation_contract,
            embedding_contract=target_meta.embedding_contract,
            compatibility_evidence=ev,
            context_contract=target_meta.context_contract,
        ).to_dict()
        vk_target = compute_variant_key(target_meta)
        m_target["variant_key"] = vk_target
        await self.exact_cache.set(
            "fp_compat_rank26",
            CacheEntry(
                response="list(reversed(x))",
                model="llama-3.3-70b-versatile",
                usage=UsageMetrics(),
                latency_ms=2.0,
                provider="groq",
                visibility="tenant_shared",
            ),
            namespace="default",
        )
        await self.vector_store.upsert(
            VectorRecord(
                cache_namespace="default",
                fingerprint="fp_compat_rank26",
                query_text="Reverse list in Python",
                vector=_topic_vec(768, 12.0),
                model="nomic-embed-text",
                visibility="tenant_shared",
                metadata=m_target,
                variant_key=vk_target,
                response_text="list(reversed(x))",
            )
        )

        # With default budget 25: rank-26 compatible entry is outside budget -> budget exhausted cleanly recorded
        svc_25 = SemanticCacheService(embedding_provider=embedder, vector_store=self.vector_store, policy=self.policy, evidence_authority=self.authority)
        diag = []
        miss = await svc_25.lookup(_req("How do I reverse a list in Python?"), diagnostics_hook=diag.append)
        self.assertIsNone(miss)
        self.assertTrue(diag[-1]["all_candidates_rejected"])
        self.assertTrue(diag[-1]["retrieval_budget_exhausted"])
        self.assertEqual(diag[-1]["retrieval_budget"], 25)
        self.assertEqual(svc_25.get_stats("default")["all_candidates_rejected_misses"], 1)
        self.assertEqual(svc_25.get_stats("default")["retrieval_budget_exhaustions"], 1)

        # Expanding budget to 30 recovers rank-26 compatible hit
        policy_30 = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            retrieval_budget=30,
            evidence_authority=self.authority,
        )
        svc_30 = SemanticCacheService(embedding_provider=embedder, vector_store=self.vector_store, policy=policy_30, evidence_authority=self.authority)
        hit = await svc_30.lookup(_req("How do I reverse a list in Python?"))
        self.assertIsNotNone(hit)
        self.assertEqual(hit.cached_response, "list(reversed(x))")

    async def test_scenarios_12_and_13_and_14_security_fail_open_and_namespaces(self):
        # Scenario 13: Vector store upsert failure during index() fails open without breaking request
        embedder = TopicRoutingEmbedder([("python", 0.0)])
        svc, upstream, core = self._build_core(
            embedder,
            lambda req: GenerationResult(
                text="def f(): return 1",
                model=req.model,
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=4.0,
                finish_reason="stop",
            ),
        )
        with patch.object(self.vector_store, "upsert", side_effect=RuntimeError("DB write timeout")):
            res = await core.execute_query(_req("Write helper in Python"), upstream)
        self.assertEqual(res.text, "def f(): return 1")
        self.assertFalse(res.cached)
        # Exact cache STILL serves immediately even though vector upsert failed!
        res_exact = await core.execute_query(_req("Write helper in Python"), upstream)
        self.assertTrue(res_exact.cached)
        self.assertEqual(res_exact.cache_type, "exact")

        # Scenario 14: Namespace isolation
        req_ns_a = _req("Write helper in Python to sum numbers", namespace="tenant_a")
        await self.exact_cache.set(
            "fp_ns_a",
            await self.exact_cache.get(
                generate_fingerprint(
                    messages=_req("Write helper in Python").messages,
                    model=req_ns_a.model,
                    temperature=req_ns_a.temperature,
                    max_tokens=req_ns_a.max_tokens,
                    provider=req_ns_a.provider,
                ),
                namespace="default",
            ),
            namespace="tenant_a",
        )
        res_a = GenerationResult(
            text="def sum_nums(a,b): return a+b",
            model=req_ns_a.model,
            usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
            latency_ms=4.0,
            fingerprint="fp_ns_a",
            finish_reason="stop",
        )
        await self.exact_cache.set(
            "fp_ns_a",
            type(await self.exact_cache.get(generate_fingerprint(
                messages=_req("Write helper in Python").messages,
                model=req_ns_a.model,
                temperature=req_ns_a.temperature,
                max_tokens=req_ns_a.max_tokens,
                provider=req_ns_a.provider,
            ), namespace="default"))(
                response="def sum_nums(a,b): return a+b",
                model=req_ns_a.model,
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=4.0,
                provider="groq",
            ),
            namespace="tenant_a",
        )
        await svc.index(req_ns_a, res_a)
        hit_b = await svc.lookup(_req("Python function to sum numbers", namespace="tenant_b"))
        self.assertIsNone(hit_b)

    async def test_granular_semantic_and_admission_metrics_without_substring_fallback(self):
        embedder = TopicRoutingEmbedder([("alpha", 0.0), ("beta", 45.0)])
        svc, upstream, core = self._build_core(
            embedder,
            lambda req: GenerationResult(
                text="{broken" if "bad" in req.messages[0].content.lower() else '{"ok":true}',
                model=req.model,
                usage=UsageMetrics(prompt_tokens=5, completion_tokens=5, total_tokens=10),
                latency_ms=4.0,
                finish_reason="stop",
            ),
        )
        # Empty vector store -> no_candidate_misses += 1
        await core.execute_query(_req("Write alpha json parser in Python and output strict JSON"), upstream)
        # Bad JSON -> admission_rejected += 1
        await core.execute_query(_req("Write bad beta json parser in Python and output strict JSON"), upstream)
        # Threshold rejection (alpha vector vs beta query -> cos(45 deg) ~0.707 < 0.92)
        await svc.lookup(_req("Parse beta payload in Python and output strict JSON"))
        # Custom policy returning None without diagnostics must NOT trigger substring heuristic fallbacks
        with patch.object(self.policy, "evaluate_candidates", return_value=(None, "custom ambiguous compatible failure")):
            self.policy._last_evaluation_diagnostics = None
            await svc.lookup(_req("Write alpha helper in Python and output strict JSON"))

        stats = svc.get_stats("default")
        self.assertEqual(stats["admission_verified"], 1)
        self.assertEqual(stats["admission_rejected"], 1)
        self.assertEqual(stats["no_candidate_misses"], 1)
        self.assertEqual(stats["threshold_rejections"], 2)
        # Ensure substring-based counts remained 0 when structured diagnostics were absent
        self.assertEqual(stats["ambiguous_candidates"], 0)
        self.assertEqual(stats["rejected_candidates"], 0)


class TestStage353HttpIngressHardening(unittest.TestCase):
    """Verifies HTTP /query rejects client-supplied compatibility/verification evidence."""

    def setUp(self):
        get_metrics_collector().reset()
        self.env_patcher = patch.dict(os.environ, {"ALLOW_UNAUTHENTICATED_LOOPBACK": "true"}, clear=False)
        self.env_patcher.start()
        reload_settings()
        self.client = TestClient(app)

    def tearDown(self):
        self.env_patcher.stop()
        reload_settings()
        app.dependency_overrides.clear()

    def test_client_forged_evidence_rejected_at_api_ingress(self):
        for payload in (
            {"prompt": "Hello in Python", "verified_json_valid": True},
            {"prompt": "Hello in Python", "version_independent": True},
            {"prompt": "Hello in Python", "compatibility_evidence": {"evidence_tier": "response_verified"}},
            {"prompt": "Hello in Python", "metadata": {"evidence_tier": "response_verified"}},
        ):
            resp = self.client.post("/query", json=payload)
            self.assertEqual(resp.status_code, 422, f"Expected 422 for forged payload {payload}, got {resp.status_code}")


class TestStage353PostgresPgvectorLifecycle(unittest.IsolatedAsyncioTestCase):
    """
    Live PostgreSQL + pgvector (+ FastEmbed when available) end-to-end verification:
    - Starvation prevention with >=5 incompatible and legacy v2 rows outranking compatible v3
    - Legacy v2 inspection, real ResponseArtifactVerifier migration, reversible exclusion & rollback
    - Variant coexistence + cryptographic tamper/copy detection against real DB rows
    - Latency measurement & EXPLAIN ANALYZE plan collection
    """

    async def asyncSetUp(self):
        db_url = os.getenv("TEST_DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_test"
        parsed = urlsplit(db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.skipTest(f"Database '{db_name}' is not a test DB.")
        try:
            self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=3)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=768)
        except Exception as exc:
            self.skipTest(f"PostgreSQL unavailable: {exc}")

        self.ns = "test_s353_pg"
        self.authority = EvidenceAuthority(secret_key="pg-s353-secret")
        self.cache = PostgresCache(pool=self.pool, max_entries=1000, ttl_seconds=3600, default_namespace=self.ns)
        self.store = PostgresVectorStore(
            pool=self.pool,
            default_namespace=self.ns,
            default_model="nomic-embed-text",
            dimensions=768,
        )
        self.policy = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            retrieval_budget=25,
            evidence_authority=self.authority,
        )
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)

    async def asyncTearDown(self):
        if hasattr(self, "pool"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", self.ns)
                    await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
                    await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)
            except Exception:
                pass
            await self.pool.close()

    async def _seed_raw_row(self, *, fp: str, query_text: str, resp: str | None, meta: dict, angle_deg: float, visibility: str = "tenant_shared"):
        vec = _topic_vec(768, angle_deg)
        v_str = "[" + ",".join(f"{x:.6f}" for x in vec) + "]"
        async with self.pool.acquire() as conn:
            if resp is not None:
                await conn.execute(
                    """
                    INSERT INTO cache_entries (
                        cache_namespace, fingerprint, response, model, provider,
                        prompt_tokens, completion_tokens, total_tokens, latency_ms, hit_count, visibility, metadata
                    )
                    VALUES ($1, $2, $3, 'llama-3.3-70b-versatile', 'groq', 8, 12, 20, 12.0, 0, $4, $5::jsonb)
                    ON CONFLICT (cache_namespace, fingerprint) DO UPDATE SET response = EXCLUDED.response, visibility = EXCLUDED.visibility, metadata = EXCLUDED.metadata
                    """,
                    self.ns, fp, resp, visibility, json.dumps(meta),
                )
            await conn.execute(
                """
                INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector, visibility, metadata)
                VALUES ($1, $2, $3, 'nomic-embed-text', $4::vector, $5, $6::jsonb)
                ON CONFLICT (cache_namespace, fingerprint, model) DO UPDATE SET visibility = EXCLUDED.visibility, metadata = EXCLUDED.metadata, vector = EXCLUDED.vector
                """,
                self.ns, fp, query_text, v_str, visibility, json.dumps(meta),
            )

    async def test_scenario_9_starvation_prevention_and_legacy_lifecycle(self):
        ext = CompatibilityExtractor()
        v2_base = {
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

        # Seed 7 higher-similarity decoys (angles 0..6 deg => cos > 0.994):
        # 4 incompatible language variants + 3 legacy v2 rows
        for idx, lang_prompt in enumerate([
            "Parse JSON in Java",
            "Parse JSON in Rust",
            "Parse JSON in Go",
            "Parse JSON in Ruby",
        ]):
            m = ext.extract_from_text(lang_prompt, configured_model="nomic-embed-text").to_dict()
            await self._seed_raw_row(
                fp=f"fp_incompat_{idx}",
                query_text=lang_prompt,
                resp=f"code_{idx}",
                meta=m,
                angle_deg=float(idx),
            )

        # 2 legacy v2 rows WITH valid plain Python response, and 1 legacy v2 row requiring JSON with broken response
        v2_json_broken = json.loads(json.dumps(v2_base))
        v2_json_broken["dimensions"]["output_format"] = {"status": "KNOWN", "value": "json"}
        for idx, (qp, resp, meta) in enumerate([
            ("Reverse Python string v2 a", "s[::-1]", v2_base),
            ("Reverse Python string v2 b", "''.join(reversed(s))", v2_base),
            ("Parse log in Python and output strict JSON", "{not_valid_json", v2_json_broken),
        ]):
            await self._seed_raw_row(
                fp=f"fp_v2_{idx}",
                query_text=qp,
                resp=resp,
                meta=meta,
                angle_deg=4.0 + idx * 0.5,
            )

        # Seed rank-8 compatible v3 target at 15 degrees (cos(15 deg) = 0.9659 >= 0.92)
        embedder = TopicRoutingEmbedder([("reverse", 0.0)], dim=768)
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.store,
            policy=self.policy,
            default_namespace=self.ns,
            evidence_authority=self.authority,
        )
        req_target = _req("Write Python code to reverse a string", namespace=self.ns)
        # Index genuine verified target via IntelliCacheCore/service flow
        from app.cache.exact_cache import CacheEntry
        await self.cache.set(
            "fp_v3_target",
            CacheEntry(
                response="def rev(s): return s[::-1]",
                model="llama-3.3-70b-versatile",
                usage=UsageMetrics(prompt_tokens=6, completion_tokens=8, total_tokens=14),
                latency_ms=11.0,
                provider="groq",
                visibility="tenant_shared",
            ),
            namespace=self.ns,
        )
        # Seed compatible v3 at rank 8 (angle 15 deg so similarity ~0.9659)
        m_v3 = ext.extract(req_target, "nomic-embed-text")
        ver_target = svc.verifier.verify(response_text="def rev(s): return s[::-1]", request_metadata=m_v3, finish_reason="stop")
        ev_target = self.authority.mint_response_verified_evidence(
            cache_namespace=self.ns,
            fingerprint="fp_v3_target",
            response_text="def rev(s): return s[::-1]",
            metadata=m_v3,
            verification=ver_target,
            visibility="tenant_shared",
        )
        m_v3_dict = SemanticCompatibilityMetadata(
            schema_version=m_v3.schema_version,
            extractor_version=m_v3.extractor_version,
            task_kind=m_v3.task_kind,
            dimensions=m_v3.dimensions,
            generation_contract=m_v3.generation_contract,
            embedding_contract=m_v3.embedding_contract,
            compatibility_evidence=ev_target,
            context_contract=m_v3.context_contract,
        ).to_dict()
        m_v3_dict["variant_key"] = compute_variant_key(m_v3)
        await self._seed_raw_row(
            fp="fp_v3_target",
            query_text="Write Python code to reverse a string",
            resp="def rev(s): return s[::-1]",
            meta=m_v3_dict,
            angle_deg=15.0,
        )

        # With old top_k=5 only, rank-8 v3 target would NOT be retrieved!
        narrow = await self.store.search(_topic_vec(768, 0.0), top_k=5, namespace=self.ns, model="nomic-embed-text")
        self.assertNotIn("fp_v3_target", [c.fingerprint for c in narrow])

        # With Stage 3.5.3 bounded retrieval_budget=25, SemanticCacheService retrieves all 8,
        # filters incompatible & legacy v2, and serves rank-8 compatible v3 hit!
        diag_captured = []
        hit = await svc.lookup(
            _req("How do I reverse a string in Python?", namespace=self.ns),
            diagnostics_hook=diag_captured.append,
        )
        self.assertIsNotNone(hit)
        self.assertEqual(hit.fingerprint, "fp_v3_target")
        self.assertEqual(hit.cached_response, "def rev(s): return s[::-1]")
        self.assertEqual(diag_captured[-1]["candidates_retrieved"], 8)
        self.assertEqual(diag_captured[-1]["candidates_legacy"], 3)

        # Scenario 10: Legacy inspection -> dry_run -> migrate -> lookup -> rollback
        mgr = PostgresLegacyLifecycleManager(pool=self.pool, authority=self.authority)
        insp = await mgr.inspect(namespace=self.ns)
        self.assertEqual(insp.v2_records, 3)
        self.assertEqual(insp.v3_records, 5)
        self.assertEqual(insp.verifiable_candidates, 3)

        dry = await mgr.migrate(namespace=self.ns, dry_run=True)
        self.assertEqual(dry.upgraded_to_v3, 2)
        self.assertEqual(dry.excluded_legacy, 1)
        # DB unchanged after dry_run
        self.assertEqual((await mgr.inspect(namespace=self.ns)).v2_records, 3)

        mig = await mgr.migrate(namespace=self.ns, dry_run=False)
        self.assertEqual(mig.upgraded_to_v3, 2)
        self.assertEqual(mig.excluded_legacy, 1)

        insp_after = await mgr.inspect(namespace=self.ns)
        self.assertEqual(insp_after.v2_records, 0)
        self.assertEqual(insp_after.excluded_records, 1)
        self.assertEqual(insp_after.v3_records, 7)

        # Exact cache entry for excluded row (fp_v2_2) MUST still be intact!
        exact_broken = await self.cache.get("fp_v2_2", namespace=self.ns)
        self.assertIsNotNone(exact_broken)
        self.assertEqual(exact_broken.response, "{not_valid_json")

        # Excluded legacy row does not appear in search results
        after_search = await self.store.search(_topic_vec(768, 0.0), top_k=25, namespace=self.ns, model="nomic-embed-text")
        self.assertNotIn("fp_v2_2", [c.fingerprint for c in after_search])

        # Reversible rollback restores legacy_excluded row cleanly
        rb = await mgr.restore_excluded(namespace=self.ns, dry_run=False)
        self.assertEqual(rb.restored, 1)
        after_rb = await self.store.search(_topic_vec(768, 0.0), top_k=25, namespace=self.ns, model="nomic-embed-text")
        self.assertIn("fp_v2_2", [c.fingerprint for c in after_rb])

    async def test_scenario_12_postgres_tamper_and_copied_evidence_rejected(self):
        embedder = TopicRoutingEmbedder([("json", 0.0)], dim=768)
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.store,
            policy=self.policy,
            default_namespace=self.ns,
            evidence_authority=self.authority,
        )
        upstream = MockUpstream(
            lambda req: GenerationResult(
                text='{"status":"ok"}',
                model=req.model,
                usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12),
                latency_ms=5.0,
                finish_reason="stop",
            )
        )
        core = IntelliCacheCore(
            cache=self.cache,
            cache_enabled=True,
            semantic_cache=svc,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )
        req1 = _req("Format record in Python and output strict JSON", namespace=self.ns)
        r1 = await core.execute_query(req1, upstream)
        self.assertFalse(r1.cached)
        fp1 = r1.fingerprint

        # Baseline verified hit works
        diag = []
        hit_ok = await svc.lookup(_req("Serialize record in Python and output strict JSON", namespace=self.ns), diagnostics_hook=diag.append)
        self.assertIsNotNone(hit_ok)

        # Attack 1: Tamper cached_response in semantic_variant_records without updating digest/signature -> rejected UNCERTAIN
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE semantic_variant_records SET cached_response = $3 WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns, fp1, '{"status":"tampered"}',
            )
        diag.clear()
        hit_tampered = await svc.lookup(_req("Serialize record in Python and output strict JSON", namespace=self.ns), diagnostics_hook=diag.append)
        self.assertIsNone(hit_tampered)
        self.assertEqual(diag[-1]["reason_code"], CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH.value)

        # Restore response, then Attack 2: Copy row 1's signed metadata into a different fingerprint fp_copied
        async with self.pool.acquire() as conn:
            await conn.execute(
                "UPDATE semantic_variant_records SET cached_response = $3 WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns, fp1, '{"status":"ok"}',
            )
            row1_meta = await conn.fetchval(
                "SELECT metadata FROM semantic_variant_records WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns, fp1,
            )
            await conn.execute(
                "DELETE FROM semantic_variant_records WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns, fp1,
            )
            await conn.execute(
                """
                INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider, prompt_tokens, completion_tokens, total_tokens, latency_ms, hit_count, visibility, metadata)
                VALUES ($1, 'fp_copied', '{"status":"ok"}', 'llama-3.3-70b-versatile', 'groq', 6, 6, 12, 5.0, 0, 'tenant_shared', '{}'::jsonb)
                """,
                self.ns,
            )
            v_str = "[" + ",".join(f"{x:.6f}" for x in _topic_vec(768, 0.0)) + "]"
            await conn.execute(
                """
                INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector, visibility, metadata)
                VALUES ($1, 'fp_copied', 'Copied evidence query', 'nomic-embed-text', $2::vector, 'tenant_shared', $3::jsonb)
                """,
                self.ns, v_str, row1_meta,
            )

        diag.clear()
        hit_copied = await svc.lookup(_req("Serialize record in Python and output strict JSON", namespace=self.ns), diagnostics_hook=diag.append)
        self.assertIsNone(hit_copied)
        self.assertEqual(diag[-1]["reason_code"], CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH.value)

    async def test_postgres_same_fingerprint_concurrent_variant_coexistence_and_cascade(self):
        schema_a = {"type": "object", "properties": {"a": {"type": "integer"}}, "required": ["a"]}
        schema_b = {"type": "object", "properties": {"b": {"type": "string"}}, "required": ["b"]}
        schema_c = {"type": "object", "properties": {"c": {"type": "boolean"}}, "required": ["c"]}

        embedder = TopicRoutingEmbedder([("serialize", 0.0)], dim=768)
        svc = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.store,
            policy=self.policy,
            default_namespace=self.ns,
            evidence_authority=self.authority,
        )

        # Same prompt, same model, temperature, max_tokens, provider -> identical exact-cache base fingerprint
        prompt_text = "Serialize structured payload in Python conforming to schema"
        req_a = _req(prompt_text, namespace=self.ns, metadata={"json_schema": schema_a})
        req_b = _req(prompt_text, namespace=self.ns, metadata={"json_schema": schema_b})
        req_c = _req(prompt_text, namespace=self.ns, metadata={"json_schema": schema_c})
        shared_fp = generate_fingerprint(
            messages=req_a.messages,
            model=req_a.model,
            temperature=req_a.temperature,
            max_tokens=req_a.max_tokens,
            provider=req_a.provider,
        )

        from app.cache.exact_cache import CacheEntry
        await self.cache.set(
            shared_fp,
            CacheEntry(
                response='{"a": 1}',
                model=req_a.model,
                usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12),
                latency_ms=5.0,
                provider="groq",
                visibility="tenant_shared",
            ),
            namespace=self.ns,
        )

        res_a = GenerationResult(text='{"a": 1}', model=req_a.model, usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12), latency_ms=5.0, fingerprint=shared_fp, finish_reason="stop")
        res_b = GenerationResult(text='{"b": "two"}', model=req_b.model, usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12), latency_ms=5.0, fingerprint=shared_fp, finish_reason="stop")
        res_c = GenerationResult(text='{"c": true}', model=req_c.model, usage=UsageMetrics(prompt_tokens=6, completion_tokens=6, total_tokens=12), latency_ms=5.0, fingerprint=shared_fp, finish_reason="stop")

        outcomes = await asyncio.gather(svc.index(req_a, res_a), svc.index(req_b, res_b), svc.index(req_c, res_c))
        self.assertEqual(outcomes, [True, True, True])

        async with self.pool.acquire() as conn:
            row_count = await conn.fetchval(
                "SELECT COUNT(*) FROM semantic_variant_records WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                shared_fp,
            )
        self.assertEqual(row_count, 3)

        # Each schema retrieves ONLY its own verified variant response
        self.assertEqual((await svc.lookup(req_a)).cached_response, '{"a": 1}')
        self.assertEqual((await svc.lookup(req_b)).cached_response, '{"b": "two"}')
        self.assertEqual((await svc.lookup(req_c)).cached_response, '{"c": true}')

        # Cascade deletion from cache_entries deletes all 3 coexisting variant rows automatically
        async with self.pool.acquire() as conn:
            await conn.execute(
                "DELETE FROM cache_entries WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                shared_fp,
            )
            remaining = await conn.fetchval(
                "SELECT COUNT(*) FROM semantic_variant_records WHERE cache_namespace = $1 AND fingerprint = $2",
                self.ns,
                shared_fp,
            )
        self.assertEqual(remaining, 0)

    async def test_legacy_migration_survives_authority_restart_and_rotation(self):
        ext = CompatibilityExtractor()
        k1 = "legacy-migration-persistent-k1-0123456789abcdef"
        k2 = "legacy-migration-rotated-k2-fedcba9876543210"
        authority_before = EvidenceAuthority(secret_key=k1, allow_ephemeral=False)

        v2_valid = {
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
        v2_bad_json = json.loads(json.dumps(v2_valid))
        v2_bad_json["dimensions"]["output_format"] = {"status": "KNOWN", "value": "json"}

        await self._seed_raw_row(fp="fp_mig_valid", query_text="Reverse a string in Python", resp="s[::-1]", meta=v2_valid, angle_deg=0.0)
        await self._seed_raw_row(fp="fp_mig_bad", query_text="Parse data in Python as strict JSON", resp="{bad", meta=v2_bad_json, angle_deg=1.0)

        mgr = PostgresLegacyLifecycleManager(pool=self.pool, authority=authority_before)
        mig = await mgr.migrate(namespace=self.ns, dry_run=False)
        self.assertEqual(mig.upgraded_to_v3, 1)
        self.assertEqual(mig.excluded_legacy, 1)

        # Simulate full process restart / separate gateway worker using K1
        embedder = TopicRoutingEmbedder([("reverse", 0.0)], dim=768)
        authority_restarted = EvidenceAuthority(secret_key=k1, allow_ephemeral=False)
        policy_restarted = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            retrieval_budget=25,
            evidence_authority=authority_restarted,
        )
        svc_restarted = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.store,
            policy=policy_restarted,
            default_namespace=self.ns,
            evidence_authority=authority_restarted,
        )
        hit_after_restart = await svc_restarted.lookup(_req("How to reverse a string in Python?", namespace=self.ns))
        self.assertIsNotNone(hit_after_restart)
        self.assertEqual(hit_after_restart.cached_response, "s[::-1]")

        # Key rotation: primary=K2 with previous=[K1] keeps migrated record valid
        authority_rotated = EvidenceAuthority(secret_key=k2, previous_secret_keys=[k1], allow_ephemeral=False)
        policy_rotated = SemanticCachePolicy(
            min_similarity=0.92,
            min_margin=0.03,
            top_k=5,
            configured_model="nomic-embed-text",
            retrieval_budget=25,
            evidence_authority=authority_rotated,
        )
        svc_rotated = SemanticCacheService(
            embedding_provider=embedder,
            vector_store=self.store,
            policy=policy_rotated,
            default_namespace=self.ns,
            evidence_authority=authority_rotated,
        )
        hit_rotated = await svc_rotated.lookup(_req("How to reverse a string in Python?", namespace=self.ns))
        self.assertIsNotNone(hit_rotated)

        # Invalid/unverifiable v2 record remains excluded from semantic search
        search_res = await self.store.search(_topic_vec(768, 0.0), top_k=25, namespace=self.ns, model="nomic-embed-text")
        self.assertNotIn("fp_mig_bad", [r.fingerprint for r in search_res])

    async def test_performance_measurements_and_pgvector_explain_analyze(self):
        # Populate 40 realistic records split across BOTH vector_records (20) and semantic_variant_records (20,
        # including multi-variant same-fingerprint rows) and measure latency + production combined UNION ALL EXPLAIN ANALYZE
        ext = CompatibilityExtractor()
        for i in range(20):
            lang = "java" if i < 6 else "python"
            qp = f"Sort items {i} in {lang}"
            m = ext.extract_from_text(qp, configured_model="nomic-embed-text").to_dict()
            m["variant_key"] = compute_variant_key(SemanticCompatibilityMetadata.from_dict(m))
            await self._seed_raw_row(fp=f"fp_bench_vr_{i}", query_text=qp, resp=f"vr_resp_{i}", meta=m, angle_deg=float(i) * 0.5)

        for j in range(20):
            schema_j = {"type": "object", "properties": {f"k{j}": {"type": "integer"}}, "required": [f"k{j}"]}
            shared_fp = f"fp_bench_sv_{j // 2}"
            qp = f"Encode variant payload {j // 2} in Python"
            m_obj = ext.extract_from_text(qp, configured_model="nomic-embed-text", request_metadata={"json_schema": schema_j})
            m = m_obj.to_dict()
            vk = compute_variant_key(m_obj, schema_digest=compute_canonical_schema_digest(schema_j))
            m["variant_key"] = vk
            await self.cache.set(
                shared_fp,
                CacheEntry(
                    response=f'{{"k{j}": {j}}}',
                    model="llama-3.3-70b-versatile",
                    usage=UsageMetrics(prompt_tokens=8, completion_tokens=6, total_tokens=14),
                    latency_ms=3.0,
                    provider="groq",
                    visibility="tenant_shared",
                ),
                namespace=self.ns,
            )
            await self.store.upsert(
                VectorRecord(
                    cache_namespace=self.ns,
                    fingerprint=shared_fp,
                    query_text=qp,
                    vector=_topic_vec(768, 10.0 + float(j) * 0.5),
                    model="nomic-embed-text",
                    visibility="tenant_shared",
                    metadata=m,
                    variant_key=vk,
                    response_text=f'{{"k{j}": {j}}}',
                )
            )

        verifier = ResponseArtifactVerifier()
        req_meta = ext.extract_from_text("Serialize in Python and output strict JSON")
        ver_lat_ms = []
        for _ in range(20):
            t0 = time.perf_counter()
            verifier.verify(response_text='{"a":1,"b":"ok"}', request_metadata=req_meta, finish_reason="stop")
            ver_lat_ms.append((time.perf_counter() - t0) * 1000.0)

        q_vec = _topic_vec(768, 3.0)
        t5_ms, t25_ms = [], []
        for _ in range(15):
            t0 = time.perf_counter()
            await self.store.search(q_vec, top_k=5, namespace=self.ns, model="nomic-embed-text")
            t5_ms.append((time.perf_counter() - t0) * 1000.0)
            t1 = time.perf_counter()
            await self.store.search(q_vec, top_k=25, namespace=self.ns, model="nomic-embed-text")
            t25_ms.append((time.perf_counter() - t1) * 1000.0)

        plan = await self.store.explain_search(
            q_vec,
            top_k=25,
            namespace=self.ns,
            model="nomic-embed-text",
            analyze=True,
        )
        plan_text = "\n".join(plan["execution_plan_summary"])
        self.assertIn("Execution Time", plan_text)
        self.assertIn("vector_records", plan_text)
        self.assertIn("semantic_variant_records", plan_text)
        print(
            f"\n[STAGE_3_5_3_BENCHMARK] verifier_median_ms={statistics.median(ver_lat_ms):.3f} "
            f"search_top5_median_ms={statistics.median(t5_ms):.3f} "
            f"search_top25_median_ms={statistics.median(t25_ms):.3f}\n"
            f"[EXPLAIN_ANALYZE_COMBINED]\n{plan_text}"
        )

    async def test_combined_union_all_hnsw_explain_analyze(self):
        # Seed HNSW-scale datasets in BOTH vector_records and semantic_variant_records (3,000 rows each)
        # to inspect and verify the actual production UNION ALL plan under HNSW cost selection.
        hnsw_ns = "test_s353_hnsw_union"
        import random
        rng = random.Random(353)

        def _rand_vec_str() -> str:
            raw = [rng.gauss(0.0, 1.0) for _ in range(768)]
            norm = math.sqrt(sum(v * v for v in raw)) or 1.0
            return "[" + ",".join(f"{(v / norm):.6f}" for v in raw) + "]"

        n_rows = 3000
        try:
            async with self.pool.acquire() as conn:
                ce_rows = [(hnsw_ns, f"fp_hu_{i}", f"resp_{i}", "llama-3.3-70b-versatile", "groq") for i in range(n_rows)]
                vr_rows = [(hnsw_ns, f"fp_hu_{i}", f"query {i}", "nomic-embed-text", _rand_vec_str()) for i in range(n_rows)]
                sv_rows = [
                    (hnsw_ns, f"fp_hu_{i}", f"v3:variant_{i}", f"variant query {i}", "nomic-embed-text", _rand_vec_str(), f"var_resp_{i}")
                    for i in range(n_rows)
                ]
                for start in range(0, n_rows, 500):
                    await conn.executemany(
                        "INSERT INTO cache_entries (cache_namespace, fingerprint, response, model, provider) VALUES ($1, $2, $3, $4, $5) ON CONFLICT DO NOTHING",
                        ce_rows[start : start + 500],
                    )
                    await conn.executemany(
                        "INSERT INTO vector_records (cache_namespace, fingerprint, query_text, model, vector) VALUES ($1, $2, $3, $4, $5::vector) ON CONFLICT DO NOTHING",
                        vr_rows[start : start + 500],
                    )
                    await conn.executemany(
                        "INSERT INTO semantic_variant_records (cache_namespace, fingerprint, variant_key, query_text, model, vector, cached_response) VALUES ($1, $2, $3, $4, $5, $6::vector, $7) ON CONFLICT DO NOTHING",
                        sv_rows[start : start + 500],
                    )
                await conn.execute("VACUUM ANALYZE cache_entries;")
                await conn.execute("VACUUM ANALYZE vector_records;")
                await conn.execute("VACUUM ANALYZE semantic_variant_records;")

            q_vec = _topic_vec(768, 7.0)
            plan = await self.store.explain_search(
                q_vec,
                top_k=25,
                namespace=hnsw_ns,
                model="nomic-embed-text",
                analyze=True,
            )
            plan_text = "\n".join(plan["execution_plan_summary"])
            self.assertIn("Execution Time", plan_text)
            self.assertIn("vector_records", plan_text)
            self.assertIn("semantic_variant_records", plan_text)
            print(
                f"\n[EXPLAIN_ANALYZE_COMBINED_HNSW] vr_hnsw={plan['vector_records_hnsw_used']} "
                f"sv_hnsw={plan['semantic_variant_records_hnsw_used']}\n{plan_text}"
            )
        finally:
            async with self.pool.acquire() as conn:
                await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", hnsw_ns)
                await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", hnsw_ns)
                await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", hnsw_ns)
                await conn.execute("VACUUM ANALYZE semantic_variant_records;")
                await conn.execute("VACUUM ANALYZE vector_records;")
                await conn.execute("VACUUM ANALYZE cache_entries;")


class TestStage353FastEmbedRealPostgresE2E(unittest.IsolatedAsyncioTestCase):
    """Real FastEmbed 384-dim + PostgreSQL pgvector end-to-end test when test DB is available."""

    async def asyncSetUp(self):
        db_url = os.getenv("FASTEMBED_TEST_DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_fastembed_test"
        parsed = urlsplit(db_url)
        db_name = (parsed.path or "").lstrip("/")
        if "test" not in db_name.lower():
            self.skipTest(f"Database '{db_name}' is not a test DB.")
        try:
            from app.embeddings.fastembed_adapter import FastEmbedProvider
            self.embedder = FastEmbedProvider(model="BAAI/bge-small-en-v1.5")
            probe = await self.embedder.embed("probe query", task=EmbeddingTask.QUERY)
            if len(probe.vector) != 384:
                self.skipTest("Unexpected FastEmbed dimension")
            self.pool = await asyncpg.create_pool(db_url, min_size=1, max_size=2)
            await run_migrations(self.pool)
            await run_vector_migrations(self.pool, dimensions=384)
        except Exception as exc:
            self.skipTest(f"FastEmbed/Postgres integration unavailable: {exc}")

        self.ns = "test_s353_fe"
        self.authority = EvidenceAuthority(secret_key="fe-s353-secret")
        self.cache = PostgresCache(pool=self.pool, max_entries=500, ttl_seconds=3600, default_namespace=self.ns)
        self.store = PostgresVectorStore(
            pool=self.pool,
            default_namespace=self.ns,
            default_model="BAAI/bge-small-en-v1.5",
            dimensions=384,
        )
        self.policy = SemanticCachePolicy(
            min_similarity=0.80,
            min_margin=0.02,
            top_k=5,
            configured_model="BAAI/bge-small-en-v1.5",
            retrieval_budget=25,
            evidence_authority=self.authority,
        )
        self.svc = SemanticCacheService(
            embedding_provider=self.embedder,
            vector_store=self.store,
            policy=self.policy,
            default_namespace=self.ns,
            evidence_authority=self.authority,
        )
        async with self.pool.acquire() as conn:
            await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
            await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)

    async def asyncTearDown(self):
        if hasattr(self, "embedder"):
            await self.embedder.close()
        if hasattr(self, "pool"):
            try:
                async with self.pool.acquire() as conn:
                    await conn.execute("DELETE FROM semantic_variant_records WHERE cache_namespace = $1", self.ns)
                    await conn.execute("DELETE FROM vector_records WHERE cache_namespace = $1", self.ns)
                    await conn.execute("DELETE FROM cache_entries WHERE cache_namespace = $1", self.ns)
            except Exception:
                pass
            await self.pool.close()

    async def test_real_fastembed_postgres_admission_and_variant_coexistence(self):
        def responder(req: GenerationRequest) -> GenerationResult:
            p = req.messages[0].content.lower()
            if "broken" in p:
                return GenerationResult(
                    text="{invalid",
                    model=req.model,
                    usage=UsageMetrics(prompt_tokens=8, completion_tokens=4, total_tokens=12),
                    latency_ms=4.0,
                    finish_reason="stop",
                )
            if "java" in p:
                return GenerationResult(
                    text="public String reverse(String s) { return new StringBuilder(s).reverse().toString(); }",
                    model=req.model,
                    usage=UsageMetrics(prompt_tokens=8, completion_tokens=10, total_tokens=18),
                    latency_ms=4.0,
                    finish_reason="stop",
                )
            return GenerationResult(
                text='{"reversed": "cba"}',
                model=req.model,
                usage=UsageMetrics(prompt_tokens=8, completion_tokens=8, total_tokens=16),
                latency_ms=4.0,
                finish_reason="stop",
            )

        upstream = MockUpstream(responder)
        core = IntelliCacheCore(
            cache=self.cache,
            cache_enabled=True,
            semantic_cache=self.svc,
            semantic_serving_controller=SemanticServingController(initial_enabled=True),
        )

        async def _count_rows() -> int:
            async with self.pool.acquire() as conn:
                vr = int(await conn.fetchval("SELECT COUNT(*) FROM vector_records WHERE cache_namespace = $1", self.ns))
                svr = int(await conn.fetchval("SELECT COUNT(*) FROM semantic_variant_records WHERE cache_namespace = $1", self.ns))
                return vr + svr

        # Admits valid JSON in Python
        await core.execute_query(
            _req("Reverse a string in Python and output strict JSON", namespace=self.ns),
            upstream,
        )
        self.assertEqual(self.svc.last_admission_decision.status, AdmissionStatus.ADMIT)

        # Admits Java variant too
        await core.execute_query(
            _req("Write a string reversal helper in Java", namespace=self.ns),
            upstream,
        )
        self.assertEqual(await _count_rows(), 2)

        # Blocks broken JSON response from admission even though exact cache stores response
        await core.execute_query(
            _req("Generate broken cron scheduler matrix in Go and output strict JSON", namespace=self.ns),
            upstream,
        )
        self.assertEqual(self.svc.last_admission_decision.status, AdmissionStatus.REJECT)
        self.assertEqual(await _count_rows(), 2)

        # Paraphrased Python JSON query hits ONLY valid JSON variant
        r_hit = await core.execute_query(
            _req("How to reverse a string in Python and output strict JSON", namespace=self.ns),
            upstream,
        )
        self.assertTrue(r_hit.cached)
        self.assertEqual(r_hit.cache_type, "semantic")
        self.assertEqual(r_hit.text, '{"reversed": "cba"}')


if __name__ == "__main__":
    unittest.main()
