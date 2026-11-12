# IntelliCache Gateway

A hardened, modular, provider-independent Python backend and exact-match LLM caching proxy for **IntelliCache**. The Gateway intercepts LLM inference requests, conservatively normalizes queries, computes deterministic SHA-256 query fingerprints across all answer-affecting parameters, coordinates concurrent identical requests via cancellation-safe single-flight execution, and reuses cached responses to reduce inference latency and API costs.

---

## Architecture & Directory Structure

The backend is organized into decoupled layers where the caching core is strictly independent of web frameworks (FastAPI) and LLM vendor SDKs (Groq):

```
gateway/
├── app/
│   ├── api/                  # HTTP route handling, request/response conversion, dependency wiring
│   │   ├── __init__.py
│   │   ├── conversations.py  # /conversations CRUD and turn messages endpoints
│   │   └── routes.py         # /query endpoints, client auth, SSE streaming formatter, error mappers
│   ├── context/              # Bounded conversation context window reconstruction and budgeting
│   │   ├── __init__.py
│   │   ├── builder.py        # ContextBuilder (turns pairing, char/message bounds, state classification)
│   │   ├── contracts.py      # ContextStatus (complete, compacted, incomplete) and ContextBuildResult
│   │   └── policy.py         # ContextPolicy configuration and defaults
│   ├── conversations/        # Persistent conversation models, HMAC principal derivation, and repository
│   │   ├── __init__.py
│   │   ├── identity.py       # HMAC-SHA256 client principal derivation and auth dependencies
│   │   ├── models.py         # Conversation, turn, and message records and Pydantic schemas
│   │   ├── repository.py     # PostgresConversationRepository (turn atomicity, row locks, idempotency)
│   │   └── service.py        # ConversationService
│   ├── core/                 # Provider-independent caching & orchestration core (Zero FastAPI/Groq imports)
│   │   ├── __init__.py
│   │   ├── contracts.py      # Normalized generation requests, results, stream chunks, usage metrics
│   │   ├── errors.py         # ProviderException hierarchy and sensitive secret/path redaction
│   │   ├── fingerprinting.py # Exact text normalization and deterministic SHA-256 fingerprinting
│   │   ├── singleflight.py   # Cancellation-safe SingleFlightCoordinator
│   │   └── service.py        # IntelliCacheCore orchestrator
│   ├── cache/                # Cache interface and storage implementations
│   │   ├── __init__.py
│   │   ├── interfaces.py     # BaseCache abstract async interface, CacheEntry, CacheStats
│   │   ├── exact_cache.py    # LRU + TTL in-memory exact-match cache (ExactCache / QueryCache)
│   │   ├── postgres_cache.py # Persistent PostgreSQL-backed exact-match cache with LRU eviction
│   │   └── migration.py      # Automated SQL migration runner for asyncpg
│   ├── providers/            # Replaceable LLM provider adapters
│   │   ├── __init__.py
│   │   ├── interfaces.py     # LLMProvider abstract interface (generate, stream, close)
│   │   ├── factory.py        # ProviderFactory registry (LLM_PROVIDER selection)
│   │   └── groq_adapter.py   # Groq SDK adapter (isolated AsyncGroq mapping & lifecycle)
│   ├── embeddings/           # Provider-independent vector embeddings layer
│   │   ├── __init__.py
│   │   ├── contracts.py      # EmbeddingResult and EmbeddingTask definitions
│   │   ├── errors.py         # Embedding error hierarchy
│   │   ├── interfaces.py     # EmbeddingProvider abstract interface
│   │   ├── fastembed_adapter.py # Optional local FastEmbed (ONNX CPU) adapter
│   │   ├── ollama_adapter.py # Ollama embedding adapter with Nomic retrieval prefixes
│   │   └── factory.py        # EmbeddingProviderFactory
│   ├── vector_store/         # In-memory and PostgreSQL + pgvector vector storage
│   │   ├── __init__.py
│   │   ├── contracts.py      # VectorRecord, VectorSearchResult, and similarity formulas
│   │   ├── errors.py         # VectorStore error hierarchy
│   │   ├── interfaces.py     # BaseVectorStore interface
│   │   ├── in_memory_store.py # Bounded InMemoryVectorStore with dynamic pruning
│   │   ├── postgres_store.py # PostgresVectorStore implementation
│   │   └── migration.py      # Independent pgvector migration runner
│   ├── semantic_cache/       # Semantic caching policy, candidate validation, and orchestration
│   │   ├── __init__.py
│   │   ├── contracts.py      # EligibilityResult, SemanticHit, and SemanticCacheStats
│   │   ├── interfaces.py     # BaseSemanticCache interface
│   │   ├── policy.py         # Deterministic technical query eligibility & candidate compatibility
│   │   └── service.py        # SemanticCacheService (lookup, indexing, and failure safe-handling)
│   ├── metrics/              # Runtime metrics and observability layer (Step 11)
│   │   ├── __init__.py       # Singleton collector access and lifecycle
│   │   ├── contracts.py      # Pydantic schemas for namespace, admin, and latency metrics
│   │   └── collector.py      # Thread-safe, bounded-window, namespace-isolated RuntimeMetricsCollector
│   ├── routes/               # Backward compatibility facade for query routes
│   │   ├── __init__.py
│   │   └── query.py
│   ├── config.py             # Pydantic Settings, environment loader, and payload size bounds
│   └── main.py               # FastAPI application factory, lifespan, CORS, and health/admin routes
├── migrations/               # SQL migration scripts
│   ├── 001_create_cache_entries.sql
│   ├── 002_create_conversations.sql # Persistent conversation, turn, and message schema
│   └── vector/               # Vector store migrations (applied only when SEMANTIC_CACHE_ENABLED=true)
│       ├── 001_create_vector_records.sql
│       └── 002_create_hnsw_index.sql
├── tests/
│   ├── test_core.py          # Core orchestration tests using pure FakeProvider & AST import boundary tests
│   ├── test_context_builder.py # ContextBuilder budgeting, truncation, and status tests
│   ├── test_conversations.py # Persistent conversation CRUD, isolation, idempotency, and concurrency tests
│   ├── test_providers.py     # Groq adapter, client closure, and ProviderFactory tests
│   ├── test_embeddings.py    # Embedding contracts, Ollama adapter, prefixes, error tests
│   ├── test_vector_store.py  # Vector storage contracts, pgvector nearest-neighbour, cascade delete
│   ├── test_semantic_cache.py # Semantic policy, eligibility, candidate compatibility, ambiguity margin
│   ├── test_metrics.py       # Metrics collector math, percentiles, and schema verification (Step 11)
│   ├── test_metrics_integration.py # Multi-client metrics isolation, token sharing & admin aggregation
│   ├── test_query.py         # API route integration tests, streaming SSE, and exact caching
│   ├── test_multi_client.py  # Multi-client auth, namespace isolation, and PostgreSQL persistence tests
│   ├── test_config.py        # Settings validation, env overrides, and payload limits
│   └── test_main.py          # Root, health, CORS, and admin clear endpoint tests
├── .env                      # Local environment configuration (git-ignored)
├── .env.example              # Template environment configuration
├── .gitignore                # Python and environment ignore rules
├── requirements.txt          # Python package dependencies
└── README.md                 # Gateway documentation
```

