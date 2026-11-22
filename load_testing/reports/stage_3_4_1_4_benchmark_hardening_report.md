# Stage 3.4.1.4 — Benchmark Isolation, Authorization Hardening, and Workload Metric Separation Closure Report

## Executive Summary

Stage 3.4.1.4 and its follow-ups (Stage 3.4.1.4.2, Stage 3.4.1.4.3, and Stage 3.4.1.4.4) address and close all findings from the Stage 3.4.1.3 and Stage 3.4.1.4 Independent Technical Reviews. Specifically, this report establishes:

1. **Workload-Only Metric Separation**: Separates the 1,000 measured scenario iterations from the 6 warm-up priming requests and 2 administrative telemetry requests. Throughput is calculated using the exact active duration of the 1,000 measured iterations rather than the full test lifecycle window.
2. **Workload-Only Throughput Equivalence**: Under the measured 1,000-request window, independent baseline workload throughput median was 90.80 req/s (median duration 11.01s) and independent shadow workload throughput median was 90.90 req/s (median duration 11.00s), exhibiting a paired difference median of **-0.17 req/s (-0.19%)** across 3 alternating pairs (individual pair range: -0.43% to +0.42%). In accordance with conservative engineering standards, we conclude there was **no observed throughput degradation in this limited test**, while recognizing that three pairs under a local mock provider do not establish zero throughput cost or production capacity.
3. **Exact-Hit Tail Latency Investigation (Sample Size & Attribution Hypothesis)**:
   - **Sample Reliability**: Across all 6 trials, exactly **711 exact-cache-hit observations** were recorded per trial (out of 1,000 total requests, alongside 218 exact misses, 70 bypasses, and 1 ineligible query). In a sample of $N = 711$, the 95th percentile corresponds to the 675th observation (top 35 items), establishing that the tail measurement is statistically stable and not a small-sample artifact.
   - **Observable Regression**: Across the 3 alternating pairs, exact-hit P95 latency increased by a median of **+2.61 ms (+48.48%)** (baseline median P95: 5.49 ms; shadow median P95: 8.10 ms; trial range: +2.40 ms to +4.40 ms).
   - **Attribution Hypothesis**: We classify resource contention (concurrent FastEmbed ONNX float32 compute, PostgreSQL pgvector query latency, and thread scheduling on the asyncio event loop) as the **primary attribution hypothesis** rather than a confirmed root cause, noting that low-level profiling (CPU utilization traces, event-loop lag histograms, and database wait times) must be conducted in staging to establish conclusive attribution.
