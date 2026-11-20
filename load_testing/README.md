# IntelliCache — Grafana k6 Load Testing Framework (Stage 3.4.1)

This directory contains the production-grade **Grafana k6 load-testing integration** for IntelliCache. It provides reproducible, Docker-based load and performance testing for exact caching, asynchronous shadow evaluation, concurrency handling, and system stability under load without mutating production cache entries or vector records.

---

## 1. Directory Structure

```text
load_testing/
├── README.md                          # Architecture, execution instructions, and safety guidelines
├── docker-compose.load-test.yml       # Isolated container environment (Gateway + Postgres + k6)
├── .env.load-test.example             # Safe environment configuration template with placeholders
├── config/
│   └── test-profiles.js               # Common k6 execution profiles, thresholds, and options
├── helpers/
│   ├── auth.js                        # Client and admin token header construction
│   ├── metrics.js                     # Custom k6 metrics (exact hits, misses, bypass, invariants)
│   └── assertions.js                  # Standardized response validation and invariant checks
├── datasets/
│   ├── smoke.json                     # Minimal verification dataset (seed, repeat, miss, bypass)
│   ├── baseline_workload.json         # Exact cache baseline dataset
│   ├── semantic_workload.json         # Semantic paraphrases, hard negatives, and ineligible queries
│   └── mixed_workload.json            # Realistic blend of exact hits, misses, paraphrases, and ineligibles
├── scripts/
│   ├── 00_smoke_test.js               # Phase 2: Gateway connectivity & contract smoke test
│   ├── 01_baseline_exact_cache.js     # Phase 4: Baseline performance (shadow disabled)
│   ├── 02_shadow_evaluation.js        # Phase 5: Shadow evaluation load test (sampling 10%, 50%, 100%)
│   ├── 03_concurrency_stress.js       # Phase 6: Concurrency bursts & SingleFlight stress test
│   ├── 04_failure_injection.js        # Phase 6: Error handling, bad payloads, and fail-open test
│   └── 05_soak_test.js                # Phase 7: Configurable long-running soak test (30m, 1h, 24h)
└── reports/
    └── stage_3_4_1_load_test_report.md# Formal test evidence and operational report
```

---

## 2. Docker-Based Execution (Using `grafana/k6:latest`)

All tests execute via the pre-pulled **`grafana/k6:latest`** Docker image. No local host installation of k6 is required.

### 2.1 Mode A: Host Gateway with Host Networking (Recommended for Local Dev)
When running the gateway locally (e.g. `uvicorn app.main:app --host 0.0.0.0 --port 8000`):

```bash
docker run --rm -i \
  --network host \
  -v "$(pwd)/load_testing:/load_testing" \
  -e BASE_URL="http://127.0.0.1:8000" \
  -e CLIENT_TOKEN="test-client-token" \
  -e ADMIN_TOKEN="test-admin-token" \
  grafana/k6:latest run /load_testing/scripts/00_smoke_test.js
```

### 2.2 Mode B: Isolated Docker Compose Environment
For a completely containerized test run:

```bash
# 1. Start isolated PostgreSQL with pgvector and Gateway
docker compose -f load_testing/docker-compose.load-test.yml up -d gateway

# 2. Run k6 container inside the isolated network
docker compose -f load_testing/docker-compose.load-test.yml run --rm \
  k6 run /load_testing/scripts/00_smoke_test.js

# 3. Teardown
docker compose -f load_testing/docker-compose.load-test.yml down -v
```

---

## 3. Workload Datasets & Expected Behaviour

| Dataset | File | Query Composition | Purpose |
| :--- | :--- | :--- | :--- |
| **Smoke** | `datasets/smoke.json` | 1 seed, 1 repeat, 1 miss, 1 bypass | Quick verification of endpoint contracts and invariants |
| **Baseline** | `datasets/baseline_workload.json` | 10 exact targets, 5 unique misses, 2 bypasses | Exact-cache baseline latency and hit rate measurement |
| **Semantic** | `datasets/semantic_workload.json` | 2 seeds, 2 paraphrases, 2 hard negatives, 4 ineligibles | Shadow evaluation decision quality and rejection verification |
| **Mixed** | `datasets/mixed_workload.json` | 4 exact hits, 2 paraphrases, 2 incompatibles, 2 misses, 1 ineligible, 1 bypass | Realistic production traffic simulation |

---

## 4. Test Execution Profiles & Invariant Gates

### Invariant Checks Enforced in All Tests:
- `http_req_failed < 1%`: HTTP 4xx/5xx error rate must remain under 1%.
- `semantic_hits_served == 0`: **CRITICAL PRODUCTION INVARIANT**. Semantic cache responses must NEVER be served to users.
- `http_req_duration P95 < 500ms`: P95 request duration must satisfy service thresholds.

---

## 5. Test Data Cleanup Procedure

- In-memory cache testing requires no cleanup (reset on process restart).
- PostgreSQL cache and vector records can be purged using the admin endpoint:
  ```bash
  curl -X POST http://127.0.0.1:8000/cache/clear \
    -H "X-Admin-Token: test-admin-token"
  ```
- Or using the dedicated cleanup script in `gateway/scripts/`:
  ```bash
  cd gateway && python scripts/backfill_shadow_candidates.py --cleanup
  ```
