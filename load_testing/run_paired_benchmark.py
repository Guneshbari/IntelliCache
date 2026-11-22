#!/usr/bin/env python3
"""
run_paired_benchmark.py (Stage 3.4.1.4)
Orchestrates genuine controlled paired benchmarks between Baseline (exact-cache only)
and Shadow Evaluation modes under identical cold-cache initial conditions.

Key Improvements in Stage 3.4.1.4:
1. Fail-Fast Cache & Metrics Reset: Explicit database table purge (`DELETE FROM cache_entries;`)
   and verification that count == 0, plus admin cache clear and metrics reset with hard assertion
   that entries == 0 before every single trial. Aborts immediately on any failure.
2. Isolated Shadow-Mode Warm-Up: Setup priming requests pass `X-Warmup-Prime: true` so the gateway
   populates exact-cache targets without enqueuing background FastEmbed/pgvector shadow tasks.
   In shadow mode, a 0.5s quiescence pause guarantees all workers are idle before measurements.
3. Alternating Execution Order:
   - Pair 1: Baseline 1 -> Shadow 1
   - Pair 2: Shadow 2 -> Baseline 2
   - Pair 3: Baseline 3 -> Shadow 3
   Eliminates warm-up bias, database growth artifacts, and system drift.
4. Genuinely Unique Miss Prompts: Unique misses parameterized by pair ID
   (`[pair-X-vu-Y-iter-Z]`), ensuring identical request sequences within each pair
   and distinct cold misses across pairs.
5. Per-Pair Difference Calculation: Calculates true paired deltas (Shadow_i - Baseline_i)
   for each individual trial pair before synthesizing the median, min, max, and stddev.
6. Cryptographic State Verification: Pre- and post-test SHA-256 integrity inspection
   over all 9 columns of `vector_records` to verify zero mutations in the load-testing DB,
   with hard missing-table verification guards.
"""

import asyncio
import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

import asyncpg
import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "gateway"
LOAD_TEST_DIR = REPO_ROOT / "load_testing"
REPORTS_DIR = LOAD_TEST_DIR / "reports"
PYTHON_BIN = GATEWAY_DIR / ".venv" / "bin" / "python3"
UVICORN_BIN = GATEWAY_DIR / ".venv" / "bin" / "uvicorn"

BASE_URL = "http://127.0.0.1:8000"
DB_URL = os.getenv("DATABASE_URL", "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_loadtest")
SEED = "1337"
VUS = 10
ITERATIONS = 1000


def get_base_env(shadow_enabled: bool) -> Dict[str, str]:
    """Generates standard environment variables for Gateway execution."""
    env = os.environ.copy()
    env.update(
        {
            "PYTHONPATH": str(GATEWAY_DIR),
            "HOST": "127.0.0.1",
            "PORT": "8000",
            "LLM_PROVIDER": "mock",
            "GROQ_MODEL": "mock-qwen-27b",
            "CACHE_BACKEND": "postgres",
            "DATABASE_URL": DB_URL,
            "VECTOR_STORE_BACKEND": "postgres",
            "VECTOR_DIMENSIONS": "384",
            "EMBEDDING_PROVIDER": "fastembed",
            "EMBEDDING_MODEL": "BAAI/bge-small-en-v1.5",
            "SEMANTIC_CACHE_ENABLED": "false",
            "SEMANTIC_CACHE_MIN_SIMILARITY": "0.92",
            "SEMANTIC_CACHE_MIN_MARGIN": "0.03",
            "SHADOW_EVALUATION_ENABLED": "true" if shadow_enabled else "false",
            "SHADOW_SAMPLING_RATE": "1.0",
            "SHADOW_MAX_CONCURRENCY": "4",
            "SHADOW_QUEUE_MAX_SIZE": "1000",
            "SHADOW_TIMEOUT_SECONDS": "5.0",
            "ADMIN_API_TOKEN": "test-admin-token",
            "CLIENT_API_TOKENS": '{"test-client-token": "default", "tenant-b-token": "tenant_b"}',
            "ALLOW_UNAUTHENTICATED_LOOPBACK": "false",
        }
    )
    return env


