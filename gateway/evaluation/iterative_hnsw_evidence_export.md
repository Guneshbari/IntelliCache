# Step 9.2.2 Hardening: Iterative HNSW Evidence Export & Reproducibility

## 1. Overview & Problem Definition

During high-dimensional vector search with PostgreSQL and pgvector, post-filtering by relational attributes (such as `cache_namespace` and `model`) can cause candidate starvation when using default approximate nearest-neighbour (ANN) indexing. When distractor records from unrelated namespaces or models are geometrically closer to the query vector than valid target records, the standard HNSW search budget (`ef_search`) can be completely exhausted exploring irrelevant candidates that are subsequently discarded by the `WHERE` filter.

In pgvector 0.8.0+, iterative index scanning (`SET LOCAL hnsw.iterative_scan = 'strict_order';`) addresses this limitation by continuing HNSW graph traversal past filtered records until the requested `LIMIT` is reached or configured scan bounds are hit.

This document records the empirical validation, plan inspection, and reproducible benchmarks comparing default retrieval (`off`) and iterative scanning (`strict_order`) in IntelliCache.

---

## 2. Experimental Setup & Workload Design

- **PostgreSQL Version**: 18.6
- **pgvector Version**: 0.8.7
- **Embedding Model**: `nomic-embed-text` (768 dimensions)
- **Dataset Size**: 5,000 unit-normalized float vectors
  - **Target Namespace (`test_iter_target_ns`)**: 3,800 vectors (76% table selectivity)
  - **Background Distractor Namespace (`test_iter_distractor_ns`)**: 1,200 vectors
- **Challenging Post-Filter Scenario**:
  - 50 distractor vectors were specifically perturbed to have cosine distance ~0.0002–0.0012 to the query vector.
  - Valid target namespace vectors have cosine distance ~0.0112 to the query vector.
  - At `ef_search` values of 10, 20, and 40, all nearest candidate slots in graph traversal are occupied by distractor vectors, testing whether the planner starves without iterative scanning.
- **Evaluation Settings**:
  - `ef_search` sweep: 10, 20, 40
  - Iteration count: 30 deterministic interleaved measurements per condition
  - Median Calculation: Python standard `statistics.median`
  - Statement Cache: Dedicated connection pool with `statement_cache_size=0` to eliminate prepared statement plan reuse across transaction-scoped GUC changes.
  - Baseline: Exact nearest-neighbour retrieval with index scans explicitly disabled (`SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;`).

---

## 3. Benchmark Results & Plan Verification

| `ef_search` | `hnsw.iterative_scan` | HNSW Selected | Plan Limit Rows | Returned Rows | Exact Overlap | Recall@5 | Recall@10 | Mean Latency | Median (p50) | p95 Latency |
|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|:---:|
| **10** | `off` (default) | Yes | 0.00 | 0/10 | 0/10 | 0.000 | 0.000 | 0.857 ms | 0.753 ms | 1.493 ms |
| **10** | `strict_order` | Yes | 10.00 | 10/10 | 10/10 | 1.000 | 1.000 | 1.040 ms | 0.843 ms | 2.025 ms |
| **20** | `off` (default) | Yes | 0.00 | 0/10 | 0/10 | 0.000 | 0.000 | 0.733 ms | 0.723 ms | 0.853 ms |
| **20** | `strict_order` | Yes | 10.00 | 10/10 | 10/10 | 1.000 | 1.000 | 0.810 ms | 0.802 ms | 0.872 ms |
| **40** | `off` (default) | Yes | 0.00 | 0/10 | 0/10 | 0.000 | 0.000 | 0.752 ms | 0.748 ms | 0.813 ms |
| **40** | `strict_order` | Yes | 10.00 | 10/10 | 10/10 | 1.000 | 1.000 | 0.937 ms | 0.927 ms | 1.024 ms |

### Key Findings

