"""
Unit tests for strict k6 benchmark metric validation and request accounting (Stage 3.4.1.4.5 / Final Closure).
Verifies that extract_metrics() strictly validates all required metrics and fails fast on any missing,
malformed, or inconsistent summary data instead of substituting fabricated defaults.
"""

import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure load_testing module is on path
REPO_ROOT = Path(__file__).resolve().parent.parent.parent
LOAD_TEST_DIR = REPO_ROOT / "load_testing"
if str(LOAD_TEST_DIR) not in sys.path:
    sys.path.insert(0, str(LOAD_TEST_DIR))

from run_paired_benchmark import (  # noqa: E402
    K6_EXPECTED_RUNTIME_VERSION,
    K6_PINNED_IMAGE,
    K6RuntimeVerificationError,
    MetricValidationError,
    extract_metrics,
    run_k6,
    verify_and_record_k6_runtime_version,
)


def _make_valid_summary_data() -> dict:
    """Creates a valid, fully reconciled k6 benchmark summary dictionary."""
    return {
        "metrics": {
            "workload_requests": {"count": 1000, "rate": 90.9},
            "warmup_requests": {"count": 6, "rate": 12.0},
            "admin_requests": {"count": 2, "rate": 4.0},
            "http_reqs": {"count": 1008, "rate": 75.3},
            "exact_cache_hits": {"count": 711, "rate": 64.6},
            "exact_cache_misses": {"count": 218, "rate": 19.8},
            "bypass_requests": {"count": 70, "rate": 6.3},
            "ineligible_requests": {"count": 1, "rate": 0.1},
            "invalid_requests": {"count": 0, "rate": 0.0},
            "semantic_hits_served": {"count": 0, "rate": 0.0},
            "workload_req_duration_ms": {
                "count": 1000,
                "avg": 30.5,
                "min": 1.5,
                "med": 4.2,
                "max": 180.2,
                "p(90)": 120.1,
                "p(95)": 126.5,
                "p(99)": 150.0,
            },
            "warmup_req_duration_ms": {
                "count": 6,
                "avg": 5.0,
                "min": 2.0,
                "med": 4.0,
                "max": 10.0,
                "p(90)": 8.0,
                "p(95)": 9.5,
                "p(99)": 9.9,
            },
            "admin_req_duration_ms": {
                "count": 2,
                "avg": 3.0,
                "min": 2.5,
                "med": 3.0,
                "max": 3.5,
                "p(90)": 3.4,
                "p(95)": 3.45,
                "p(99)": 3.48,
            },
            "workload_start_time": {
                "count": 1000,
                "avg": 1700000005000.0,
                "min": 1700000000000.0,
                "med": 1700000005000.0,
                "max": 1700000010000.0,
                "p(90)": 1700000009000.0,
                "p(95)": 1700000009500.0,
                "p(99)": 1700000009800.0,
            },
            "workload_end_time": {
                "count": 1000,
                "avg": 1700000006000.0,
                "min": 1700000001000.0,
                "med": 1700000006000.0,
                "max": 1700000011000.0,
                "p(90)": 1700000010000.0,
                "p(95)": 1700000010500.0,
                "p(99)": 1700000010800.0,
            },
            "exact_hit_latency_ms": {
                "count": 711,
                "avg": 3.5,
                "min": 1.2,
                "med": 3.2,
                "max": 12.0,
                "p(90)": 5.0,
                "p(95)": 8.1,
                "p(99)": 10.5,
            },
            "cache_miss_latency_ms": {
                "count": 218,
                "avg": 125.0,
                "min": 110.0,
                "med": 124.5,
                "max": 180.2,
                "p(90)": 135.0,
                "p(95)": 140.0,
                "p(99)": 160.0,
            },
            "http_req_duration": {
                "count": 1008,
                "avg": 31.0,
                "min": 1.2,
                "med": 4.3,
                "max": 180.2,
                "p(90)": 121.0,
                "p(95)": 127.0,
                "p(99)": 150.0,
            },
        }
    }


class BenchmarkMetricValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.summary_file = Path(self.temp_dir.name) / "test_summary.json"

    def tearDown(self):
        self.temp_dir.cleanup()

    def _write_summary(self, data: dict):
        with open(self.summary_file, "w", encoding="utf-8") as f:
            json.dump(data, f)

    def test_valid_summary_passes_without_defaults(self):
        """A valid, complete, and reconciled summary extracts cleanly."""
        data = _make_valid_summary_data()
        self._write_summary(data)
        metrics = extract_metrics(self.summary_file, expected_workload_requests=1000)

        self.assertEqual(metrics["workload_count"], 1000)
        self.assertEqual(metrics["exact_hit_count"], 711)
        self.assertEqual(metrics["exact_miss_count"], 218)
        self.assertEqual(metrics["bypass_count"], 70)
        self.assertEqual(metrics["ineligible_count"], 1)
        self.assertEqual(metrics["invalid_count"], 0)
        self.assertEqual(metrics["semantic_hits_served"], 0)
        self.assertEqual(metrics["warmup_count"], 6)
        self.assertEqual(metrics["admin_count"], 2)
        self.assertEqual(metrics["total_http_count"], 1008)
        self.assertEqual(metrics["workload_duration_sec"], 11.0)
        self.assertEqual(metrics["workload_rate"], round(1000 / 11.0, 2))
        self.assertEqual(metrics["exact_hit"]["count"], 711)
        self.assertEqual(metrics["exact_hit"]["p95"], 8.1)
        self.assertEqual(metrics["exact_miss"]["count"], 218)

    def test_missing_summary_file(self):
        """Missing summary file raises MetricValidationError."""
        missing = Path(self.temp_dir.name) / "nonexistent.json"
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(missing)
        self.assertIn("does not exist", str(ctx.exception))

    def test_malformed_json_summary(self):
        """Non-JSON or malformed JSON raises MetricValidationError."""
        with open(self.summary_file, "w") as f:
            f.write("{ invalid json")
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("Malformed JSON", str(ctx.exception))

    def test_missing_metrics_dictionary(self):
        """Missing or empty 'metrics' key raises MetricValidationError."""
        self._write_summary({"root_group": {}})
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("missing required non-empty 'metrics'", str(ctx.exception))

    def test_missing_workload_requests(self):
        """Missing workload_requests counter causes immediate failure (no fallback to 1000)."""
        data = _make_valid_summary_data()
        del data["metrics"]["workload_requests"]
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("Required counter metric 'workload_requests' is missing", str(ctx.exception))

    def test_missing_workload_start_and_end_time(self):
        """Missing workload_start_time or workload_end_time causes failure (no fallback to global rate)."""
        data = _make_valid_summary_data()
        del data["metrics"]["workload_start_time"]
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("workload_start_time", str(ctx.exception))

        data = _make_valid_summary_data()
        del data["metrics"]["workload_end_time"]
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("workload_end_time", str(ctx.exception))

    def test_missing_workload_req_duration_ms(self):
        """Missing workload_req_duration_ms causes failure (no fallback to http_req_duration)."""
        data = _make_valid_summary_data()
        del data["metrics"]["workload_req_duration_ms"]
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("workload_req_duration_ms", str(ctx.exception))

    def test_exact_hit_counter_and_trend_count_mismatch(self):
        """Mismatch between exact_cache_hits and exact_hit_latency_ms count causes failure (no count overwrite)."""
        data = _make_valid_summary_data()
        data["metrics"]["exact_hit_latency_ms"]["count"] = 700  # Counter is 711
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("exact_hit_latency_ms observation count (700) does not match exact_cache_hits (711)", str(ctx.exception))

    def test_exact_miss_counter_and_trend_count_mismatch(self):
        """Mismatch between exact_cache_misses and cache_miss_latency_ms count causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["cache_miss_latency_ms"]["count"] = 215  # Counter is 218
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("cache_miss_latency_ms observation count (215) does not match exact_cache_misses (218)", str(ctx.exception))

    def test_incorrect_total_http_request_count(self):
        """Total HTTP request count not matching expected total causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["http_reqs"]["count"] = 1007  # Expected 1008
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("Total HTTP request count mismatch: expected 1008, got 1007", str(ctx.exception))

    def test_workload_categories_do_not_sum_to_workload_requests(self):
        """Sum of categories not matching workload_requests causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["bypass_requests"]["count"] = 69  # Sum is 999 instead of 1000
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("Workload category sum (999) does not equal workload_requests (1000)", str(ctx.exception))

    def test_nonzero_invalid_requests_fails(self):
        """Non-zero invalid_requests causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["invalid_requests"]["count"] = 1
        data["metrics"]["bypass_requests"]["count"] = 69  # Keep sum at 1000
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("invalid_requests must be 0", str(ctx.exception))

    def test_nonzero_semantic_hits_served_critical_failure(self):
        """Non-zero semantic_hits_served causes immediate critical failure."""
        data = _make_valid_summary_data()
        data["metrics"]["semantic_hits_served"]["count"] = 1
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("CRITICAL INVARIANT VIOLATION: semantic_hits_served must be 0", str(ctx.exception))

    def test_invalid_workload_timestamps_boundary(self):
        """End timestamp <= start timestamp raises MetricValidationError."""
        data = _make_valid_summary_data()
        # Set workload_end_time to be earlier than workload_start_time (valid internally but inverted boundary)
        data["metrics"]["workload_start_time"]["min"] = 1700000005000.0
        data["metrics"]["workload_end_time"]["min"] = 1700000001000.0
        data["metrics"]["workload_end_time"]["med"] = 1700000002000.0
        data["metrics"]["workload_end_time"]["avg"] = 1700000002000.0
        data["metrics"]["workload_end_time"]["p(90)"] = 1700000003000.0
        data["metrics"]["workload_end_time"]["p(95)"] = 1700000004000.0
        data["metrics"]["workload_end_time"]["p(99)"] = 1700000004000.0
        data["metrics"]["workload_end_time"]["max"] = 1700000004000.0
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("must be strictly greater than start timestamp", str(ctx.exception))

    def test_malformed_metric_values_rejected(self):
        """Boolean counts, negative counts, NaN, and non-numeric values are rejected."""
        # Boolean count
        data = _make_valid_summary_data()
        data["metrics"]["workload_requests"]["count"] = True
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("cannot be a boolean", str(ctx.exception))

        # Negative count
        data = _make_valid_summary_data()
        data["metrics"]["bypass_requests"]["count"] = -5
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("cannot be negative", str(ctx.exception))

        # Non-numeric
        data = _make_valid_summary_data()
        data["metrics"]["warmup_requests"]["count"] = "six"
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("must be numeric", str(ctx.exception))

    def test_warmup_trend_count_mismatch(self):
        """Mismatch between warmup_requests and warmup_req_duration_ms count causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["warmup_req_duration_ms"]["count"] = 5  # Counter is 6
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("warmup_req_duration_ms observation count (5) does not match warmup_requests (6)", str(ctx.exception))

    def test_admin_trend_count_mismatch(self):
        """Mismatch between admin_requests and admin_req_duration_ms count causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["admin_req_duration_ms"]["count"] = 1  # Counter is 2
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("admin_req_duration_ms observation count (1) does not match admin_requests (2)", str(ctx.exception))

    def test_http_req_duration_trend_count_mismatch(self):
        """Mismatch between http_reqs and http_req_duration count causes failure."""
        data = _make_valid_summary_data()
        data["metrics"]["http_req_duration"]["count"] = 1000  # Counter is 1008
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("http_req_duration observation count (1000) does not match http_reqs (1008)", str(ctx.exception))

    def test_missing_p99_in_trend_raises(self):
        """Missing p(99) raises MetricValidationError without fallback to p(95)."""
        data = _make_valid_summary_data()
        del data["metrics"]["workload_req_duration_ms"]["p(99)"]
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("Trend 'workload_req_duration_ms' is missing required stat 'p(99)'", str(ctx.exception))

    def test_negative_latency_in_trend_stat_raises(self):
        """Negative latency in trend stat raises MetricValidationError."""
        data = _make_valid_summary_data()
        data["metrics"]["workload_req_duration_ms"]["min"] = -0.5
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("cannot be negative", str(ctx.exception))

    def test_negative_or_zero_timestamp_stat_raises(self):
        """Zero or negative timestamp raises MetricValidationError."""
        data = _make_valid_summary_data()
        data["metrics"]["workload_start_time"]["min"] = 0.0
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("timestamp stat 'min' must be positive", str(ctx.exception))

    def test_non_monotonic_percentiles_raises(self):
        """Non-monotonic percentiles (p90 > p95 or p95 > p99) raise MetricValidationError."""
        data = _make_valid_summary_data()
        data["metrics"]["workload_req_duration_ms"]["p(95)"] = 160.0  # Greater than p(99) which is 150.0
        self._write_summary(data)
        with self.assertRaises(MetricValidationError) as ctx:
            extract_metrics(self.summary_file)
        self.assertIn("percentiles must be non-decreasing", str(ctx.exception))


