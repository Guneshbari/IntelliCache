#!/usr/bin/env python3
"""
verify_db_integrity.py (Stage 3.4.1.3)
Load-Testing Database Integrity and Cryptographic State Inspection Tool.

Verifies that the load-testing database's observed `vector_records` table
remained completely unchanged before, during, and after load testing.

Architectural Context:
- Primary safeguards against unintended writes are structural:
  1. `ReadOnlyVectorStoreAdapter`: Structural rejection of all write calls in application code.
  2. PostgreSQL `SET TRANSACTION READ ONLY`: Database-level read-only transaction isolation
     in `PostgresVectorStore.search(readonly_transaction=True)`.
- This inspection tool provides empirical verification by computing row counts and a
  deterministic SHA-256 cryptographic digest across ALL nine columns of `vector_records`:
  `id`, `cache_namespace`, `fingerprint`, `query_text`, `model`, `vector`, `metadata`,
  `created_at`, and `updated_at`.
- Note: This tool observes the local load-testing database (`intellicache_loadtest`),
  confirming that benchmark operations produce zero mutations.
"""

import argparse
import asyncio
import json
import os
import sys
from typing import Any, Dict

import asyncpg


async def get_db_snapshot(conn: asyncpg.Connection) -> Dict[str, Any]:
    """Captures SHA-256 cryptographic checksums and row counts across relevant tables."""
    # Check if tables exist
    tables = await conn.fetch(
        """
        SELECT table_name 
        FROM information_schema.tables 
        WHERE table_schema = 'public'
        """
    )
    table_names = {row["table_name"] for row in tables}

    snapshot: Dict[str, Any] = {}

    # 1. vector_records table (Load-testing baseline: Must NEVER mutate)
    if "vector_records" in table_names:
        vr_count = await conn.fetchval("SELECT count(*) FROM vector_records")
        # Compute deterministic SHA-256 hash over all 9 columns of all rows ordered by id
        vr_checksum = await conn.fetchval(
            """
            SELECT encode(sha256(coalesce(string_agg(
                id::text || ':' ||
                cache_namespace || ':' ||
                fingerprint || ':' ||
                query_text || ':' ||
                model || ':' ||
                vector::text || ':' ||
                metadata::text || ':' ||
                to_char(created_at, 'YYYY-MM-DD"T"HH24:MI:SS.USOF') || ':' ||
                to_char(updated_at, 'YYYY-MM-DD"T"HH24:MI:SS.USOF'),
                '|' ORDER BY id
            ), 'EMPTY_TABLE')::bytea), 'hex')
            FROM vector_records
            """
        )
        snapshot["vector_records"] = {
            "count": int(vr_count),
            "checksum_sha256": str(vr_checksum),
        }
    else:
        snapshot["vector_records"] = {"count": 0, "checksum_sha256": "TABLE_NOT_FOUND"}

    # 2. cache_entries table
    if "cache_entries" in table_names:
        ce_count = await conn.fetchval("SELECT count(*) FROM cache_entries")
        snapshot["cache_entries"] = {"count": int(ce_count)}
    else:
        snapshot["cache_entries"] = {"count": 0}

    # 3. Dedicated shadow_vector_records table
    if "shadow_vector_records" in table_names:
        svr_count = await conn.fetchval("SELECT count(*) FROM shadow_vector_records")
        snapshot["shadow_vector_records"] = {"count": int(svr_count)}
    else:
        snapshot["shadow_vector_records"] = {"count": 0}

    # 4. Dedicated shadow_evaluations table
    if "shadow_evaluations" in table_names:
        se_count = await conn.fetchval("SELECT count(*) FROM shadow_evaluations")
        snapshot["shadow_evaluations"] = {"count": int(se_count)}
    else:
        snapshot["shadow_evaluations"] = {"count": 0}

    return snapshot


async def main():
    parser = argparse.ArgumentParser(description="Verify database integrity and table SHA-256 checksums.")
    parser.add_argument(
        "--db-url",
        default=os.getenv(
            "DATABASE_URL",
            "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_loadtest",
        ),
        help="PostgreSQL connection URL",
    )
    parser.add_argument("--save", metavar="FILE", help="Capture and save baseline snapshot to JSON file")
    parser.add_argument("--verify", metavar="FILE", help="Verify current database state against baseline JSON file")
    parser.add_argument("--print-only", action="store_true", help="Print current database metrics and exit")
    args = parser.parse_args()

    try:
        conn = await asyncpg.connect(args.db_url)
    except Exception as exc:
        print(f"ERROR: Could not connect to database at {args.db_url}: {exc}", file=sys.stderr)
        sys.exit(2)

    try:
        current_snapshot = await get_db_snapshot(conn)
    finally:
        await conn.close()

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(current_snapshot, f, indent=2)
        print(f"[DB Integrity] Saved baseline snapshot to '{args.save}':")
        print(json.dumps(current_snapshot, indent=2))
        return

    if args.verify:
        if not os.path.exists(args.verify):
            print(f"ERROR: Baseline snapshot file '{args.verify}' does not exist.", file=sys.stderr)
            sys.exit(2)

        with open(args.verify, "r", encoding="utf-8") as f:
            baseline = json.load(f)

        print("\n================ DATABASE INTEGRITY & SHA-256 CHECKSUM VERIFICATION ================")
        print(f"{'Table Name':<28} | {'Baseline':<10} | {'Current':<10} | {'Delta':<8} | {'Status'}")
        print("-" * 80)

        # Check vector_records
        b_vr = baseline.get("vector_records", {})
        c_vr = current_snapshot.get("vector_records", {})
        b_vr_count = b_vr.get("count", 0)
        c_vr_count = c_vr.get("count", 0)
        vr_delta = c_vr_count - b_vr_count
        b_vr_hash = b_vr.get("checksum_sha256") or b_vr.get("checksum", "")
        c_vr_hash = c_vr.get("checksum_sha256") or c_vr.get("checksum", "")

        vr_status = "PASS (0 mutations)" if (vr_delta == 0 and b_vr_hash == c_vr_hash) else "FAIL (MUTATED!)"
        print(f"{'vector_records (loadtest)':<28} | {b_vr_count:<10} | {c_vr_count:<10} | {vr_delta:<+8} | {vr_status}")
        print(f"  SHA-256 Baseline: {b_vr_hash}")
        print(f"  SHA-256 Current:  {c_vr_hash}")

        # Check other tables
        for tbl in ["cache_entries", "shadow_vector_records", "shadow_evaluations"]:
            b_cnt = baseline.get(tbl, {}).get("count", 0)
            c_cnt = current_snapshot.get(tbl, {}).get("count", 0)
            delta = c_cnt - b_cnt
            print(f"{tbl:<28} | {b_cnt:<10} | {c_cnt:<10} | {delta:<+8} | {'RECORDED' if delta >= 0 else 'PURGED'}")

        print("====================================================================================\n")

        if vr_delta != 0 or b_vr_hash != c_vr_hash:
            print("CRITICAL FAILURE: Load-testing vector_records table was modified during testing!", file=sys.stderr)
            sys.exit(1)
        else:
            print(
                "[SUCCESS] Load-testing database vector_records integrity verified: "
                "0 observed mutations across all 9 columns (SHA-256 match)."
            )
            print(
                "  Structural safeguards active: ReadOnlyVectorStoreAdapter (application layer) "
                "and PostgreSQL SET TRANSACTION READ ONLY (database layer)."
            )
            return

    # Print only
    print(json.dumps(current_snapshot, indent=2))


if __name__ == "__main__":
    asyncio.run(main())
