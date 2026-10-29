# IntelliCache Gateway

A hardened, high-performance Python backend and exact-match LLM caching proxy for **IntelliCache**. The Gateway intercepts LLM inference requests, conservatively normalizes queries, computes deterministic SHA-256 query fingerprints across all answer-affecting parameters, coordinates concurrent identical requests via cancellation-safe single-flight execution, and reuses cached responses to reduce inference latency and API costs.

---

## Architecture & Directory Structure

```
gateway/
├── app/
│   ├── __init__.py           # Application package definition & version
│   ├── main.py               # FastAPI factory, lifespan, RFC-compliant CORS, and root/health routes
│   ├── config.py             # Pydantic Settings, environment loader, and payload size bounds
│   └── routes/
│       ├── __init__.py       # Route exports
│       └── query.py          # /query execution, Groq client, exact-match cache, single-flight coordinator
├── tests/
│   ├── __init__.py           # Test package initialization
│   ├── test_config.py        # Settings validation, env overrides, and payload limits tests
│   ├── test_main.py          # Root, health, CORS, and cache lifecycle integration tests
│   └── test_query.py         # Exact caching, parameter isolation, single-flight, streaming, & security tests
├── .env                      # Local environment configuration (git-ignored)
├── .env.example              # Template environment configuration
├── .gitignore                # Python and environment ignore rules
├── requirements.txt          # Python package dependencies
└── README.md                 # Gateway documentation
```

---

## Exact-Match Caching Semantics

The gateway uses conservative, deterministic exact matching to guarantee that answers are only reused when all answer-affecting inputs match.

### 1. Fingerprint Components
The SHA-256 cache fingerprint is computed by serializing a canonical JSON payload with sorted keys containing:
- **`model`**: Lowercase, trimmed effective model identifier (e.g. `qwen/qwen3.8-27b`).
- **`messages`**: Canonical sequence of `[{"role": role, "content": normalized_content}]`. If `system_prompt` is provided, it is prepended with role `"system"`.
- **`temperature`**: Effective sampling temperature float rounded to 4 decimals (resolving to server default `0.7` if omitted).
- **`max_tokens`**: Effective integer token ceiling (or `null` if unconstrained).

Different models, temperatures, token limits, or message sequences produce strictly different fingerprints and never collide.

### 2. Conservative Whitespace Normalization
Unlike lossy text-search engines, IntelliCache preserves syntax-sensitive code structures:
- **Line Ending Canonicalization**: `\r\n` (CRLF) and `\r` (CR) are normalized to `\n` (LF).
- **Outer Trimming**: Leading and trailing outer whitespace of the entire prompt/message string is trimmed.
- **Code Indentation Preserved**: Internal indentation (spaces and tabs) is preserved intact. Code with 4-space indent vs 2-space indent produces distinct cache entries.
- **Line Structure Preserved**: Blank lines, internal linebreaks, and line-trailing spaces (e.g. Markdown double-space line breaks) are never collapsed.

### 3. Strict Cache Bypass (`bypass_cache=true`)
When `bypass_cache: true` is supplied:
- **No Cache Read**: Does not retrieve existing cached responses.
- **No Cache Write**: Never inserts or replaces entries in the cache upon completion.
- **Single-Flight Isolation**: Never joins an in-flight single-flight group; executes its own independent upstream generation.

---

## Single-Flight Concurrency Coordination

To eliminate "thundering herd" duplicates when multiple identical queries arrive simultaneously, the gateway implements an in-memory **Single-Flight Coordinator** for eligible non-streaming requests:

1. **Leader Election**: The first request for a given fingerprint becomes the Leader and initiates inference with Groq.
2. **Follower Awaiting**: Subsequent concurrent requests for the exact same fingerprint register as Followers and await the Leader's completion via `asyncio.shield`.
3. **Cancellation Isolation**: If a follower disconnects or cancels its HTTP request, the Leader's generation is protected and continues running.
4. **Failure Propagation & Cleanup**: If the Leader encounters an exception or cancellation, the error is cleanly propagated to all waiting followers, and the in-flight state is cleared unconditionally in a `finally` block so subsequent requests can retry normally.
5. **No Unretrieved Warnings**: If a Leader fails with zero followers waiting, the exception is consumed internally to prevent Python `Future exception was never retrieved` warnings.

*Limitation*: Streaming requests (`stream: true`) currently bypass single-flight coordination; concurrent streaming misses execute independent Groq calls, but successfully completed streams are cached upon completion.

---

## Streaming Response Contract (SSE)

When `stream: true` is requested, the endpoint returns a `text/event-stream` stream:

- **Incremental Chunks**:
  ```
  data: {"delta": "token", "done": false, "cached": false, "fingerprint": "..."}
  ```
- **Completion Event**:
  ```
  data: {"delta": "", "done": true, "cached": false, "latency_ms": 312.4, "model": "qwen/qwen3.8-27b", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "fingerprint": "..."}
  ```
- **Sentinel**:
  ```
  data: [DONE]
  ```
- **Failure Isolation**: If a stream is interrupted or encounters an upstream API error, an error event is emitted, and the partial response is **never cached**.

---

## Configuration & Security

### Environment Settings (`.env`)

```dotenv
# Groq API Configuration (Required)
GROQ_API_KEY=gsk_your_actual_groq_api_key_here
GROQ_MODEL=qwen/qwen3.8-27b

# Server Configuration (Optional)
HOST=0.0.0.0
PORT=8000
DEBUG=false
CORS_ORIGINS=*

# Caching Configuration (Optional)
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
- **Payload Size Limits**: Character limits protect gateway memory against denial-of-service payloads. These represent inbound HTTP body limits, not model context token limits.
- **Secret Redaction**: Upstream error messages and logs automatically redact Groq API keys (`gsk_***`) and authorization bearer tokens.
- **CORS Behavior**: When `CORS_ORIGINS=*`, `allow_credentials` is set to `False` to maintain strict W3C specification compliance.
- **Network Exposure**: Binding to `0.0.0.0` exposes the gateway to the local network. In production or shared environments, bind to `127.0.0.1` unless behind a reverse proxy, and note that administrative cache endpoints (`/cache/clear`) are unauthenticated.

---

## Running the Server

```bash
cd gateway
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
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
python -m unittest tests/test_config.py
python -m unittest tests/test_main.py
python -m unittest tests/test_query.py
```
