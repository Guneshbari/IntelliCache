# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Primary: AI Engineers, Backend Engineers, and LLM Platform Architects who are building, evaluating, or deploying LLM-backed applications and seeking to reduce inference latency, eliminate API cost redundancy, prevent cache stampedes, and benchmark exact vs. semantic caching behavior.

Secondary: Enterprise Platform Administrators monitoring multi-tenant namespace usage, cache hit ratios, token savings, and single-flight coalescing metrics under production workloads.

## Product Purpose

IntelliCache intercepts LLM inference requests to provide ultra-low latency response reuse through exact-match SHA-256 caching and vector-similarity semantic caching with pgvector. It shields upstream LLM providers (e.g. Groq, Ollama) from redundant calls, coalesces concurrent identical requests with SingleFlight, and provides transparent, namespace-isolated performance telemetry.

Success means:

- Sub-millisecond exact cache retrieval and low-latency pgvector semantic retrieval.
- Zero cache stampedes during concurrent spikes via cancellation-safe SingleFlight coalescing.
- Complete tenant isolation with cryptographically verified HMAC client tokens.
- Measurable token and latency savings reflected in real-time telemetry.

## Positioning

Unlike basic key-value caches or opaque proxy solutions, IntelliCache combines:

1. Deterministic 3-tier prompt fingerprinting that accounts for all answer-affecting parameters.
2. Technical eligibility filtering for semantic cache candidates using pgvector HNSW cosine similarity, preventing hallucinated reuse on non-deterministic queries.
3. Cancellation-safe process-local SingleFlight execution that merges identical in-flight upstream requests into a single provider call without memory leaks or race conditions.
4. Hardened server-managed conversation lifecycle with atomic leasing, recovery fencing, and contiguity guarantees in PostgreSQL.

## Operating Context

- **Developer / Evaluator Workflow**: Developers interact with the IntelliCache Gateway via the high-contrast Neo-Brutalist web demonstration interface or direct REST API (`POST /query`, `GET /metrics/me`, `/conversations`).
- **Runtime Environment**: Python 3.11+ FastAPI gateway backed by PostgreSQL with the `pgvector` extension, and a vanilla TypeScript/Vite frontend client.

## Capabilities and Constraints

- **Exact-Match Cache**: In-memory and PostgreSQL LRU cache indexed by deterministic SHA-256 fingerprint.
- **Semantic Similarity Cache**: HNSW vector index using pgvector and Ollama/Nomic embeddings for single-turn technical prompts.
- **Concurrency Control**: Process-local SingleFlight coordinator coalescing concurrent requests.
- **Streaming & SSE**: Incremental token delivery via Server-Sent Events (`text/event-stream`) alongside standard JSON response payloads.
- **Persistent Conversation Management**: Server-managed conversation turns, status fencing (`pending`, `completed`, `failed`), atomic lease renewals, and heartbeat-guarded completion.
- **Client Security Hygiene**: Client API tokens and conversation state reside strictly in JavaScript memory closures—never written to `localStorage`, `sessionStorage`, `IndexedDB`, or cookies.
- **Multi-Tenant Isolation**: Constant-time HMAC client token verification mapping to isolated namespace metrics (`/metrics/me`) and database partitions.

## Brand Commitments

- **Aesthetic**: Technical Dark Neo-Brutalist design language with hard borders (`2px solid`), tactile offset shadows (`var(--shadow-sm)`, `var(--shadow-md)`), and monospace telemetry elements.
- **Palette**: Deep cyber dark background (`#0b0c10`), surface raised (`#1b1e2a`), signature IntelliCache Lime (`#b8ff00`), and semantic outcome color coding:
  - Exact Match: Emerald (`#10b981`)
  - Semantic Match: Cyan (`#38bdf8`)
  - Upstream Provider: Amber (`#f59e0b`)
  - Cache Bypass: Purple (`#c084fc`)
  - Error: Coral Red (`#f87171`)
- **Tone**: Authoritative, transparent, diagnostic, developer-focused, no marketing fluff.

## Evidence on Hand

- **Working Gateway Implementation**: `gateway/` contains full FastAPI implementation, comprehensive unit/integration test suite, and SQL migrations (`migrations/001_create_cache_entries.sql` through `004_conversation_fencing.sql`).
- **Frontend Web Application**: `frontend/` contains the full vanilla TypeScript frontend with robust Vitest test suites and zero mechanical defects.
- **Evaluation Dataset**: `gateway/evaluation/dataset.json` contains benchmark queries with verified semantic similarity recall tests.

## Product Principles

1. **Zero Trust in Client Storage**: Sensitive credentials (client API tokens) and temporary session keys must never touch persistent client storage (`localStorage`, cookies, IndexedDB).
2. **Transparent Cache Diagnostics**: Never synthesize or falsify cache outcomes; every response reports exact backend metadata (`cache_type`, `latency_ms`, `cache_hit_count`, `fingerprint`, `usage`).
3. **Concurrency-Safe Coalescing**: Protect upstream providers from stampedes using cancellation-safe SingleFlight deduplication so identical concurrent queries resolve to a single generation.
4. **Strict Multi-Tenant Isolation**: Ensure authenticated tenants can never inspect or contaminate another tenant's metrics, cache entries, or conversation turns.
5. **Deterministic State Fencing**: Manage multi-turn conversation states with atomic server-side leasing and idempotency keys to guarantee ledger consistency.

## Accessibility & Inclusion

- Adherence to WCAG 2.1 AA standards for color contrast across all UI surfaces (e.g. secondary text contrast ratio >= 5.5:1).
- Full keyboard operability with explicit `:focus-visible` rings (`var(--accent)`).
- Minimum 36px touch targets for mobile viewports (`<= 768px`).
- Native screen-reader semantic HTML tags with accessible ARIA live regions for streaming tokens and notifications.
- Support for `prefers-reduced-motion` media queries disabling animations and transitions.
