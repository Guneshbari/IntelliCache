# IntelliCache

Intelligent semantic caching gateway that reduces LLM inference latency, API costs, and redundant computation through high-performance exact and vector-similarity response reuse.

---

## Overview

IntelliCache intercepts LLM inference requests to provide ultra-low latency response reuse through exact-match SHA-256 caching and vector-similarity semantic caching with pgvector. It shields upstream LLM providers (e.g., Groq, Ollama) from redundant calls, coalesces concurrent identical requests with SingleFlight, and provides transparent, namespace-isolated performance telemetry.

### Key Capabilities

- **Exact-Match Cache**: Sub-millisecond in-memory and PostgreSQL LRU cache indexed by deterministic SHA-256 prompt fingerprinting.
- **Semantic Similarity Cache**: Vector similarity caching supported in two modes: local CPU in-memory vector store via FastEmbed (`BAAI/bge-small-en-v1.5`), and persistent PostgreSQL pgvector with HNSW indexing and Ollama/Nomic embeddings.
- **Concurrency Control**: Process-local SingleFlight coordinator coalescing concurrent requests to eliminate cache stampedes.
- **Server-Managed Conversations**: PostgreSQL conversation turns with atomic leasing, recovery fencing, and contiguity guarantees.
- **Streaming & SSE**: Incremental token delivery via Server-Sent Events (`text/event-stream`) alongside standard JSON response payloads with strict frame validation.
- **Multi-Tenant Isolation**: Constant-time HMAC client token verification mapping to isolated namespace metrics (`/metrics/me`) and database partitions.
- **Demonstration Chatbot Interface**: Responsive dark Neo-Brutalist web client built with Vanilla TypeScript and Vite for testing, benchmarking, and real-time telemetry inspection.

---

## Repository Structure

```
IntelliCache/
├── frontend/                   # Vanilla TypeScript frontend web client
│   ├── index.html              # HTML5 entry point
│   ├── public/                 # Static assets (favicon)
│   ├── src/
│   │   ├── api-client.ts       # GatewayApiClient with SSE streaming & auth
│   │   ├── main.ts             # Application entry point
│   │   ├── styles.css          # Neo-Brutalist design tokens and layouts
│   │   ├── types.ts            # Frontend domain contracts and interfaces
│   │   ├── ui-controller.ts    # Main UI interaction coordinator
│   │   └── ui/                 # Specialized UI components (conversations, messages, metrics)
│   ├── tests/                  # Frontend unit & integration test suite (Vitest)
│   │   ├── api-client.test.ts
│   │   ├── conversation-manager.test.ts
│   │   ├── markdown-and-security.test.ts
│   │   ├── ui-components.test.ts
│   │   └── setup.ts
│   └── README.md               # Frontend interface documentation
├── gateway/                    # FastAPI Semantic Cache Gateway
│   ├── app/
│   │   ├── api/                # FastAPI routing and controllers
│   │   ├── cache/              # Exact-match in-memory and PostgreSQL caches
│   │   ├── conversations/      # Server-managed conversation state & repositories
│   │   ├── core/               # SingleFlight, hashing, and gateway service logic
│   │   ├── embeddings/         # Embedding providers (Ollama / Nomic)
│   │   ├── metrics/            # Thread-safe namespace metrics collector
│   │   ├── providers/          # LLM providers (Groq, Mock)
│   │   ├── semantic_cache/     # Semantic policy, validation, and evaluation
│   │   └── vector_store/       # pgvector store and HNSW migration scripts
│   ├── migrations/             # SQL database migration scripts
│   └── tests/                  # Gateway unit and integration test suite
├── assets/                     # Project branding, emblems, and logos
├── package.json                # Frontend scripts and dependencies
├── vite.config.ts              # Vite configuration for frontend application
├── vitest.config.ts            # Vitest test runner configuration
└── eslint.config.js            # Flat ESLint configuration
```

---

## Quick Start

### 1. Gateway Backend Setup

#### Prerequisites

- Python 3.11+
- PostgreSQL 16+ with `pgvector` extension enabled

#### Installation & Startup

```bash
cd gateway

# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp .env.example .env
# Edit .env with your PostgreSQL credentials and provider API keys

# Apply migrations
psql -U postgres -d intellicache -f migrations/001_create_cache_entries.sql
psql -U postgres -d intellicache -f migrations/002_create_conversations.sql
psql -U postgres -d intellicache -f migrations/003_conversation_hardening.sql
psql -U postgres -d intellicache -f migrations/004_conversation_fencing.sql
psql -U postgres -d intellicache -f migrations/vector/001_create_vector_records.sql
psql -U postgres -d intellicache -f migrations/vector/002_create_hnsw_index.sql

# Start the gateway server
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

### 2. Frontend Application Setup

```bash
# Install dependencies
pnpm install

# Start development server
pnpm dev

# Build production bundle
pnpm build

# Preview production build
pnpm preview
```

The web application will be available at `http://localhost:5174`.

---

## Development & Verification Commands

```bash
# Run TypeScript typecheck
pnpm typecheck

# Run ESLint
pnpm lint

# Check code formatting with Prettier
pnpm format:check

# Format code with Prettier
pnpm format

# Run frontend Vitest test suite
pnpm test

# Run gateway unit tests
cd gateway && python -m unittest discover tests

# Run gateway PostgreSQL integration tests
cd gateway && python -m unittest tests.test_multi_client tests.test_vector_store tests.test_semantic_cache tests.test_conversations
```

---

## Security & Architecture Principles

- **Zero Trust in Client Storage**: Sensitive credentials (client API tokens) and temporary session keys never touch persistent client storage (`localStorage`, cookies, or IndexedDB); they reside strictly in JavaScript private memory closures.
- **Cryptographic Namespace Isolation**: Every tenant request is verified with HMAC authentication and mapped to isolated cache namespaces and metrics snapshots.
- **Transparent Diagnostics**: Every response returns exact cache outcomes, fingerprints, and provider latencies without synthesizing fake metadata.
- **Deterministic Fencing**: Multi-turn conversation states use atomic server-side leasing, heartbeat guards, and idempotency keys to ensure ledger consistency.
