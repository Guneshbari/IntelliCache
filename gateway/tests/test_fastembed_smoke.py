"""
Real runtime smoke test for FastEmbedProvider.
Validates unmocked CPU inference, cold vs warm latencies, query and passage tasks,
vector properties (384-dim, finite, non-zero), and InMemoryVectorStore ranking.

Gated behind RUN_FASTEMBED_SMOKE=1 so normal CI test discovery remains fast and deterministic.
"""

from __future__ import annotations

import math
import os
import time
import unittest

from app.embeddings.contracts import EmbeddingTask
from app.embeddings.fastembed_adapter import FastEmbedProvider
from app.vector_store.contracts import VectorRecord
from app.vector_store.in_memory_store import InMemoryVectorStore


@unittest.skipUnless(
    os.environ.get("RUN_FASTEMBED_SMOKE") == "1",
    "Real FastEmbed smoke test is opt-in; run with RUN_FASTEMBED_SMOKE=1 to execute.",
)
class TestFastEmbedSmoke(unittest.IsolatedAsyncioTestCase):
    """Opt-in unmocked smoke tests for FastEmbed embedding provider."""

    async def asyncSetUp(self):
        self.model_name = "BAAI/bge-small-en-v1.5"
        self.dimensions = 384
        self.provider = FastEmbedProvider(
            model=self.model_name,
            dimensions=self.dimensions,
        )

    async def asyncTearDown(self):
        await self.provider.close()

    async def test_cold_vs_warm_inference_and_vector_properties(self):
        """
        Verifies unmocked inference on CPU, measures cold init vs warm latency,
        and validates vector dimensional and numeric invariants.
        """
        # 1. Measure cold initialization + first query embed
        t0 = time.perf_counter()
        query_result = await self.provider.embed(
            "How does Rust achieve memory safety without a garbage collector?",
            task=EmbeddingTask.QUERY,
        )
        cold_latency_ms = (time.perf_counter() - t0) * 1000.0

        # Validate query embedding result properties
        self.assertEqual(query_result.dimensions, self.dimensions)
        self.assertEqual(len(query_result.vector), self.dimensions)
        self.assertEqual(query_result.model, self.model_name)

        # Check all elements are finite and vector is non-zero
        for i, val in enumerate(query_result.vector):
            self.assertTrue(math.isfinite(val), f"Query vector element at {i} is not finite: {val}")
        norm = math.sqrt(sum(v * v for v in query_result.vector))
        self.assertGreater(norm, 0.1, "Query vector norm must be non-zero")

        # 2. Measure warm passage/document embed
        t1 = time.perf_counter()
        doc_result = await self.provider.embed(
            "Rust uses affine type systems, ownership and borrow checking to guarantee memory safety at compile time.",
            task=EmbeddingTask.DOCUMENT,
        )
        warm_latency_ms = (time.perf_counter() - t1) * 1000.0

        # Validate document embedding result properties
        self.assertEqual(doc_result.dimensions, self.dimensions)
        self.assertEqual(len(doc_result.vector), self.dimensions)

        for i, val in enumerate(doc_result.vector):
            self.assertTrue(math.isfinite(val), f"Doc vector element at {i} is not finite: {val}")
        doc_norm = math.sqrt(sum(v * v for v in doc_result.vector))
        self.assertGreater(doc_norm, 0.1, "Doc vector norm must be non-zero")

        print(
            f"\n[FastEmbed Smoke] Cold init + embed: {cold_latency_ms:.2f}ms | "
            f"Warm passage embed: {warm_latency_ms:.2f}ms | "
            f"Vector norm: {norm:.4f}"
        )

    async def test_in_memory_vector_store_real_ranking(self):
        """
        Verifies that real unmocked embeddings integrated with InMemoryVectorStore
        rank semantically related documents higher than unrelated documents.
        """
        store = InMemoryVectorStore(
            dimensions=self.dimensions,
            default_namespace="smoke_test",
            default_model=self.model_name,
        )

        doc_rust = (
            "Rust memory management relies on ownership, borrows, and lifetimes enforced by the borrow checker."
        )
        doc_python = (
            "Python manages memory automatically using reference counting and a cyclic garbage collector."
        )
        doc_cooking = (
            "Preheat the oven to 375 degrees and bake the chocolate chip cookies for 12 minutes."
        )

        emb_rust = await self.provider.embed(doc_rust, task=EmbeddingTask.DOCUMENT)
        emb_python = await self.provider.embed(doc_python, task=EmbeddingTask.DOCUMENT)
        emb_cooking = await self.provider.embed(doc_cooking, task=EmbeddingTask.DOCUMENT)

        await store.upsert(
            VectorRecord(
                cache_namespace="smoke_test",
                fingerprint="fp_rust",
                query_text=doc_rust,
                vector=emb_rust.vector,
                model=self.model_name,
                metadata={"topic": "rust_memory"},
            )
        )
        await store.upsert(
            VectorRecord(
                cache_namespace="smoke_test",
                fingerprint="fp_python",
                query_text=doc_python,
                vector=emb_python.vector,
                model=self.model_name,
                metadata={"topic": "python_memory"},
            )
        )
        await store.upsert(
            VectorRecord(
                cache_namespace="smoke_test",
                fingerprint="fp_cooking",
                query_text=doc_cooking,
                vector=emb_cooking.vector,
                model=self.model_name,
                metadata={"topic": "cooking"},
            )
        )

        # Query about Rust ownership
        query_text = "How does memory ownership work in Rust?"
        q_emb = await self.provider.embed(query_text, task=EmbeddingTask.QUERY)

        search_results = await store.search(
            query_vector=q_emb.vector,
            top_k=3,
            namespace="smoke_test",
            model=self.model_name,
        )

        self.assertEqual(len(search_results), 3)

        # Top result must be Rust
        top_hit = search_results[0]
        self.assertEqual(top_hit.fingerprint, "fp_rust")
        self.assertEqual(top_hit.metadata.get("topic"), "rust_memory")

        # Second hit should be Python memory, third should be cooking
        self.assertEqual(search_results[1].fingerprint, "fp_python")
        self.assertEqual(search_results[2].fingerprint, "fp_cooking")

        # Similarity of Rust must be substantially higher than cooking
        sim_rust = search_results[0].similarity
        sim_python = search_results[1].similarity
        sim_cooking = search_results[2].similarity

        self.assertGreater(sim_rust, sim_python)
        self.assertGreater(sim_python, sim_cooking)
        self.assertGreater(sim_rust - sim_cooking, 0.25)

        print(
            f"[FastEmbed Smoke Ranking] Query: '{query_text}'\n"
            f"  1. {top_hit.fingerprint} (sim: {sim_rust:.4f}, dist: {top_hit.distance:.4f})\n"
            f"  2. {search_results[1].fingerprint} (sim: {sim_python:.4f}, dist: {search_results[1].distance:.4f})\n"
            f"  3. {search_results[2].fingerprint} (sim: {sim_cooking:.4f}, dist: {search_results[2].distance:.4f})"
        )

        await store.close()


if __name__ == "__main__":
    unittest.main()
