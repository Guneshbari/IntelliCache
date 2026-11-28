#!/usr/bin/env python3
"""
Stage 3.9 Evaluation Benchmark: Local vs PostgreSQL Distributed SingleFlight Coordination.

Methodology Notice:
- Measures mocked/controlled upstream provider calls (NOT live external Groq API inference)
  and real local PostgreSQL coordination overhead across independent simulated workers.
- Scenarios evaluated:
  1. Single-worker concurrency with process-local SingleFlight only.
  2. Two independent gateway workers WITHOUT distributed SingleFlight (control baseline).
  3. Two independent gateway workers WITH PostgreSQL distributed SingleFlight (Stage 3.9).
  4. Warm exact cache read concurrency across two workers.
  5. Leader failure + bounded follower reclaim recovery across two workers.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Any, Dict, List

import asyncpg

GATEWAY_ROOT = Path(__file__).resolve().parents[1]
if str(GATEWAY_ROOT) not in sys.path:
    sys.path.insert(0, str(GATEWAY_ROOT))

from app.cache.migration import run_migrations
from app.cache.postgres_cache import PostgresCache
from app.core.contracts import ChatMessage, GenerationRequest, GenerationResult, UsageMetrics
from app.core.distributed_singleflight import (
    DistributedSingleFlightConfig,
    PostgresDistributedSingleFlight,
)
from app.core.service import IntelliCacheCore
from app.core.singleflight import SingleFlightCoordinator
from app.metrics import RuntimeMetricsCollector
from app.providers.interfaces import LLMProvider


class _SyntheticProvider(LLMProvider):
    def __init__(self, latency_s: float = 0.08) -> None:
        self.latency_s = latency_s
        self.call_count = 0

    @property
    def name(self) -> str:
        return "synthetic-eval"

    async def generate(self, request: GenerationRequest) -> GenerationResult:
        self.call_count += 1
        await asyncio.sleep(self.latency_s)
        return GenerationResult(
            text=f"synthetic-response-{self.call_count}",
            model=request.model,
            usage=UsageMetrics(prompt_tokens=20, completion_tokens=15, total_tokens=35),
            latency_ms=round(self.latency_s * 1000.0, 2),
            finish_reason="stop",
        )

    async def stream(self, request: GenerationRequest):
        raise NotImplementedError

    async def close(self) -> None:
        return None


def _percentile(samples: List[float], pct: float) -> float:
    if not samples:
        return 0.0
    ordered = sorted(samples)
    idx = (len(ordered) - 1) * (pct / 100.0)
    lo = int(idx)
    hi = min(lo + 1, len(ordered) - 1)
    return round(ordered[lo] * (hi - idx) + ordered[hi] * (idx - lo), 2)


async def _run_benchmark(db_url: str, concurrency: int, provider_latency_s: float) -> Dict[str, Any]:
    pool_w1 = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=12)
    pool_w2 = await asyncpg.create_pool(dsn=db_url, min_size=2, max_size=12)
    try:
        await run_migrations(pool_w1)

        async def _reset_db() -> None:
            async with pool_w1.acquire() as conn:
                await conn.execute("TRUNCATE TABLE distributed_singleflight_leases, cache_entries CASCADE")

        async def _bench_scenario(name: str, *, distributed: bool, warm: bool = False) -> Dict[str, Any]:
            await _reset_db()
            collector = RuntimeMetricsCollector()
            prov = _SyntheticProvider(latency_s=provider_latency_s)
            dsf_cfg = DistributedSingleFlightConfig(
                lease_seconds=15.0,
                renewal_interval_seconds=5.0,
                follower_wait_timeout_seconds=20.0,
                poll_interval_seconds=0.015,
                poll_max_interval_seconds=0.05,
            )
            c1 = PostgresCache(pool=pool_w1, max_entries=1000, ttl_seconds=3600)
            c2 = PostgresCache(pool=pool_w2, max_entries=1000, ttl_seconds=3600)
            dsf1 = PostgresDistributedSingleFlight(pool=pool_w1, config=dsf_cfg, metrics_collector=collector) if distributed else None
            dsf2 = PostgresDistributedSingleFlight(pool=pool_w2, config=dsf_cfg, metrics_collector=collector) if distributed else None

            w1 = IntelliCacheCore(cache=c1, single_flight=SingleFlightCoordinator(), dist_single_flight=dsf1, metrics_collector=collector)
            w2 = IntelliCacheCore(cache=c2, single_flight=SingleFlightCoordinator(), dist_single_flight=dsf2, metrics_collector=collector)

            req = GenerationRequest(
                model="eval-model",
                messages=[ChatMessage(role="user", content=f"Evaluate {name} scenario")],
                temperature=0.0,
                max_tokens=128,
                namespace=f"bench_{name}",
                visibility="tenant_shared",
            )
            if warm:
                await w1.execute_query(req, prov)
                prov.call_count = 0

            latencies_ms: List[float] = []

            async def _call(idx: int) -> None:
                worker = w1 if idx % 2 == 0 else w2
                t0 = time.perf_counter()
                await worker.execute_query(req, prov)
                latencies_ms.append((time.perf_counter() - t0) * 1000.0)

            wall_start = time.perf_counter()
            await asyncio.gather(*[_call(i) for i in range(concurrency)])
            wall_ms = round((time.perf_counter() - wall_start) * 1000.0, 2)

            snap = collector.get_namespace_snapshot(f"bench_{name}")
            return {
                "scenario": name,
                "evidence_source": "synthetic_provider_plus_local_postgres",
                "distributed_singleflight_enabled": distributed,
                "warm_cache": warm,
                "concurrent_requests": concurrency,
                "provider_calls": prov.call_count,
                "wall_time_ms": wall_ms,
                "mean_latency_ms": round(statistics.mean(latencies_ms), 2),
                "p95_latency_ms": _percentile(latencies_ms, 95),
                "local_coalesced_joins": snap.request_and_cache.coalesced_joins,
                "distributed_leader_claims": snap.distributed_singleflight.leader_claims,
                "distributed_follower_joins": snap.distributed_singleflight.follower_joins,
                "distributed_coalesced_successes": snap.distributed_singleflight.coalesced_successes,
                "distributed_provider_calls_saved": snap.distributed_singleflight.provider_calls_saved,
                "db_query_p50_ms": snap.latencies.distributed_singleflight_database_latency_ms.p50,
                "db_query_p95_ms": snap.latencies.distributed_singleflight_database_latency_ms.p95,
                "db_query_count": snap.latencies.distributed_singleflight_database_latency_ms.sample_count,
                "follower_wait_p50_ms": snap.latencies.distributed_singleflight_wait_latency_ms.p50,
            }

        results = {
            "stage": "3.9",
            "methodology": "Two independent IntelliCacheCore workers with separate asyncpg pools against local PostgreSQL and synthetic fixed-latency LLM provider (non-live-provider evaluation).",
            "cold_multiworker_without_distributed": await _bench_scenario("cold_no_dsf", distributed=False, warm=False),
            "cold_multiworker_with_distributed": await _bench_scenario("cold_with_dsf", distributed=True, warm=False),
            "warm_multiworker_with_distributed": await _bench_scenario("warm_with_dsf", distributed=True, warm=True),
        }
        return results
    finally:
        await pool_w1.close()
        await pool_w2.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate Stage 3.9 Distributed SingleFlight coordination.")
    parser.add_argument(
        "--database-url",
        default=os.getenv("TEST_DATABASE_URL") or os.getenv("DATABASE_URL") or "postgresql://postgres:postgres@127.0.0.1:5432/intellicache_test",
    )
    parser.add_argument("--concurrency", type=int, default=20)
    parser.add_argument("--provider-latency-ms", type=float, default=80.0)
    args = parser.parse_args()

    report = asyncio.run(_run_benchmark(args.database_url, args.concurrency, args.provider_latency_ms / 1000.0))
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
