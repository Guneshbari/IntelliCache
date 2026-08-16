# IntelliCache

Intelligent semantic caching that reduces LLM latency, API costs, and redundant inference through response reuse.

---

## Phase 1: AI Conversation Data Collector

### Step 2: Local Data Layer (IndexedDB & Dexie.js)

A lightweight, maintainable, local-only Manifest V3 browser extension and local storage layer built with TypeScript, Dexie.js, Web Crypto API, and Vite.

### Architecture Overview

```
IntelliCache/
├── src/
│   ├── background/
│   │   └── service-worker.ts       # Manifest V3 service worker & async DB coordinator
│   ├── content/
│   │   └── content.ts              # Content script entry point (handshake hook)
│   ├── database/
│   │   ├── db.ts                   # Dexie database class (IntelliCacheDB) & singleton
│   │   ├── index.ts                # Database module exports
│   │   ├── metrics.ts              # Character count, UTF-8 byte calculation utilities
│   │   ├── schema.ts               # Schema definitions and table indexes (v1)
│   │   ├── types.ts                # Interaction & Conversation entities, domain errors
│   │   └── repositories/
│   │       ├── conversation-repository.ts # Conversation data access & recency updates
│   │       └── interaction-repository.ts  # Interaction persistence & deduplication
│   ├── fingerprint/
│   │   ├── fingerprint.ts          # Web Crypto SHA-256 (3-tier fallback strategy)
│   │   ├── index.ts                # Fingerprint module exports
│   │   └── normalize.ts            # Isolated text normalization for hashing
│   ├── popup/
│   │   ├── index.html              # Diagnostic popup UI
│   │   ├── popup.css               # Clean, accessible styling
│   │   └── popup.ts                # Popup controller & DB stats diagnostic
│   └── shared/
│       ├── types.ts                # Discriminated union messages & domain re-exports
│       └── messages.ts             # Message builders, type guards & dispatch helpers
├── tests/
│   ├── database.test.ts            # Database initialization and lifecycle tests
│   ├── fingerprint.test.ts         # Deterministic fingerprinting & 3-tier strategy tests
│   ├── messages.test.ts            # Message contracts and URL detection tests
│   ├── metrics.test.ts             # UTF-8 byte counting and data model tests
│   ├── repositories.test.ts        # Interaction & Conversation repository CRUD tests
│   └── service-worker-db.test.ts   # Async message handling and DB error envelope tests
├── manifest.config.ts              # Type-safe Manifest V3 definition
├── vite.config.ts                  # Vite build setup with @crxjs/vite-plugin
├── vitest.config.ts                # Vitest test runner configuration
├── eslint.config.js                # Flat ESLint configuration
├── .prettierrc                     # Code style configuration
├── tsconfig.json                   # Strict TypeScript configuration
└── package.json                    # Scripts and dependencies
```

### Data Layer Specifications

#### Interaction Schema (Version 1)

```typescript
interface Interaction {
  schema_version: 1
  id: string
  fingerprint: string
  platform: string
  conversation_id: string | null
  message_id: string | null
  observed_at: string // ISO-8601 string
  model: {
    provider: string | null
    name: string | null
  }
  query: {
    text: string
    characters: number
    bytes: number
    estimated_tokens: number | null
  }
  response: {
    text: string
    characters: number
    bytes: number
    estimated_tokens: number | null
  }
  conversation_title: string | null
  collector_version: string
}
```

#### Conversation Schema (Version 1)

```typescript
interface Conversation {
  id: string
  platform: string
  title: string | null
  first_observed_at: string // ISO-8601 string
  last_observed_at: string // ISO-8601 string
}
```

### Fingerprinting Strategy

Deterministic SHA-256 fingerprint generation using native Web Crypto API with a 3-tier fallback model:

1. **Level 1** (`L1|platform|conversation_id|message_id`): Direct platform identifiers when message ID and conversation ID exist.
2. **Level 2** (`L2|platform|conversation_id|normalized_query|normalized_response`): Conversation thread content fallback when message ID is unavailable.
3. **Level 3** (`L3|platform|normalized_query|normalized_response|observed_at_hourly_bucket`): Content and time bucket fallback for standalone/ephemeral queries.

_Note: Raw query and response texts are preserved unmodified. Normalization is applied strictly during hashing._

### Development Commands

```bash
# Install dependencies
pnpm install

# Start Vite in development mode (with HMR)
pnpm dev

# Build the unpacked extension for production (outputs to dist/)
pnpm build

# Run Vitest unit & integration tests
pnpm test

# Run ESLint
pnpm lint

# Format code with Prettier
pnpm format
```

### Loading the Unpacked Extension in Chrome / Edge / Brave

1. Run `pnpm build` to compile the extension into the `dist/` directory.
2. Open your Chromium-based browser and navigate to `chrome://extensions`.
3. Enable **Developer mode** (toggle located in the top-right corner).
4. Click **Load unpacked**.
5. Select the `dist/` directory within this repository (`/home/trespasser/coding_stuff/IntelliCache/dist`).
6. Click the IntelliCache Collector extension icon to open the popup and view diagnostic connection and storage stats.
