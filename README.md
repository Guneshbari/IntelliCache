# IntelliCache

Intelligent semantic caching that reduces LLM latency, API costs, and redundant inference through response reuse.

---

## Phase 1: AI Conversation Data Collector

### Step 3: ChatGPT Data Collection Adapter

A specialized, robust, event-driven DOM observation and extraction adapter for ChatGPT conversations. Persists full query-response interactions, code blocks, and conversation metadata locally into IndexedDB through the Step 2 repository layer.

### Architecture Overview

```
IntelliCache/
├── src/
│   ├── background/
│   │   └── service-worker.ts       # Manifest V3 service worker & async DB coordinator
│   ├── content/
│   │   └── content.ts              # Content script entry point & adapter activator
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
│   ├── platforms/
│   │   ├── index.ts                # Platform module exports
│   │   ├── registry.ts             # Central adapter registry & URL-based discovery
│   │   ├── types.ts                # PlatformAdapter and ExtractedInteraction types
│   │   └── chatgpt/
│   │       ├── adapter.ts          # MutationObserver, streaming stability & persistence
│   │       ├── parser.ts           # Pure extraction functions & code block preservation
│   │       └── selectors.ts        # Semantic DOM selector definitions
│   ├── popup/
│   │   ├── index.html              # Diagnostic popup UI
│   │   ├── popup.css               # Clean, accessible styling
│   │   └── popup.ts                # Popup controller & DB stats diagnostic
│   └── shared/
│       ├── types.ts                # Discriminated union messages & domain re-exports
│       └── messages.ts             # Message builders, type guards & dispatch helpers
├── tests/
│   ├── chatgpt-adapter.test.ts     # Adapter lifecycle, debouncing & DB integration tests
│   ├── chatgpt-fixtures.test.ts    # Static HTML DOM snapshot tests (multi-turn, streaming, code)
│   ├── chatgpt-parser.test.ts      # Pure parser unit tests for selectors and extraction
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

### ChatGPT Extraction & Observation Strategy

1. **Semantic DOM Selectors**: Uses stable semantic attributes (`[data-message-author-role]`, `article[data-testid^="conversation-turn-"]`) to isolate user queries and assistant responses.
2. **Streaming & Completion Guard**: Observes stop button (`button[data-testid="stop-button"]`) and `.result-streaming` classes. Responses are only captured once generation has completely finished and stabilized.
3. **Code Block Preservation**: Preserves multiline formatting and code blocks with language tags (` ```python ... ``` `).
4. **UI Noise Exclusion**: Strips copy buttons, regenerate buttons, feedback thumbs, and web search citations from extracted text.
5. **Deduplication**: Maintains in-memory session tracking alongside database-level SHA-256 fingerprint uniqueness to avoid redundant operations on DOM re-renders.

### Supported Metadata & Limitations

- **Conversation ID**: Extracted reliably from URL (`/c/{uuid}` or Custom GPT `/g/.../c/{uuid}`). If on root `/` prior to URL generation, captured once the URL updates.
- **Message ID**: Extracted from `data-message-id` attribute when exposed by the ChatGPT interface.
- **Model Name**: Extracted from model switcher header if visible (e.g. `GPT-4o`, `o1-preview`).
- **Known Limitations**: If ChatGPT alters major DOM container semantics in future UI updates, selectors in `selectors.ts` can be updated without touching the underlying storage or messaging layers.

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
6. Navigate to `https://chatgpt.com` to observe live query/response collection.