---

## Layer Responsibilities

1. **API Layer (`gateway/app/api/`)**:
   - Converts inbound HTTP JSON bodies to provider-neutral `GenerationRequest` contracts.
   - Enforces character payload boundaries (`MAX_PROMPT_CHARS`, `MAX_MESSAGES_COUNT`).
   - Translates domain exceptions (`ProviderAuthenticationError`, `ProviderRateLimitError`, etc.) to HTTP status codes (401, 429, 502, 503, 500).
   - Formats streaming chunks as compliant Server-Sent Events (`text/event-stream`).

2. **IntelliCache Core (`gateway/app/core/`)**:
   - Owns exact-cache orchestration, fingerprinting, admission policies, and single-flight execution.
   - **Zero Vendor Dependencies**: Does not import FastAPI, Starlette, or the Groq SDK.
   - Operates entirely on provider-neutral contracts (`GenerationRequest`, `GenerationResult`, `StreamChunk`).
   - Verified via automated Abstract Syntax Tree (AST) boundary tests in `test_core.py`.

3. **Cache Storage Layer (`gateway/app/cache/`)**:
   - Exposes the asynchronous `BaseCache` interface (`get`, `peek`, `set`, `record_miss`, `record_coalesced_join`, `record_coalesced_success`, `clear`, `stats`).
   - Supports two interchangeable implementations:
     - `ExactCache`: In-memory LRU capacity eviction, non-mutating `peek` for vector candidate inspection, and TTL-based entry expiration for local test/dev.
     - `PostgresCache`: Persistent, centralized exact cache backed by PostgreSQL and `asyncpg` with bounded capacity, LRU eviction, and atomic access tracking.
   - Automated database schema migrations (`run_migrations`) executed during application startup.
   - Accurate request accounting: `total_requests = hits + misses + coalesced_requests`.

4. **Provider Adapter Layer (`gateway/app/providers/`)**:
   - Defines the `LLMProvider` contract (`generate`, `stream`, `close`).
   - `GroqProvider` encapsulates Groq SDK interactions, translates vendor exceptions, extracts token usage, and guarantees explicit `await client.close()` connection termination.
   - `ProviderFactory` allows runtime provider selection (`LLM_PROVIDER=groq`) and clean extension for future providers.

---

## Provider Selection & Extensibility

The active provider is chosen via configuration:

```dotenv
LLM_PROVIDER=groq
```

A new provider adapter can be registered dynamically without modifying core caching logic:

```python
from app.providers.factory import ProviderFactory
from app.providers.interfaces import LLMProvider

class CustomProvider(LLMProvider):
    async def generate(self, request): ...
    async def stream(self, request): ...
    async def close(self): ...

ProviderFactory.register("custom", CustomProvider)
```

---

## Correctness & Metric Guarantees

### 1. Consistent Model Identity (PRE-02)

The effective model identifier (e.g. `qwen/qwen3.8-27b` or custom casing) is resolved once. The exact same canonical string is used for the SHA-256 cache fingerprint payload and the upstream LLM request, eliminating any casing or alias discrepancies between cache keys and upstream inference.

### 2. Internally Consistent Single-Flight Metrics (PRE-01)

To eliminate duplicate "thundering herd" inference calls when multiple identical queries arrive simultaneously:

- **Leader Election**: The first request registers as the Leader and executes the provider generation.
- **Followers Join & Record Immediately**: Subsequent requests register as Followers and record their join immediately in `coalesced_requests`.
- **Cancellation Isolation**: Followers await `asyncio.shield(shared_future)` so a disconnecting follower cannot abort the leader.
- **Consistent Total Accounting**: Even if the leader encounters an upstream failure or cancellation, every request is counted exactly once:
  $$\text{total\_requests} = \text{hits} + \text{misses} + \text{coalesced\_requests}$$

### 3. Conservative Normalization & Strict Cache Bypass

- **Whitespace Preserved**: CRLF and CR are normalized to LF, and outer prompt whitespace is trimmed. Internal spaces, tabs, code indentation, and blank lines are preserved intact.
- **Strict Bypass (`bypass_cache=true`)**: Requests with `bypass_cache=true` bypass cache reads, never join single-flight groups, and never write or overwrite entries in the cache.

---

## Lazy Client Lifecycle & Offline Cache Availability

The provider client is lazily constructed only when an actual upstream inference call is required:

- **Offline / Credential-Free Cache Serving**: If an identical query exists in the cache, the response is served immediately without instantiating `AsyncGroq` and without requiring `GROQ_API_KEY` to be configured.
- **Resource Management**: When an upstream call is made (streaming or non-streaming), the HTTP client connection is explicitly closed (`await client.close()`) in a `finally` block to prevent lingering sockets.

---

## Centralized Architecture & Multi-Client Topology

In a centralized deployment, multiple client machines communicate with a single central IntelliCache API service over HTTP or HTTPS:

```
[ Client Application 1 ] ---\
                            +---> [ Central IntelliCache API Server ] ===> [ PostgreSQL (Persistent Cache) ]
[ Client Application 2 ] ---/          (FastAPI Gateway)                            ||
                                              ||                                   ||
                                              \/                                   \/
                                    [ Groq LLM Provider ]                 (Authoritative Shared Store)
```

### Key Architectural Principles:

1. **Direct Database Isolation**: Clients **never** connect directly to PostgreSQL. All read, write, eviction, and single-flight operations are managed exclusively by the central IntelliCache service.
2. **PostgreSQL Persistence & asyncpg Pooling**: Persistent exact caching is handled by `PostgresCache` using asynchronous, non-blocking `asyncpg` connection pools (`DB_POOL_MIN_SIZE` to `DB_POOL_MAX_SIZE`), with automatic lifespan cleanup on shutdown. Persistence tests verify data reusability across fresh database connection pools within the application lifecycle.
3. **Database Migrations**: SQL schema migrations are versioned under `gateway/migrations/` and automatically applied at startup via `gateway/app/cache/migration.py`.
   - `001_create_cache_entries.sql`: Creates `cache_entries` with a unique constraint on `(cache_namespace, fingerprint)` for conflict-safe concurrent upserts, plus indexes for lookups, expiration sweeps, and LRU eviction.
