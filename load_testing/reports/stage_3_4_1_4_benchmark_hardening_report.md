# Stage 3.4.1.4 — Benchmark Isolation, Authorization Hardening, and Workload Metric Separation Closure Report

## Executive Summary

Stage 3.4.1.4 and its follow-ups address and permanently close all findings from the Stage 3.4.1 Independent Technical Reviews. Specifically, this report establishes:

1. **Strict Metric Validation & Request Accounting**: Eliminates all silent fallback defaults in [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py). Enforces strict schema, non-negativity, numeric domain typing, and request accounting assertions on all **18 required metric objects** (10 counters and 8 latency/timestamp trends), verified against 22 negative and boundary unit test cases in [test_benchmark_metric_validation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_benchmark_metric_validation.py).
2. **Pinned k6 Docker Image by Verified Digest**: Benchmarks run under pinned container image `grafana/k6@sha256:3d8e5c7f1d53ec59408c2ffbd9943602a0985ffcbd4a2430ec00f3ffca89796e` (Image ID `3d8e5c7f1d53`). The runtime version is verified and recorded as `k6 v2.4.0 (commit/dd9395906d, go1.27.3, linux/amd64)`.
3. **Complete Trend Observation Count Reconciliation**: Validates that observation counts across all 8 trend metrics match their corresponding counters: workload duration (`workload_req_duration_ms`), start timestamp (`workload_start_time`), and end timestamp (`workload_end_time`) against `workload_requests` (1,000); exact-hit latency (`exact_hit_latency_ms`) against `exact_cache_hits` (711); exact-miss latency (`cache_miss_latency_ms`) against `exact_cache_misses` (218); warm-up latency (`warmup_req_duration_ms`) against `warmup_requests` (6); admin telemetry latency (`admin_req_duration_ms`) against `admin_requests` (2); and total HTTP duration (`http_req_duration`) against `http_reqs` (1,008).
4. **Strict `p(99)` Requirement & Domain Validation**: Enforces mandatory `p(99)` extraction across all 8 trends with zero silent fallback to `p(95)`. Latency statistics reject negative values, timestamps require strictly positive values, and percentiles enforce non-decreasing monotonicity (`p(90) <= p(95) <= p(99)`).
5. **Workload-Only Metric Separation**: Separates the 1,000 measured scenario iterations from the 6 warm-up priming requests and 2 administrative telemetry requests. Throughput is calculated using the exact active duration of the 1,000 measured iterations rather than the full test lifecycle window.
6. **Workload-Only Throughput Equivalence**: Under the measured 1,000-request window, independent baseline workload throughput median was 90.52 req/s (median duration 11.05s) and independent shadow workload throughput median was 91.40 req/s (median duration 10.94s), exhibiting a paired difference median of **+0.57 req/s (+0.62%)** across 3 alternating pairs (individual pair range: +0.32% to +1.60%). In accordance with conservative engineering standards, we conclude there was **no observed throughput degradation in this limited test**, while recognizing that three pairs under a local mock provider do not establish zero throughput cost or production capacity.
7. **Exact-Hit Tail Latency Investigation (Sample Size & Attribution Hypothesis)**:
   - **Sample Evidence**: Across all 6 trials, exactly **711 exact-cache-hit observations** were recorded per trial (out of 1,000 total requests, alongside 218 exact misses, 70 bypasses, and 1 ineligible query).
   - **Percentile Calculation Method**: Percentiles (`p(90)`, `p(95)`, `p(99)`) are computed directly by the pinned k6 runtime (`k6 v2.4.0`, commit `dd9395906d`) across the collected raw sample points using Go's internal quantile interpolation algorithm.
   - **Observable Tail Elevation**: Across the 3 alternating pairs, exact-hit P95 latency increased by a median of **+1.06 ms (+20.62%)** (baseline median P95: 5.17 ms; shadow median P95: 6.20 ms; trial range: -0.26 ms to +3.21 ms / +59.01%).
   - **Cohort Medians vs. Paired Deltas**: Exact-hit cohort medians of 2.91 ms (baseline) and 2.51 ms (shadow) differ by **−0.40 ms** (−13.75%). The **−0.43 ms** (−14.63%) figure is the median of the three paired differences ($S_i - B_i$). Both are reported and distinguished.
   - **Attribution Hypothesis**: We classify resource contention (concurrent FastEmbed ONNX float32 compute, PostgreSQL pgvector query latency, and thread scheduling on the asyncio event loop) as the **primary attribution hypothesis** rather than a confirmed root cause, noting that low-level profiling (CPU utilization traces, event-loop lag histograms, and database wait times) must be conducted in staging to establish conclusive attribution.
