"""
Legacy schema v2 vector record inspection, migration, and reversible exclusion manager (Stage 3.5.3).
Re-verifies legacy records against actual joined exact-cache responses via ResponseArtifactVerifier
before promoting to schema v3; records without verifiable response artifacts are reversibly marked
`legacy_excluded=true` so retrieval starvation is prevented without touching exact-cache entries.

Isolated from FastAPI, Starlette, Groq SDK, Ollama SDK, and asyncpg imports (duck-typed pool).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
from typing import Any, Dict, List, Optional, Tuple

from .compatibility import CompatibilityExtractor
from .contracts import (
    CURRENT_SCHEMA_VERSION,
    MetadataStatus,
    SemanticCompatibilityMetadata,
    compute_variant_key,
)
from .evidence import EvidenceAuthority, get_default_evidence_authority
from .verification import ResponseArtifactVerifier


def _validate_table_name(table_name: str) -> str:
    clean = (table_name or "").strip()
    if clean not in ("vector_records", "shadow_vector_records"):
        raise ValueError(f"Unsupported vector table_name '{table_name}'.")
    return clean


def classify_legacy_record_metadata(raw_meta: Optional[Dict[str, Any]]) -> str:
    """Classifies vector metadata JSONB into 'legacy_excluded', 'v3', 'v2', or 'missing_schema'."""
    if not isinstance(raw_meta, dict) or not raw_meta:
        return "missing_schema"
    if raw_meta.get("legacy_excluded") is True:
        return "legacy_excluded"
    sv = raw_meta.get("schema_version")
    if isinstance(sv, int) and not isinstance(sv, bool):
        if sv >= CURRENT_SCHEMA_VERSION:
            return "v3"
        if sv == 2:
            return "v2"
    return "missing_schema"


@dataclass
class LegacyInspectionReport:
    """Audit summary of vector_records schema versions in a target namespace or store."""
    total_records: int = 0
    v3_records: int = 0
    v2_records: int = 0
    missing_schema_records: int = 0
    excluded_records: int = 0
    verifiable_candidates: int = 0
    unverifiable_or_orphaned: int = 0
    sample_fingerprints: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "total_records": self.total_records,
            "v3_records": self.v3_records,
            "v2_records": self.v2_records,
            "missing_schema_records": self.missing_schema_records,
            "excluded_records": self.excluded_records,
            "verifiable_candidates": self.verifiable_candidates,
            "unverifiable_or_orphaned": self.unverifiable_or_orphaned,
            "sample_fingerprints": list(self.sample_fingerprints),
        }


@dataclass
class LegacyMigrationReport:
    """Execution summary for legacy schema migration or rollback."""
    dry_run: bool = False
    inspected: int = 0
    upgraded_to_v3: int = 0
    excluded_legacy: int = 0
    restored: int = 0
    skipped: int = 0
    errors: List[str] = field(default_factory=list)
    outcomes: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "dry_run": self.dry_run,
            "inspected": self.inspected,
            "upgraded_to_v3": self.upgraded_to_v3,
            "excluded_legacy": self.excluded_legacy,
            "restored": self.restored,
            "skipped": self.skipped,
            "errors": list(self.errors),
            "outcomes": list(self.outcomes),
        }


def build_migrated_record_metadata(
    *,
    raw_meta: Optional[Dict[str, Any]],
    cache_namespace: str,
    fingerprint: str,
    query_text: str,
    model: str,
    exact_response: Optional[str],
    extractor: Optional[CompatibilityExtractor] = None,
    verifier: Optional[ResponseArtifactVerifier] = None,
    authority: Optional[EvidenceAuthority] = None,
) -> Tuple[str, Dict[str, Any], str]:
    """
    Determines deterministic migration action for a legacy vector record.
    Returns (action, new_metadata_dict, reason):
      - action == 'upgrade_v3': valid joined response passed ResponseArtifactVerifier; new v3 metadata minted.
      - action == 'exclude': no joined response or failed/uncertain verification; reversibly flagged legacy_excluded=True.
    Never fabricates verification claims without a real response passing ResponseArtifactVerifier.
    """
    ext = extractor or CompatibilityExtractor()
    ver = verifier or ResponseArtifactVerifier()
    auth = authority or get_default_evidence_authority()

    base_meta = dict(raw_meta or {})

    if not exact_response or not str(exact_response).strip():
        updated = dict(base_meta)
        updated["legacy_excluded"] = True
        updated["legacy_excluded_reason"] = "no_exact_cache_response"
        updated["legacy_excluded_at"] = datetime.now(timezone.utc).isoformat()
        if "schema_version" in base_meta:
            updated["legacy_previous_schema_version"] = base_meta.get("schema_version")
        return "exclude", updated, "No active exact-cache response available for verification."

    gen = base_meta.get("generation_contract") if isinstance(base_meta.get("generation_contract"), dict) else {}
    provider = str(gen.get("provider") or base_meta.get("llm_provider") or "groq").strip()
    gen_model = str(gen.get("model") or base_meta.get("llm_model") or "llama-3.3-70b-versatile").strip()
    try:
        temperature = float(gen.get("temperature") if gen.get("temperature") is not None else base_meta.get("temperature", 0.7))
    except (TypeError, ValueError):
        temperature = 0.7
    max_tokens = gen.get("max_tokens") if gen.get("max_tokens") is not None else base_meta.get("max_tokens")
    if max_tokens is not None and (not isinstance(max_tokens, int) or isinstance(max_tokens, bool) or max_tokens <= 0):
        max_tokens = None

    emb = base_meta.get("embedding_contract") if isinstance(base_meta.get("embedding_contract"), dict) else {}
    emb_model = str(emb.get("model") or model or "nomic-embed-text").strip()

    extracted = ext.extract_from_text(
        text=query_text or "",
        provider=provider,
        model=gen_model,
        temperature=temperature,
        max_tokens=max_tokens,
        configured_model=emb_model,
    )

    # Reject promotion when extracted v3 requirements contain unresolved/unsafe states
    has_unknown_dim = extracted.task_kind.status == MetadataStatus.UNKNOWN or any(
        d.status == MetadataStatus.UNKNOWN for d in extracted.dimensions.values()
    )
    fresh_dim = extracted.dimensions.get("freshness")
    ctx_dim = extracted.dimensions.get("context_dependency")
    if (
        has_unknown_dim
        or (fresh_dim and fresh_dim.status != MetadataStatus.NOT_APPLICABLE)
        or (ctx_dim and ctx_dim.status != MetadataStatus.NOT_APPLICABLE)
    ):
        updated = dict(base_meta)
        updated["legacy_excluded"] = True
        updated["legacy_excluded_reason"] = "unresolvable_v3_requirements"
        updated["legacy_excluded_at"] = datetime.now(timezone.utc).isoformat()
        if "schema_version" in base_meta:
            updated["legacy_previous_schema_version"] = base_meta.get("schema_version")
        return "exclude", updated, "Legacy query has ambiguous, freshness-dependent, or context-dependent requirements."

    ver_res = ver.verify(
        response_text=exact_response,
        request_metadata=extracted,
        query_text=query_text or "",
        finish_reason=None,
        completion_tokens=0,
        request_max_tokens=max_tokens,
    )
    if not ver_res.is_verified:
        updated = dict(base_meta)
        updated["legacy_excluded"] = True
        updated["legacy_excluded_reason"] = f"verification_{ver_res.status.value.lower()}:{ver_res.reason_code.value}"
        updated["legacy_excluded_at"] = datetime.now(timezone.utc).isoformat()
        if "schema_version" in base_meta:
            updated["legacy_previous_schema_version"] = base_meta.get("schema_version")
        return "exclude", updated, ver_res.message

    evidence = auth.mint_response_verified_evidence(
        cache_namespace=cache_namespace,
        fingerprint=fingerprint,
        response_text=exact_response,
        metadata=extracted,
        verification=ver_res,
    )
    migrated = SemanticCompatibilityMetadata(
        schema_version=CURRENT_SCHEMA_VERSION,
        extractor_version=extracted.extractor_version,
        task_kind=extracted.task_kind,
        dimensions=extracted.dimensions,
        generation_contract=extracted.generation_contract,
        embedding_contract=extracted.embedding_contract,
        indexed_at=extracted.indexed_at,
        compatibility_evidence=evidence,
    )
    payload = migrated.to_dict()
    payload["variant_key"] = compute_variant_key(migrated, schema_digest=ver_res.verified_schema_digest)
    payload["legacy_migrated_from"] = base_meta.get("schema_version")
    payload.pop("legacy_excluded", None)
    return "upgrade_v3", payload, "Legacy record re-verified and migrated to schema v3."


class PostgresLegacyLifecycleManager:
    """
    PostgreSQL legacy record inspection, migration, and rollback utility.
    Accepts any asyncpg-compatible connection pool via duck typing without importing asyncpg.
    """

    def __init__(
        self,
        pool: Any,
        *,
        table_name: str = "vector_records",
        extractor: Optional[CompatibilityExtractor] = None,
        verifier: Optional[ResponseArtifactVerifier] = None,
        authority: Optional[EvidenceAuthority] = None,
    ):
        self.pool = pool
        self.table_name = _validate_table_name(table_name)
        self.extractor = extractor or CompatibilityExtractor()
        self.verifier = verifier or ResponseArtifactVerifier()
        self.authority = authority or get_default_evidence_authority()

    async def inspect(self, *, namespace: Optional[str] = None) -> LegacyInspectionReport:
        where_clause = "WHERE v.cache_namespace = $1" if namespace else ""
        params = [namespace.strip()] if namespace else []

        query = f"""
            SELECT
                v.cache_namespace,
                v.fingerprint,
                v.query_text,
                v.model,
                v.metadata,
                c.response AS exact_response
            FROM {self.table_name} v
            LEFT JOIN cache_entries c
                ON v.cache_namespace = c.cache_namespace
               AND v.fingerprint = c.fingerprint
               AND (c.expires_at IS NULL OR c.expires_at > NOW())
            {where_clause}
            ORDER BY v.id ASC;
        """
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)

        report = LegacyInspectionReport()
        for row in rows:
            report.total_records += 1
            raw_meta = row["metadata"]
            meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
            kind = classify_legacy_record_metadata(meta)
            if kind == "v3":
                report.v3_records += 1
            elif kind == "legacy_excluded":
                report.excluded_records += 1
            else:
                if kind == "v2":
                    report.v2_records += 1
                else:
                    report.missing_schema_records += 1
                if row["exact_response"]:
                    report.verifiable_candidates += 1
                else:
                    report.unverifiable_or_orphaned += 1
                if len(report.sample_fingerprints) < 20:
                    report.sample_fingerprints.append(row["fingerprint"])
        return report

    async def migrate(
        self,
        *,
        namespace: Optional[str] = None,
        dry_run: bool = False,
        exclude_only: bool = False,
    ) -> LegacyMigrationReport:
        """
        Migrates v2 / missing-schema vector records.
        Supports dry_run=True to inspect planned changes without mutating database rows.
        When exclude_only=True, marks non-v3 records as legacy_excluded=True without attempting v3 promotion.
        """
        where_clause = "WHERE v.cache_namespace = $1" if namespace else ""
        params = [namespace.strip()] if namespace else []

        query = f"""
            SELECT
                v.id,
                v.cache_namespace,
                v.fingerprint,
                v.query_text,
                v.model,
                v.metadata,
                c.response AS exact_response
            FROM {self.table_name} v
            LEFT JOIN cache_entries c
                ON v.cache_namespace = c.cache_namespace
               AND v.fingerprint = c.fingerprint
               AND (c.expires_at IS NULL OR c.expires_at > NOW())
            {where_clause}
            ORDER BY v.id ASC;
        """
        report = LegacyMigrationReport(dry_run=dry_run)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            updates: List[Tuple[int, str]] = []

            for row in rows:
                report.inspected += 1
                raw_meta = row["metadata"]
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
                kind = classify_legacy_record_metadata(meta)
                if kind in ("v3", "legacy_excluded"):
                    report.skipped += 1
                    continue

                if exclude_only:
                    updated = dict(meta)
                    updated["legacy_excluded"] = True
                    updated["legacy_excluded_reason"] = "operator_legacy_exclusion"
                    updated["legacy_excluded_at"] = datetime.now(timezone.utc).isoformat()
                    if "schema_version" in meta:
                        updated["legacy_previous_schema_version"] = meta.get("schema_version")
                    action, new_meta, reason = "exclude", updated, "Marked legacy record excluded from semantic lookup."
                else:
                    action, new_meta, reason = build_migrated_record_metadata(
                        raw_meta=meta,
                        cache_namespace=row["cache_namespace"],
                        fingerprint=row["fingerprint"],
                        query_text=row["query_text"],
                        model=row["model"],
                        exact_response=row["exact_response"],
                        extractor=self.extractor,
                        verifier=self.verifier,
                        authority=self.authority,
                    )

                if action == "upgrade_v3":
                    report.upgraded_to_v3 += 1
                else:
                    report.excluded_legacy += 1
                report.outcomes.append({
                    "fingerprint_prefix": row["fingerprint"][:12],
                    "action": action,
                    "reason": reason,
                })
                updates.append((row["id"], json.dumps(new_meta)))

            if not dry_run and updates:
                async with conn.transaction():
                    for rec_id, meta_json in updates:
                        await conn.execute(
                            f"UPDATE {self.table_name} SET metadata = $2::jsonb, updated_at = NOW() WHERE id = $1",
                            rec_id,
                            meta_json,
                        )

        return report

    async def restore_excluded(
        self,
        *,
        namespace: Optional[str] = None,
        dry_run: bool = False,
    ) -> LegacyMigrationReport:
        """Reversibly restores records previously flagged with metadata['legacy_excluded'] = True."""
        where_parts = ["COALESCE((v.metadata->>'legacy_excluded')::boolean, FALSE) = TRUE"]
        params: List[Any] = []
        if namespace:
            params.append(namespace.strip())
            where_parts.append(f"v.cache_namespace = ${len(params)}")
        where_clause = "WHERE " + " AND ".join(where_parts)

        query = f"""
            SELECT v.id, v.fingerprint, v.metadata
            FROM {self.table_name} v
            {where_clause}
            ORDER BY v.id ASC;
        """
        report = LegacyMigrationReport(dry_run=dry_run)
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(query, *params)
            updates: List[Tuple[int, str]] = []
            for row in rows:
                report.inspected += 1
                raw_meta = row["metadata"]
                meta = json.loads(raw_meta) if isinstance(raw_meta, str) else dict(raw_meta or {})
                meta.pop("legacy_excluded", None)
                meta.pop("legacy_excluded_reason", None)
                meta.pop("legacy_excluded_at", None)
                meta.pop("legacy_previous_schema_version", None)
                report.restored += 1
                report.outcomes.append({"fingerprint_prefix": row["fingerprint"][:12], "action": "restored"})
                updates.append((row["id"], json.dumps(meta)))

            if not dry_run and updates:
                async with conn.transaction():
                    for rec_id, meta_json in updates:
                        await conn.execute(
                            f"UPDATE {self.table_name} SET metadata = $2::jsonb, updated_at = NOW() WHERE id = $1",
                            rec_id,
                            meta_json,
                        )
        return report