def start_gateway(shadow_enabled: bool) -> subprocess.Popen:
    """Spawns the IntelliCache Gateway process."""
    mode_str = "SHADOW (ENABLED)" if shadow_enabled else "BASELINE (DISABLED)"
    print(f"\n[Gateway Lifecycle] Starting Gateway in {mode_str} mode...")
    env = get_base_env(shadow_enabled)
    proc = subprocess.Popen(
        [
            str(UVICORN_BIN),
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            "8000",
            "--log-level",
            "warning",
        ],
        cwd=str(GATEWAY_DIR),
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )

    # Wait for /health endpoint
    max_wait = 20.0
    start = time.perf_counter()
    healthy = False
    while time.perf_counter() - start < max_wait:
        try:
            req = urllib.request.Request(f"{BASE_URL}/health")
            with urllib.request.urlopen(req, timeout=1.0) as resp:
                if resp.status == 200:
                    healthy = True
                    break
        except Exception:
            time.sleep(0.3)

    if not healthy:
        proc.terminate()
        raise RuntimeError("Gateway process failed to respond healthy within 20s.")

    elapsed = time.perf_counter() - start
    print(f"[Gateway Lifecycle] Gateway is healthy and accepting traffic (ready in {elapsed:.2f}s, PID: {proc.pid}).")
    return proc


def stop_gateway(proc: subprocess.Popen) -> None:
    """Gracefully terminates Gateway process."""
    if proc is None:
        return
    print(f"[Gateway Lifecycle] Stopping Gateway (PID: {proc.pid})...")
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("[Gateway Lifecycle] Gateway stopped.")
    time.sleep(1.0)


async def _async_purge_and_verify_db() -> int:
    conn = await asyncpg.connect(DB_URL)
    try:
        await conn.execute("DELETE FROM cache_entries;")
        count = await conn.fetchval("SELECT count(*) FROM cache_entries;")
        return int(count)
    finally:
        await conn.close()


