#!/usr/bin/env python3
"""
run_paired_benchmark.py (Stage 3.4.1.2)
Orchestrates fixed-request-count deterministic paired benchmarks between
Baseline (exact-cache only) and Shadow Evaluation modes.
Enforces identical seeds, identical request sequences, multi-run median aggregation,
and database cryptographic integrity checks before and after testing.
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
from typing import Any, Dict, List

import numpy as np


REPO_ROOT = Path(__file__).resolve().parent.parent
GATEWAY_DIR = REPO_ROOT / "gateway"
LOAD_TEST_DIR = REPO_ROOT / "load_testing"
REPORTS_DIR = LOAD_TEST_DIR / "reports"
PYTHON_BIN = GATEWAY_DIR / ".venv" / "bin" / "python3"
UVICORN_BIN = GATEWAY_DIR / ".venv" / "bin" / "uvicorn"

BASE_URL = "http://127.0.0.1:8000"
DB_URL = "postgresql://postgres:postgres@127.0.0.1:5433/intellicache_loadtest"
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
    print(f"[Gateway Lifecycle] Stopping Gateway (PID: {proc.pid})...")
    proc.terminate()
    try:
        proc.wait(timeout=5.0)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    print("[Gateway Lifecycle] Gateway stopped.")
    time.sleep(1.0)


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


def compute_paired_comparison(
    baseline_runs: List[Dict[str, Any]],
    shadow_runs: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """Computes median, range, stddev, and paired delta overhead."""
    def _agg(runs: List[Dict[str, Any]], category: str, metric: str) -> Dict[str, float]:
        vals = [r[category][metric] for r in runs if r.get(category, {}).get(metric, 0.0) > 0]
        if not vals:
            return {"median": 0.0, "min": 0.0, "max": 0.0, "stddev": 0.0}
        return {
            "median": round(float(np.median(vals)), 2),
            "min": round(float(np.min(vals)), 2),
            "max": round(float(np.max(vals)), 2),
            "stddev": round(float(np.std(vals)), 2),
        }

    results = {}
    for cat in ["overall", "exact_hit", "exact_miss"]:
        b_med = _agg(baseline_runs, cat, "med")
        s_med = _agg(shadow_runs, cat, "med")
        b_p95 = _agg(baseline_runs, cat, "p95")
        s_p95 = _agg(shadow_runs, cat, "p95")
        b_avg = _agg(baseline_runs, cat, "avg")
        s_avg = _agg(shadow_runs, cat, "avg")

        # Delta calculation based on median of medians
        delta_med_ms = round(s_med["median"] - b_med["median"], 2)
        pct_med = round((delta_med_ms / b_med["median"]) * 100.0, 2) if b_med["median"] > 0 else 0.0

        delta_p95_ms = round(s_p95["median"] - b_p95["median"], 2)
        pct_p95 = round((delta_p95_ms / b_p95["median"]) * 100.0, 2) if b_p95["median"] > 0 else 0.0

        results[cat] = {
            "baseline": {"median_med": b_med, "p95": b_p95, "avg": b_avg},
            "shadow": {"median_med": s_med, "p95": s_p95, "avg": s_avg},
            "delta_med_ms": delta_med_ms,
            "pct_med": pct_med,
            "delta_p95_ms": delta_p95_ms,
            "pct_p95": pct_p95,
        }

    # Rates
    b_rates = [r["req_rate"] for r in baseline_runs]
    s_rates = [r["req_rate"] for r in shadow_runs]
    results["throughput"] = {
        "baseline_rate": round(float(np.median(b_rates)), 2),
        "shadow_rate": round(float(np.median(s_rates)), 2),
        "delta_rate": round(float(np.median(s_rates) - np.median(b_rates)), 2),
    }

    return results


def main():
    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    baseline_snapshot_file = REPORTS_DIR / "db_baseline_snapshot.json"

    print("================================================================================")
    print("STAGE 3.4.1.2: FIXED-REQUEST-COUNT REPEATABLE PAIRED BENCHMARK ORCHESTRATOR")
    print(f"Total Requests per Run: {ITERATIONS} (10 VUs x 100 iters)")
    print(f"Workload Seed:          {SEED}")
    print(f"PostgreSQL DB URL:      {DB_URL}")
    print("================================================================================\n")

    # Step 0: Capture Database Baseline Snapshot
    print("\n--- STEP 0: Capturing Initial Database Integrity Snapshot ---")
    db_verify_script = LOAD_TEST_DIR / "helpers" / "verify_db_integrity.py"
    res = subprocess.run([str(PYTHON_BIN), str(db_verify_script), "--save", str(baseline_snapshot_file)])
    if res.returncode != 0:
        print("ERROR: Failed to capture database baseline snapshot.", file=sys.stderr)
        sys.exit(1)

    # Step 1: Smoke Test
    print("\n--- STEP 1: Running Smoke Test Verification ---")
    proc = start_gateway(shadow_enabled=False)
    try:
        ret = run_k6("00_smoke_test.js", "smoke_test_summary.json")
        if ret != 0:
            print("ERROR: Smoke test failed.", file=sys.stderr)
            sys.exit(1)
    finally:
        stop_gateway(proc)

    # Step 2: 3 Paired Baseline Runs
    print("\n--- STEP 2: Executing 3 Paired Baseline Runs (Exact Cache Only) ---")
    proc = start_gateway(shadow_enabled=False)
    baseline_summaries = []
    try:
        for run_idx in range(1, 4):
            print(f"\n>> Executing Baseline Run #{run_idx} of 3...")
            out_json = f"baseline_run_{run_idx}.json"
            ret = run_k6("01_baseline_exact_cache.js", out_json)
            if ret != 0:
                print(f"ERROR: Baseline run {run_idx} failed.", file=sys.stderr)
                sys.exit(1)
            baseline_summaries.append(extract_metrics(REPORTS_DIR / out_json))
    finally:
        stop_gateway(proc)

    # Step 3: 3 Paired Shadow Runs
    print("\n--- STEP 3: Executing 3 Paired Shadow Runs (Shadow Active 100%) ---")
    proc = start_gateway(shadow_enabled=True)
    shadow_summaries = []
    try:
        for run_idx in range(1, 4):
            print(f"\n>> Executing Shadow Run #{run_idx} of 3...")
            out_json = f"shadow_run_{run_idx}.json"
            ret = run_k6("02_shadow_evaluation.js", out_json)
            if ret != 0:
                print(f"ERROR: Shadow run {run_idx} failed.", file=sys.stderr)
                sys.exit(1)
            shadow_summaries.append(extract_metrics(REPORTS_DIR / out_json))

        # Step 4: Concurrency Stress Test with Automated Hard Assertions
        print("\n--- STEP 4: Executing Concurrency & SingleFlight Stress Test ---")
        ret = run_k6(
            "03_concurrency_stress.js",
            "concurrency_stress_summary.json",
            extra_env={"CONCURRENT_VUS": "20", "BURST_ROUNDS": "3"},
        )
        if ret != 0:
            print("ERROR: Concurrency SingleFlight stress test failed hard assertions.", file=sys.stderr)
            sys.exit(1)

        # Step 5: Boundary & Resilience Testing
        print("\n--- STEP 5: Executing API Boundaries and Infrastructure Resilience ---")
        ret = run_k6("04_api_boundaries.js", "api_boundaries_summary.json")
        if ret != 0:
            print("ERROR: API boundaries test failed.", file=sys.stderr)
            sys.exit(1)

        ret = run_k6("04_infrastructure_resilience.js", "infrastructure_resilience_summary.json")
        if ret != 0:
            print("ERROR: Infrastructure resilience test failed.", file=sys.stderr)
            sys.exit(1)

    finally:
        stop_gateway(proc)

    # Step 6: Post-Test Database Verification
    print("\n--- STEP 6: Verifying Database Cryptographic Integrity ---")
    res = subprocess.run([str(PYTHON_BIN), str(db_verify_script), "--verify", str(baseline_snapshot_file)])
    if res.returncode != 0:
        print("CRITICAL: Database integrity verification failed! vector_records was mutated.", file=sys.stderr)
        sys.exit(1)

    # Step 7: Compute Aggregated Results & Output Report Table
    print("\n--- STEP 7: Benchmark Data Synthesis and Paired Analysis ---")
    comparison = compute_paired_comparison(baseline_summaries, shadow_summaries)

    print("\n================================================================================")
    print("STAGE 3.4.1.2 FINAL PAIRED BENCHMARK RESULTS (MEDIAN OVER 3 RUNS)")
    print("================================================================================")
    print(json.dumps(comparison, indent=2))

    summary_output_file = REPORTS_DIR / "paired_benchmark_analysis.json"
    with open(summary_output_file, "w", encoding="utf-8") as f:
        json.dump(
            {
                "baseline_runs": baseline_summaries,
                "shadow_runs": shadow_summaries,
                "comparison": comparison,
            },
            f,
            indent=2,
        )
    print(f"\n[Done] Analysis saved to '{summary_output_file}'.")


if __name__ == "__main__":
    main()