4. **Strict Administrative Authorization Boundaries**: Restricts `X-Warmup-Prime: true` behind `is_authorized_admin_request(raw_request, settings)` in [routes.py](file:///home/gnx/Projects/IntelliCache/gateway/app/api/routes.py). Validated across an exhaustive token matrix (valid/invalid `X-Admin-Token`, valid/invalid/empty Bearer tokens, and non-Bearer basic auth) across both standard and streaming queries.
5. **Strict Fail-Fast `/cache/stats` Validation**: Hardens `reset_and_verify_cache_state()` in [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py) to validate response dictionaries, verify `total_entries` existence and integer typing (rejecting booleans), and fail fast on missing, negative, or non-zero values.
6. **Test Dependency Override Cleanup**: Eliminates setting pollution across the 545 gateway test suite by safely popping `get_settings` dependency overrides in `finally` blocks when no prior override existed.
7. **Database Integrity Architecture & Scope**: Explicitly distinguishes structural write prevention (`ReadOnlyVectorStoreAdapter` and PostgreSQL `SET TRANSACTION READ ONLY`) from cryptographic snapshot verification. Confirms identical pre/post SHA-256 snapshots over all 9 columns of `vector_records` in schema `public` of `intellicache_loadtest` (0 persistent mutations).

---

## 1. Review Findings Resolution Matrix

| Review Finding                                    | Severity | Problem Description                                                                                                                                                        | Resolution Implemented                                                                                                                                                                                                                                                                                                                                                                                                            |   Status   |
| :------------------------------------------------ | :------: | :------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :-------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :--------: |
| **Exact-Hit Sample-Count Evidence**               |  **P1**  | Report did not provide the number of exact-hit observations used to calculate P95, leaving measurement stability unverified.                                               | Extracted and reported observation counts for every trial: exactly **711 exact hits** per trial (71.1% exact hit rate), 218 exact misses, 70 bypasses, and 1 ineligible query across all 6 trials. In a sample of $N=711$, P95 reflects the 675th observation, establishing statistical stability.                                                                                                                                | **Closed** |
| **P95 Attribution Classification**                |  **P1**  | FastEmbed ONNX, pgvector, and CPU contention were presented as a confirmed physical root cause without synchronized CPU/lag profiling.                                     | Reclassified the explanation as a **primary attribution hypothesis** rather than a confirmed fact. Documented that synchronized CPU utilization, event-loop lag histograms, and database lock profiling are required in staging for conclusive attribution.                                                                                                                                                                       | **Closed** |
| **Database Integrity Scope & Wording**            |  **P2**  | Claiming a checksum alone "cryptographically verifies zero mutations" on an empty table overstates what a snapshot comparison proves.                                      | Delineated structural write prevention (`ReadOnlyVectorStoreAdapter` + PostgreSQL `SET TRANSACTION READ ONLY`) from pre/post snapshot verification. Clarified that checksums prove the absence of persistent changes between snapshots, while structural guards prevent transient modifications.                                                                                                                                  | **Closed** |
| **Paired-Median Methodology Clarification**       |  **P2**  | Independent aggregate medians (90.80 vs 90.90) and median paired deltas (-0.17 req/s, +2.61 ms / +48.48%) required clearer statistical labeling.                           | Added explicit statistical methodology notes distinguishing independent medians across trial cohorts from the medians of per-pair differences and per-pair percentages.                                                                                                                                                                                                                                                           | **Closed** |
| **Workload-Only Throughput & Latency Separation** |  **P1**  | Previous k6 metric extraction read global `http_reqs` rate and `http_req_duration`, mixing warm-up requests (6) and admin requests (2) into the measured scenario (1,000). | Implemented dedicated custom metrics in [metrics.js](file:///home/gnx/Projects/IntelliCache/load_testing/helpers/metrics.js) and [run_paired_benchmark.py](file:///home/gnx/Projects/IntelliCache/load_testing/run_paired_benchmark.py): `workload_requests`, `warmup_requests`, `admin_requests`, `workload_req_duration_ms`, `warmup_req_duration_ms`, `admin_req_duration_ms`, `workload_start_time`, and `workload_end_time`. | **Closed** |
| **Conservative Performance Wording**              |  **P2**  | Previous reporting claimed "zero throughput penalty", which is stronger than 3 benchmark pairs in a single local test environment can support.                             | Updated standard interpretation to state: _no observed throughput degradation in this limited test_ (median delta of -0.17 req/s / -0.19%, with trial pair range between -0.43% and +0.42%). Noted that local mock benchmarks do not establish production capacity.                                                                                                                                                               | **Closed** |
| **Strict `/cache/stats` Validation**              |  **P1**  | Benchmark runner defaulted missing cache stats fields to 0, risking silent pass on malformed responses.                                                                    | Enforced strict type and value assertions: requires `isinstance(stats_data, dict)`, requires `"total_entries" in stats_data`, asserts `isinstance(total_entries, int) and not isinstance(total_entries, bool)`, asserts `total_entries >= 0` and `total_entries == 0`. Fails immediately on any malformed or missing key.                                                                                                         | **Closed** |
| **Bearer Authorization Matrix**                   |  **P2**  | Route-level admin authorization allowed Bearer tokens, but lacked unit test coverage for valid, invalid, empty, and malformed Bearer headers.                              | Added exhaustive authorization matrix tests in [test_shadow_evaluation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_shadow_evaluation.py): valid Bearer token (shadow suppressed), invalid Bearer token (shadow executed), empty Bearer token (shadow executed), and non-Bearer Basic auth (shadow executed).                                                                                                    | **Closed** |
| **Streaming Query Authorization**                 |  **P2**  | Streaming `/query` endpoint supported `X-Warmup-Prime`, but lacked direct tests proving unauthorized requests cannot suppress shadow evaluation.                           | Added `test_warmup_prime_streaming_authorization_boundary` in [test_shadow_evaluation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_shadow_evaluation.py) testing valid/invalid admin tokens and Bearer tokens on streaming requests.                                                                                                                                                                             | **Closed** |
| **Test Dependency Override Cleanup**              |  **P2**  | Integration tests setting `app.dependency_overrides[get_settings]` could leak modified settings if not cleanly cleared when no previous override existed.                  | Implemented safe restoration in `finally` blocks: `if orig_get_settings is None: app.dependency_overrides.pop(get_settings, None)` across all tests in [test_shadow_evaluation.py](file:///home/gnx/Projects/IntelliCache/gateway/tests/test_shadow_evaluation.py).                                                                                                                                                               | **Closed** |

---

## 2. Request Accounting & Workload Metric Separation

### 2.1 Complete Request Accounting (Per Trial Run)

In each individual trial execution across the 3 alternating pairs (6 trials total):

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

### 2.2 Workload-Only Throughput Measurement Architecture

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

- **Independent Medians**: The values listed under "Baseline Median" and "Shadow Median" are calculated independently across the three baseline trials and three shadow trials respectively (e.g., median of $\{91.29, 90.80, 90.47\} = 90.80\text{ req/s}$).
- **Paired Differences ($\Delta_i = S_i - B_i$)**: The values listed under "Paired Delta (Median)" and "Paired Delta (%)" are the medians of the three individual paired differences and paired percentage differences. They are **not** the difference between the independent medians or the quotient of the independent medians.

### 3.2 Individual Pair Results (3 Alternating Pairs / 6 Trials)

```
Pair 1 (Order: Baseline -> Shadow):
  Baseline: Workload Rate: 91.29 req/s (10.954s) | Workload Med: 3.84ms, P95: 125.57ms | Exact Hits: 711 (Med: 3.27ms, P95: 5.50ms)
  Shadow:   Workload Rate: 90.90 req/s (11.001s) | Workload Med: 4.32ms, P95: 126.54ms | Exact Hits: 711 (Med: 3.20ms, P95: 9.90ms)
  Deltas:   Throughput: -0.39 req/s (-0.43%) | Workload Med: +0.48ms (+12.50%) | Exact Hit P95: +4.40ms (+80.00%)

Pair 2 (Order: Shadow -> Baseline [Alternated]):
  Baseline: Workload Rate: 90.80 req/s (11.013s) | Workload Med: 3.84ms, P95: 125.47ms | Exact Hits: 711 (Med: 3.29ms, P95: 5.49ms)
  Shadow:   Workload Rate: 91.18 req/s (10.967s) | Workload Med: 4.42ms, P95: 126.52ms | Exact Hits: 711 (Med: 3.38ms, P95: 8.10ms)
  Deltas:   Throughput: +0.38 req/s (+0.42%) | Workload Med: +0.58ms (+15.10%) | Exact Hit P95: +2.61ms (+47.54%)

Pair 3 (Order: Baseline -> Shadow [Alternated]):
  Baseline: Workload Rate: 90.47 req/s (11.053s) | Workload Med: 3.77ms, P95: 125.72ms | Exact Hits: 711 (Med: 3.29ms, P95: 4.95ms)
  Shadow:   Workload Rate: 90.30 req/s (11.074s) | Workload Med: 3.92ms, P95: 125.99ms | Exact Hits: 711 (Med: 2.89ms, P95: 7.35ms)
  Deltas:   Throughput: -0.17 req/s (-0.19%) | Workload Med: +0.15ms (+3.98%) | Exact Hit P95: +2.40ms (+48.48%)
```

### 3.3 Synthesized Workload-Only Differences

| Workload Metric                 |  Sample Count (N)  | Baseline Median | Shadow Median | Paired Delta (Median) | Paired Delta (%) |  Min Delta  |  Max Delta  |   StdDev   |
| :------------------------------ | :----------------: | :-------------: | :-----------: | :-------------------: | :--------------: | :---------: | :---------: | :--------: |
| **Workload Throughput**         |  1,000 req/trial   |   90.80 req/s   |  90.90 req/s  |    **-0.17 req/s**    |    **-0.19%**    | -0.39 req/s | +0.38 req/s | 0.32 req/s |
| **Workload Duration**           |  1,000 req/trial   |     11.01 s     |    11.00 s    |      **-0.01 s**      |    **-0.09%**    |   -0.05 s   |   +0.05 s   |   0.04 s   |
| **Workload Latency (Median)**   |  1,000 req/trial   |     3.84 ms     |    4.32 ms    |     **+0.48 ms**      |   **+12.50%**    |  +0.15 ms   |  +0.58 ms   |  0.18 ms   |
| **Workload Latency (P95)**      |  1,000 req/trial   |    125.57 ms    |   126.52 ms   |     **+0.97 ms**      |    **+0.77%**    |  +0.27 ms   |  +1.05 ms   |  0.35 ms   |
| **Exact Hit Latency (Median)**  | **711 hits/trial** |     3.29 ms     |    3.20 ms    |     **-0.07 ms**      |    **-2.14%**    |  -0.40 ms   |  +0.09 ms   |  0.20 ms   |
| **Exact Hit Latency (P95)**     | **711 hits/trial** |     5.49 ms     |    8.10 ms    |     **+2.61 ms**      |   **+48.48%**    |  +2.40 ms   |  +4.40 ms   |  0.90 ms   |
| **Exact Miss Latency (Median)** |  218 misses/trial  |    124.40 ms    |   124.72 ms   |     **+0.30 ms**      |    **+0.24%**    |  +0.19 ms   |  +0.35 ms   |  0.07 ms   |
| **Exact Miss Latency (P95)**    |  218 misses/trial  |    127.43 ms    |   129.60 ms   |     **+1.88 ms**      |    **+1.47%**    |  +1.80 ms   |  +3.53 ms   |  0.80 ms   |
| **Global k6 Rate (Comparison)** |  1,008 req/trial   |   74.99 req/s   |  75.49 req/s  |    **+0.20 req/s**    |    **+0.27%**    | -0.07 req/s | +0.71 req/s | 0.32 req/s |

### 3.4 Lifecycle Latency Metrics (Reported Separately)

- **Warm-Up Priming Queries (6 exact-target queries per trial)**:
  - Baseline: Average 145.03 ms to 146.56 ms (Median: 147.57 ms to 148.97 ms)
  - Shadow: Average 131.61 ms to 144.92 ms (Median: 132.55 ms to 147.83 ms)
- **Administrative Telemetry Queries (2 `/admin/metrics` queries per trial)**:
  - Baseline: Average 1.36 ms to 2.07 ms (Median: 1.36 ms to 2.07 ms)
  - Shadow: Average 1.59 ms to 2.53 ms (Median: 1.59 ms to 2.53 ms)

---

## 4. Technical Investigation: Exact-Hit Tail Latency Elevation & Attribution Hypothesis

Across all three matched trial pairs, exact-hit P95 latency increased by a median of **+2.61 ms (+48.48%)**:

- **Pair 1**: Baseline 5.50 ms $\to$ Shadow 9.90 ms (+4.40 ms, +80.00%)
- **Pair 2**: Baseline 5.49 ms $\to$ Shadow 8.10 ms (+2.61 ms, +47.54%)
- **Pair 3**: Baseline 4.95 ms $\to$ Shadow 7.35 ms (+2.40 ms, +48.48%)

### 4.1 Sample Stability & Reliability

Each trial recorded exactly **711 exact-cache-hit observations**. In a sample of $N = 711$:

- The 95th percentile corresponds to the 675th sorted observation (top 35 observations).
- This sample count is sufficiently large to establish that the observed increase is not a small-sample anomaly (such as could occur with $N < 20$ where a single outlier distorts P95).
- Across all three independent pairs, the P95 tail elongation was consistently observed in every trial run (+4.40 ms, +2.61 ms, +2.40 ms).

### 4.2 Primary Attribution Hypothesis

The observed increase in exact-hit P95 latency coincided with the enablement of shadow evaluation. We formulate the following **primary attribution hypothesis**:

1. **Decoupled Architecture Verification**: Client response delivery is fully decoupled: exact-hit responses are returned via FastAPI without awaiting shadow tasks (`BackgroundTasks`).
2. **Resource Contention Hypothesis**: Under 10 concurrent VUs generating ~91 req/s, the 2 background shadow evaluation workers continuously process incoming queries. FastEmbed executes 384-dimensional ONNX floating-point inference (`BAAI/bge-small-en-v1.5`), which is computationally intensive and utilizes CPU cores.
3. **Event Loop Scheduling Latency**: When an incoming exact-cache hit arrives at the gateway, its PostgreSQL read query and JSON serialization must be scheduled on the Python asyncio event loop. When background threads are actively consuming CPU time and contending for the Python GIL / system scheduler, exact-hit responses may experience minor scheduling delays.
4. **Distinction Between Median and Tail**:
   - The median exact-hit latency is virtually identical between baseline and shadow (3.29 ms vs 3.20 ms, a -0.07 ms difference).
   - Only the 95th percentile shifts from ~5.0–5.5 ms to ~7.4–9.9 ms.
   - While an absolute increase of ~2.6 ms remains well within the gateway's sub-10ms exact-hit budget, it is an observable and measurable tail latency cost of co-locating background embedding workers on the same compute host.

### 4.3 Profiling Evidence Limitations & Next Steps

Because low-level CPU utilization traces, event-loop lag histograms, worker thread CPU utilization, and database query latency profiles were not captured synchronously during these k6 runs, **resource contention is categorized as the primary attribution hypothesis rather than an established physical fact**.

Conclusive attribution will be established during Stage 3.4.2 (Controlled Staging Shadow Evaluation) by instrumenting:

- Host CPU utilization metrics for the gateway process vs background worker threads.
- Event-loop lag distribution probes under concurrent load.
- PostgreSQL query latency and connection pool wait times.

---

## 5. Performance Interpretation & Auditor Standard Compliance

In full alignment with the independent reviewer's required interpretation standard:

> _"Under the tested local workload with separated workload-only metrics, 100% shadow sampling resulted in **no observed throughput degradation in this limited test** (workload baseline median: 90.80 req/s, shadow median: 90.90 req/s; paired delta: -0.17 req/s / -0.19%, with individual pair deltas between -0.43% and +0.42%). Workload response latency median increased by +0.48 ms (+12.50%) and P95 latency increased by +0.97 ms (+0.77%). Exact-hit P95 latency showed a median increase of +2.61 ms (+48.48%) across 711 observations per run, which is attributed to resource contention (CPU and thread scheduling during FastEmbed ONNX inference) as the primary hypothesis. These results are workload- and environment-specific and do not establish zero throughput cost, zero latency overhead, or production capacity."_

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
cache_entries             | 1000     | 1000    | +0    | RECORDED
shadow_vector_records     | 438      | 438     | +0    | RECORDED
shadow_evaluations        | 26378    | 30910   | +4532 | RECORDED
====================================================================================

[SUCCESS] Load-testing database vector_records integrity verified: Table exists in public schema and observed 0 persistent mutations across all 9 columns (SHA-256 match).
Structural safeguards active: ReadOnlyVectorStoreAdapter (application layer) and PostgreSQL SET TRANSACTION READ ONLY (database layer).
```

**Scope of Verification**:

- The SHA-256 checksum match verifies the absence of persistent additions, deletions, or modifications between the baseline snapshot and the final state.
- Because `vector_records` contained 0 rows before and after testing in the load-test environment, the snapshot match proves the final state is identical and undisturbed; it is the structural application- and database-level read-only constraints that prevent transient mutations during execution. We do not claim that a checksum of an empty table alone proves zero transient mutations throughout execution.

---

## 7. Invariant Preservation Checklist

| Architecture Invariant                    |                Contract Value                 |              Observed Value (Stage 3.4.1.4.4)              | Compliance Status |
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

- **Gateway Unit & Async Regression**: 545 tests ran, 545 passed (73 skipped in local SQLite mode) in 6.11s.
- **Shadow FastEmbed & Endpoint Authorization Suite**: 39 tests ran, 39 passed, including all Bearer and streaming authorization boundary tests.
- **Frontend Code Quality & Format**: Prettier check passed, ESLint passed.
- **Frontend Unit Test Suite**: 156 tests ran, 156 passed in 0.79s.
- **k6 Smoke Test**: 25/25 checks passed (100%).
- **Database Integrity Check**: SHA-256 match over all 9 columns of `vector_records` in `intellicache_loadtest` with 0 persistent mutations, verified against schema `public` and expected database.
- **Cache Reset Verification**: All 6 pre-trial database purges and administrative resets verified empty state before test initiation with strict type and field assertions.
