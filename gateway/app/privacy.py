"""
Leaf privacy and scope isolation primitives for IntelliCache (Stage 3.5.4).

Zero internal package dependencies (stdlib only) so cache, vector_store,
semantic_cache, core, conversations, and API layers can share deterministic
visibility/ownership rules without circular imports.
"""

from __future__ import annotations

import hashlib
from typing import Optional

CACHE_VISIBILITY_PRIVATE = "private"
CACHE_VISIBILITY_TENANT_SHARED = "tenant_shared"
CACHE_VISIBILITY_UNKNOWN = "unknown"
VALID_CACHE_VISIBILITIES = frozenset(
    {CACHE_VISIBILITY_PRIVATE, CACHE_VISIBILITY_TENANT_SHARED}
)


def normalize_cache_visibility(value: Optional[str]) -> str:
    """Normalize cache visibility strictly; unknown/invalid values fail closed to 'unknown'."""
    if not isinstance(value, str):
        return CACHE_VISIBILITY_UNKNOWN
    normalized = value.strip().lower()
    if normalized in VALID_CACHE_VISIBILITIES:
        return normalized
    return CACHE_VISIBILITY_UNKNOWN


def normalize_principal_id(value: Optional[str]) -> Optional[str]:
    """Normalize principal_id to a stripped non-empty string or None."""
    if not isinstance(value, str):
        return None
    normalized = value.strip()
    return normalized if normalized else None


def derive_scoped_exact_key(
    fingerprint: str,
    visibility: str,
    principal_id: Optional[str] = None,
) -> str:
    """
    Derive a storage exact key isolating 'private' entries per principal while
    preserving canonical fingerprints for 'tenant_shared' entries.
    Fails closed on 'unknown' or private entries without a verified principal_id.
    """
    vis = normalize_cache_visibility(visibility)
    if vis == CACHE_VISIBILITY_TENANT_SHARED:
        return fingerprint
    if vis == CACHE_VISIBILITY_PRIVATE:
        norm_principal = normalize_principal_id(principal_id)
        if norm_principal is None:
            raise ValueError("Cannot derive private scoped cache key without a verified principal_id.")
        return hashlib.sha256(f"priv:{norm_principal}:{fingerprint}".encode("utf-8")).hexdigest()
    raise ValueError(f"Unsupported cache visibility for scoped key derivation: {visibility!r}")


def matches_cache_scope(
    entry_visibility: Optional[str],
    entry_owner: Optional[str],
    requested_visibility: Optional[str],
    requested_principal: Optional[str],
    allow_shared_reads: bool = False,
) -> bool:
    """
    Shared deterministic privacy-scope enforcement rule for exact and vector stores.
    - When requested_visibility is specified:
      - 'unknown' always fails closed (False).
      - 'tenant_shared' requires entry_visibility == 'tenant_shared' and entry_owner is None.
      - 'private' requires non-empty requested_principal and matches own private entries
        (plus unowned 'tenant_shared' only when allow_shared_reads=True).
      - Legacy/unknown stored entries NEVER match explicit scoped reads.
    - When requested_visibility is None (unscoped low-level caller):
      - Stored 'private' entries NEVER match; legacy/shared entries remain readable.
    """
    entry_vis = normalize_cache_visibility(entry_visibility)
    entry_own = normalize_principal_id(entry_owner)

    if requested_visibility is None:
        return entry_vis != CACHE_VISIBILITY_PRIVATE

    req_vis = normalize_cache_visibility(requested_visibility)
    if req_vis == CACHE_VISIBILITY_UNKNOWN:
        return False

    if req_vis == CACHE_VISIBILITY_TENANT_SHARED:
        return entry_vis == CACHE_VISIBILITY_TENANT_SHARED and entry_own is None

    if req_vis == CACHE_VISIBILITY_PRIVATE:
        req_own = normalize_principal_id(requested_principal)
        if req_own is None:
            return False
        if entry_vis == CACHE_VISIBILITY_PRIVATE:
            return entry_own == req_own
        if allow_shared_reads and entry_vis == CACHE_VISIBILITY_TENANT_SHARED and entry_own is None:
            return True
        return False

    return False
