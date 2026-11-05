"""
Unit tests for RuntimeMetricsCollector, namespace isolation, bounded latency storage,
percentiles, token savings, and thread safety.
"""

from concurrent.futures import ThreadPoolExecutor
import time
import unittest

from app.metrics import (
    RuntimeMetricsCollector,
    compute_latency_summary,
    get_metrics_collector,
    set_metrics_collector,
)


class TestRuntimeMetricsCollector(unittest.TestCase):
    """Test suite for RuntimeMetricsCollector."""

    def setUp(self):
        self.collector = RuntimeMetricsCollector(max_samples=50)

    def tearDown(self):
        self.collector.reset()

    def test_namespace_isolation_for_all_counters(self):
        """Counters and samples for namespace A do not affect namespace B."""
        # Namespace A operations
        self.collector.record_request("ns_a", stream=False, bypass=False)
        self.collector.record_exact_lookup("ns_a", latency_ms=1.5)
        self.collector.record_exact_hit("ns_a", prompt_tokens=10, completion_tokens=20, total_tokens=30, latency_saved_ms=50.0)
        self.collector.record_provider_attempt("ns_a")
        self.collector.record_provider_success("ns_a", prompt_tokens=10, completion_tokens=20, total_tokens=30, latency_ms=120.0)

        # Namespace B operations
        self.collector.record_request("ns_b", stream=True, bypass=True)
        self.collector.record_exact_miss("ns_b")
        self.collector.record_provider_attempt("ns_b")
        self.collector.record_provider_failure("ns_b")

        snap_a = self.collector.get_namespace_snapshot("ns_a")
        snap_b = self.collector.get_namespace_snapshot("ns_b")

        # Verify A
        self.assertEqual(snap_a.namespace, "ns_a")
        self.assertEqual(snap_a.request_and_cache.total_authenticated_requests, 1)
        self.assertEqual(snap_a.request_and_cache.exact_cache_lookups, 1)
        self.assertEqual(snap_a.request_and_cache.exact_cache_hits, 1)
        self.assertEqual(snap_a.request_and_cache.exact_cache_misses, 0)
        self.assertEqual(snap_a.provider_and_savings.provider_calls_succeeded, 1)
        self.assertEqual(snap_a.provider_and_savings.provider_calls_failed, 0)
        self.assertEqual(snap_a.provider_and_savings.total_tokens_saved, 30)
        self.assertEqual(snap_a.provider_and_savings.provider_calls_avoided, 1)
        self.assertEqual(snap_a.latencies.exact_lookup_latency_ms.sample_count, 1)
        self.assertEqual(snap_a.latencies.exact_lookup_latency_ms.min, 1.5)

        # Verify B
        self.assertEqual(snap_b.namespace, "ns_b")
        self.assertEqual(snap_b.request_and_cache.total_authenticated_requests, 1)
        self.assertEqual(snap_b.request_and_cache.streaming_requests, 1)
        self.assertEqual(snap_b.request_and_cache.cache_bypass_requests, 1)
        self.assertEqual(snap_b.request_and_cache.exact_cache_hits, 0)
        self.assertEqual(snap_b.request_and_cache.exact_cache_misses, 1)
        self.assertEqual(snap_b.provider_and_savings.provider_calls_succeeded, 0)
        self.assertEqual(snap_b.provider_and_savings.provider_calls_failed, 1)
        self.assertEqual(snap_b.provider_and_savings.total_tokens_saved, 0)
        self.assertEqual(snap_b.provider_and_savings.provider_calls_avoided, 0)

    def test_bounded_latency_sample_storage(self):
        """Latency deque adheres strictly to max_samples limit and does not grow unbounded."""
        collector = RuntimeMetricsCollector(max_samples=10)
        for i in range(25):
            collector.record_exact_lookup("ns_bounded", latency_ms=float(i))

        snap = collector.get_namespace_snapshot("ns_bounded")
        summary = snap.latencies.exact_lookup_latency_ms
        self.assertEqual(summary.sample_count, 10)
        # Oldest samples 0..14 evicted, newest 15..24 remain
        self.assertEqual(summary.min, 15.0)
        self.assertAlmostEqual(summary.p99, 23.91, places=2)

    def test_percentile_calculation_and_small_sample_warning(self):
        """Percentile helper correctly calculates statistics and flags small samples."""
        # 1. Empty samples
        empty_summary = compute_latency_summary([])
        self.assertEqual(empty_summary.sample_count, 0)
        self.assertIsNone(empty_summary.p50)
        self.assertFalse(empty_summary.small_sample_warning)

        # 2. Small sample (<20 samples)
        small_samples = [10.0, 20.0, 30.0]
        small_summary = compute_latency_summary(small_samples)
        self.assertEqual(small_summary.sample_count, 3)
        self.assertEqual(small_summary.min, 10.0)
        self.assertEqual(small_summary.mean, 20.0)
        self.assertEqual(small_summary.p50, 20.0)
        self.assertTrue(small_summary.small_sample_warning)

        # 3. Adequate sample (>=20 samples)
        large_samples = [float(i) for i in range(1, 101)]  # 1 to 100
        large_summary = compute_latency_summary(large_samples)
        self.assertEqual(large_summary.sample_count, 100)
        self.assertEqual(large_summary.min, 1.0)
        self.assertEqual(large_summary.mean, 50.5)
        self.assertEqual(large_summary.p50, 50.5)
        self.assertAlmostEqual(large_summary.p95, 95.05, delta=0.5)
        self.assertAlmostEqual(large_summary.p99, 99.01, delta=0.5)
        self.assertFalse(large_summary.small_sample_warning)

    def test_provider_calls_avoided_accounting(self):
        """Provider calls avoided accurately sums exact hits, semantic hits, and coalesced successes."""
        ns = "ns_savings"
        self.collector.record_exact_hit(ns, total_tokens=100)
        self.collector.record_exact_hit(ns, total_tokens=150)
        self.collector.record_semantic_hit(ns, total_tokens=200)
        self.collector.record_coalesced_success(ns, total_tokens=300)

        snap = self.collector.get_namespace_snapshot(ns)
        prov = snap.provider_and_savings

        self.assertEqual(snap.request_and_cache.exact_cache_hits, 2)
        self.assertEqual(snap.request_and_cache.semantic_hits, 1)
        self.assertEqual(snap.request_and_cache.coalesced_successes, 1)
        # Avoided = 2 + 1 + 1 = 4
        self.assertEqual(prov.provider_calls_avoided, 4)
        # Tokens = 100 + 150 + 200 + 300 = 750
        self.assertEqual(prov.total_tokens_saved, 750)

    def test_admin_snapshot_aggregation(self):
        """Admin snapshot aggregates totals across all namespaces and includes per-namespace details."""
        self.collector.record_request("team_1")
        self.collector.record_exact_hit("team_1", total_tokens=50)

        self.collector.record_request("team_2")
        self.collector.record_semantic_hit("team_2", total_tokens=80)

        admin_snap = self.collector.get_admin_snapshot()
        self.assertEqual(admin_snap.scope, "admin")
        self.assertEqual(admin_snap.aggregate.request_and_cache.total_authenticated_requests, 2)
        self.assertEqual(admin_snap.aggregate.request_and_cache.exact_cache_hits, 1)
        self.assertEqual(admin_snap.aggregate.request_and_cache.semantic_hits, 1)
        self.assertEqual(admin_snap.aggregate.provider_and_savings.provider_calls_avoided, 2)
        self.assertEqual(admin_snap.aggregate.provider_and_savings.total_tokens_saved, 130)

        self.assertIn("team_1", admin_snap.namespaces)
        self.assertIn("team_2", admin_snap.namespaces)
        self.assertEqual(admin_snap.namespaces["team_1"].provider_and_savings.total_tokens_saved, 50)
        self.assertEqual(admin_snap.namespaces["team_2"].provider_and_savings.total_tokens_saved, 80)

    def test_concurrent_updates_thread_safety(self):
        """Concurrent record updates across multiple threads do not drop counts or crash."""
        ns = "ns_concurrency"
        iterations = 500

        def worker():
            for _ in range(iterations):
                self.collector.record_request(ns)
                self.collector.record_exact_lookup(ns, latency_ms=1.0)
                self.collector.record_exact_hit(ns, prompt_tokens=1, completion_tokens=2, total_tokens=3)

        with ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(worker) for _ in range(5)]
            for f in futures:
                f.result()

        snap = self.collector.get_namespace_snapshot(ns)
        expected_total = iterations * 5
        self.assertEqual(snap.request_and_cache.total_authenticated_requests, expected_total)
        self.assertEqual(snap.request_and_cache.exact_cache_lookups, expected_total)
        self.assertEqual(snap.request_and_cache.exact_cache_hits, expected_total)
        self.assertEqual(snap.provider_and_savings.total_tokens_saved, expected_total * 3)

    def test_fail_open_safety_on_invalid_input(self):
        """Collector handles unexpected None or edge-case arguments without raising exceptions."""
        try:
            self.collector.record_request(None)  # None namespace
            self.collector.record_exact_lookup("", latency_ms=None)
            self.collector.record_exact_hit("ns_safe", prompt_tokens=None, completion_tokens=-5, total_tokens=None, latency_saved_ms=-10.0)
            self.collector.record_provider_success("ns_safe", latency_ms=None)
        except Exception as e:
            self.fail(f"Collector raised unexpected exception on malformed inputs: {e}")


if __name__ == "__main__":
    unittest.main()
