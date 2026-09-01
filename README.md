# IntelliCache

Intelligent semantic caching that reduces LLM latency, API costs, and redundant inference through response reuse.

---

## Phase 1: AI Conversation Data Collector

### Multi-Platform & Multi-Browser Data Collection Support

A unified, event-driven DOM observation and extraction extension for collecting real AI interaction datasets across multiple LLM web platforms (**ChatGPT**, **Claude**, and **Gemini**) and running reliably across major Chromium-based browsers (**Google Chrome**, **Microsoft Edge**, and **Brave**) and **Mozilla Firefox**.

Persists full query-response interactions, code blocks, and conversation metadata locally into IndexedDB through the repository layer.

### Cross-Browser Compatibility Architecture

IntelliCache employs a clean, unified browser runtime abstraction (`src/shared/browser.ts`) that standardizes:

- **Runtime Resolution**: Auto-resolves `browser.runtime` (Firefox) and `chrome.runtime` (Chromium).
- **Dual Message Semantics**: Seamlessly handles both Promise-based messaging in Firefox and callback/`sendResponse` channel keeping in Chromium.
- **Event-Driven Background Handling**: Compatible with both Service Workers (Chromium MV3) and Event Pages (`background.scripts` in Firefox MV3).
- **Extension Identity**: Configured with Gecko ID (`intellicache-collector@research.local`) for stable IndexedDB namespace persistence.

---

### Development & Build Commands

```bash
# Install dependencies
pnpm install

# Start Vite in development mode (with HMR)
pnpm dev

# Build the unpacked extension for Chromium (outputs to dist/)
pnpm build

# Build the unpacked extension for Mozilla Firefox (outputs to dist-firefox/)
pnpm build:firefox

# Build both Chromium and Firefox distribution targets
pnpm build:all

# Run Vitest unit & integration tests (28 test suites)
pnpm test

# Run TypeScript typecheck
pnpm exec tsc --noEmit

# Run ESLint
pnpm lint

# Check code formatting with Prettier
pnpm format:check

# Auto-format code with Prettier
pnpm format
```

---

### Multi-Browser Loading Guide (Chrome, Edge, Brave, Firefox)

#### 1. Google Chrome

1. Run `pnpm build`.
2. Navigate to `chrome://extensions` in the address bar.
3. Enable **Developer mode** using the toggle switch in the top-right corner.
4. Click **Load unpacked** in the top-left toolbar.
5. Select the `dist/` directory inside this repository.

#### 2. Microsoft Edge

1. Run `pnpm build`.
2. Navigate to `edge://extensions` in the address bar.
3. Enable **Developer mode** in the left sidebar.
4. Click **Load unpacked** at the top.
5. Select the `dist/` directory.

#### 3. Brave Browser

1. Run `pnpm build`.
2. Navigate to `brave://extensions` in the address bar.
3. Enable **Developer mode** toggle in the top-right corner.
4. Click **Load unpacked**.
5. Select the `dist/` directory.

#### 4. Mozilla Firefox

1. Run `pnpm build:firefox`.
2. Open Firefox and navigate to `about:debugging#/runtime/this-firefox` in the address bar.
3. Click **Load Temporary Add-on...**.
4. Select `manifest.json` inside the `dist-firefox/` directory (or any file within `dist-firefox/`).
5. The extension is now active with full ChatGPT, Claude, and Gemini collection support.

**Verified Firefox Compatibility**:

- **Supported Versions**: Firefox 109.0+ (Manifest V3 support), Firefox 115+ ESR, Firefox 128+ ESR, Firefox 130+ Current Release.
- **Private Browsing Note**: In Firefox Private Browsing mode, IndexedDB persistence is partitioned or restricted by Firefox by default. For reliable local collection, standard browsing mode is recommended.

