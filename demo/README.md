# IntelliCache Gateway Demonstration Chatbot

A standalone web application demonstrating real-time cache behavior, exact matching, semantic similarity response caching, process-local request coalescing, and namespace-isolated metrics for the IntelliCache LLM gateway.

---

## 1. Architecture Overview

```mermaid
flowchart TD
    User["Demo Web Interface (Vanilla TS/Vite)"]
    MemoryAuth["In-Memory Token Store (No LocalStorage)"]
    Gateway["IntelliCache FastAPI Gateway (127.0.0.1:8000)"]
    AuthDep["verify_client_token (HMAC constant-time)"]
    ExactCache["Exact Match Cache (SHA-256)"]
    SemanticPolicy["Semantic Cache Policy (Eligibility Engine)"]
    VectorStore["pgvector / HNSW Vector Store"]
    SingleFlight["SingleFlight Coordinator (Process-local coalescing)"]
    Upstream["Upstream LLM Provider (Groq / Mock)"]
    MetricsCollector["Runtime Metrics Collector (/metrics/me)"]

    User -->|In-memory credentials| MemoryAuth
    User -->|POST /query / X-Client-Token| Gateway
    User -->|GET /metrics/me| Gateway
    Gateway --> AuthDep
    AuthDep --> ExactCache
    ExactCache -->|Miss| SemanticPolicy
    SemanticPolicy -->|Eligible Single-Turn| VectorStore
    VectorStore -->|Miss| SingleFlight
    SingleFlight --> Upstream
    Gateway --> MetricsCollector
    MetricsCollector -->|Namespace Snapshot| User
```

### Key Architectural Principles

1. **Zero External Framework Dependencies**: Built with vanilla TypeScript, semantic HTML5, and responsive CSS bundled by Vite. No heavy UI framework (React, Vue) overhead.
2. **Strict In-Memory Credential & Session Hygiene**: Client API tokens and conversation sessions are held strictly in private closures within memory. They are **never** persisted to `localStorage`, `sessionStorage`, `IndexedDB`, or cookies.
3. **CommonMark/GFM Markdown & Code Highlighting**: Model responses render rich Markdown (tables, headings, blockquotes, lists) and syntax-highlighted code blocks with 1-click clipboard copy, strictly sanitized through `DOMPurify` to neutralize XSS vectors.
4. **Transparent Caching Diagnostics**: Responses display the exact backend diagnostic metadata (`cached`, `cache_type: "exact" | "semantic" | "none"`, `latency_ms`, `cache_hit_count`, `model`, `usage`, and shortened `fingerprint`). No fabricated cache indicators.
5. **Namespace-Isolated Telemetry**: The UI displays authenticated namespace-level hit rates, token savings, coalesced joins, cache capacity, and latency distributions via `GET /metrics/me` and `GET /cache/stats`. Administrative metrics (`/admin/metrics`) remain isolated and unreachable to ordinary client tokens.
6. **Responsive Dark Neo-Brutalist Design**: High-contrast cyberpunk brutalist aesthetics with tactile offset box-shadows, semantic cache badges (emerald exact, cyan semantic, amber provider), responsive mobile drawer navigation, collapsible metrics, and full keyboard accessibility.
7. **Server-Managed Persistent Conversations**: In multi-turn mode, conversation sessions and turns persist centrally in PostgreSQL. On connection, the client loads user conversations via `GET /conversations` and loads messages on demand via `GET /conversations/{id}/messages`. Multi-turn queries submit `conversation_id`, `prompt`, and a client-generated `Idempotency-Key` without sending raw historical messages arrays, relying on the gateway's bounded Context Builder while maintaining strictly memory-only token storage.

---

## 2. Prerequisites & Quickstart

### Step 1: Start the IntelliCache Gateway

Ensure your Python virtual environment has the required dependencies and start the FastAPI service:

```bash
cd gateway
# Configure environment variables (or create gateway/.env)
export GROQ_API_KEY="your-groq-api-key"              # or mock key for testing
export CLIENT_API_TOKENS='{"demo-token": "default", "tenant-alpha": "tenant_alpha"}'
# Use explicit demo origins (recommended for authenticated sessions with credentials):
export CORS_ORIGINS="http://localhost:5174,http://127.0.0.1:5174"
# Or universal wildcard for uncredentialed development:
# export CORS_ORIGINS="*"
export CACHE_BACKEND="memory"                        # or "postgres" with DATABASE_URL
export SEMANTIC_CACHE_ENABLED="false"                # or "true" with pgvector and Ollama

# Start the gateway server on 127.0.0.1:8000
.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8000 --reload
```

Verify that the gateway is running:

```bash
curl http://127.0.0.1:8000/
# Expected: {"service":"IntelliCache Gateway","version":"1.0.0","status":"online",...}
```

### Step 2: Start the Demonstration Web Application

In the root repository directory:

```bash
# Start Vite development server for the demo chatbot (port 5174)
pnpm demo:dev
```

Open your browser at:

```
http://localhost:5174
```

### Production Build & Preview

```bash
# Build production bundle to dist-demo/
pnpm demo:build

# Preview built application locally
pnpm demo:preview
```

---

## 3. Configuring Connection & Client Authentication

1. **Gateway REST API URL**: Set to `http://127.0.0.1:8000` (default) or your remote gateway address.
2. **Client API Token**: Enter a configured client token (e.g. `demo-token` from `CLIENT_API_TOKENS`).
3. Click **"Connect / Test"**:
   - The application checks reachability against `GET /`.
   - The application verifies authentication by querying `GET /metrics/me`.
   - Once verified, the status indicator turns green (`Connected: default`) and the initial namespace metrics are loaded.

---

## 4. Cache Demonstration Verification Workflows

### A. Demonstrating Exact-Match Cache Hits

1. In the chat interface, ensure **"Single-Turn (Semantic Eligible)"** mode is selected.
2. Click the quick prompt:
   ```
   Explain Python asyncio event loop and tasks
   ```
3. Click **Send (↵)**.
   - **First Turn (Cache Miss)**: Upstream provider generation executes.
   - Badge displays `🌐 Upstream Provider Generation` (amber/gray).
   - Latency reflects live LLM generation (typically ~800 ms - 2,500 ms depending on model and network).
   - Note the generated SHA-256 fingerprint (e.g. `a3f9c2...`).
4. Click **Send (↵)** again with the identical text.
   - **Second Turn (Exact Cache Hit)**: Gateway matches exact canonical hash.
   - Badge displays `⚡ Exact Cache Hit` (emerald green).
   - Latency drops significantly (typically ~2 ms - 15 ms, representing a >98% latency reduction).
   - Hit count increments to `1` (or higher).
   - Token usage indicates cached savings.

> [!NOTE]
> **Performance Observations Notice**: The latency ranges cited above are empirical observations from local development benchmarks. Real-world latency varies based on server hardware, database I/O, network proximity, vector store index size, and upstream LLM provider load. Automated test suites use mocked gateway responses and do not enforce universal latency guarantees.

### B. Demonstrating Semantic Similarity Cache Hits

_(Requires `CACHE_BACKEND=postgres`, `SEMANTIC_CACHE_ENABLED=true`, and running Ollama embedding provider)_

1. Submit a base technical query:
   ```
   What is the difference between let, const, and var in JavaScript?
   ```
   - First submission results in an upstream provider generation and indexes the vector embedding in `pgvector`.
2. Submit a paraphrased variation of the question:
   ```
   How do let, const, and var differ in JavaScript?
   ```
3. **Outcome (Semantic Reuse)**:
   - Gateway vector search identifies the candidate within cosine distance `< 0.08` (similarity `> 0.92`).
   - Badge displays `🧠 Semantic Cache Hit` (vibrant cyan).
   - Latency reflects the embedding and vector retrieval time (typically ~20 ms - 60 ms on local hardware) compared to seconds of generative inference.
   - Telemetry increments `semantic_hits` in the Namespace Telemetry panel.

### C. Demonstrating Cache Bypass

1. Check the **"Bypass Cache (Force Upstream Call)"** box.
2. Submit an identical query that was previously cached.
3. **Outcome**: The gateway bypasses local and vector caches, issuing a fresh upstream LLM generation. The response badge shows upstream provider generation.