1. **Diagnostic Plan vs Retrieval Alignment**:
   - `EXPLAIN (ANALYZE, BUFFERS)` root `Limit` actual row counts match measured retrieval counts exactly across all configurations:
     - Under `off`: `Limit ... rows=0.00 loops=1` with `Rows Removed by Filter: 10, 20, 40` and inner cache join `never executed`.
     - Under `strict_order`: `Limit ... rows=10.00 loops=1` with `Rows Removed by Filter: 50` and inner cache join `loops=10`.
2. **Recall & Candidate Completeness**:
   - Default retrieval (`off`) suffers candidate starvation across all tested `ef_search` values (0 candidates returned, 0.0 Recall@5, 0.0 Recall@10).
   - Iterative scanning (`strict_order`) dynamically traverses past pruned distractor nodes, discovering all 10 target candidates (10/10 candidates returned, 10/10 exact top-K overlap, 1.0 Recall@5, 1.0 Recall@10).
3. **Latency & Quality Trade-Off**:
   - Default retrieval recorded lower median latencies (0.72–0.75 ms p50) solely because zero candidates survived post-filtering; the scan aborted without joining `cache_entries`.
   - Iterative scanning recorded 0.80–0.93 ms p50 because it successfully continued traversal to locate all 10 candidates and completed 10 nested-loop cache index lookups.
   - The lower latency of `off` represents a candidate starvation failure mode rather than a performance advantage.

---

## 4. Technical Scope & Scan Limits

- **Completeness Scope**: Iterative scanning can improve candidate completeness under post-filtering by continuing HNSW traversal, subject to configured scan limits; it is not a latency reduction optimization.
- **Planner Selection Boundary**: Where PostgreSQL's cost-based query planner chooses a B-tree index scan or sequential scan over HNSW graph traversal, iterative HNSW scanning is not applicable.
- **Configured Scan Bounds**: Iterative scans do not guarantee unlimited candidate completeness. In pgvector, scans continue until enough results are found or configured scan limits are reached:
  - `hnsw.max_scan_tuples` (default 20,000): Upper bound on tuples inspected during iterative scan.
  - `hnsw.scan_mem_multiplier` (default 1): Limits working memory for candidate tracking.

---

## 5. Machine-Readable Evidence Artifact

