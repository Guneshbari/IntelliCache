"""
Cryptographic provenance binding and server-side minting authority for CompatibilityEvidence.
Binds response verification evidence to canonical request/candidate metadata, namespace,
exact-cache fingerprint, and SHA-256 response digest so forged, stale, or copied metadata
cannot authorize semantic cache hits.

Supports stable secret enforcement for persistent backends and zero-downtime key rotation via
`previous_secret_keys`. Zero external framework or database dependencies (stdlib only).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from typing import Any, Dict, List, Optional, Sequence, Tuple

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

# Ephemeral runtime secret used ONLY for isolated memory dev/tests when allow_ephemeral=True.
_PROCESS_RUNTIME_SECRET: bytes = secrets.token_bytes(32)


def _derive_hmac_key_and_id(secret_bytes: bytes) -> Tuple[bytes, str]:
    key_id = hashlib.sha256(secret_bytes).hexdigest()[:12]
    return secret_bytes, key_id


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
    Supports persistent shared HMAC secrets across multiple workers/restarts as well as
    zero-downtime key rotation via `previous_secret_keys`.
    """

    def __init__(
        self,
        secret_key: Optional[str] = None,
        previous_secret_keys: Optional[Sequence[str]] = None,
        allow_ephemeral: bool = True,
    ):
        resolved_secret: Optional[str] = None
        if secret_key is not None and str(secret_key).strip():
            resolved_secret = str(secret_key).strip()
        else:
            env_secret = (os.getenv("SEMANTIC_CACHE_EVIDENCE_SECRET") or "").strip()
            if env_secret:
                resolved_secret = env_secret

        if resolved_secret is not None:
            key_bytes = hashlib.sha256(resolved_secret.encode("utf-8")).digest()
            self._key, self._primary_key_id = _derive_hmac_key_and_id(key_bytes)
            self._explicit_secret = True
        elif allow_ephemeral:
            self._key, self._primary_key_id = _derive_hmac_key_and_id(_PROCESS_RUNTIME_SECRET)
            self._explicit_secret = False
        else:
            raise ValueError(
                "Persistent EvidenceAuthority requires an explicit SEMANTIC_CACHE_EVIDENCE_SECRET "
                "(ephemeral keys are disabled when allow_ephemeral=False)."
            )

        prev_sources: List[str] = []
        if previous_secret_keys:
            prev_sources.extend(str(s).strip() for s in previous_secret_keys if str(s).strip())
        else:
            raw_env_prev = (os.getenv("SEMANTIC_CACHE_EVIDENCE_PREVIOUS_SECRETS") or "").strip()
            if raw_env_prev:
                prev_sources.extend(p.strip() for p in raw_env_prev.split(",") if p.strip())

        self._previous_keys: List[Tuple[str, bytes]] = []
        seen_ids = {self._primary_key_id}
        for prev_raw in prev_sources:
            p_bytes = hashlib.sha256(prev_raw.encode("utf-8")).digest()
            p_bytes, p_id = _derive_hmac_key_and_id(p_bytes)
            if p_id not in seen_ids:
                seen_ids.add(p_id)
                self._previous_keys.append((p_id, p_bytes))

    @property
    def has_explicit_secret(self) -> bool:
        return self._explicit_secret

    @property
    def primary_key_id(self) -> str:
        return self._primary_key_id

    @staticmethod
    def _sign_with_key(key_bytes: bytes, binding_digest: str) -> str:
        return hmac.new(key_bytes, binding_digest.encode("utf-8"), hashlib.sha256).hexdigest()

    def sign_binding_digest(self, binding_digest: str) -> str:
        """Computes HMAC-SHA256 signature over the canonical binding digest using primary key."""
        return self._sign_with_key(self._key, binding_digest)

    def _verify_signature_across_keyring(
        self,
        binding_digest: str,
        signature: str,
        signing_key_id: Optional[str],
    ) -> bool:
        """Constant-time signature check against primary key plus configured rotation keys."""
        candidates: List[Tuple[str, bytes]] = [(self._primary_key_id, self._key), *self._previous_keys]
        if signing_key_id:
            # Order matching key_id first for fast verification, still verifying exact HMAC
            candidates.sort(key=lambda item: 0 if item[0] == signing_key_id else 1)
        for _kid, key_bytes in candidates:
            expected_sig = self._sign_with_key(key_bytes, binding_digest)
            if hmac.compare_digest(expected_sig, signature):
                return True
        return False

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
            signing_key_id=self._primary_key_id,
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
            signing_key_id=self._primary_key_id,
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
        Supports active key and configured `previous_secret_keys` during rotation transitions.
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

        if not self._verify_signature_across_keyring(
            expected_binding_digest,
            evidence.evidence_signature,
            evidence.signing_key_id,
        ):
            code = CompatibilityReasonCode.EVIDENCE_SIGNATURE_INVALID
            return False, code, f"{code}: Evidence signature verification failed."

        return True, CompatibilityReasonCode.COMPATIBLE, "Evidence cryptographic binding verified."


_DEFAULT_AUTHORITY_CACHE: Dict[Tuple[Optional[str], Tuple[str, ...], bool], EvidenceAuthority] = {}


def get_default_evidence_authority(
    secret_key: Optional[str] = None,
    previous_secret_keys: Optional[Sequence[str]] = None,
    allow_ephemeral: bool = True,
) -> EvidenceAuthority:
    """Returns a reusable process-scoped EvidenceAuthority instance."""
    norm = secret_key.strip() if isinstance(secret_key, str) and secret_key.strip() else os.getenv("SEMANTIC_CACHE_EVIDENCE_SECRET")
    if norm is not None:
        norm = norm.strip() or None

    if previous_secret_keys is not None:
        norm_prev = tuple(str(p).strip() for p in previous_secret_keys if str(p).strip())
    else:
        raw_env_prev = (os.getenv("SEMANTIC_CACHE_EVIDENCE_PREVIOUS_SECRETS") or "").strip()
        norm_prev = tuple(p.strip() for p in raw_env_prev.split(",") if p.strip()) if raw_env_prev else ()

    cache_key = (norm, norm_prev, bool(allow_ephemeral))
    if cache_key not in _DEFAULT_AUTHORITY_CACHE:
        _DEFAULT_AUTHORITY_CACHE[cache_key] = EvidenceAuthority(
            secret_key=norm,
            previous_secret_keys=norm_prev,
            allow_ephemeral=allow_ephemeral,
        )
    return _DEFAULT_AUTHORITY_CACHE[cache_key]