4. **Server-Enforced Cache Namespaces**: Cache isolation (e.g. between development, staging, or tenant teams) is strictly enforced by the server's authenticated token-to-namespace mapping. The gateway ignores any client payload claiming a custom namespace, preventing unauthorized cross-tenant cache access.
5. **Multi-Client Authentication & Fail-Closed Security**:
   - **Default Requirement**: All query endpoints (`POST /query`, `POST /api/v1/query`) require authentication by default. Missing or invalid credentials return `401 Unauthorized` without calling the LLM provider or writing to cache.
   - **Multi-Client Configuration (`CLIENT_API_TOKENS`)**: Supports multi-tenant deployments by mapping unique client credentials to designated namespaces. Accepts either JSON objects (`{"token_a": "team_alpha", "token_b": "team_beta"}`) or delimiter-separated pairs (`token_a:team_alpha,token_b:team_beta`).
   - **Authoritative Precedence**: When `CLIENT_API_TOKENS` is configured, it is strictly authoritative. Legacy `CLIENT_API_TOKEN` is ignored (with a non-sensitive warning emitted). An explicitly empty `CLIENT_API_TOKENS=""` fails configuration validation and never falls back.
   - **Legacy Single-Client Fallback**: If `CLIENT_API_TOKENS` is absent, the gateway supports single-token `CLIENT_API_TOKEN`, mapping to `CACHE_NAMESPACE`.
   - **Loopback Development Mode (`ALLOW_UNAUTHENTICATED_LOOPBACK`)**: Disabled by default (`false`). When explicitly enabled for local development, the gateway inspects both the direct TCP peer address (`request.client.host`) and the actual ASGI server bind address (`request.scope["server"]`), failing closed if either is non-loopback. Any request presenting forwarded headers (`X-Forwarded-For`, `Forwarded`) is rejected with `401 Unauthorized`. This mode must **never** be enabled behind a publicly accessible reverse proxy.
6. **Health Diagnostics (`GET /health`)**:
   - Performs a lightweight `SELECT 1` check when the PostgreSQL backend is enabled to verify live database connectivity.
   - If PostgreSQL is unreachable, the endpoint returns HTTP 503 Service Unavailable with `database: "unhealthy"`.
   - Database credentials, passwords, and sensitive connection strings are never exposed in health output or error responses.

---

## Configuration & Security

### Environment Settings (`.env`)

```dotenv
# LLM Provider Configuration
LLM_PROVIDER=groq
GROQ_API_KEY=gsk_your_actual_groq_api_key_here
GROQ_MODEL=qwen/qwen3.8-27b

# Server Configuration (Optional)
HOST=127.0.0.1
PORT=8000
DEBUG=false
CORS_ORIGINS=*
ADMIN_API_TOKEN=replace_with_admin_secret_token

# Client Authentication & Multi-Tenancy
# Option A: Multi-Client Token Mapping (Authoritative)
CLIENT_API_TOKENS={"client_token_a": "team_alpha", "client_token_b": "team_alpha", "client_token_c": "team_gamma"}
# Delimiter alternative: CLIENT_API_TOKENS=client_token_a:team_alpha,client_token_b:team_alpha,client_token_c:team_gamma

# Option B: Legacy Single Client Token (Used only if CLIENT_API_TOKENS is absent)
# CLIENT_API_TOKEN=replace_with_client_secret_token

# Local Development Exception (Disabled by default; requires direct loopback connection)
ALLOW_UNAUTHENTICATED_LOOPBACK=false

# Caching Configuration
# Backend: "memory" (default for local test/dev) or "postgres" (for centralized shared cache)
CACHE_BACKEND=memory
DATABASE_URL=postgresql://user:password@localhost:5432/intellicache
CACHE_NAMESPACE=default
DB_POOL_MIN_SIZE=1
DB_POOL_MAX_SIZE=10
CACHE_ENABLED=true
CACHE_TTL_SECONDS=3600
CACHE_MAX_ENTRIES=1000

# Inference Defaults (Optional)
DEFAULT_TEMPERATURE=0.7

# Input Payload Size Bounds (Character limits, distinct from token limits)
MAX_PROMPT_CHARS=131072
MAX_SYSTEM_PROMPT_CHARS=65536
MAX_MESSAGES_COUNT=100
MAX_MESSAGE_CHARS=131072

# Embedding Configuration (Optional - prepares for future semantic retrieval)
EMBEDDING_PROVIDER=ollama
EMBEDDING_MODEL=nomic-embed-text
EMBEDDING_BASE_URL=http://127.0.0.1:11434
EMBEDDING_TIMEOUT_SECONDS=30.0
```

### Security Considerations

- **Client Authentication (`POST /query` & `POST /api/v1/query`)**: Protected by `CLIENT_API_TOKENS` (or legacy `CLIENT_API_TOKEN`) via `X-Client-Token` or `Authorization: Bearer <token>`. Constant-time comparison (`hmac.compare_digest`) prevents timing side-channel attacks.
- **Fail-Closed Protection**: Unauthenticated requests receive HTTP 401 by default. Even when `ALLOW_UNAUTHENTICATED_LOOPBACK=true` is set, external peers, Uvicorn `--host 0.0.0.0` bindings, and forwarded headers are strictly rejected.
- **Admin Endpoint Protection (`POST /cache/clear`)**: Administrative operations require `ADMIN_API_TOKEN` to be set on the server. Clients must supply the token via `X-Admin-Token: <token>` or `Authorization: Bearer <token>`. Requests are validated using constant-time comparison (`hmac.compare_digest`). If unset or invalid, the endpoint responds with HTTP 403 Forbidden.
- **Credential Storage & Rotation**: Real client API tokens and database passwords must **never** be committed to Git or logged in plaintext. Token rotation can be achieved by updating `CLIENT_API_TOKENS` and reloading gateway settings.
- **Secret & Path Redaction**: Upstream error messages and logs automatically redact Groq API keys (`gsk_***`), Bearer tokens, absolute filesystem paths (`<redacted_path>`), and IP addresses (`<redacted_ip>`).
- **Payload Size Limits**: Inbound payload character limits protect gateway memory against denial-of-service payloads.

---

## Embedding Layer & Ollama Setup