The machine-readable JSON artifact is tracked in the repository at [`gateway/evaluation/reports/iterative_hnsw_benchmark.json`](file:///home/gnx/Projects/IntelliCache/gateway/evaluation/reports/iterative_hnsw_benchmark.json).

```json
{
  "schema_version": "1.0",
  "generated_at": "2026-11-02T17:16:55.600598+00:00",
  "postgres_version": "18.6",
  "pgvector_version": "0.8.7",
  "dataset_size": 5000,
  "target_namespace_selectivity": 0.76,
  "embedding_model": "nomic-embed-text",
  "results": [
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 10,
      "hnsw.iterative_scan": "off",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=92.87..171.23 rows=10 width=22) (actual time=0.085..0.086 rows=0.00 loops=1)",
        "  Buffers: shared hit=157",
        "  ->  Nested Loop  (cost=92.87..29854.08 rows=3798 width=22) (actual time=0.085..0.085 rows=0.00 loops=1)",
        "        Buffers: shared hit=157",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=92.59..27196.50 rows=3800 width=52) (actual time=0.085..0.085 rows=0.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 10",
        "              Index Searches: 1",
        "              Buffers: shared hit=157",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (never executed)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 0",
        "Planning:",
        "  Buffers: shared hit=7"
      ],
      "returned_candidate_count": 0,
      "exact_candidate_overlap": 0,
      "exact_top_k_overlap": 0,
      "recall_at_5": 0.0,
      "recall_at_10": 0.0,
      "mean_latency_ms": 0.857,
      "p50_latency_ms": 0.753,
      "p95_latency_ms": 1.493,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=10, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    },
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 10,
      "hnsw.iterative_scan": "strict_order",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=92.87..171.23 rows=10 width=22) (actual time=0.283..0.327 rows=10.00 loops=1)",
        "  Buffers: shared hit=371",
        "  ->  Nested Loop  (cost=92.87..29854.08 rows=3798 width=22) (actual time=0.283..0.325 rows=10.00 loops=1)",
        "        Buffers: shared hit=371",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=92.59..27196.50 rows=3800 width=52) (actual time=0.261..0.263 rows=10.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 50",
        "              Index Searches: 1",
        "              Buffers: shared hit=311",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (actual time=0.004..0.004 rows=1.00 loops=10)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 10",
        "              Buffers: shared hit=30",
        "Planning:"
      ],
      "returned_candidate_count": 10,
      "exact_candidate_overlap": 10,
      "exact_top_k_overlap": 10,
      "recall_at_5": 1.0,
      "recall_at_10": 1.0,
      "mean_latency_ms": 1.04,
      "p50_latency_ms": 0.843,
      "p95_latency_ms": 2.025,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=10, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    },
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 20,
      "hnsw.iterative_scan": "off",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=93.67..172.03 rows=10 width=22) (actual time=0.090..0.090 rows=0.00 loops=1)",
        "  Buffers: shared hit=190",
        "  ->  Nested Loop  (cost=93.67..29854.08 rows=3798 width=22) (actual time=0.089..0.090 rows=0.00 loops=1)",
        "        Buffers: shared hit=190",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=93.39..27196.50 rows=3800 width=52) (actual time=0.089..0.089 rows=0.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 20",
        "              Index Searches: 1",
        "              Buffers: shared hit=190",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (never executed)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 0",
        "Planning:",
        "  Buffers: shared hit=7"
      ],
      "returned_candidate_count": 0,
      "exact_candidate_overlap": 0,
      "exact_top_k_overlap": 0,
      "recall_at_5": 0.0,
      "recall_at_10": 0.0,
      "mean_latency_ms": 0.733,
      "p50_latency_ms": 0.723,
      "p95_latency_ms": 0.853,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=20, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    },
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 20,
      "hnsw.iterative_scan": "strict_order",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=93.67..172.03 rows=10 width=22) (actual time=0.174..0.226 rows=10.00 loops=1)",
        "  Buffers: shared hit=371",
        "  ->  Nested Loop  (cost=93.67..29854.08 rows=3798 width=22) (actual time=0.174..0.225 rows=10.00 loops=1)",
        "        Buffers: shared hit=371",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=93.39..27196.50 rows=3800 width=52) (actual time=0.159..0.164 rows=10.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 50",
        "              Index Searches: 1",
        "              Buffers: shared hit=311",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (actual time=0.002..0.002 rows=1.00 loops=10)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 10",
        "              Buffers: shared hit=30",
        "Planning:"
      ],
      "returned_candidate_count": 10,
      "exact_candidate_overlap": 10,
      "exact_top_k_overlap": 10,
      "recall_at_5": 1.0,
      "recall_at_10": 1.0,
      "mean_latency_ms": 0.81,
      "p50_latency_ms": 0.802,
      "p95_latency_ms": 0.872,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=20, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    },
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 40,
      "hnsw.iterative_scan": "off",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=95.10..173.46 rows=10 width=22) (actual time=0.096..0.096 rows=0.00 loops=1)",
        "  Buffers: shared hit=219",
        "  ->  Nested Loop  (cost=95.10..29854.08 rows=3798 width=22) (actual time=0.095..0.096 rows=0.00 loops=1)",
        "        Buffers: shared hit=219",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=94.82..27196.50 rows=3800 width=52) (actual time=0.095..0.095 rows=0.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 40",
        "              Index Searches: 1",
        "              Buffers: shared hit=219",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (never executed)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 0",
        "Planning:",
        "  Buffers: shared hit=7"
      ],
      "returned_candidate_count": 0,
      "exact_candidate_overlap": 0,
      "exact_top_k_overlap": 0,
      "recall_at_5": 0.0,
      "recall_at_10": 0.0,
      "mean_latency_ms": 0.752,
      "p50_latency_ms": 0.748,
      "p95_latency_ms": 0.813,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=40, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    },
    {
      "postgres_version": "18.6",
      "pgvector_version": "0.8.7",
      "dataset_size": 5000,
      "namespace_selectivity": 0.76,
      "target_namespace_selectivity": 0.76,
      "embedding_model": "nomic-embed-text",
      "hnsw.ef_search": 40,
      "hnsw.iterative_scan": "strict_order",
      "execution_plan_confirmed": true,
      "execution_plan_summary": [
        "Limit  (cost=95.10..173.46 rows=10 width=22) (actual time=0.901..0.936 rows=10.00 loops=1)",
        "  Buffers: shared hit=830",
        "  ->  Nested Loop  (cost=95.10..29854.08 rows=3798 width=22) (actual time=0.901..0.935 rows=10.00 loops=1)",
        "        Buffers: shared hit=830",
        "        ->  Index Scan using idx_vector_records_embedding_hnsw on vector_records v  (cost=94.82..27196.50 rows=3800 width=52) (actual time=0.882..0.884 rows=10.00 loops=1)",
        "              Order By: (vector <=> [vector(...)])",
        "              Filter: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((model)::text = 'nomic-embed-text'::text))",
        "              Rows Removed by Filter: 50",
        "              Index Searches: 1",
        "              Buffers: shared hit=770",
        "        ->  Index Scan using idx_cache_lookup on cache_entries c  (cost=0.28..0.70 rows=1 width=34) (actual time=0.002..0.002 rows=1.00 loops=10)",
        "              Index Cond: (((cache_namespace)::text = 'test_iter_target_ns'::text) AND ((fingerprint)::text = (v.fingerprint)::text))",
        "              Filter: ((expires_at IS NULL) OR (expires_at > now()))",
        "              Index Searches: 10",
        "              Buffers: shared hit=30",
        "Planning:"
      ],
      "returned_candidate_count": 10,
      "exact_candidate_overlap": 10,
      "exact_top_k_overlap": 10,
      "recall_at_5": 1.0,
      "recall_at_10": 1.0,
      "mean_latency_ms": 0.937,
      "p50_latency_ms": 0.927,
      "p95_latency_ms": 1.024,
      "sample_count": 30,
      "recall_improvement_note": "At ef_search=40, default retrieval ('off') suffered complete candidate starvation (0 candidates returned, 0.00 Recall@10) because distractor candidates exhausted the search budget prior to post-filtering. Iterative scanning ('strict_order') successfully traversed past distractor vectors to achieve full recall (10/10 candidates returned, 1.00 Recall@10, 10/10 exact top-K overlap)."
    }
  ],
  "summary": {
    "workload_description": "Evaluated 5000 normalized 768-dimensional float vectors (3800 target namespace 'test_iter_target_ns', 1200 background namespace 'test_iter_distractor_ns'). The target namespace contains 76% of the dataset, so the workload is not globally highly selective by table partition. However, the distractor namespace contains 50 background vectors specifically perturbed to be much closer to the query vector (cosine distance ~0.0002–0.0012) than any target namespace vector (cosine distance ~0.0112). This creates a challenging post-filtering scenario.",
    "recall_conclusion": "Iterative scanning ('strict_order') demonstrated a decisive recall improvement over default retrieval ('off'). Across all tested ef_search values (10, 20, 40), default retrieval returned 0 candidates (0.0 Recall@5, 0.0 Recall@10) because the ef_search candidate budget was exhausted entirely on closer background distractor vectors that were pruned by the namespace filter. Iterative scanning continued graph traversal past filtered distractor vectors to retrieve all 10 target candidates, achieving full recall (1.0 Recall@5, 1.0 Recall@10, 10/10 exact top-K overlap) across all tested configurations. Diagnostic EXPLAIN ANALYZE row counts matched measured retrieval counts exactly across all configurations (0 rows for 'off', 10 rows for 'strict_order').",
    "latency_conclusion": "Latency measurements reflect the algorithmic trade-off: default retrieval ('off') terminates quickly after examining ef_search candidates without joining cache entries (since 0 candidates survive filtering), while iterative scanning ('strict_order') incurs additional graph traversal and 10 cache index lookups to find valid matching entries. Iterative scanning can improve candidate completeness under post-filtering by continuing HNSW traversal, subject to configured scan limits; it is not a latency reduction optimization."
  }
}
```