def reset_and_verify_cache_state():
    """
    Purges cache_entries and resets telemetry counters with strict fail-fast validation.
    Aborts immediately if the database purge, administrative endpoints, or empty-state
    verification fail (Stage 3.4.1.4).
    """
    # 1. Direct database purge and verification
    try:
        db_count = asyncio.run(_async_purge_and_verify_db())
    except Exception as exc:
        raise RuntimeError(f"Database cache purge failed with connection error: {exc}") from exc

    if db_count != 0:
        raise RuntimeError(f"Database cache purge failed: {db_count} entries remain in cache_entries!")

    # 2. Administrative cache purge verification
    req_clear = urllib.request.Request(
        f"{BASE_URL}/admin/cache/clear?namespace=all",
        headers={"X-Admin-Token": "test-admin-token"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_clear, timeout=5.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Admin cache clear failed with HTTP status {resp.status}")
            data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                raise RuntimeError(f"Admin cache clear payload rejected: {data}")
    except Exception as exc:
        raise RuntimeError(f"Admin cache clear failed: {exc}") from exc

    # 3. Administrative metrics reset verification
    req_reset = urllib.request.Request(
        f"{BASE_URL}/admin/metrics/reset",
        headers={"X-Admin-Token": "test-admin-token"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req_reset, timeout=5.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Admin metrics reset failed with HTTP status {resp.status}")
            data = json.loads(resp.read().decode())
            if data.get("status") != "ok":
                raise RuntimeError(f"Admin metrics reset payload rejected: {data}")
    except Exception as exc:
        raise RuntimeError(f"Admin metrics reset failed: {exc}") from exc

    # 4. Final verification: Verify zero entries exist in cache
    req_stats = urllib.request.Request(
        f"{BASE_URL}/cache/stats",
        headers={"X-Admin-Token": "test-admin-token"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req_stats, timeout=5.0) as resp:
            if resp.status != 200:
                raise RuntimeError(f"Cache stats check failed with HTTP status {resp.status}")
            stats_data = json.loads(resp.read().decode())
            current_entries = stats_data.get("current_entries", stats_data.get("total_entries", 0))
            if current_entries != 0:
                raise RuntimeError(f"Pre-trial cache verification failed: cache stats report {current_entries} entries!")
    except Exception as exc:
        raise RuntimeError(f"Pre-trial cache stats verification failed: {exc}") from exc

    print("[Cache Reset] Cache state verified completely empty (0 entries) and telemetry reset for upcoming run.")


def run_k6(script_name: str, output_json: str, extra_env: Dict[str, str] = None) -> int:
    """Executes a k6 script and writes the summary to a JSON file."""
    script_path = LOAD_TEST_DIR / "scripts" / script_name
    summary_path = REPORTS_DIR / output_json
    env = os.environ.copy()
    env.update(
        {
            "BASE_URL": BASE_URL,
            "CLIENT_TOKEN": "test-client-token",
            "ADMIN_TOKEN": "test-admin-token",
            "WORKLOAD_SEED": SEED,
            "VUS": str(VUS),
            "ITERATIONS": str(ITERATIONS),
        }
    )
    if extra_env:
        env.update(extra_env)

    cmd = [
        "k6",
        "run",
        "--summary-export",
        str(summary_path),
        str(script_path),
    ]

    print(f"[k6 Runner] Executing: {' '.join(cmd)}")
    start = time.perf_counter()
    res = subprocess.run(cmd, env=env)
    elapsed = time.perf_counter() - start
    print(f"[k6 Runner] Finished '{script_name}' in {elapsed:.2f}s (Exit code: {res.returncode})")
    return res.returncode


def extract_metrics(summary_file: Path) -> Dict[str, Any]:
    """Extracts duration, hit latency, miss latency, and throughput from k6 summary JSON."""
    with open(summary_file, "r", encoding="utf-8") as f:
        data = json.load(f)

    metrics = data.get("metrics", {})

    def _trend(name: str) -> Dict[str, float]:
        raw = metrics.get(name, {})
        t = raw.get("values", raw) if isinstance(raw, dict) else {}
        return {
            "avg": round(float(t.get("avg", 0.0)), 2),
            "min": round(float(t.get("min", 0.0)), 2),
            "med": round(float(t.get("med", 0.0)), 2),
            "p90": round(float(t.get("p(90)", 0.0)), 2),
            "p95": round(float(t.get("p(95)", 0.0)), 2),
            "p99": round(float(t.get("p(99)", 0.0)), 2),
            "max": round(float(t.get("max", 0.0)), 2),
            "count": int(t.get("count", 0)),
        }

    # Overall duration
    overall = _trend("http_req_duration")
    # Sub-metrics
    exact_hit = _trend("exact_hit_latency_ms")
    exact_miss = _trend("cache_miss_latency_ms")
    # Throughput
    raw_reqs = metrics.get("http_reqs", {})
    t_reqs = raw_reqs.get("values", raw_reqs) if isinstance(raw_reqs, dict) else {}
    req_count = int(t_reqs.get("count", 0))
    req_rate = round(float(t_reqs.get("rate", 0.0)), 2)

    return {
        "req_count": req_count,
        "req_rate": req_rate,
        "overall": overall,
        "exact_hit": exact_hit,
        "exact_miss": exact_miss,
    }


def compute_paired_comparison(pairs: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Computes per-pair differences and synthesizes aggregate statistics across pairs.
    Each element of `pairs` contains {'pair_id': str, 'baseline': dict, 'shadow': dict}.
    """
    pair_deltas: List[Dict[str, Any]] = []

    for p in pairs:
        b = p["baseline"]
        s = p["shadow"]
        pid = p["pair_id"]

        def _cohort_diff(cat: str):
            b_cat = b[cat]
            s_cat = s[cat]
            d_med = round(s_cat["med"] - b_cat["med"], 2)
            pct_med = round((d_med / b_cat["med"]) * 100.0, 2) if b_cat["med"] > 0 else 0.0
            d_p95 = round(s_cat["p95"] - b_cat["p95"], 2)
            pct_p95 = round((d_p95 / b_cat["p95"]) * 100.0, 2) if b_cat["p95"] > 0 else 0.0
            d_avg = round(s_cat["avg"] - b_cat["avg"], 2)
            pct_avg = round((d_avg / b_cat["avg"]) * 100.0, 2) if b_cat["avg"] > 0 else 0.0
            return {
                "baseline_med": b_cat["med"],
                "shadow_med": s_cat["med"],
                "delta_med_ms": d_med,
                "pct_med": pct_med,
                "baseline_p95": b_cat["p95"],
                "shadow_p95": s_cat["p95"],
                "delta_p95_ms": d_p95,
                "pct_p95": pct_p95,
                "baseline_avg": b_cat["avg"],
                "shadow_avg": s_cat["avg"],
                "delta_avg_ms": d_avg,
                "pct_avg": pct_avg,
            }

        d_rate = round(s["req_rate"] - b["req_rate"], 2)
        pct_rate = round((d_rate / b["req_rate"]) * 100.0, 2) if b["req_rate"] > 0 else 0.0

        pair_deltas.append(
            {
                "pair_id": pid,
                "overall": _cohort_diff("overall"),
                "exact_hit": _cohort_diff("exact_hit"),
                "exact_miss": _cohort_diff("exact_miss"),
                "throughput": {
                    "baseline_rate": b["req_rate"],
                    "shadow_rate": s["req_rate"],
                    "delta_rate": d_rate,
                    "pct_rate": pct_rate,
                },
            }
        )

    # Aggregate statistics across the pairs
    def _agg_deltas(cat: str, field: str) -> Dict[str, float]:
        vals = [pd[cat][field] for pd in pair_deltas]
        return {
            "median": round(float(np.median(vals)), 2),
            "min": round(float(np.min(vals)), 2),
            "max": round(float(np.max(vals)), 2),
            "stddev": round(float(np.std(vals)), 2),
        }

    aggregated = {}
    for cat in ["overall", "exact_hit", "exact_miss"]:
        aggregated[cat] = {
            "delta_med_ms": _agg_deltas(cat, "delta_med_ms"),
            "pct_med": _agg_deltas(cat, "pct_med"),
            "delta_p95_ms": _agg_deltas(cat, "delta_p95_ms"),
            "pct_p95": _agg_deltas(cat, "pct_p95"),
            "delta_avg_ms": _agg_deltas(cat, "delta_avg_ms"),
        }

    aggregated["throughput"] = {
        "delta_rate": _agg_deltas("throughput", "delta_rate"),
        "pct_rate": _agg_deltas("throughput", "pct_rate"),
        "baseline_rate_median": round(float(np.median([p["baseline"]["req_rate"] for p in pairs])), 2),
        "shadow_rate_median": round(float(np.median([p["shadow"]["req_rate"] for p in pairs])), 2),
    }

    return {
        "per_pair_results": pair_deltas,
        "aggregated_paired_deltas": aggregated,
    }


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    baseline_snapshot_file = REPORTS_DIR / "db_baseline_snapshot.json"
    db_verify_script = LOAD_TEST_DIR / "helpers" / "verify_db_integrity.py"

    print("================================================================================")
    print("STAGE 3.4.1.4: CONTROLLED PAIRED BENCHMARK ORCHESTRATOR")
    print(f"Total Requests per Run: {ITERATIONS} (10 VUs x 100 iters)")
    print(f"Workload Seed:          {SEED}")
    print(f"PostgreSQL DB URL:      {DB_URL}")
    print("Design:                 Alternating execution order, cold-cache state isolation,")
    print("                        per-pair difference aggregation, SHA-256 DB verification.")
    print("================================================================================\n")

    # Step 0: Capture Database Baseline Snapshot (SHA-256 over all 9 columns)
    print("\n--- STEP 0: Capturing Initial Database SHA-256 Integrity Snapshot ---")
    res = subprocess.run(
        [
            str(PYTHON_BIN),
            str(db_verify_script),
            "--save",
            str(baseline_snapshot_file),
            "--expected-db",
            "intellicache_loadtest",
        ]
    )
    if res.returncode != 0:
        print("ERROR: Failed to capture database baseline snapshot.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Smoke Test
    print("\n--- STEP 1: Running Smoke Test Verification ---")
    proc = start_gateway(shadow_enabled=False)
    try:
        reset_and_verify_cache_state()
        ret = run_k6("00_smoke_test.js", "smoke_test_summary.json")
        if ret != 0:
            print("ERROR: Smoke test failed.", file=sys.stderr)
            sys.exit(1)
    finally:
        stop_gateway(proc)

    # Step 2: 3 Alternating Controlled Paired Runs
    # Pair 1: Baseline 1 -> Shadow 1
    # Pair 2: Shadow 2   -> Baseline 2
    # Pair 3: Baseline 3 -> Shadow 3
    print("\n--- STEP 2: Executing 3 Alternating Controlled Paired Runs ---")
    pairs_data: List[Dict[str, Any]] = []

    # Track current active gateway process and mode
    current_proc: Optional[subprocess.Popen] = None
    current_mode: Optional[bool] = None

    def ensure_gateway(shadow_enabled: bool) -> subprocess.Popen:
        nonlocal current_proc, current_mode
        if current_proc is not None and current_mode == shadow_enabled:
            return current_proc
        if current_proc is not None:
            stop_gateway(current_proc)
        current_proc = start_gateway(shadow_enabled)
        current_mode = shadow_enabled
        return current_proc

    try:
        # ---------------- Pair 1 (Order: Baseline -> Shadow) ----------------
        print("\n>>> STARTING PAIR 1 (Token: pair1, Order: Baseline -> Shadow) <<<")
        # 1A: Baseline
        ensure_gateway(shadow_enabled=False)
        reset_and_verify_cache_state()
        ret = run_k6("01_baseline_exact_cache.js", "baseline_run_1.json", extra_env={"PAIR_ID": "pair1"})
        if ret != 0:
            raise RuntimeError("Baseline run 1 failed")
        b1_metrics = extract_metrics(REPORTS_DIR / "baseline_run_1.json")

        # 1B: Shadow
        ensure_gateway(shadow_enabled=True)
        reset_and_verify_cache_state()
        ret = run_k6("02_shadow_evaluation.js", "shadow_run_1.json", extra_env={"PAIR_ID": "pair1"})
        if ret != 0:
            raise RuntimeError("Shadow run 1 failed")
        s1_metrics = extract_metrics(REPORTS_DIR / "shadow_run_1.json")

        pairs_data.append({"pair_id": "pair_1", "baseline": b1_metrics, "shadow": s1_metrics})

        # ---------------- Pair 2 (Order: Shadow -> Baseline) ----------------
        print("\n>>> STARTING PAIR 2 (Token: pair2, Order: Shadow -> Baseline [Alternated]) <<<")
        # 2A: Shadow
        ensure_gateway(shadow_enabled=True)
        reset_and_verify_cache_state()
        ret = run_k6("02_shadow_evaluation.js", "shadow_run_2.json", extra_env={"PAIR_ID": "pair2"})
        if ret != 0:
            raise RuntimeError("Shadow run 2 failed")
        s2_metrics = extract_metrics(REPORTS_DIR / "shadow_run_2.json")

        # 2B: Baseline
        ensure_gateway(shadow_enabled=False)
        reset_and_verify_cache_state()
        ret = run_k6("01_baseline_exact_cache.js", "baseline_run_2.json", extra_env={"PAIR_ID": "pair2"})
        if ret != 0:
            raise RuntimeError("Baseline run 2 failed")
        b2_metrics = extract_metrics(REPORTS_DIR / "baseline_run_2.json")

        pairs_data.append({"pair_id": "pair_2", "baseline": b2_metrics, "shadow": s2_metrics})

        # ---------------- Pair 3 (Order: Baseline -> Shadow) ----------------
        print("\n>>> STARTING PAIR 3 (Token: pair3, Order: Baseline -> Shadow [Alternated]) <<<")
        # 3A: Baseline
        ensure_gateway(shadow_enabled=False)
        reset_and_verify_cache_state()
        ret = run_k6("01_baseline_exact_cache.js", "baseline_run_3.json", extra_env={"PAIR_ID": "pair3"})
        if ret != 0:
            raise RuntimeError("Baseline run 3 failed")
        b3_metrics = extract_metrics(REPORTS_DIR / "baseline_run_3.json")

        # 3B: Shadow
        ensure_gateway(shadow_enabled=True)
        reset_and_verify_cache_state()
        ret = run_k6("02_shadow_evaluation.js", "shadow_run_3.json", extra_env={"PAIR_ID": "pair3"})
        if ret != 0:
            raise RuntimeError("Shadow run 3 failed")
        s3_metrics = extract_metrics(REPORTS_DIR / "shadow_run_3.json")

        pairs_data.append({"pair_id": "pair_3", "baseline": b3_metrics, "shadow": s3_metrics})

        # Step 3: Concurrency Stress Test with Automated Hard Assertions
        print("\n--- STEP 3: Executing Concurrency & SingleFlight Stress Test ---")
        ensure_gateway(shadow_enabled=True)
        ret = run_k6(
            "03_concurrency_stress.js",
            "concurrency_stress_summary.json",
            extra_env={"CONCURRENT_VUS": "20", "BURST_ROUNDS": "3"},
        )
        if ret != 0:
            print("ERROR: Concurrency SingleFlight stress test failed hard assertions.", file=sys.stderr)
            sys.exit(1)

        # Step 4: Boundary & Resilience Testing
        print("\n--- STEP 4: Executing API Boundaries and Infrastructure Resilience ---")
        ret = run_k6("04_api_boundaries.js", "api_boundaries_summary.json")
        if ret != 0:
            print("ERROR: API boundaries test failed.", file=sys.stderr)
            sys.exit(1)

        ret = run_k6("04_infrastructure_resilience.js", "infrastructure_resilience_summary.json")
        if ret != 0:
            print("ERROR: Infrastructure resilience test failed.", file=sys.stderr)
            sys.exit(1)

    finally:
        if current_proc is not None:
            stop_gateway(current_proc)

    # Step 5: Post-Test Database Verification (SHA-256 over all 9 columns)
    print("\n--- STEP 5: Verifying Load-Testing Database Cryptographic SHA-256 Integrity ---")
    res = subprocess.run(
        [
            str(PYTHON_BIN),
            str(db_verify_script),
            "--verify",
            str(baseline_snapshot_file),
            "--expected-db",
            "intellicache_loadtest",
        ]
    )
    if res.returncode != 0:
        print("CRITICAL: Database integrity verification failed! vector_records was mutated.", file=sys.stderr)
        sys.exit(1)

    # Step 6: Compute Aggregated Results & Output Report Table
    print("\n--- STEP 6: Paired Benchmark Data Synthesis ---")
    paired_analysis = compute_paired_comparison(pairs_data)

    print("\n================================================================================")
    print("STAGE 3.4.1.4 CONTROLLED PAIRED BENCHMARK RESULTS")
    print("================================================================================")
    print(json.dumps(paired_analysis, indent=2))

    summary_output_file = REPORTS_DIR / "paired_benchmark_analysis.json"
    with open(summary_output_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "pairs": pairs_data,
                "analysis": paired_analysis,
            },
            f,
            indent=2,
        )
    print(f"\n[Done] Analysis saved to '{summary_output_file}'.")


if __name__ == "__main__":
    main()