IntelliCache includes a provider-independent embedding abstraction in `gateway/app/embeddings/` designed for future semantic retrieval. The embedding layer defines the `EmbeddingProvider` interface and normalizes outputs into `EmbeddingResult` vectors while validating dimensionality and finite numeric values.

### Local Ollama Setup:

1. **Install Ollama**: Follow instructions for your platform at [ollama.com](https://ollama.com).
2. **Download Model**: Pull the recommended Nomic Embed Text model:
   ```bash
   ollama pull nomic-embed-text
   ```
3. **Verify Local Service**: Confirm the embedding endpoint is responsive:
   ```bash
   curl http://127.0.0.1:11434/api/embed -d '{"model": "nomic-embed-text", "input": "test embedding"}'
   ```
4. **Asymmetric Task Prefixes**:
   Nomic models require asymmetric task prefixes: `search_query: ` for search queries and `search_document: ` for stored documents. The `OllamaEmbeddingProvider` automatically encapsulates and formats these prefixes according to the requested task (`task="query"` vs `task="document"`).
5. **Zero Test Dependency**: Ollama is **not** required to be running for standard unit testing or gateway execution. Gateway unit tests mock the HTTP transport.

---

## Vector Storage & pgvector Setup

IntelliCache includes an optional, provider-independent vector storage layer in `gateway/app/vector_store/` backed by PostgreSQL and the `pgvector` extension.

### Configuration

```bash
# Enable semantic vector storage (default: false)
SEMANTIC_CACHE_ENABLED=false

# Expected vector dimension (default: 768 for Nomic Embed Text)
VECTOR_DIMENSIONS=768
```

### Exact-Only Preservation

- `SEMANTIC_CACHE_ENABLED=false` by default. When disabled, the gateway operates as an exact-only cache and does not require `pgvector` to be installed on PostgreSQL.
- Vector migrations (`migrations/vector/001_create_vector_records.sql`, `migrations/vector/002_create_hnsw_index.sql`) are isolated from exact-cache migrations and are executed only when `SEMANTIC_CACHE_ENABLED=true`.
- If `SEMANTIC_CACHE_ENABLED=true` but `pgvector` is not installed on the database server, the server fails during startup with a descriptive error.

### HNSW Approximate Nearest-Neighbour (ANN) Indexing

- **Index Migration**: [`002_create_hnsw_index.sql`](file:///home/gnx/Projects/IntelliCache/gateway/migrations/vector/002_create_hnsw_index.sql) establishes an HNSW index on `vector_records(vector vector_cosine_ops)` configured with `m = 16, ef_construction = 64`.
- **Query Execution Plan Inspection**: `PostgresVectorStore.explain_search()` executes `EXPLAIN (ANALYZE, BUFFERS)` to verify whether the query planner selected the HNSW index scan vs exact B-tree/seq scan, with sensitive embedding coordinates sanitized in summaries (`[vector(...)]`).
- **Index Availability vs Index Usage**: Reports clearly distinguish `vector_index_available` (the HNSW index exists on disk) from `vector_index_used` (whether the query planner actually chose to use HNSW for a given query workload).
- **Cost-Based Query Planner Selection**: The crossover between HNSW graph traversal and B-tree/sequential scans is data- and cost-dependent (not a universal constant row count). In tested environments, PostgreSQL's planner selected `Index Scan using idx_vector_records_embedding_hnsw` when the target namespace comprised a large fraction of the table (~4,000 of 5,200 rows), while selecting `idx_vector_records_ns_model` B-tree index or sequential scan when queries targeted selective namespaces (~50 rows) because B-tree index lookup and heapsort incurred lower cost than traversing the HNSW graph with post-filtering.
- **Iterative Scan Under Selective Filtering**: In pgvector 0.8+, `hnsw.iterative_scan` can be enabled locally (`SET LOCAL hnsw.iterative_scan = 'strict_order';`) within a transaction to maintain strict distance ordering across filtered partitions without leaking settings to subsequent transactions.
  - **Empirical Evaluation Results**: Evaluated in a controlled workload of 5,000 normalized 768-dimensional float vectors (3,800 in target namespace `test_iter_target_ns`, 1,200 background distractor vectors). While the target namespace comprises 76% of table rows (not globally selective table-wide), 50 distractor vectors were specifically perturbed to be much closer to the query vector (cosine distance ~0.0002–0.0012) than any target candidate (cosine distance ~0.0112), creating a challenging post-filtering test comparing default (`off`) and iterative (`strict_order`) modes across `ef_search` values 10, 20, and 40 with 30 deterministic interleaved measurements:
    - **Recall & Overlap**: Default retrieval (`off`) suffered complete candidate starvation across all tested `ef_search` settings, returning 0 candidates (`Recall@5 = 0.0, Recall@10 = 0.0, 0/10` top-K overlap) because all `ef_search` nearest candidates explored were distractor vectors discarded by the namespace filter (`Rows Removed by Filter: 10, 20, 40`). Iterative scanning (`strict_order`) successfully traversed past filtered distractor nodes to achieve full recall (`1.0 Recall@5, 1.0 Recall@10, 10/10` exact top-K overlap).
    - **Execution Plan Alignment**: Diagnostic `EXPLAIN (ANALYZE, BUFFERS)` actual row counts matched measured retrieval counts exactly across all configurations: the root `Limit` node returned `rows=0.00` and the cache table join was `never executed` for `off`, whereas `strict_order` returned `rows=10.00` with 10 cache index loops.
    - **Latency & Retrieval Trade-off**: Default retrieval recorded lower median latencies (0.72–0.75ms p50) solely because it suffered complete candidate starvation, halting traversal after inspecting `ef_search` nodes and returning zero candidates (0.0 Recall@10) without performing cache table joins. In contrast, iterative scanning recorded 0.80–0.93ms p50 because it successfully retrieved all 10 matching candidates (1.0 Recall@10) by continuing graph traversal and executing 10 nested-loop cache index lookups. The lower latency of `off` represents a candidate starvation failure mode rather than a performance advantage.
    - **Conclusion & Scan Limits**: Iterative scanning can improve candidate completeness under post-filtering by continuing HNSW traversal, subject to configured scan limits; it is not a latency reduction optimization. Where PostgreSQL selects a B-tree or sequential scan, iterative HNSW scanning is not applicable. Furthermore, iterative scans continue until enough results are found or configured scan limits are reached; they do not guarantee unlimited candidate completeness (relevant limits: `hnsw.max_scan_tuples` and `hnsw.scan_mem_multiplier`).
  - **Tracked Evidence Artifact**: Results are exported and tracked in the repository at [`gateway/evaluation/reports/iterative_hnsw_benchmark.json`](file:///home/gnx/Projects/IntelliCache/gateway/evaluation/reports/iterative_hnsw_benchmark.json) (`schema_version: "1.0"`) and formatted with plan summaries in [`gateway/evaluation/iterative_hnsw_evidence_export.md`](file:///home/gnx/Projects/IntelliCache/gateway/evaluation/iterative_hnsw_evidence_export.md), capturing environment metadata, execution plan summaries, recall, candidate counts, and latency percentiles.
- **Deterministic Recall Validation**: Evaluated against 5,000+ diverse, normalized 768-dimensional float vectors generated from a fixed seed with non-degenerate cosine distance spread. Top-5 and top-10 nearest neighbours achieved Recall@5 = 1.0 and Recall@10 = 1.0 against a controlled exact nearest-neighbour baseline (`SET LOCAL enable_indexscan = off; SET LOCAL enable_bitmapscan = off;`).

### Distance & Similarity Formulas

PostgreSQL `pgvector` uses the cosine distance operator `<=>`:
$$\text{distance} = 1 - \frac{u \cdot v}{\|u\| \|v\|}$$
Cosine similarity is computed from the distance as:
$$\text{similarity} = 1.0 - \text{distance} \quad (\text{clamped to } [-1.0, 1.0])$$

### Re-Embedding & Migration Notice

Vector dimensions are enforced at the database table level (`vector(768)`). If you change the embedding model or vector dimensions in the future:

- PostgreSQL will reject vectors of different dimensions.
- You must perform a deliberate database migration and re-embed existing cache entries.

---

## Semantic Cache Pipeline & Policy

IntelliCache includes a deterministic semantic caching layer in `gateway/app/semantic_cache/`. It operates between exact cache lookups and LLM provider calls for eligible, non-streaming programming queries.

### Pipeline Execution Order

1. **Exact-Cache Lookup**: Checks SHA-256 exact fingerprint. Returns immediately on hit (`cache_type="exact"`).
2. **Eligibility Evaluation**: If exact miss, checks query against deterministic single-turn programming eligibility and sensitive credential detection.
3. **Semantic Lookup**: Generates query embedding via the configured `EmbeddingProvider` (`fastembed` or `ollama`) and searches nearest neighbours via the configured `BaseVectorStore` (`InMemoryVectorStore` or `PostgresVectorStore`).
4. **Candidate Verification**: Filters candidates by namespace, model, temperature, max_tokens, similarity threshold ($\ge 0.92$), and ambiguity margin ($\ge 0.03$). Candidate inspection uses non-mutating `peek` on the exact cache to preserve LRU recency and prevent hit metric inflation.
5. **Semantic Hit**: Returns cached answer without calling LLM (`cache_type="semantic"`). Semantic hits are not recursively indexed under new fingerprints.
6. **Provider Fallback**: If semantic miss or error, invokes configured LLM provider (`cache_type="none"`).
7. **Safe Indexing**: Writes exact-cache entry first, then indexes vector record in vector store. Indexing failures fail safely and do not fail the query.

### Supported Operating Modes

IntelliCache supports two cohesive operational modes for semantic caching:

1. **Local In-Memory Mode (Zero Daemons / Local Development)**:
   - `CACHE_BACKEND=memory`
   - `VECTOR_STORE_BACKEND=memory`
   - `SEMANTIC_CACHE_ENABLED=true`
   - `EMBEDDING_PROVIDER=fastembed` (default model: `BAAI/bge-small-en-v1.5`, 384 dimensions)
   - `VECTOR_STORE_MAX_ENTRIES=1000`
   - In-memory vector store with bounded FIFO eviction, dynamic orphaned entry pruning, and in-process CPU embedding generation via ONNX Runtime. Install FastEmbed via `pip install -r requirements-fastembed.txt`.

2. **Persistent PostgreSQL Mode (Production / Shared Instances)**:
   - `CACHE_BACKEND=postgres`
   - `VECTOR_STORE_BACKEND=postgres`
   - `DATABASE_URL=postgresql://user:password@localhost:5432/intellicache`
   - `SEMANTIC_CACHE_ENABLED=true`
   - `EMBEDDING_PROVIDER=ollama` (default model: `nomic-embed-text`, 768 dimensions) or `fastembed`
   - Distributed persistence backed by PostgreSQL 16+ with `pgvector` HNSW cosine indexing.

### Configuration

```bash
SEMANTIC_CACHE_ENABLED=false           # Disabled by default
CACHE_BACKEND=memory                   # "memory" or "postgres"
VECTOR_STORE_BACKEND=memory            # "memory" or "postgres" (must match CACHE_BACKEND)
VECTOR_STORE_MAX_ENTRIES=1000          # Maximum vector records in memory store
EMBEDDING_PROVIDER=fastembed           # "fastembed" or "ollama"
EMBEDDING_MODEL=BAAI/bge-small-en-v1.5 # BAAI/bge-small-en-v1.5 (FastEmbed) or nomic-embed-text (Ollama)
VECTOR_DIMENSIONS=384                  # 384 for bge-small, 768 for nomic-embed-text
SEMANTIC_CACHE_MIN_SIMILARITY=0.92     # Minimum cosine similarity threshold
SEMANTIC_CACHE_MIN_MARGIN=0.03         # Minimum margin required between top candidate and alternative candidate
SEMANTIC_CACHE_TOP_K=5                 # Nearest neighbour candidates evaluated
```

### Observability Counters

Available at `GET /cache/semantic/stats` and within `GET /cache/stats`:

- `lookups`: Total semantic lookup attempts.
- `hits`: Successful semantic cache hits.
- `misses`: Lookups where no candidate passed similarity or compatibility checks.
- `rejected_candidates`: Candidates rejected due to metadata, provider, or temperature mismatches.
- `ambiguous_candidates`: Candidates rejected because an alternative candidate with a different answer fell within the margin.
- `eligibility_rejections`: Queries rejected from semantic caching (multi-turn, non-technical, secrets).
- `embedding_failures`: Embedding provider timeouts or connection failures.
- `vector_store_failures`: Database retrieval errors.
- `indexing_successes`: Successfully persisted vector records.
- `indexing_failures`: Safely caught indexing errors.

---

## Cancellation-Safe SingleFlight Coordination (Step 11.2)

IntelliCache implements a cancellation-safe, process-local request coalescing coordinator in [`gateway/app/core/singleflight.py`](file:///home/gnx/Projects/IntelliCache/gateway/app/core/singleflight.py). SingleFlight ensures that simultaneous identical cache misses within the same tenant namespace execute exactly one upstream provider generation, sharing the resulting response among all waiting followers.

### Concurrency & Cancellation Model

1. **Namespace-Isolated Flight Keys**:
   - SingleFlight keys use the composite tuple `(namespace, fingerprint)`.
   - Identical queries from distinct tenant namespaces execute independently without coalescing or cross-tenant cache contamination.
2. **Deterministic Leader Lifecycle**:
   - The first request to arrive for a `(namespace, fingerprint)` key registers as the leader; concurrent identical requests register as followers and await the leader's shielded future (`asyncio.shield(shared_future)`).
   - Every registered leader is guaranteed to complete its shared future on all exit paths: success, failure, or cancellation. No abandoned futures remain in `_in_flight`.
3. **Controlled Follower Resolution on Leader Cancellation**:
   - If a leader task is cancelled (e.g. client disconnect during embedding generation, vector search, or provider inference), the coordinator completes the shared future with `SingleFlightLeaderCancelledError` rather than silently cancelling unrelated follower tasks.
   - Followers catch `SingleFlightLeaderCancelledError`, record a coalesced failure (`coalesced_failures`), and the gateway returns a controlled `503 Service Unavailable` response advising client retry.
   - Followers are never counted as cancelled requests because their client connections remained intact.
4. **Follower Cancellation Isolation**:
   - Followers await the leader's shared future wrapped in `asyncio.shield`. If a follower client disconnects or is cancelled, only that follower coroutine exits. The leader continues executing unhindered and persists its completed result to cache.
   - The cancelled follower increments `request_cancellations` without affecting provider metrics or corrupting the in-flight coordinator.
5. **Immediate Follower Unblocking**:
   - When the upstream provider returns a generated answer, the leader immediately resolves the SingleFlight future for waiting followers before initiating exact-cache database writes (`cache.set`) or post-generation vector indexing (`semantic_cache.index`).
   - Followers receive their response instantly without waiting for disk or database I/O. Subsequent leader cancellation during cache writes does not impact already-completed followers.
6. **Cancellation Metric Separation**:
   - Client request cancellations are tracked in a dedicated `request_cancellations` metric counter and are strictly excluded from upstream provider failures (`provider_calls_failed`).
7. **Process-Local Boundary**:
   - SingleFlight operates strictly within the current Python asyncio event loop and process. Multi-worker deployments (e.g. multiple Uvicorn worker processes) do not coalesce requests across worker boundaries.

---

---

## Runtime Metrics & Observability (Step 11)

IntelliCache incorporates an in-memory, thread-safe, and namespace-isolated metrics collection layer in [`gateway/app/metrics/`](file:///home/gnx/Projects/IntelliCache/gateway/app/metrics/). The metrics engine captures fine-grained runtime telemetry across exact caching, semantic caching, SingleFlight request coalescing, upstream provider execution, token savings, and bounded-window latency distributions.

### Architecture & Security Model

1. **Namespace Isolation**:
   - Every metric counter and latency sample is partitioned by the client's authenticated cache namespace (`NamespaceMetricsSnapshot`).
   - Clients authenticated via `CLIENT_API_TOKENS` can only access metrics for their mapped namespace (`GET /metrics/me`).
   - Requests from unrelated namespaces cannot inspect or deduce another client's request volume, cache hit rates, or token savings.
2. **Role-Based Access Control**:
   - `GET /metrics/me`: Requires a valid client token (`X-Client-Token` or Bearer header). Returns the caller's namespace metrics snapshot.
   - `GET /admin/metrics`: Requires a valid admin token (`X-Admin-Token` or Bearer header). Returns aggregate gateway metrics and a map of per-namespace snapshots.
   - `GET /cache/stats` and `GET /cache/semantic/stats`: Protected by `verify_metrics_auth`, returning namespace-scoped data for clients and global/unrestricted data for administrators.
   - Unauthorized requests receive `401 Unauthorized` (or `403 Forbidden` if an invalid admin token is presented). Authentication failures are never counted as cache misses or provider attempts.
3. **Fail-Open & Non-Blocking Design**:
   - All metrics recording operations are executed within non-blocking, exception-shielded facades. Any unexpected error in the metrics collector fails open, ensuring client LLM inference is never interrupted or failed by observability overhead.
4. **Bounded Rolling Latencies**:
   - Latencies are recorded in bounded ring buffers (`collections.deque(maxlen=1000)`) per metric and per namespace to eliminate unbounded memory growth.
   - Percentiles (`p50`, `p95`, `p99`) are computed via deterministic linear interpolation over the active window.
   - A `small_sample_warning` boolean flag is set to `true` whenever a metric's sample count is below 20.

### Metric Categories & Contracts

#### 1. Request & Cache Lifecycle (`RequestAndCacheMetrics`)

- `total_authenticated_requests`: Total valid requests admitted to the gateway.
- `exact_cache_lookups`: Exact-cache lookup attempts.
- `exact_cache_hits`: Requests served immediately from exact cache.
- `exact_cache_misses`: Independent requests that missed exact cache.
- `cache_write_failures`: Failures encountered while persisting entries to the exact cache store.
- `coalesced_joins`: Requests that joined an in-flight single-flight group.
- `coalesced_successes`: Followers that successfully received the leader's generated response.
- `coalesced_failures`: Followers that received an error due to leader failure.
- `request_cancellations`: Total requests cancelled by clients (client disconnects or cancelled coroutines). Strictly observable and never counted as provider failures.
- `cache_bypass_requests`: Requests with `bypass_cache=true` that deliberately skipped caching.
- `streaming_requests`: Streaming inference requests (`stream=true`).

#### 2. Semantic Cache Metrics (`SemanticMetrics`)

- `semantic_eligibility_rejections`: Queries rejected prior to embedding (multi-turn, non-technical, secrets).
- `semantic_lookups`: Vector similarity search attempts.
- `semantic_hits`: Valid semantic candidate reuses.
- `semantic_misses`: Semantic searches yielding no candidate meeting similarity and margin thresholds.
- `ambiguous_candidate_rejections`: Rejections where an alternative candidate fell within the ambiguity margin.
- `incompatible_candidate_rejections`: Rejections due to model, temperature, or namespace mismatch.
- `embedding_failures`: Timeout or connection failures during embedding generation.
- `vector_store_failures`: Database errors during vector retrieval.
- `indexing_successes`: Successfully persisted vector records.
- `indexing_failures`: Safely caught vector indexing errors.

#### 3. Upstream Provider & Savings (`ProviderAndSavingsMetrics`)

- `provider_calls_attempted`: Inferences dispatched to the upstream provider (Groq).
- `provider_calls_succeeded`: Completed upstream provider generations.
- `provider_calls_failed`: Upstream failures (rate limits, auth errors, network disconnects).
- `provider_prompt_tokens_used`, `provider_completion_tokens_used`, `provider_total_tokens_used`: Actual tokens billed by the upstream provider.
- `prompt_tokens_saved`, `completion_tokens_saved`, `total_tokens_saved`: Cumulative tokens saved across exact hits, semantic hits, and successful coalesced followers.
- `provider_calls_avoided`: Cumulative upstream provider calls saved without double-counting:
  $$\text{provider\_calls\_avoided} = \text{exact\_cache\_hits} + \text{semantic\_hits} + \text{coalesced\_successes}$$

#### 4. Derived Rates (`DerivedRates`)

- $\text{overall\_cache\_hit\_rate} = \frac{\text{exact\_cache\_hits} + \text{semantic\_hits} + \text{coalesced\_successes}}{\text{total\_authenticated\_requests}}$
- $\text{exact\_hit\_rate} = \frac{\text{exact\_cache\_hits}}{\text{exact\_cache\_lookups}}$
- $\text{semantic\_hit\_rate} = \frac{\text{semantic\_hits}}{\text{semantic\_hits} + \text{semantic\_misses}}$ (over evaluated candidate outcomes)
- $\text{coalesced\_reuse\_rate} = \frac{\text{coalesced\_successes}}{\text{coalesced\_joins}}$

#### 5. Latency Distributions (`LatencyMetrics`)

Each latency metric provides a `LatencySummary` containing `sample_count`, `min`, `mean`, `p50`, `p95`, `p99`, and `small_sample_warning`:

- `total_request_latency_ms`: End-to-end gateway response latency.
- `exact_lookup_latency_ms`: In-memory or PostgreSQL exact cache lookup time.
- `semantic_lookup_latency_ms`: Total semantic lookup time (embedding + vector search + policy check).
- `embedding_latency_ms`: Ollama embedding request latency.
- `vector_search_latency_ms`: PostgreSQL + pgvector query execution time.
- `candidate_policy_latency_ms`: Policy verification time for retrieved candidates.
- `provider_generation_latency_ms`: Upstream provider API latency.
- `exact_cache_write_latency_ms`: Exact-cache write latency.
- `vector_indexing_latency_ms`: Vector embedding + database upsert latency.
- `estimated_latency_saved_ms`: Latency saved on cache hits ($\max(0, \text{original\_latency} - \text{lookup\_latency})$).

### Architectural Boundaries & Limitations

- **Process-Local Storage**: Metrics are stored in process memory. Restarting the gateway process resets all counters and latency buffers.
- **Fixed Sample Capacity**: Latency buffers retain the most recent 1,000 samples per metric per namespace.
- **Multi-Worker Clustering**: In multi-process deployments (e.g. multiple Uvicorn workers), each worker maintains independent in-memory counters. Aggregation across workers requires a distributed metrics sink (such as Prometheus or OpenTelemetry).
- **Zero Monetary Claims**: Monetary savings calculations are deliberately excluded because token pricing varies dynamically across models and providers. Observability focuses strictly on verified token counts and latency distributions.

---

IntelliCache provides an offline evaluation and benchmarking harness at `gateway/scripts/evaluate_semantic_cache.py` using a curated dataset of 114 technical queries (`gateway/evaluation/dataset.json`).

### Benchmark Modes

1. **`--benchmark-mode component` (Offline Synthetic)**:
   - Uses `DeterministicMockEmbeddingProvider`.
   - Measures local CPU vector operations, cosine similarity ranking, and policy candidate evaluation.
   - Requires no external daemons or database.
2. **`--benchmark-mode real` (In-Memory Real Embeddings)**:
   - Uses `OllamaEmbeddingProvider` with `nomic-embed-text`.
   - Measures live HTTP query embedding, candidate embedding, in-memory cosine ranking, and policy checks.
   - Fails fast if Ollama is unavailable without silent mock fallback.
3. **`--benchmark-mode pgvector` (End-to-End PostgreSQL Retrieval)**:
   - Uses `OllamaEmbeddingProvider` with real embeddings and live PostgreSQL + pgvector.
   - **Database Safety**: Requires an explicit `TEST_DATABASE_URL` whose database name contains `test`. Never uses production `DATABASE_URL`.
   - **Foreign-Key Order**: Inserts exact-cache entry via `PostgresCache.set()` first, then upserts `VectorRecord` via `PostgresVectorStore.upsert()`.
   - **Retrieval**: Executes nearest-neighbour vector search via `PostgresVectorStore.search()` using the `<=>` pgvector index operator.
   - **Cleanup**: Cleaned up in a `finally` block using namespace-scoped deletion.
   - **Latency Accounting Separation**: Strictly isolates Stage A (write-time candidate indexing) from Stage B (read-time semantic lookup). Lookup latency saved calculations are not penalized by write setup.
   - **Per-Metric Sample Counts**: Reports explicit sample counts per measured operation with warnings for sample sizes under 10.
   - **HNSW Plan Verification**: Checks `pg_indexes` and executes query plan inspections to report whether HNSW index scan was chosen.
4. **`--benchmark-mode auto`**: Automatically selects `component` for mock provider and `real` for Ollama provider.

### Example Commands

```bash
# Offline evaluation on development split
python scripts/evaluate_semantic_cache.py --dataset evaluation/dataset.json --split development

# Offline parameter grid sweep (strictly development partition)
python scripts/evaluate_semantic_cache.py --sweep

# Offline component latency benchmark
python scripts/evaluate_semantic_cache.py --benchmark --benchmark-mode component --iterations 10

# Real Ollama + PostgreSQL/pgvector evaluation and benchmarking
export TEST_DATABASE_URL="postgresql://gnx@127.0.0.1:5433/intellicache_test"
python scripts/evaluate_semantic_cache.py \
    --provider ollama \
    --model nomic-embed-text \
    --benchmark-mode pgvector \
    --benchmark \
    --iterations 5
```

---

## Persistent Conversation Management & Bounded Context Window (Stage 1)

IntelliCache provides server-managed persistent conversations stored centrally in PostgreSQL, bounded context window reconstruction, client identity isolation, and deterministic turn orchestration:

### Architecture & Data Model

1. **Centralized PostgreSQL Storage**:
   - `conversations`: Stores conversation metadata, title, `cache_namespace`, and `owner_principal_id`.
   - `conversation_turns`: Manages turns with status (`pending`, `completed`, `failed`, `cancelled`), `idempotency_key`, error codes, and completion timestamps. A partial unique index `uq_conversation_active_turn ON conversation_turns (conversation_id) WHERE status = 'pending'` enforces at the database level that exactly one active turn can be in progress per conversation. Concurrent requests receive `409 Conflict`.
   - `conversation_messages`: Stores chronological user and assistant messages with sequential numbering (`sequence_number`), role, content, and generation metadata.
2. **Client Identity Isolation**:
   - Client tokens are mapped to cryptographic principal IDs via `HMAC-SHA256(token, CONVERSATION_IDENTITY_SECRET)`.
   - Tokens are never persisted to database tables. Clients cannot view, manipulate, or delete conversations belonging to different principals, even within the same shared namespace.
3. **Bounded Context Window (`ContextBuilder`)**:
   - Formulates the inference context from chronological completed turns up to `CONVERSATION_MAX_CONTEXT_MESSAGES` (default 50) and `CONVERSATION_MAX_CONTEXT_CHARS` (default 65536).
   - Preserves complete user-assistant turn pairs and original chronological order.
   - Categorizes context state explicitly:
     - `complete`: Entire conversation history fits within configured budgets. Exact-match caching is enabled.
     - `compacted`: Older turns were trimmed to fit within message or character limits. Cache reads and writes are bypassed (`effective_bypass_cache = true`) to prevent corrupting exact cache with truncated contexts.
     - `incomplete`: Truncation occurred in non-turn-pair fashion or single turn exceeded budget. Cache is bypassed.
   - If the current query and system prompt alone exceed the character budget, the gateway rejects the request immediately with `HTTP 413 Content Too Large`.
4. **Idempotency Replay**:
   - Repeating a request with the same `Idempotency-Key` on a completed turn returns the existing stored assistant response with `cached = true`, `cache_type = "exact"`, and `fingerprint = "idempotent_replay"` without calling the upstream provider.
5. **REST API Endpoints**:
   - `POST /conversations`: Create conversation (`title`, optional `id`).
   - `GET /conversations`: List caller's conversations with pagination (`limit`, `offset`, `total`).
   - `GET /conversations/{id}`: Retrieve conversation metadata.
   - `GET /conversations/{id}/messages`: Retrieve messages in chronological order.
   - `DELETE /conversations/{id}`: Delete conversation, cascading to all turns and messages.
   - `POST /query`: When `conversation_id` is passed, runs in server-managed mode, automatically persisting the user turn, reconstructing context, orchestrating generation, and recording the assistant message.

---

## Running the Server

```bash
cd gateway
uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Interactive documentation:

- Swagger UI: `http://localhost:8000/docs`
- ReDoc: `http://localhost:8000/redoc`

---

## Running Tests

Run the complete test suite:

```bash
cd gateway
.venv/bin/python -m unittest discover tests
```

To run individual test modules:

```bash
python -m unittest tests/test_core.py          # Core orchestration & AST boundary tests
python -m unittest tests/test_singleflight_cancellation.py # Cancellation lifecycle, early release & follower protection
python -m unittest tests/test_providers.py     # Groq adapter & provider factory tests
python -m unittest tests/test_embeddings.py    # Embedding contracts, Ollama adapter, prefixes
python -m unittest tests/test_vector_store.py  # Vector store, nearest-neighbour, cascade delete
python -m unittest tests/test_semantic_cache.py # Semantic policy, eligibility, ambiguity, core pipeline
python -m unittest tests/test_metrics.py       # Metrics collector math, percentiles, and schema verification
python -m unittest tests/test_metrics_integration.py # Multi-client metrics isolation & admin aggregation
python -m unittest tests/test_query.py         # API route & streaming tests
python -m unittest tests/test_multi_client.py  # Multi-client auth, namespaces & PostgreSQL tests
python -m unittest tests/test_evaluation.py    # Semantic evaluation, threshold sweeps & pgvector benchmarking
python -m unittest tests/test_config.py        # Configuration tests
python -m unittest tests/test_main.py          # Application integration tests
```

---

## Frontend Web Interface & End-to-End Integration

IntelliCache includes a lightweight, standalone web client under [`frontend/`](file:///home/gnx/Projects/IntelliCache/frontend/) that connects exclusively to the gateway's REST API (`POST /query`, `GET /metrics/me`, `GET /cache/stats`).

### Features:

- **Zero Heavy Framework Dependencies**: Written in vanilla TypeScript and semantic HTML/CSS, bundled independently with Vite.
- **In-Memory Credential Security**: Client API tokens are retained strictly in memory closures and never persisted to browser storage (`localStorage`, `sessionStorage`, `IndexedDB`) or cookies.
- **Live Cache Diagnostics**: Every assistant response is paired with a real-time Cache & Performance Card distinguishing exact cache hits (`⚡ EXACT CACHE HIT`), semantic hits (`🧠 SEMANTIC CACHE HIT`), and provider generations (`🌐 UPSTREAM PROVIDER GENERATION`), along with latency in milliseconds, token counts, and shortened SHA-256 fingerprints.
- **Namespace Telemetry Panel**: A collapsible metrics panel fetching real-time tenant telemetry from `GET /metrics/me`.

### Starting the Frontend:

```bash
# In repository root
pnpm dev
```

Access the client at `http://localhost:5174`. See [`frontend/README.md`](file:///home/gnx/Projects/IntelliCache/frontend/README.md) for full verification workflows and instructions.

---

## Current Scope & Architectural Boundaries

The following capabilities represent the current implementation state:

- **Current Scope**: Exact-match SHA-256 caching, centralized PostgreSQL storage, in-memory LRU/TTL storage, cancellation-safe SingleFlight coalescing for non-streaming requests, Groq LLM provider adapter, Ollama embedding provider, PostgreSQL + pgvector vector storage (`PostgresVectorStore`), semantic response caching service with deterministic eligibility policy, namespace-isolated runtime metrics collection, server-managed persistent conversation storage and bounded context building, and standalone web client (`frontend/`).
- **SingleFlight Boundary**: Request coalescing is strictly process-local (within a single Python asyncio event loop and process).
- **Multi-Turn Semantic Policy**: Semantic caching explicitly evaluates independent single-turn technical queries (`messages` count = 1, role = 'user', no `conversation_id`). Multi-turn conversations and requests with `conversation_id` are excluded from semantic caching by design to prevent contextual ambiguity.
- **Persistent Conversation Store**: Persistent conversation management, bounded context windows, HMAC principal isolation, and concurrency safety are fully implemented and integrated across the gateway and demo client.