class K6RuntimeVerificationTests(unittest.TestCase):
    """
    Tests for fail-closed k6 runtime verification:
    - Rejects overridden or mismatched image digests.
    - Rejects mismatched runtime versions.
    - Rejects failed Docker command execution.
    - Rejects missing Docker executable or disabled K6_USE_DOCKER.
    - Verifies that run_k6 fails closed if Docker is unavailable.
    - Verifies that exact pinned image and expected version pass cleanly.
    """

    def test_overridden_image_parameter_raises(self):
        """Passing an unpinned or mismatched image raises K6RuntimeVerificationError."""
        with self.assertRaises(K6RuntimeVerificationError) as ctx:
            verify_and_record_k6_runtime_version(image="grafana/k6:latest")
        self.assertIn("does not match pinned image digest", str(ctx.exception))

    def test_overridden_image_env_raises(self):
        """Setting K6_IMAGE to an unpinned image raises K6RuntimeVerificationError."""
        with patch.dict(os.environ, {"K6_IMAGE": "grafana/k6:v0.45.0"}):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                verify_and_record_k6_runtime_version()
            self.assertIn("does not match pinned image digest", str(ctx.exception))

    def test_mismatched_runtime_version_raises(self):
        """When Docker returns a different k6 version string, verification fails fast."""
        mock_proc = subprocess.CompletedProcess(
            args=["docker", "run", "--rm", K6_PINNED_IMAGE, "version"],
            returncode=0,
            stdout="k6 v0.49.0 (commit/12345678, go1.22.0, linux/amd64)\n",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run", return_value=mock_proc
        ):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                verify_and_record_k6_runtime_version()
            self.assertIn("k6 runtime version mismatch", str(ctx.exception))
            self.assertIn("expected 'k6 v2.4.0", str(ctx.exception))

    def test_failed_docker_command_raises(self):
        """When docker run exits non-zero, verification raises K6RuntimeVerificationError."""
        err = subprocess.CalledProcessError(
            returncode=125,
            cmd=["docker", "run", "--rm", K6_PINNED_IMAGE, "version"],
            stderr="docker: Error response from daemon: container error\n",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run", side_effect=err
        ):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                verify_and_record_k6_runtime_version()
            self.assertIn("Failed to execute k6 version check via Docker", str(ctx.exception))
            self.assertIn("exit code 125", str(ctx.exception))

    def test_unavailable_docker_runtime_raises(self):
        """When Docker executable is not in PATH, verification raises K6RuntimeVerificationError."""
        with patch("shutil.which", return_value=None):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                verify_and_record_k6_runtime_version()
            self.assertIn("Docker executable not found in PATH", str(ctx.exception))

    def test_disabled_docker_env_raises(self):
        """When K6_USE_DOCKER is disabled ('0'), verification raises K6RuntimeVerificationError."""
        with patch.dict(os.environ, {"K6_USE_DOCKER": "0"}):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                verify_and_record_k6_runtime_version()
            self.assertIn("K6_USE_DOCKER is disabled", str(ctx.exception))

    def test_run_k6_without_docker_raises(self):
        """run_k6() fails fast if Docker is missing from PATH (no silent fallback)."""
        with patch("shutil.which", return_value=None):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                run_k6("00_smoke_test.js", "dummy.json")
            self.assertIn("Docker executable not found in PATH", str(ctx.exception))

    def test_run_k6_disabled_docker_raises(self):
        """run_k6() fails fast if K6_USE_DOCKER is '0'."""
        with patch.dict(os.environ, {"K6_USE_DOCKER": "0"}):
            with self.assertRaises(K6RuntimeVerificationError) as ctx:
                run_k6("00_smoke_test.js", "dummy.json")
            self.assertIn("K6_USE_DOCKER is disabled", str(ctx.exception))

    def test_valid_pinned_runtime_succeeds(self):
        """When Docker returns the exact expected version for the pinned digest, verification succeeds."""
        mock_proc = subprocess.CompletedProcess(
            args=["docker", "run", "--rm", K6_PINNED_IMAGE, "version"],
            returncode=0,
            stdout=f"{K6_EXPECTED_RUNTIME_VERSION}\n",
        )
        with patch("shutil.which", return_value="/usr/bin/docker"), patch(
            "subprocess.run", return_value=mock_proc
        ):
            ver = verify_and_record_k6_runtime_version()
            self.assertEqual(ver, K6_EXPECTED_RUNTIME_VERSION)

    def test_real_docker_verification_if_available(self):
        """If host has Docker and image is present, live verification succeeds."""
        if not shutil.which("docker") or os.environ.get("CI"):
            self.skipTest("Skipping live docker test in environment without docker or in CI unit test runner")
        try:
            ver = verify_and_record_k6_runtime_version()
            self.assertEqual(ver, K6_EXPECTED_RUNTIME_VERSION)
        except K6RuntimeVerificationError as exc:
            self.fail(f"Live verification failed unexpectedly on host: {exc}")


if __name__ == "__main__":
    unittest.main()
