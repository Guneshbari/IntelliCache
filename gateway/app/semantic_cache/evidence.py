"""
Cryptographic provenance binding and server-side minting authority for CompatibilityEvidence.
Binds response verification evidence to canonical request/candidate metadata, namespace,
exact-cache fingerprint, and SHA-256 response digest so forged, stale, or copied metadata
cannot authorize semantic cache hits.

Zero external framework or database dependencies (stdlib only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Dict, Optional, Tuple

from .contracts import (
    CompatibilityEvidence,
    CompatibilityReasonCode,
    EVIDENCE_TIER_RESPONSE_VERIFIED,
    ResponseVerificationResult,
    SemanticCompatibilityMetadata,
    TRUSTED_CURATED_EVIDENCE_ID,
    TRUSTED_RESPONSE_VERIFIER_ID,
    TruncationStatus,
    VerificationStatus,
)

# Ephemeral runtime secret used when SEMANTIC_CACHE_EVIDENCE_SECRET is not explicitly configured.
# Guarantees in-process tamper detection across services within the gateway lifecycle.
_PROCESS_RUNTIME_SECRET: bytes = secrets.token_bytes(32)


def compute_response_digest(response_text: str) -> str:
    """Computes canonical lowercase SHA-256 hex digest of the cached response text."""
    return hashlib.sha256((response_text or "").encode("utf-8")).hexdigest()


def compute_canonical_schema_digest(schema: Dict[str, Any]) -> str:
    """Computes canonical lowercase SHA-256 hex digest of a JSON Schema mapping."""
    serialized = json.dumps(schema, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def compute_evidence_binding_digest(
    *,
    cache_namespace: str,
    fingerprint: str,
    response_digest: str,
    metadata: SemanticCompatibilityMetadata,
    source: str,
    evidence_tier: str,
    verifier_version: Optional[str],
    verified_output_format: Optional[str],
    verified_json_valid: bool,
    verified_schema_digest: Optional[str],
    artifact_schema_version: Optional[int],
    verified_constraints: Tuple[str, ...],
    version_independent: bool,
    compatible_language_versions: Tuple[str, ...],
    compatible_framework_versions: Tuple[str, ...],
    compatible_dependency_versions: Tuple[str, ...],
    language_independent: bool,
    context_independent: bool,
    freshness_verified: bool,
) -> str:
    """
    Computes canonical SHA-256 binding digest over ALL security-critical compatibility dimensions,
    generation/embedding contracts, namespace, exact-cache fingerprint, response digest, and claims.
    Modifying any bound field invalidates the evidence binding.
    """
    payload = {
        "cache_namespace": (cache_namespace or "").strip(),
        "fingerprint": (fingerprint or "").strip(),
        "response_digest": (response_digest or "").strip().lower(),
        "schema_version": metadata.schema_version,
        "task_kind": metadata.task_kind.to_dict(),
        "dimensions": {k: metadata.dimensions[k].to_dict() for k in sorted(metadata.dimensions.keys())},
        "generation_contract": metadata.generation_contract.to_dict(),
        "embedding_contract": metadata.embedding_contract.to_dict(),
        "source": source,
        "evidence_tier": evidence_tier,
        "verifier_version": verifier_version,
        "verified_output_format": verified_output_format,
        "verified_json_valid": bool(verified_json_valid),
        "verified_schema_digest": verified_schema_digest,
        "artifact_schema_version": artifact_schema_version,
        "verified_constraints": list(sorted(set(verified_constraints or ()))),
        "version_independent": bool(version_independent),
        "compatible_language_versions": list(sorted(set(compatible_language_versions or ()))),
        "compatible_framework_versions": list(sorted(set(compatible_framework_versions or ()))),
        "compatible_dependency_versions": list(sorted(set(compatible_dependency_versions or ()))),
        "language_independent": bool(language_independent),
        "context_independent": bool(context_independent),
        "freshness_verified": bool(freshness_verified),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class EvidenceAuthority:
    """
    Internal server-side minting and verification authority for CompatibilityEvidence.
    Ensures response_verified evidence can only be created by trusted internal verification
    and remains bound to its exact namespace, fingerprint, response body, and compatibility metadata.
    """

    def __init__(self, secret_key: Optional[str] = None):
        if secret_key is not None and str(secret_key).strip():
            self._key = hashlib.sha256(str(secret_key).strip().encode("utf-8")).digest()
            self._explicit_secret = True
        else:
            env_secret = os.getenv("SEMANTIC_CACHE_EVIDENCE_SECRET")
            if env_secret and env_secret.strip():
                self._key = hashlib.sha256(env_secret.strip().encode("utf-8")).digest()
                self._explicit_secret = True
            else:
                self._key = _PROCESS_RUNTIME_SECRET
                self._explicit_secret = False

    @property
    def has_explicit_secret(self) -> bool:
        return self._explicit_secret

    def sign_binding_digest(self, binding_digest: str) -> str:
        """Computes HMAC-SHA256 signature over the canonical binding digest."""
        return hmac.new(self._key, binding_digest.encode("utf-8"), hashlib.sha256).hexdigest()

    def mint_response_verified_evidence(
        self,
        *,
        cache_namespace: str,
        fingerprint: str,
        response_text: str,
        metadata: SemanticCompatibilityMetadata,
        verification: ResponseVerificationResult,
        version_independent: bool = False,
        compatible_language_versions: Tuple[str, ...] = (),
        compatible_framework_versions: Tuple[str, ...] = (),
        compatible_dependency_versions: Tuple[str, ...] = (),
        language_independent: bool = False,
        context_independent: bool = False,
        freshness_verified: bool = False,
    ) -> CompatibilityEvidence:
        """
        Mints cryptographically bound response_verified CompatibilityEvidence ONLY from a VERIFIED,
        non-truncated ResponseVerificationResult.
        """
        if not isinstance(verification, ResponseVerificationResult):
            raise ValueError("verification must be a ResponseVerificationResult instance.")
        if verification.status != VerificationStatus.VERIFIED:
            raise ValueError(
                f"Cannot mint response_verified evidence from verification status '{verification.status}'."
            )
        if verification.truncation_status == TruncationStatus.TRUNCATED:
            raise ValueError("Cannot mint response_verified evidence for a truncated response.")
        if verification.unverified_constraints:
            raise ValueError("Cannot mint response_verified evidence when unverified constraints remain.")

        response_digest = compute_response_digest(response_text)
        verifier_ver = verification.verifier_version or TRUSTED_RESPONSE_VERIFIER_ID
        source = TRUSTED_RESPONSE_VERIFIER_ID
        tier = EVIDENCE_TIER_RESPONSE_VERIFIED

        binding_digest = compute_evidence_binding_digest(
            cache_namespace=cache_namespace,
            fingerprint=fingerprint,
            response_digest=response_digest,
            metadata=metadata,
            source=source,
            evidence_tier=tier,
            verifier_version=verifier_ver,
            verified_output_format=verification.verified_output_format,
            verified_json_valid=verification.verified_json_valid,
            verified_schema_digest=verification.verified_schema_digest,
            artifact_schema_version=verification.verified_schema_version,
            verified_constraints=verification.verified_constraints,
            version_independent=version_independent,
            compatible_language_versions=compatible_language_versions,
            compatible_framework_versions=compatible_framework_versions,
            compatible_dependency_versions=compatible_dependency_versions,
            language_independent=language_independent,
            context_independent=context_independent,
            freshness_verified=freshness_verified,
        )
        sig = self.sign_binding_digest(binding_digest)

        return CompatibilityEvidence(
            source=source,
            evidence_tier=tier,
            version_independent=version_independent,
            compatible_language_versions=compatible_language_versions,
            compatible_framework_versions=compatible_framework_versions,
            compatible_dependency_versions=compatible_dependency_versions,
            language_independent=language_independent,
            context_independent=context_independent,
            verified_output_format=verification.verified_output_format,
            verified_json_valid=verification.verified_json_valid,
            freshness_verified=freshness_verified,
            artifact_schema_version=verification.verified_schema_version,
            verifier_version=verifier_ver,
            response_digest=response_digest,
            binding_digest=binding_digest,
            evidence_signature=sig,
            verified_constraints=verification.verified_constraints,
            verified_schema_digest=verification.verified_schema_digest,
            _authority_minted=True,
        )

    def mint_curated_evidence(
        self,
        *,
        cache_namespace: str,
        fingerprint: str,
        response_text: str,
        metadata: SemanticCompatibilityMetadata,
        verified_output_format: Optional[str] = None,
        verified_json_valid: bool = False,
        verified_schema_digest: Optional[str] = None,
        artifact_schema_version: Optional[int] = None,
        verified_constraints: Tuple[str, ...] = (),
        version_independent: bool = False,
        compatible_language_versions: Tuple[str, ...] = (),
        compatible_framework_versions: Tuple[str, ...] = (),
        compatible_dependency_versions: Tuple[str, ...] = (),
        language_independent: bool = False,
        context_independent: bool = False,
        freshness_verified: bool = False,
    ) -> CompatibilityEvidence:
        """Mints cryptographically bound curated evidence for offline/operator evaluations."""
        response_digest = compute_response_digest(response_text)
        source = TRUSTED_CURATED_EVIDENCE_ID
        tier = EVIDENCE_TIER_RESPONSE_VERIFIED
        verifier_ver = TRUSTED_CURATED_EVIDENCE_ID

        binding_digest = compute_evidence_binding_digest(
            cache_namespace=cache_namespace,
            fingerprint=fingerprint,
            response_digest=response_digest,
            metadata=metadata,
            source=source,
            evidence_tier=tier,
            verifier_version=verifier_ver,
            verified_output_format=verified_output_format,
            verified_json_valid=verified_json_valid,
            verified_schema_digest=verified_schema_digest,
            artifact_schema_version=artifact_schema_version,
            verified_constraints=verified_constraints,
            version_independent=version_independent,
            compatible_language_versions=compatible_language_versions,
            compatible_framework_versions=compatible_framework_versions,
            compatible_dependency_versions=compatible_dependency_versions,
            language_independent=language_independent,
            context_independent=context_independent,
            freshness_verified=freshness_verified,
        )
        sig = self.sign_binding_digest(binding_digest)

        return CompatibilityEvidence(
            source=source,
            evidence_tier=tier,
            version_independent=version_independent,
            compatible_language_versions=compatible_language_versions,
            compatible_framework_versions=compatible_framework_versions,
            compatible_dependency_versions=compatible_dependency_versions,
            language_independent=language_independent,
            context_independent=context_independent,
            verified_output_format=verified_output_format,
            verified_json_valid=verified_json_valid,
            freshness_verified=freshness_verified,
            artifact_schema_version=artifact_schema_version,
            verifier_version=verifier_ver,
            response_digest=response_digest,
            binding_digest=binding_digest,
            evidence_signature=sig,
            verified_constraints=verified_constraints,
            verified_schema_digest=verified_schema_digest,
            _authority_minted=True,
        )

    def verify_evidence_binding(
        self,
        evidence: CompatibilityEvidence,
        *,
        cache_namespace: str,
        fingerprint: str,
        response_text: str,
        metadata: SemanticCompatibilityMetadata,
    ) -> Tuple[bool, CompatibilityReasonCode, str]:
        """
        Verifies cryptographic provenance and artifact binding of response_verified evidence.
        Fails closed with structured CompatibilityReasonCode on missing, forged, copied, or stale bindings.
        """
        if evidence.has_conflicting_claims():
            code = CompatibilityReasonCode.AMBIGUOUS_COMPATIBILITY_EVIDENCE
            return False, code, f"{code}: Evidence carries internally conflicting claims."

        if not evidence.is_trusted_response_verified():
            code = CompatibilityReasonCode.UNVERIFIED_COMPATIBILITY_EVIDENCE
            return False, code, f"{code}: Evidence producer '{evidence.source}' is not trusted for tier '{evidence.evidence_tier}'."

        if not evidence.response_digest or not evidence.binding_digest:
            code = CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH
            return False, code, f"{code}: Response-verified evidence lacks mandatory cryptographic response/binding digests."

        actual_response_digest = compute_response_digest(response_text)
        if not hmac.compare_digest(actual_response_digest, evidence.response_digest):
            code = CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH
            return False, code, f"{code}: Evidence response_digest does not match cached response content."

        expected_binding_digest = compute_evidence_binding_digest(
            cache_namespace=cache_namespace,
            fingerprint=fingerprint,
            response_digest=actual_response_digest,
            metadata=metadata,
            source=evidence.source,
            evidence_tier=evidence.evidence_tier,
            verifier_version=evidence.verifier_version,
            verified_output_format=evidence.verified_output_format,
            verified_json_valid=evidence.verified_json_valid,
            verified_schema_digest=evidence.verified_schema_digest,
            artifact_schema_version=evidence.artifact_schema_version,
            verified_constraints=evidence.verified_constraints,
            version_independent=evidence.version_independent,
            compatible_language_versions=evidence.compatible_language_versions,
            compatible_framework_versions=evidence.compatible_framework_versions,
            compatible_dependency_versions=evidence.compatible_dependency_versions,
            language_independent=evidence.language_independent,
            context_independent=evidence.context_independent,
            freshness_verified=evidence.freshness_verified,
        )
        if not hmac.compare_digest(expected_binding_digest, evidence.binding_digest):
            code = CompatibilityReasonCode.EVIDENCE_BINDING_MISMATCH
            return (
                False,
                code,
                f"{code}: Evidence binding_digest does not match candidate namespace, fingerprint, or metadata.",
            )

        if not evidence.evidence_signature:
            code = CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID
            return False, code, f"{code}: Response-verified evidence is missing HMAC signature."

        expected_sig = self.sign_binding_digest(expected_binding_digest)
        if not hmac.compare_digest(expected_sig, evidence.evidence_signature):
            code = CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID
            return False, code, f"{code}: Evidence signature verification failed."

        return True, CompatibilityReasonCode.COMPATIBLE, "Evidence cryptographic binding verified."


_DEFAULT_AUTHORITY_CACHE: Dict[Optional[str], EvidenceAuthority] = {}


def get_default_evidence_authority(secret_key: Optional[str] = None) -> EvidenceAuthority:
    """Returns a reusable process-scoped EvidenceAuthority instance."""
    norm = secret_key.strip() if isinstance(secret_key, str) and secret_key.strip() else os.getenv("SEMANTIC_CACHE_EVIDENCE_SECRET")
    if norm is not None:
        norm = norm.strip() or None
    if norm not in _DEFAULT_AUTHORITY_CACHE:
        _DEFAULT_AUTHORITY_CACHE[norm] = EvidenceAuthority(secret_key=norm)
    return _DEFAULT_AUTHORITY_CACHE[norm]