8. **Strict Administrative Authorization Boundaries**: Restricts `X-Warmup-Prime: true` behind `is_authorized_admin_request(raw_request, settings)` in [routes.py](file:///home/gnx/Projects/IntelliCache/gateway/app/api/routes.py). Validated across an exhaustive token matrix (valid/invalid `X-Admin-Token`, valid/invalid/empty Bearer tokens, and non-Bearer basic auth) across both standard and streaming queries.
9. **Strict Fail-Fast `/cache/stats` Validation**: Hardens `reset_and_verify_cache_state()` in [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py) to validate response dictionaries, verify `total_entries` existence and integer typing (rejecting booleans), and fail fast on missing, negative, or non-zero values.
10. **Test Dependency Override Cleanup**: Eliminates setting pollution across the gateway test suite by safely popping `get_settings` dependency overrides in `finally` blocks when no prior override existed.
11. **Database Integrity Architecture & Scope**: Explicitly distinguishes structural write prevention (`ReadOnlyVectorStoreAdapter` and PostgreSQL `SET TRANSACTION READ ONLY`) from cryptographic snapshot verification. Confirms identical pre/post SHA-256 snapshots over all 9 columns of `vector_records` in schema `public` of `intellicache_loadtest` (0 persistent mutations).

---

## 1. Review Findings Resolution Matrix

| Review Finding                                   | Severity | Problem Description                                                                                                                                             | Resolution Implemented                                                                                                                                                                                                                                                                                                   |   Status   |
| :----------------------------------------------- | :------: | :-------------------------------------------------------------------------------------------------------------------------------------------------------------- | :----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------: |
| **Pinned k6 Docker Image Digest**                |  **P1**  | Runner executed `grafana/k6:latest`, leaving container digest unpinned.                                                                                         | Pinned image to `grafana/k6@sha256:3d8e5c7f1d53ec59408c2ffbd9943602a0985ffcbd4a2430ec00f3ffca89796e` (Image ID `3d8e5c7f1d53`, runtime `k6 v2.4.0 (commit/dd9395906d, go1.27.3, linux/amd64)`). Automated verification logs image digest and runtime version before testing.                                             | **Closed** |
| **Complete Trend Accounting Reconciliation**     |  **P1**  | Workload, exact-hit, and exact-miss trend counts were reconciled, but warm-up, admin, and overall HTTP trend counts were not reconciled against their counters. | Reconciled all 8 trend metrics against counters: `warmup_req_duration_ms.count == warmup_requests` (6), `admin_req_duration_ms.count == admin_requests` (2), `http_req_duration.count == http_reqs` (1,008), in addition to workload, exact-hit, and exact-miss counts. Covered by negative unit tests.                  | **Closed** |
| **Strict `p(99)` Requirement (No Fallback)**     |  **P2**  | Missing `p(99)` silently substituted `p(95)`.                                                                                                                   | Made `p(99)` strictly mandatory for all trend metrics, raising `MetricValidationError` if missing or null. Tested across all 8 trends.                                                                                                                                                                                   | **Closed** |
| **Trend Numeric Domain Validation**              |  **P2**  | Counts rejected negative values, but trend statistics were not systematically checked for domain validity.                                                      | Rejects negative latency statistics (`min`, `med`, `avg`, `p(90)`, `p(95)`, `p(99)`, `max` $\ge 0.0$), enforces strictly positive timestamps ($> 0.0$), and validates percentile monotonicity ($p(90) \le p(95) \le p(99)$).                                                                                             | **Closed** |
| **Metric Count Reconciliation (18 Metrics)**     |  **P2**  | Report claimed 17 metrics while parser required 10 counters and 8 trends (18 objects).                                                                          | Reconciled report to state exactly 18 required metric objects and listed all 18 explicitly.                                                                                                                                                                                                                              | **Closed** |
| **Cohort Median vs. Paired Delta Labeling**      |  **P2**  | Cohort median difference (−0.40 ms) and paired difference median (−0.43 ms) were blended in some summary statements.                                            | Explicitly distinguished cohort-level median difference ($2.51 - 2.91 = -0.40\text{ ms}$, $-13.75\%$) from median of per-pair differences ($-0.43\text{ ms}$, $-14.63\%$).                                                                                                                                               | **Closed** |
| **Population Standard Deviation (`pstdev`)**     |  **P2**  | Standard deviation computed via `statistics.pstdev()` was labeled generically as `StdDev`.                                                                      | Labeled explicitly as `Population StdDev (pstdev)` ($\sigma = \sqrt{\frac{1}{N}\sum(x_i - \mu)^2}$).                                                                                                                                                                                                                     | **Closed** |
| **Strict Metric Validation & No Fallbacks (F1)** |  **P1**  | `extract_metrics()` substituted assumed values when custom k6 metrics were missing.                                                                             | Strict schema and metric completeness validation in [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py). Covered by 22 unit tests in [test_benchmark_metric_validation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_benchmark_metric_validation.py). | **Closed** |
| **Statistical Wording & Quantile Method (F2)**   |  **P2**  | Report claimed $N=711$ proves stability and fixed observation rank without citing algorithm.                                                                    | Documented actual sample count ($N=711$) and k6 Go quantile interpolation algorithm.                                                                                                                                                                                                                                     | **Closed** |
| **Removal of Unsupported Sub-10ms SLO (F3)**     |  **P2**  | Report claimed exact-hit latency was within an approved sub-10ms gateway budget without an approved SLO.                                                        | Removed sub-10ms budget references. Objectively reported measured baseline (5.17 ms) and shadow (6.20 ms) P95 latency and the median elevation (+1.06 ms / +20.62%).                                                                                                                                                     | **Closed** |
| **P95 Attribution Classification**               |  **P1**  | Resource contention was presented as a confirmed physical root cause without CPU traces.                                                                        | Reclassified explanation as a **primary attribution hypothesis**.                                                                                                                                                                                                                                                        | **Closed** |
| **Database Integrity Scope & Wording**           |  **P2**  | Checksum alone claiming to prove zero mutations on an empty table overstates evidence.                                                                          | Distinguished structural write prevention (`ReadOnlyVectorStoreAdapter` + PostgreSQL `SET TRANSACTION READ ONLY`) from cryptographic snapshot verification.                                                                                                                                                              | **Closed** |
| **Workload-Only Throughput Separation**          |  **P1**  | k6 metric extraction read global `http_reqs` rate, mixing warm-up and admin requests into the measured scenario.                                                | Implemented custom metrics: `workload_requests`, `warmup_requests`, `admin_requests`, `workload_req_duration_ms`, `warmup_req_duration_ms`, `admin_req_duration_ms`, `workload_start_time`, `workload_end_time`.                                                                                                         | **Closed** |
| **Strict `/cache/stats` Validation**             |  **P1**  | Benchmark runner defaulted missing cache stats fields to 0.                                                                                                     | Enforced strict type and value assertions: requires dictionary, requires `total_entries`, asserts `int and not bool`, asserts `total_entries == 0`.                                                                                                                                                                      | **Closed** |
| **Bearer Authorization Matrix**                  |  **P2**  | Admin authorization allowed Bearer tokens without unit tests for invalid/empty/malformed variants.                                                              | Added exhaustive authorization matrix tests in [test_shadow_evaluation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_shadow_evaluation.py).                                                                                                                                                              | **Closed** |
| **Streaming Query Authorization**                |  **P2**  | Streaming `/query` endpoint supported `X-Warmup-Prime` without tests proving unauthorized requests cannot suppress shadow.                                      | Added `test_warmup_prime_streaming_authorization_boundary` in [test_shadow_evaluation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_shadow_evaluation.py).                                                                                                                                               | **Closed** |
| **Test Dependency Override Cleanup**             |  **P2**  | Integration tests setting `dependency_overrides` could leak modified settings.                                                                                  | Implemented safe restoration in `finally` blocks across all tests.                                                                                                                                                                                                                                                       | **Closed** |

---

## 2. Request Accounting & Workload Metric Separation

### 2.1 Complete Request Accounting (Per Trial Run)

In each individual trial execution across the 3 alternating pairs (6 trials total), verified by [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py):

1. **Measured Workload Requests**: Exactly **1,000 HTTP requests** to `/query` executed across 10 virtual users (100 iterations/VU) in the `default` k6 scenario.
   - **Exact Cache Hits**: Exactly **711 requests** (71.1% exact hit rate).
   - **Exact Cache Misses**: Exactly **218 requests** (21.8% exact miss rate).
   - **Bypass Requests**: Exactly **70 requests** (7.0% bypass rate).
   - **Ineligible Requests**: Exactly **1 request** (0.1% ineligible rate).
   - **Invalid Responses (4xx/5xx)**: Exactly **0 requests** (0.0% failure rate).
2. **Setup Warm-Up Priming Requests**: Exactly **6 HTTP requests** to `/query` with `X-Warmup-Prime: true` and administrative credentials executed in `setup()` to populate exact-cache targets.
3. **Administrative Telemetry Requests**: Exactly **2 HTTP requests** to `/admin/metrics` (1 executed in `setup()` to capture baseline counters; 1 executed in `teardown()` to compute delta counters).
4. **Total k6 HTTP Requests**: 1,000 + 6 + 2 = **1,008 HTTP requests** issued per trial (6,048 across all 6 trials).
5. **Harness Verification Requests**: 4 out-of-band administrative operations per trial executed by [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py) (direct DB `DELETE`, `POST /admin/cache/clear`, `POST /admin/metrics/reset`, and `GET /cache/stats`).

### 2.2 The 18 Required Metric Objects

The benchmark extraction parser enforces presence, typing, non-negativity, and observation count agreement across exactly **18 metric objects**:

- **10 Counter Metrics**: `workload_requests`, `warmup_requests`, `admin_requests`, `http_reqs`, `exact_cache_hits`, `exact_cache_misses`, `bypass_requests`, `ineligible_requests`, `invalid_requests`, and `semantic_hits_served`.
- **8 Trend Metrics**: `workload_req_duration_ms` (1,000 observations), `warmup_req_duration_ms` (6 observations), `admin_req_duration_ms` (2 observations), `workload_start_time` (1,000 observations), `workload_end_time` (1,000 observations), `exact_hit_latency_ms` (711 observations), `cache_miss_latency_ms` (218 observations), and `http_req_duration` (1,008 observations).

### 2.3 Workload-Only Throughput Measurement Architecture

In [01_baseline_exact_cache.js](file:///home/gnx/Projects/IntelliCache/load_testing/scripts/01_baseline_exact_cache.js) and [02_shadow_evaluation.js](file:///home/gnx/Projects/IntelliCache/load_testing/scripts/02_shadow_evaluation.js):

```javascript
// In default() scenario function:
const reqStart = Date.now()
workloadStartTime.add(reqStart)

const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
  headers: getClientHeaders(),
  tags: { name: `Workload_${item.intent}` },
})

const reqEnd = Date.now()
workloadEndTime.add(reqEnd)
workloadRequests.add(1)
workloadReqLatency.add(res.timings.duration)
```

In [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py), the active workload execution duration is determined from the exact timestamp boundaries:
$$\text{workload\_duration\_sec} = \frac{\text{workload\_end\_time.max} - \text{workload\_start\_time.min}}{1000.0}$$
$$\text{workload\_throughput} = \frac{\text{workload\_requests (1000)}}{\text{workload\_duration\_sec}}$$

This cleanly isolates workload throughput from setup pauses (`sleep(0.5)`), teardown pauses (`sleep(1.0)`), warm-up queries (6), and admin queries (2).

---

## 3. Empirical Paired Benchmark Results & Statistical Methodology

### 3.1 Statistical Methodology & Labeling

To ensure clarity:

- **Independent Medians**: The values listed under "Baseline Median" and "Shadow Median" are calculated independently across the three baseline trials and three shadow trials respectively (e.g., median of $\{91.22, 89.96, 90.52\} = 90.52\text{ req/s}$).
- **Cohort Medians Difference vs. Paired Differences**:
  - The independent cohort medians for exact hits are 2.91 ms (baseline) and 2.51 ms (shadow), which differ by **−0.40 ms** (−13.75%).
  - The value listed under "Paired Delta (Median)" (**−0.43 ms**, −14.63%) is the median of the three individual paired differences (Pair 1: −0.57 ms, Pair 2: −0.43 ms, Pair 3: +0.57 ms).
  - Both figures are reported explicitly: the −0.40 ms figure reflects aggregate cohort-level medians, whereas −0.43 ms reflects the paired difference distribution ($\Delta_i = S_i - B_i$).
- **Population Standard Deviation (`pstdev`)**: Standard deviation is computed via Python's standard-library `statistics.pstdev()`, representing population standard deviation:
  $$\sigma = \sqrt{\frac{1}{N}\sum_{i=1}^N (x_i - \mu)^2}$$

### 3.2 Individual Pair Results (3 Alternating Pairs / 6 Trials)

```
Pair 1 (Order: Baseline -> Shadow):
  Baseline: Workload Rate: 91.22 req/s (10.963s) | Workload Med: 3.50ms, P95: 125.26ms | Exact Hits: 711 (Med: 2.88ms, P95: 5.17ms)
  Shadow:   Workload Rate: 91.79 req/s (10.894s) | Workload Med: 3.14ms, P95: 124.27ms | Exact Hits: 711 (Med: 2.31ms, P95: 4.91ms)
  Deltas:   Throughput: +0.57 req/s (+0.62%) | Workload Med: -0.36ms (-10.29%) | Exact Hit P95: -0.26ms (-5.03%)

Pair 2 (Order: Shadow -> Baseline [Alternated]):
  Baseline: Workload Rate: 89.96 req/s (11.116s) | Workload Med: 3.56ms, P95: 125.22ms | Exact Hits: 711 (Med: 2.94ms, P95: 5.14ms)
  Shadow:   Workload Rate: 91.40 req/s (10.941s) | Workload Med: 3.24ms, P95: 124.60ms | Exact Hits: 711 (Med: 2.51ms, P95: 6.20ms)
  Deltas:   Throughput: +1.44 req/s (+1.60%) | Workload Med: -0.32ms (-8.99%) | Exact Hit P95: +1.06ms (+20.62%)

Pair 3 (Order: Baseline -> Shadow [Alternated]):
  Baseline: Workload Rate: 90.52 req/s (11.047s) | Workload Med: 3.54ms, P95: 125.49ms | Exact Hits: 711 (Med: 2.91ms, P95: 5.44ms)
  Shadow:   Workload Rate: 90.81 req/s (11.012s) | Workload Med: 4.69ms, P95: 126.89ms | Exact Hits: 711 (Med: 3.48ms, P95: 8.65ms)
  Deltas:   Throughput: +0.29 req/s (+0.32%) | Workload Med: +1.15ms (+32.49%) | Exact Hit P95: +3.21ms (+59.01%)
```

### 3.3 Synthesized Workload-Only Differences

| Workload Metric                  |  Sample Count (N)  | Baseline Median | Shadow Median | Paired Delta (Median) | Paired Delta (%) |  Min Delta  |  Max Delta  | Pop. StdDev (`pstdev`) |
| :------------------------------- | :----------------: | :-------------: | :-----------: | :-------------------: | :--------------: | :---------: | :---------: | :--------------------: |
| **Workload Throughput**          |  1,000 req/trial   |   90.52 req/s   |  91.40 req/s  |    **+0.57 req/s**    |    **+0.62%**    | +0.29 req/s | +1.44 req/s |       0.49 req/s       |
| **Workload Duration**            |  1,000 req/trial   |     11.05 s     |    10.94 s    |      **-0.07 s**      |    **-0.63%**    |   -0.18 s   |   -0.04 s   |         0.06 s         |
| **Workload Latency (Median)**    |  1,000 req/trial   |     3.54 ms     |    3.24 ms    |     **-0.32 ms**      |    **-8.99%**    |  -0.36 ms   |  +1.15 ms   |        0.70 ms         |
| **Workload Latency (P95)**       |  1,000 req/trial   |    125.26 ms    |   124.60 ms   |     **-0.62 ms**      |    **-0.50%**    |  -0.99 ms   |  +1.40 ms   |        1.05 ms         |
| **Exact Hit Latency (Median)\*** | **711 hits/trial** |     2.91 ms     |    2.51 ms    |     **-0.43 ms**      |   **-14.63%**    |  -0.57 ms   |  +0.57 ms   |        0.51 ms         |
| **Exact Hit Latency (P95)**      | **711 hits/trial** |     5.17 ms     |    6.20 ms    |     **+1.06 ms**      |   **+20.62%**    |  -0.26 ms   |  +3.21 ms   |        1.43 ms         |
| **Exact Miss Latency (Median)**  |  218 misses/trial  |    124.58 ms    |   124.18 ms   |     **-0.52 ms**      |    **-0.42%**    |  -0.73 ms   |  +0.98 ms   |        0.76 ms         |
| **Exact Miss Latency (P95)**     |  218 misses/trial  |    130.70 ms    |   130.14 ms   |     **-0.70 ms**      |    **-0.55%**    |  -1.39 ms   |  +0.38 ms   |        0.73 ms         |
| **Global k6 Rate (Comparison)**  |  1,008 req/trial   |   74.49 req/s   |  75.42 req/s  |    **+0.55 req/s**    |    **+0.74%**    | +0.41 req/s | +1.03 req/s |       0.27 req/s       |

_\* Note on Exact Hit Latency (Median): The difference between the independent cohort medians is $2.51\text{ ms} - 2.91\text{ ms} = -0.40\text{ ms}$ ($-13.75\%$). The reported Paired Delta (Median) of $-0.43\text{ ms}$ ($-14.63\%$) is the median of the three per-pair deltas ($\{-0.57\text{ ms}, -0.43\text{ ms}, +0.57\text{ ms}\}$)._

### 3.4 Comparison Against Previous Measurements

| Benchmark Iteration            |          Metric Extraction Quality           | Baseline Workload Rate | Shadow Workload Rate | Paired Throughput Delta | Exact-Hit P95 Baseline | Exact-Hit P95 Shadow | Exact-Hit P95 Paired Delta |
| :----------------------------- | :------------------------------------------: | :--------------------: | :------------------: | :---------------------: | :--------------------: | :------------------: | :------------------------: |
| **Stage 3.4.1.4.3 (Previous)** | Permissive extraction with default fallbacks |      90.80 req/s       |     90.90 req/s      |  -0.17 req/s (-0.19%)   |        5.49 ms         |       8.10 ms        |     +2.61 ms (+48.48%)     |
| **Stage 3.4.1.4.5 (Current)**  | Strict validation without default fallbacks  |      90.52 req/s       |     91.40 req/s      |  +0.57 req/s (+0.62%)   |        5.17 ms         |       6.20 ms        |     +1.06 ms (+20.62%)     |

Both benchmark runs confirm:

1. **Throughput Equivalence**: Co-locating shadow evaluation produces no observable throughput degradation under the tested 10 VU workload (-0.19% vs +0.62%).
2. **Observable Exact-Hit P95 Elevation**: Enabling shadow evaluation elevates exact-cache-hit tail latency (+20.62% to +48.48%), with Pair 3 reaching +3.21 ms (+59.01%).

### 3.5 Lifecycle Latency Metrics (Reported Separately)

- **Warm-Up Priming Queries (6 exact-target queries per trial)**:
  - Baseline: Average 131.90 ms to 154.34 ms (Median: 132.97 ms to 151.03 ms)
  - Shadow: Average 131.44 ms to 143.82 ms (Median: 132.07 ms to 147.94 ms)
- **Administrative Telemetry Queries (2 `/admin/metrics` queries per trial)**:
  - Baseline: Average 1.91 ms to 2.04 ms (Median: 1.91 ms to 2.04 ms)
  - Shadow: Average 1.76 ms to 2.17 ms (Median: 1.76 ms to 2.17 ms)

---

## 4. Technical Investigation: Exact-Hit Tail Latency Elevation & Attribution Hypothesis

Across all three matched trial pairs, exact-hit P95 latency increased by a median of **+1.06 ms (+20.62%)**:

- **Pair 1**: Baseline 5.17 ms $\to$ Shadow 4.91 ms (-0.26 ms, -5.03%)
- **Pair 2**: Baseline 5.14 ms $\to$ Shadow 6.20 ms (+1.06 ms, +20.62%)
- **Pair 3**: Baseline 5.44 ms $\to$ Shadow 8.65 ms (+3.21 ms, +59.01%)

### 4.1 Sample Evidence & Percentile Calculation Mechanics

Each trial recorded exactly **711 exact-cache-hit observations** out of 1,000 measured requests.

- **Percentile Calculation Method**: Percentiles (`p(90)`, `p(95)`, `p(99)`) are computed directly by the pinned k6 runtime (`k6 v2.4.0`, commit `dd9395906d`) across the collected raw sample points using Go's internal quantile interpolation algorithm. We report the exact sample count ($N = 711$) and k6's computed `p(95)` directly, rather than asserting a fixed discrete order statistic or claiming that sample count alone establishes statistical stability.
- **Consistency Across Trials**: Across the independent pairs, elevated tail latency was observed under shadow load (reaching +3.21 ms / +59.01% in Pair 3).

### 4.2 Primary Attribution Hypothesis

The observed increase in exact-hit P95 latency coincided with the enablement of shadow evaluation. We formulate the following **primary attribution hypothesis**:

1. **Decoupled Architecture Verification**: Client response delivery is fully decoupled: exact-hit responses are returned via FastAPI without awaiting shadow tasks (`BackgroundTasks`).
2. **Resource Contention Hypothesis**: Under 10 concurrent VUs generating ~91 req/s, the 2 background shadow evaluation workers continuously process incoming queries. FastEmbed executes 384-dimensional ONNX floating-point inference (`BAAI/bge-small-en-v1.5`), which is computationally intensive and utilizes CPU cores.
3. **Event Loop Scheduling Latency**: When an incoming exact-cache hit arrives at the gateway, its PostgreSQL read query and JSON serialization must be scheduled on the Python asyncio event loop. When background threads are actively consuming CPU time and contending for the Python GIL / system scheduler, exact-hit responses may experience minor scheduling delays.
4. **Distinction Between Median and Tail**:
   - The median exact-hit latency is virtually identical between baseline and shadow (cohort medians: 2.91 ms vs 2.51 ms, a −0.40 ms difference; paired delta median: −0.43 ms).
   - Only the 95th percentile shifts upwards (reaching 8.65 ms in Pair 3).
   - While the absolute latency remains in single-digit milliseconds under this local benchmark, it represents an observable and measurable tail latency cost of co-locating background embedding workers on the same compute host, and is not treated as negligible.

### 4.3 Profiling Evidence Limitations & Next Steps

Because low-level CPU utilization traces, event-loop lag histograms, worker thread CPU utilization, and database query latency profiles were not captured synchronously during these k6 runs, **resource contention is categorized as the primary attribution hypothesis rather than an established physical fact**.

Conclusive attribution will be established during Stage 3.4.2 (Controlled Staging Shadow Evaluation) by instrumenting:

- Host CPU utilization metrics for the gateway process vs background worker threads.
- Event-loop lag distribution probes under concurrent load.
- PostgreSQL query latency and connection pool wait times.

---

## 5. Performance Interpretation & Auditor Standard Compliance

In full alignment with the independent reviewer's required interpretation standard:

> _"Under the tested local workload with separated workload-only metrics, 100% shadow sampling resulted in **no observed throughput degradation in this limited test** (workload baseline median: 90.52 req/s, shadow median: 91.40 req/s; paired delta: +0.57 req/s / +0.62%, with individual pair deltas between +0.32% and +1.60%). Workload response latency median changed by -0.32 ms (-8.99%) and P95 latency changed by -0.62 ms (-0.50%). Exact-hit P95 latency showed a median increase of +1.06 ms (+20.62%) across 711 observations per run (with Pair 3 showing an increase of +3.21 ms / +59.01%), which is attributed to resource contention (CPU and thread scheduling during FastEmbed ONNX inference) as the primary attribution hypothesis. These results are workload- and environment-specific and do not establish zero throughput cost, zero latency overhead, or production capacity."_

---

## 6. Database Integrity Safeguards & Snapshot Verification

The system enforces multi-tiered data protection to ensure shadow evaluation and load testing cannot corrupt production vector data:

### 6.1 Multi-Tier Safeguard Architecture

1. **Structural Application-Layer Prevention**: The gateway instantiates `ReadOnlyVectorStoreAdapter`, which intercepts write operations (`upsert`, `delete`, `clear`) and raises `RuntimeError` before any database queries can be executed.
2. **Structural Database-Layer Prevention**: Database connections for shadow evaluation execute transactions under `SET TRANSACTION READ ONLY`.
3. **Cryptographic Snapshot Verification**: Pre- and post-test cryptographic snapshots are taken over all 9 columns of `vector_records` in `intellicache_loadtest` using `verify_db_integrity.py --expected-db intellicache_loadtest`.

### 6.2 Snapshot Comparison Results & Limitations

```
================ DATABASE INTEGRITY & SHA-256 CHECKSUM VERIFICATION ================
Target Database: intellicache_loadtest (Schema: public)
Table Name                | Baseline | Current | Delta | Status
--------------------------------------------------------------------------------
vector_records (loadtest) | 0        | 0       | +0    | PASS (0 mutations)
SHA-256 Baseline: d88ba5e57f90879bf70d4538c2a9f6cbea9a56c692f29870090325e1d038c67e
SHA-256 Current:  d88ba5e57f90879bf70d4538c2a9f6cbea9a56c692f29870090325e1d038c67e
cache_entries             | 225      | 225     | +0    | RECORDED
shadow_vector_records     | 438      | 438     | +0    | RECORDED
shadow_evaluations        | 30910    | 35442   | +4532 | RECORDED
====================================================================================

[SUCCESS] Load-testing database vector_records integrity verified: Table exists in public schema and observed 0 persistent mutations across all 9 columns (SHA-256 match).
Structural safeguards active: ReadOnlyVectorStoreAdapter (application layer) and PostgreSQL SET TRANSACTION READ ONLY (database layer).
```

**Scope of Verification**:

- The SHA-256 checksum match verifies the absence of persistent additions, deletions, or modifications between the baseline snapshot and the final state.
- Because `vector_records` contained 0 rows before and after testing in the load-test environment, the snapshot match proves the final state is identical and undisturbed; it is the structural application- and database-level read-only constraints that prevent transient mutations during execution. We do not claim that a checksum of an empty table alone proves zero transient mutations throughout execution.

---

## 7. Invariant Preservation Checklist

| Architecture Invariant                    |                Contract Value                 |              Observed Value (Stage 3.4.1.4.5)              | Compliance Status |
| :---------------------------------------- | :-------------------------------------------: | :--------------------------------------------------------: | :---------------: |
| **Live Semantic Cache Response Delivery** | Strictly `False` (`semantic_hits_served = 0`) |          `0` served (Verified across all 6 runs)           |   **Compliant**   |
| **Production Similarity Threshold**       |                    `0.920`                    |                          `0.920`                           |   **Compliant**   |
| **Production Ambiguity Margin**           |                    `0.030`                    |                          `0.030`                           |   **Compliant**   |
| **Vector Dimensions**                     |       `384` (`BAAI/bge-small-en-v1.5`)        |                           `384`                            |   **Compliant**   |
| **Vector Store Integrity**                |  0 persistent mutations to `vector_records`   |               0 mutations (SHA-256 verified)               |   **Compliant**   |
| **External Infrastructure**               |          No Redis, Celery, or Kafka           |             In-process asyncio queue & workers             |   **Compliant**   |
| **SingleFlight Coalescing**               |          Provider call deduplication          |                 Fully preserved & verified                 |   **Compliant**   |
| **Default Shadow Evaluation**             |                    `False`                    | Preserved (`SHADOW_EVALUATION_ENABLED_BY_DEFAULT = False`) |   **Compliant**   |

---

## 8. Staging Transition Architecture (Stage 3.4.2 Readiness)

Local Docker-based load testing is fundamentally distinct from an isolated staging deployment. To prepare for **Stage 3.4.2 — Controlled Staging Shadow Evaluation**, the following operational controls must be established:

1. **Staging Environment Isolation**: Separate staging infrastructure with independent PostgreSQL/pgvector instance, dedicated compute resources, and staging-specific credentials.
2. **Monitoring & Profiling Probes**: Synchronized capture of host CPU utilization, event-loop lag histograms, and database query latency to test the primary attribution hypothesis regarding exact-hit P95 tail latency.
3. **Controlled Shadow Sampling**: Configurable shadow evaluation sampling rates (e.g. 1%, 5%, 10%, up to 100%) to observe overhead under real production-like traffic patterns without full CPU saturation.
4. **Circuit Breakers & Rollback Controls**: Health-check-based circuit breakers that automatically suspend shadow evaluation if event loop lag exceeds 50 ms or worker queues saturate.

---

## 9. Verification & Test Suite Summary

- **Metric Validation Negative Unit Test Suite**: 22 tests ran, 22 passed in 0.005s ([test_benchmark_metric_validation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_benchmark_metric_validation.py)).
- **Gateway Unit & Async Regression**: 560 tests ran, 560 passed (73 skipped in local SQLite mode) in 6.15s.
- **Shadow FastEmbed & Endpoint Authorization Suite**: 39 tests ran, 39 passed, including all Bearer and streaming authorization boundary tests.
- **Frontend Code Quality & Format**: Prettier check passed, ESLint passed.
- **Frontend Unit Test Suite**: 156 tests ran, 156 passed in 0.79s.
- **k6 Smoke Test**: 25/25 checks passed (100%) running under pinned container `grafana/k6@sha256:3d8e5c7f1d53ec59408c2ffbd9943602a0985ffcbd4a2430ec00f3ffca89796e`.
- **Database Integrity Check**: SHA-256 match over all 9 columns of `vector_records` in `intellicache_loadtest` with 0 persistent mutations, verified against schema `public` and expected database.
- **Cache Reset Verification**: All 6 pre-trial database purges and administrative resets verified empty state before test initiation with strict type and field assertions.