```
IntelliCache/
├── src/
│   ├── background/
│   │   └── service-worker.ts       # Manifest V3 service worker & async DB coordinator
│   ├── content/
│   │   └── content.ts              # Content script entry point & adapter discovery
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
│   │   ├── chatgpt/
│   │   │   ├── adapter.ts          # MutationObserver, streaming stability & persistence
│   │   │   ├── parser.ts           # Pure extraction functions & code block preservation
│   │   │   └── selectors.ts        # ChatGPT semantic DOM selector definitions
│   │   ├── claude/
│   │   │   ├── adapter.ts          # Claude event-driven observer & persistence
│   │   │   ├── parser.ts           # Pure Claude extraction functions & code formatting
│   │   │   └── selectors.ts        # Claude semantic DOM selector definitions
│   │   └── gemini/
│   │       ├── adapter.ts          # Gemini event-driven observer & persistence
│   │       ├── parser.ts           # Pure Gemini extraction functions & code formatting
│   │       └── selectors.ts        # Gemini semantic DOM selector definitions
│   ├── popup/
│   │   ├── index.html              # Diagnostic popup UI
│   │   ├── popup.css               # Clean, accessible styling
│   │   └── popup.ts                # Popup controller & DB stats diagnostic
│   └── shared/
│       ├── types.ts                # Discriminated union messages & domain re-exports
│       └── messages.ts             # Message builders, type guards & URL platform detector
├── tests/
│   ├── chatgpt-adapter.test.ts     # ChatGPT adapter lifecycle, debouncing & DB integration
│   ├── chatgpt-fixtures.test.ts    # ChatGPT DOM snapshot fixtures (multi-turn, code, streaming)
│   ├── chatgpt-parser.test.ts      # Pure ChatGPT parser unit tests
│   ├── claude-adapter.test.ts      # Claude adapter lifecycle & DB integration tests
│   ├── claude-fixtures.test.ts     # Claude DOM snapshot fixtures (multi-turn, code, streaming)
│   ├── claude-parser.test.ts       # Pure Claude parser unit tests
│   ├── gemini-adapter.test.ts      # Gemini adapter lifecycle & DB integration tests
│   ├── gemini-fixtures.test.ts     # Gemini DOM snapshot fixtures (multi-turn, code, streaming)
│   ├── gemini-parser.test.ts       # Pure Gemini parser unit tests
│   ├── cross-platform.test.ts      # Cross-platform normalization contract tests
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

---

### Normalized Data Contract

Every platform adapter (`ChatGPTAdapter`, `ClaudeAdapter`, `GeminiAdapter`) extracts conversation turns and produces the exact same normalized `ExtractedInteraction` contract:

| Field                | Type                                | Description                                                                  |
| -------------------- | ----------------------------------- | ---------------------------------------------------------------------------- |
| `platform`           | `'chatgpt' \| 'claude' \| 'gemini'` | AI platform identifier                                                       |
| `conversation_id`    | `string \| null`                    | Namespaced conversation ID (`chatgpt:${id}`, `claude:${id}`, `gemini:${id}`) |
| `user_message_id`    | `string \| null`                    | Platform user message ID when available (or `null`)                          |
| `message_id`         | `string \| null`                    | Platform assistant message ID when available (or `null`)                     |
| `query.text`         | `string`                            | Extracted user prompt text                                                   |
| `response.text`      | `string`                            | Extracted assistant response (with Markdown code blocks)                     |
| `model.provider`     | `string \| null`                    | Provider name (`openai`, `claude`, `google`)                                 |
| `model.name`         | `string \| null`                    | Explicit model name if exposed by UI (or `null`)                             |
| `conversation_title` | `string \| null`                    | Page/chat title with brand suffixes stripped                                 |
| `observed_at`        | `string` (ISO-8601)                 | Timestamp interaction was observed                                           |
| `capture_context`    | `'on_load' \| 'on_generate'`        | Provenance classification                                                    |
| `source_timestamp`   | `string \| null`                    | Platform timestamp if present in DOM                                         |
| `collector_version`  | `string`                            | Extension collector version (`1.0.0`)                                        |

---

### Platform Adapter Implementations

1. **ChatGPT (`chatgpt.com`, `chat.openai.com`)**
   - **User selector**: `div[class*="text-message"], div[class*="content"], [data-message-author-role="user"]`
   - **Assistant selector**: `.markdown, .prose, div[class*="markdown"]`
   - **Streaming guard**: `button[data-testid="stop-button"]`, `.result-streaming`, `span.streaming-cursor`
   - **URL pattern**: `/c/{uuid}`

2. **Claude (`claude.ai`)**
   - **User selector**: `[data-testid="user-message"]`
   - **Assistant selector**: `[data-testid="assistant-message"], .font-claude-message`
   - **Streaming guard**: `button[aria-label="Stop Response"]`, `[data-testid="streaming-indicator"]`, `.streaming`
   - **URL pattern**: `/chat/{uuid}`

3. **Gemini (`gemini.google.com`)**
   - **User selector**: `user-query, [data-message-author-role="user"], .user-query-container`
   - **Assistant selector**: `model-response, [data-message-author-role="assistant"], .model-response-container`
   - **Streaming guard**: `button[aria-label="Stop response"]`, `mat-spinner`, `.loading`
   - **URL pattern**: `/app/{id}`, `/chat/{id}`

---

### Privacy & Security Guarantee

- **100% Local**: All interaction and conversation records are persisted strictly to the local browser IndexedDB.
- **Zero Remote Telemetry**: No tracking, external databases, analytics endpoints, or external network requests.
- **No Credential Access**: Extension never accesses session tokens, cookies, auth headers, or passwords.
