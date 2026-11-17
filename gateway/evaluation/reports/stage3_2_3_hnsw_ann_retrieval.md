# IntelliCache Stage 3.2.3 — HNSW vs Exact Retrieval Benchmark Report

**Generated:** `2026-11-17T21:56:50.143160+00:00`  
**PostgreSQL Version:** `16.16 (Debian 16.16-1.pgdg12+1)`  
**pgvector Extension:** `v0.8.7`  
**Embedding Model:** `BAAI/bge-small-en-v1.5` (`384` dimensions)  
**Corpus Size:** `2000` documents (`20` evaluation queries)  
**Corpus SHA-256:** `4b8300fdbdc62939...`  

## 1. Executive Summary & Retrieval Quality

| Metric | Target Gate | Measured Real FastEmbed (384-d) | Verdict |
| :--- | :---: | :---: | :---: |
| **Average Recall@1** | >= 0.85 | **100.00%** (min: 100.00%) | PASS |
| **Average Recall@5** | >= 0.90 | **100.00%** (min: 100.00%) | PASS |
| **Average Recall@10** | >= 0.90 | **100.00%** (min: 100.00%) | PASS |

## 2. Namespace & Selectivity Filtering Evaluation

| Scenario | Selectivity | Eligible Count | Returned Count | Plan Method | Recall@1 | Recall@5 | Recall@10 | Leakage |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| `large_partition_70pct` | 70.0% | 1400 | 10 | `HNSW approximate nearest-neighbour search` | 1.00 | 1.00 | 1.00 | NS: 0, Model: 0 |
| `moderate_partition_20pct` | 20.0% | 400 | 10 | `HNSW approximate nearest-neighbour search` | 1.00 | 1.00 | 1.00 | NS: 0, Model: 0 |
| `selective_partition_10pct` | 10.0% | 200 | 10 | `Exact filtered vector search (B-tree index / sequential scan with exact distance sort)` | 1.00 | 1.00 | 1.00 | NS: 0, Model: 0 |
| `highly_selective_1pct` | 1.0% | 20 | 10 | `Exact filtered vector search (B-tree index / sequential scan with exact distance sort)` | 1.00 | 1.00 | 1.00 | NS: 0, Model: 0 |

## 3. Database-Only Retrieval Latency Benchmarks (>= 30 samples)

| Retrieval Configuration | p50 Latency (ms) | p95 Latency (ms) | Mean Latency (ms) | Sample Count |
| :--- | :---: | :---: | :---: | :---: |
| `exact_sequential_scan` | **5.54 ms** | 7.53 ms | 5.64 ms | 30 |
| `hnsw_default` | **1.75 ms** | 2.22 ms | 1.80 ms | 30 |
| `hnsw_iterative_strict_order` | **0.67 ms** | 0.92 ms | 0.63 ms | 30 |
| `hnsw_iterative_relaxed_order` | **0.52 ms** | 1.07 ms | 0.59 ms | 30 |

## 4. Query-by-Query Real FastEmbed Evaluation

| QID | Topic | Technical Query | HNSW Used | Recall@1 | Recall@5 | Recall@10 |
| :---: | :--- | :--- | :---: | :---: | :---: | :---: |
| 1 | `Rust` | How does Rust manage memory ownership and borrowing wit... | YES | 1.00 | 1.00 | 1.00 |
| 2 | `Python` | Explain Python asyncio event loop tasks and non-blockin... | YES | 1.00 | 1.00 | 1.00 |
| 3 | `Go` | How do Go channels and goroutines implement concurrent ... | YES | 1.00 | 1.00 | 1.00 |
| 4 | `TypeScript` | How to use TypeScript discriminated unions for exhausti... | YES | 1.00 | 1.00 | 1.00 |
| 5 | `FastAPI` | FastAPI dependency injection with Depends and asynchron... | YES | 1.00 | 1.00 | 1.00 |
| 6 | `React` | How does React concurrent rendering and Server Componen... | YES | 1.00 | 1.00 | 1.00 |
| 7 | `PyTorch` | PyTorch autograd dynamic computation graph and tensor b... | YES | 1.00 | 1.00 | 1.00 |
| 8 | `Tokio` | How does Tokio work-stealing runtime schedule asynchron... | YES | 1.00 | 1.00 | 1.00 |
| 9 | `PostgreSQL` | PostgreSQL write-ahead logging WAL crash recovery and c... | YES | 1.00 | 1.00 | 1.00 |
| 10 | `pgvector` | pgvector HNSW index tuning with ef_search and m paramet... | YES | 1.00 | 1.00 | 1.00 |
| 11 | `SQLite` | How does SQLite write-ahead logging handle concurrent r... | YES | 1.00 | 1.00 | 1.00 |
| 12 | `DistributedStorage` | LSM-tree compaction and SSTable storage in distributed ... | YES | 1.00 | 1.00 | 1.00 |
| 13 | `Raft` | Raft consensus leader election randomized timers and lo... | YES | 1.00 | 1.00 | 1.00 |
| 14 | `SingleFlight` | SingleFlight pattern for suppressing duplicate concurre... | YES | 1.00 | 1.00 | 1.00 |
| 15 | `DistributedLocks` | Distributed lock implementation using Redlock vs ZooKee... | YES | 1.00 | 1.00 | 1.00 |
| 16 | `EventDriven` | Event-driven architecture with Kafka message ordering a... | YES | 1.00 | 1.00 | 1.00 |
| 17 | `HTTP2` | HTTP/2 multiplexed streams and HPACK header compression... | YES | 1.00 | 1.00 | 1.00 |
| 18 | `WebSockets` | WebSockets bidirectional full-duplex TCP communication ... | YES | 1.00 | 1.00 | 1.00 |
| 19 | `SSE` | Server-Sent Events SSE unidirectional streaming with La... | YES | 1.00 | 1.00 | 1.00 |
| 20 | `TLS13` | TLS 1.3 cryptographic handshake latency reduction and f... | YES | 1.00 | 1.00 | 1.00 |

## 5. Architectural Guardrail Preservation

- `SEMANTIC_CACHE_ENABLED`: `False` (Verified default preserved)
- `RUNTIME_SIMILARITY_THRESHOLD`: `0.92` (Preserved)
- `RUNTIME_AMBIGUITY_MARGIN`: `0.03` (Preserved)
- `Exact Cache Behaviour`: Independent and preserved
- `PostgreSQL Safety`: All planner settings transaction-scoped via `SET LOCAL` without connection leaks
