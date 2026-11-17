#!/usr/bin/env python3
"""
IntelliCache Stage 3.2.3 — HNSW vs Exact Retrieval Validation CLI.

Executes real FastEmbed 384-dimensional pgvector HNSW benchmark, measures recall against
exact nearest-neighbour baseline, verifies filtering and iterative scans, and exports
reports to gateway/evaluation/reports/.
"""

import argparse
import asyncio
import logging
import os
import sys
from pathlib import Path

# Add gateway root to sys.path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from evaluation.hnsw_benchmarks import run_stage3_2_3_benchmark

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("benchmark_hnsw")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Stage 3.2.3 HNSW vs Exact Retrieval Benchmark CLI")
    parser.add_argument(
        "--test-database-url",
        type=str,
        default=os.environ.get("TEST_FASTEMBED_DATABASE_URL") or os.environ.get("TEST_DATABASE_URL"),
        help="PostgreSQL connection URL with pgvector support",
    )
    parser.add_argument(
        "--output-dir",
        type=str,
        default=str(Path(__file__).resolve().parent.parent / "evaluation" / "reports"),
        help="Directory to write output JSON and Markdown reports",
    )
    parser.add_argument(
        "--total-docs",
        type=int,
        default=2000,
        help="Number of documents to generate and embed in the technical corpus",
    )
    return parser.parse_args()


async def async_main() -> None:
    args = parse_args()
    if not args.test_database_url:
        logger.error("No PostgreSQL database URL provided. Set TEST_DATABASE_URL or pass --test-database-url.")
        sys.exit(1)

    output_dir = Path(args.output_dir)
    logger.info("Starting Stage 3.2.3 HNSW Benchmark on %s (docs=%d)...", args.test_database_url, args.total_docs)
    payload = await run_stage3_2_3_benchmark(
        database_url=args.test_database_url,
        output_dir=output_dir,
        total_docs=args.total_docs,
    )
    agg = payload["real_fastembed_384d_evaluation"]["aggregate_metrics"]
    logger.info(
        "Benchmark complete! Recall@1: %.2f%%, Recall@5: %.2f%%, Recall@10: %.2f%%",
        agg["avg_recall_at_1"] * 100,
        agg["avg_recall_at_5"] * 100,
        agg["avg_recall_at_10"] * 100,
    )
    logger.info("Reports written to %s", output_dir)


def main() -> None:
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
