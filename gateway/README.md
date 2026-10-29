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
│   │   └── routes.py         # /query endpoints, client auth, SSE streaming formatter, error mappers
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
│   ├── routes/               # Backward compatibility facade for query routes
│   │   ├── __init__.py
│   │   └── query.py
│   ├── config.py             # Pydantic Settings, environment loader, and payload size bounds
│   └── main.py               # FastAPI application factory, lifespan, CORS, and health/admin routes
├── migrations/               # SQL migration scripts
│   └── 001_create_cache_entries.sql
├── tests/
│   ├── test_core.py          # Core orchestration tests using pure FakeProvider & AST import boundary tests
│   ├── test_providers.py     # Groq adapter, client closure, and ProviderFactory tests
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
   - Exposes the asynchronous `BaseCache` interface (`get`, `set`, `record_miss`, `record_coalesced_join`, `record_coalesced_success`, `clear`, `stats`).
   - Supports two interchangeable implementations:
     - `ExactCache`: In-memory LRU capacity eviction and TTL-based entry expiration for local test/dev.
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
[ Client PC 1 (Browser Ext / App) ] ---\
                                        +---> [ Central IntelliCache API Server ] ===> [ PostgreSQL (Persistent Cache) ]
[ Client PC 2 (Browser Ext / App) ] ---/          (FastAPI Gateway)                            ||
                                                          ||                                   ||
                                                          \/                                   \/
                                                [ Groq LLM Provider ]                 (Authoritative Shared Store)
```

### Key Architectural Principles:
1. **Direct Database Isolation**: Clients **never** connect directly to PostgreSQL. All read, write, eviction, and single-flight operations are managed exclusively by the central IntelliCache service.
2. **PostgreSQL Persistence & asyncpg Pooling**: Persistent exact caching is handled by `PostgresCache` using asynchronous, non-blocking `asyncpg` connection pools (`DB_POOL_MIN_SIZE` to `DB_POOL_MAX_SIZE`), with automatic lifespan cleanup on shutdown.
3. **Database Migrations**: SQL schema migrations are versioned under `gateway/migrations/` and automatically applied at startup via `gateway/app/cache/migration.py`.
   - `001_create_cache_entries.sql`: Creates `cache_entries` with a unique constraint on `(cache_namespace, fingerprint)` for conflict-safe concurrent upserts, plus indexes for lookups, expiration sweeps, and LRU eviction.
4. **Server-Enforced Cache Namespaces (`CACHE_NAMESPACE`)**: Cache isolation (e.g. between development, staging, or teams) is strictly enforced by the server configuration. The gateway does not allow client requests to override the namespace, preventing cache poisoning across tenants.
5. **Client Authentication & Fail-Closed Security**:
   - `CLIENT_API_TOKEN`: When configured, all inference queries (`/query`, `/api/v1/query`) require an authentication token via header `X-Client-Token: <token>` or `Authorization: Bearer <token>`.
   - **Fail-Closed Beyond Loopback**: If the server binds to a non-loopback interface (such as `0.0.0.0` or a public IP) without `CLIENT_API_TOKEN` configured, the gateway refuses to serve queries (returning 401 Unauthorized) to avoid operating as an unintended open LLM relay.
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
CLIENT_API_TOKEN=replace_with_client_secret_token

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
```

### Security Considerations
- **Client Authentication (`/query`)**: Protected by `CLIENT_API_TOKEN` via `X-Client-Token` or `Authorization: Bearer`. Constant-time comparison (`hmac.compare_digest`) prevents timing attacks.
- **Fail-Closed Protection**: Binding to interfaces other than `127.0.0.1` requires `CLIENT_API_TOKEN`. Unauthenticated requests on exposed interfaces are strictly rejected.
- **Admin Endpoint Protection (`/cache/clear`)**: Administrative operations require `ADMIN_API_TOKEN` to be set on the server. Clients must supply the token via `X-Admin-Token: <token>` or `Authorization: Bearer <token>`. Requests are validated using constant-time comparison (`hmac.compare_digest`). If unset or invalid, the endpoint responds with HTTP 403 Forbidden.
- **Network Exposure**: By default, the gateway binds to `127.0.0.1` (localhost) rather than `0.0.0.0` to avoid unintentionally exposing endpoints to the local network or container host interfaces.
- **Secret & Path Redaction**: Upstream error messages and logs automatically redact Groq API keys (`gsk_***`), Bearer tokens, absolute filesystem paths (`<redacted_path>`), and IP addresses (`<redacted_ip>`).
- **Payload Size Limits**: Inbound payload character limits protect gateway memory against denial-of-service payloads.

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
python -m unittest tests/test_providers.py     # Groq adapter & provider factory tests
python -m unittest tests/test_query.py         # API route & streaming tests
python -m unittest tests/test_multi_client.py  # Multi-client auth, namespaces & PostgreSQL tests
python -m unittest tests/test_config.py        # Configuration tests
python -m unittest tests/test_main.py          # Application integration tests
```

---

## Current Scope & Future Architectural Boundaries

The following capabilities represent intentional architectural boundaries for future steps:
- **Current Scope**: Exact-match SHA-256 caching, in-memory LRU/TTL storage, cancellation-safe single flight for non-streaming requests, and the Groq provider adapter.
- **Embedding Provider**: Future embedding models will implement a separate `EmbeddingProvider` interface distinct from `LLMProvider`.
- **Semantic Cache**: Future semantic retrieval will be introduced as an independent cache layer with similarity scoring and safety guardrails.
- **Conversation Manager**: Multi-turn conversation persistence will record every delivered prompt, response, and provenance trace in a persistent store.
- **Demonstration Chatbot**: The web UI chatbot will consume the stable public REST API (`POST /query`).