---

## 5. Technical Constraints & Policies

### 1. Process-Local SingleFlight Request Coalescing

IntelliCache implements process-local SingleFlight consolidation using Python `asyncio.Future` coordination:

- Concurrent identical requests within the same FastAPI process share a single in-flight leader execution.
- SingleFlight is strictly **process-local** (in-memory within a single worker process); it does not coordinate across separate physical server nodes without a distributed broker.

### 2. Multi-Turn Semantic Cache Exclusion Policy

- IntelliCache backend policy evaluates semantic similarity **strictly on independent single-turn technical queries**.
- Multi-turn conversation requests (where `messages` count > 1 or `conversation_id` is supplied) are **explicitly excluded from semantic caching** to avoid contextual ambiguity and hallucinations resulting from missing conversational history.
- Exact-match caching remains fully active for multi-turn requests if the exact message sequence and conversation context match.

### 3. On-Demand Metrics Refresh Model

The demonstration interface operates on an **on-demand telemetry model**:

- Metrics are queried when the user explicitly clicks the **"Refresh"** button.
- Metrics are also automatically refreshed after each successful query execution.
- There is deliberately no continuous background polling loop, avoiding unnecessary load on the gateway.

### 4. Optional SSE Streaming & Semantic Cache Limitations

The chatbot interface provides optional Server-Sent Events (SSE) token streaming via the `Stream response tokens` toggle:

- **Standard (Non-Streaming) Mode (Default)**: Sends a regular JSON `POST /query`. Eligible for exact-match caching and, for single-turn queries, HNSW semantic response caching.
- **Streaming Mode (`stream: true`)**: Streams incremental delta chunks via SSE (`text/event-stream`). Tokens are rendered progressively using safe `textContent` DOM updates to avoid layout reflows and flicker. Markdown and syntax highlighting are rendered upon completion.
- **Cache Policy for Streaming**:
  > [!IMPORTANT]
  > On the current FastAPI gateway backend (`gateway/app/api/routes.py`), streaming queries evaluate **deterministic exact-match caching (SHA-256)**, but **semantic similarity vector caching is not evaluated for streaming requests**.
  > If a query matches an exact cached entry, the gateway replays the full cached response. If the query does not match an exact cache entry, upstream generative streaming executes.
- **Stop Generation / Abort Controller**: While streaming is active, a prominent `⏹ Stop` button replaces the send button. Clicking Stop aborts the underlying `fetch()` stream immediately via `AbortController.abort()`. The turn is recorded as a stopped attempt with a `⏹️ Generation Stopped` badge and a `🔄 Retry Query` button, without fabricating misleading cache diagnostics.

### 5. UI Polish, Accessibility & Keyboard Controls

- **Auto-Scroll & Jump to Latest**: The chat view automatically scrolls to the newest token if the user is near the bottom. If the user scrolls up to review prior messages, auto-scroll pauses and a floating `↓ Jump to latest` button appears.
- **Keyboard Navigation**:
  - `Enter` / `Space` on conversation items in the sidebar selects and switches conversations.
  - `Delete` / `Backspace` on a selected conversation item deletes it.
  - `Enter` in the composer textarea sends the message; `Shift + Enter` inserts a newline.
  - `Escape` dismisses settings modal dialogs with focus trapping (`Tab` / `Shift + Tab`) and focus restoration.
- **Action Toast Notifications**: Copying response markdown, syntax-highlighted code blocks, or SHA-256 query fingerprints displays transient visual toast feedback without disruptive alert popups.

### 6. Cross-Origin Resource Sharing (CORS) Configuration

When running the gateway and demo on different origins (e.g. gateway on `http://127.0.0.1:8000` and demo on `http://localhost:5174`):

- Set `CORS_ORIGINS="http://localhost:5174,http://127.0.0.1:5174"` in `gateway/.env` to enable explicit origin permissions with credential support.
- Alternatively, set `CORS_ORIGINS="*"` for universal open access (disabling credentials per CORS specification). Avoid mixing explicit origin lists with wildcard characters.
