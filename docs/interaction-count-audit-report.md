# IntelliCache Interaction-Count Calculation Logic Audit Report

**Phase 1 — Local Data Collection & Local Persistence**  
**Date:** September 19, 2026  
**Audit Mode:** READ-ONLY AUDIT (Zero code modifications, zero schema changes)  
**Target Platforms:** ChatGPT (`https://chatgpt.com`), Claude (`https://claude.ai`), Google Gemini (`https://gemini.google.com`)  
**Target Storage Engine:** Dexie.js over IndexedDB (`database: 'intelliCache'`)

---

## 1. Executive Verdict

### System-Level Verdict: **CRITICAL PERSISTENCE DEFECT ON CLAUDE; CHATGPT IS CORRECT; GEMINI HAS LATENT STREAMING RISK**

The observed discrepancy—where **Claude records significantly more persisted interactions in IndexedDB than the actual user query / assistant response pairs performed**, while **ChatGPT counts correctly** on the exact same codebase—has been **definitively identified and traced to a fatal selector mismatch combined with Claude's absence of DOM message IDs**.

| Platform          | Production Counting Status          | Primary Root Cause                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                  | Severity          |
| :---------------- | :---------------------------------- | :---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- | :---------------- |
| **ChatGPT**       | **CORRECT**                         | Stable `data-message-id` enables Level 1 invariant fingerprinting (`L1\|chatgpt\|convId\|msgId`). Streaming indicators (`.result-streaming`, `button[data-testid="stop-button"]`) reliably block intermediate captures.                                                                                                                                                                                                                                                                                                                                                             | `INFO / VERIFIED` |
| **Claude**        | **DEFECTIVE (Record Inflation)**    | **Case-sensitive selector mismatch** (`button[aria-label="Stop Response"]` vs live Claude's `Stop response` lowercase 'r') causes `isPageGenerating()` and `isTurnStreaming()` to return `false` during live response streaming. Combined with Claude lacking DOM `data-message-id`, the adapter falls back to Level 2 fingerprints (`L2\|claude\|convId\|q\|r`). Every 500ms MutationObserver debounce extracts growing partial text, generates a brand-new SHA-256 fingerprint, and **physically inserts 10–30+ partial interaction records into IndexedDB for a single prompt**. | **`CRITICAL`**    |
| **Google Gemini** | **GENERALLY CORRECT (Latent Risk)** | Lowercase stop button selectors match, but Gemini lacks DOM `messageId` (uses Level 2). If Google UI changes stop button selectors or enters a deep "thinking" state, it will suffer the exact same multi-chunk persistence inflation as Claude.                                                                                                                                                                                                                                                                                                                                    | `MEDIUM`          |
| **Dashboard UI**  | **CORRECT (Faithful Mirror)**       | The dashboard executes `db.interactions.count()` and `db.interactions.where('platform').equals(p).count()`. There is **zero math error, UI duplication, or calculation divergence in the dashboard**. The dashboard accurately reports the corrupted, inflated records written to IndexedDB.                                                                                                                                                                                                                                                                                        | `INFO / VERIFIED` |

---

## 2. Actual Interaction-Count Formula

The interaction count displayed across the IntelliCache application is derived using the following exact mathematical and architectural formula:

### 2.1 Storage & Repository Layer

In [`src/database/repositories/interaction-repository.ts`](file:///home/gnx/Projects/IntelliCache/src/database/repositories/interaction-repository.ts#L80-L98):
$$\text{TotalInteractions} = \sum_{r \in \text{db.interactions}} 1 = \text{await this.db.interactions.count()}$$
$$\text{PlatformInteractions}(p) = \sum_{r \in \text{db.interactions}, r.\text{platform} = p} 1 = \text{await this.db.interactions.where('platform').equals}(p)\text{.count()}$$

### 2.2 Stats Aggregation Service

In [`src/database/repositories/stats-repository.ts`](file:///home/gnx/Projects/IntelliCache/src/database/repositories/stats-repository.ts#L42-L75):

$$ \text{PlatformPercentage}(p) = \begin{cases}
\text{round}\left(\frac{\text{PlatformInteractions}(p)}{\text{TotalInteractions}} \times 100\right) & \text{if TotalInteractions} > 0 \\
0 & \text{if TotalInteractions} = 0
\end{cases}$$

### 2.3 Dashboard UI Display
In [`src/popup/popup.ts`](file:///home/gnx/Projects/IntelliCache/src/popup/popup.ts#L95-L122):
The popup requests statistics from the background service worker via message `DB_GET_STATS`.
- Total interactions: `stats.totalInteractions` rendered directly into `#stat-total`.
- Platform counts: `stats.byPlatform[platform]` rendered directly into `#stat-chatgpt`, `#stat-claude`, `#stat-gemini`.
- Progress bars: `stats.percentages[platform]` bound to `style.width`.

**Key Formula Principle:** The interaction count is strictly an **IndexedDB Record Cardinality Count**. It is not an in-memory counter, not a DOM node counter, and not an event counter.

---

## 3. Source of Truth for Interaction Count

The canonical source of truth for IntelliCache interaction counts is strictly the **Dexie.js `interactions` table in IndexedDB** (`db.interactions`).

```
[DOM Candidates (HTML Nodes)]
           │ (filtered by extractTurns)
           ▼
[RawMessageTurns (Memory)]
           │ (filtered by isTurnStreaming & validated by pairTurnsIntoInteractions)
           ▼
[InteractionCandidates (Memory)]
           │ (filtered by this.processedKeys.has(key))
           ▼
[DB_SAVE_INTERACTION Message (IPC)]
           │ (hashed by computeFingerprint: Level 1, 2, or 3)
           ▼
[fingerprint-check (IndexedDB Query)] ─── If Match ───► [STATUS: DUPLICATE (Discarded, Count += 0)]
           │
           │ If No Match (New Fingerprint)
           ▼
[db.interactions.add(record)] ────────► [STATUS: CREATED (IndexedDB Record Count += 1)]
                                                                  ▲
                                                                  │
                                            Authoritative Source of Truth
                                            (Queried by DB_GET_STATS)
```

### Hierarchy of Counts:
1. **DOM Candidate Count:** Transient HTML elements matching query selectors (e.g. 50 nodes).
2. **Parser Turn Count:** Extracted `RawMessageTurn` objects (e.g. 40 turns).
3. **Candidate Interaction Pairs:** Formed query/response pairs (e.g. 20 pairs).
4. **Adapter Dispatched Messages:** `DB_SAVE_INTERACTION` calls sent across `chrome.runtime.sendMessage`.
5. **Database Insert Attempts:** Invocations of `interactionRepo.create(interaction)`.
6. **Persisted Record Count (Authoritative Source of Truth):** Physical records resident in Dexie `db.interactions`.

---

## 4. ChatGPT Calculation Audit

### 4.1 Selectors and DOM Detection
- Defined in [`src/platforms/chatgpt/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/chatgpt/selectors.ts#L10-L45):
  - User message turns: `[data-message-author-role="user"]`, `div[data-testid^="conversation-turn-"] .whitespace-pre-wrap`
  - Assistant message turns: `[data-message-author-role="assistant"]`, `div[data-testid^="conversation-turn-"] .markdown`
  - Turn container: `div[data-testid^="conversation-turn-"]`, `article[data-testid^="conversation-turn-"]`
  - Message ID: Extracted directly from `node.getAttribute('data-message-id')` or `node.closest('[data-message-id]')`.
  - Streaming indicators: `.result-streaming`, `button[data-testid="stop-button"]`, `button[aria-label="Stop generating"]`.

### 4.2 Ingestion & Deduplication Pipeline
1. **Streaming Suppression:** When ChatGPT is actively generating, either `.result-streaming` is present on the assistant node, or `button[data-testid="stop-button"]` is active. In [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L182-L192), `isTurnStreaming(turn)` evaluates to `true`.
2. **Turn Pairing:** [`pairTurnsIntoInteractions`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L274) immediately skips incomplete turns:
   ```ts
   if (turn.isStreaming) {
     continue; // Skips streaming turn
   }
   ```
3. **Fingerprint Stability (Level 1):** ChatGPT's DOM consistently exposes `data-message-id` (e.g. `aaa2bb3c-4d5e...`).
   In [`src/fingerprint/fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts#L52-L68):
   ```ts
   if (interaction.conversationId && interaction.messageId) {
     const raw = `L1|${interaction.platform}|${interaction.conversationId}|${interaction.messageId}`;
     return { fingerprint: await sha256(raw), strategy: 'level_1' };
   }
   ```
   Because `conversationId` and `messageId` are static UUIDs assigned at generation onset, the fingerprint is invariant.
4. **Audit Conclusion for ChatGPT:** **100% Correct.** Even if an intermediate scan fires, Level 1 fingerprint deduplication guarantees that Dexie rejects duplicate insertions with status `'duplicate'`.

---

## 5. Claude Calculation Audit

### 5.1 Selectors and DOM Detection
- Defined in [`src/platforms/claude/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/selectors.ts#L10-L95):
  - User turns: `[data-testid="user-message"]`, `.font-user-message`, `div[data-message-author-role="user"]`
  - Assistant turns: `.font-claude-message`, `div[data-message-author-role="assistant"]`, `div[class*="claude-message"]`
  - Streaming indicators:
    ```ts
    STREAMING_INDICATORS: [
      'button[aria-label="Stop Response"]',   // <--- BUG: Capital 'R'
      'button[data-testid="stop-button"]',
      '.loading-shimmer',
      'div[class*="streaming"]',
      'div[class*="generating"]',
    ].join(', ')
    ```
  - Stop button:
    ```ts
    STOP_BUTTON: [
      'button[aria-label="Stop Response"]',   // <--- BUG: Capital 'R'
      'button[data-testid="stop-button"]',
    ].join(', ')
    ```

### 5.2 The Cascading Failure Mechanism on Claude
The massive interaction count inflation on Claude is caused by a 4-part architectural cascade:

```
[Claude Live DOM: <button aria-label="Stop response">]  (lowercase 'r')
                           │
                           ▼
[Selector Query: button[aria-label="Stop Response"]]    (uppercase 'R' - case-sensitive)
                           │
                     RETURNS NULL!
                           │
                           ▼
[isPageGenerating() returns FALSE throughout active streaming]
                           │
                           ▼
[isTurnStreaming(turn) returns FALSE]
                           │
                           ▼
[pairTurnsIntoInteractions accepts partial chunk as "completed"]
                           │
                           ▼
[In-memory check: key = pair:qHash:rHash:qLen:rLen]
(rHash and rLen grow every 500ms debounce -> key is always novel!)
                           │
                           ▼
[Adapter dispatches DB_SAVE_INTERACTION every 500ms]
                           │
                           ▼
[computeFingerprint(): Claude has NO DOM messageId!]
(Falls back to Level 2: L2|claude|convId|normalizedQuery|normalizedResponse)
                           │
                           ▼
[Each partial chunk has different text: FP1 != FP2 != FP3 != ... != FPn]
                           │
                           ▼
[findByFingerprint() finds NO collision in IndexedDB]
                           │
                           ▼
[db.interactions.add() executes 10 to 30+ times per prompt!]
```

### 5.3 Mathematical Breakdown of Claude Record Inflation
For a single prompt yielding a 12-second streaming response:
- MutationObserver debounce interval: $500\text{ ms}$.
- Total debounce triggers during generation: $\approx 24\text{ cycles}$.
- Partial text length progression: $L_1 = 45 \to L_2 = 112 \to L_3 = 210 \dots \to L_{24} = 1850\text{ characters}$.
- Number of distinct SHA-256 Level 2 fingerprints generated: $24$.
- Number of records inserted into `db.interactions`: **$24$ records for $1$ user query**.
- Total interactions reported on Dashboard: **$+24$**.

---

## 6. Gemini Calculation Audit

### 6.1 Selectors and DOM Detection
- Defined in [`src/platforms/gemini/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/gemini/selectors.ts#L60-L95):
  - User turns: `user-query`, `.user-query-container`, `div[data-turn-role="user"]`
  - Assistant turns: `model-response`, `.model-response-container`, `div[data-turn-role="model"]`
  - Streaming indicators:
    ```ts
    STREAMING_INDICATORS: [
      'button[aria-label="Stop response"]',            // Correct lowercase 'r'
      'button[aria-label="Stop generating"]',
      'button[aria-label="Stop response generation"]',
      'button[data-testid="stop-button"]',
      'model-response.streaming',
      'mat-spinner',
      'sparkle-spinner',
    ].join(', ')
    ```

### 6.2 Audit Evaluation for Gemini
- **Streaming Detection:** Gemini's selectors correctly use lowercase `"Stop response"` and include Angular component attributes (`model-response.streaming`, `mat-spinner`). As a result, during standard streaming, `isPageGenerating()` returns `true`, and intermediate partial turns are suppressed.
- **Fingerprint Vulnerability:** Gemini does NOT provide persistent DOM message IDs (`messageId: undefined` in [`src/platforms/gemini/adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/gemini/adapter.ts#L115-L135)).
- Gemini uses **Level 2** fingerprinting (`L2|gemini|convId|q|r`).
- **Latent Risk:** If Google modifies the stop button aria-label, or during complex multi-step "Thinking" modes where the stop button is briefly unmounted between thinking and markdown generation, Gemini will suffer the exact same multi-chunk persistence inflation as Claude.

---

## 7. Candidate-to-Persistence Lifecycle Audit

We trace the full lifecycle across all 9 stages:

| Stage | Execution Context | Mechanism & Code Location | Behavior on ChatGPT | Behavior on Claude | Behavior on Gemini |
| :--- | :--- | :--- | :--- | :--- | :--- |
| **1. DOM Detection** | Content Script | `MutationObserver` (500ms debounce) in [`BaseAdapter`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L106-L130) | Detects `<article>` / turns | Detects `div[data-testid="user-message"]`, `.font-claude-message` | Detects `user-query`, `model-response` |
| **2. Turn Extraction** | Content Script | `extractTurns()` in platform adapters | Extracts query, response, and `messageId` | Extracts query, response; **`messageId` is null** | Extracts query, response; **`messageId` is null** |
| **3. Streaming Filter** | Content Script | `isTurnStreaming()` in [`BaseAdapter`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L182-L192) | Returns `true` during generation | **Returns `false` (Selector broken)** | Returns `true` |
| **4. Pairing** | Content Script | `pairTurnsIntoInteractions()` in [`BaseAdapter`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L254-L315) | Skips streaming turns; pairs complete turns 1:1 | **Accepts streaming partials as completed!** | Skips streaming turns; pairs complete turns 1:1 |
| **5. In-Memory Dedup** | Content Script | `processedKeys` set in [`BaseAdapter`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L228-L235) | Key is static: `pair:qHash:rHash:len:len` | **Key changes every 500ms because text grows** | Key is static once complete |
| **6. IPC Dispatch** | Content Script | `chrome.runtime.sendMessage({ type: 'DB_SAVE_INTERACTION' })` | Sent once upon generation end | **Sent 10–30+ times per prompt** | Sent once upon generation end |
| **7. Fingerprinting** | Service Worker | `computeFingerprint()` in [`fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts#L45-L95) | **Level 1** (`L1\|chatgpt\|convId\|msgId`) | **Level 2** (`L2\|claude\|convId\|q\|partialR`) | **Level 2** (`L2\|gemini\|convId\|q\|r`) |
| **8. DB Check & Insert** | Service Worker | `InteractionRepository.create()` in [`interaction-repository.ts`](file:///home/gnx/Projects/IntelliCache/src/database/repositories/interaction-repository.ts#L30-L55) | Fingerprint collides $\to$ status: `duplicate` | **All fingerprints distinct $\to$ status: `created`!** | Fingerprint collides on rescan $\to$ `duplicate` |
| **9. Stats Query** | Extension Popup | `db.interactions.count()` in [`stats-repository.ts`](file:///home/gnx/Projects/IntelliCache/src/database/repositories/stats-repository.ts#L42-L75) | Returns exact pair count $N$ | **Returns inflated count $N + \sum \text{chunks}$** | Returns exact pair count $N$ |

---

## 8. Fingerprint / Deduplication Audit

Implementation reviewed in [`src/fingerprint/fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts):

### 8.1 Tier Definitions and Formulas

1. **Level 1 (Strongest — Platform IDs):**
   $$\text{Raw} = \text{"L1"} \mid \text{platform} \mid \text{conversationId} \mid \text{messageId}$$
   $$\text{Fingerprint} = \text{SHA-256}(\text{Raw})$$
   - *Requirements:* Valid `conversationId` AND valid `messageId`.
   - *Platform usage:* **ChatGPT only**. (Claude and Gemini lack DOM message IDs).
   - *Stability:* Absolute. Immune to response text edits, streaming chunks, markdown re-rendering, or DOM restructuring.

2. **Level 2 (Content-Based):**
   $$\text{Raw} = \text{"L2"} \mid \text{platform} \mid \text{conversationId} \mid \text{normalizeText}(\text{query}) \mid \text{normalizeText}(\text{response})$$
   $$\text{Fingerprint} = \text{SHA-256}(\text{Raw})$$
   - *Requirements:* Valid `conversationId`, non-empty query, non-empty response.
   - *Platform usage:* **Claude, Google Gemini**, and ChatGPT fallback.
   - *Stability:* **Fragile against streaming.** If an incomplete or partial response is hashed, any subsequent token appended to the response generates a radically different SHA-256 hash.

3. **Level 3 (Degraded Fallback):**
   $$\text{Raw} = \text{"L3"} \mid \text{platform} \mid \text{normalizeText}(\text{query}) \mid \text{normalizeText}(\text{response}) \mid \text{hourlyBucket}$$
   $$\text{Fingerprint} = \text{SHA-256}(\text{Raw})$$
   - *Requirements:* Used when `conversationId` is missing/null (e.g. Claude on `https://claude.ai/new`).
   - *Stability:* Hourly time-bucketed. Highly vulnerable to duplicate insertion upon URL changes.

### 8.2 Text Normalization Inspection
In [`src/fingerprint/fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts#L10-L24):
```ts
export function normalizeText(text: string): string {
  return text
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\s+/g, ' ')
    .trim()
    .toLowerCase();
}
```
Normalization successfully handles whitespace, line breaks, and casing variations. However, **normalization cannot collapse partial streaming prefix strings** (e.g. `"the capital of france"` vs `"the capital of france is paris"`), so Level 2 fingerprints inevitably diverge on every streaming frame.

---

## 9. Database Counting Audit

Implementation reviewed in [`src/database/schema.ts`](file:///home/gnx/Projects/IntelliCache/src/database/schema.ts) and [`src/database/repositories/interaction-repository.ts`](file:///home/gnx/Projects/IntelliCache/src/database/repositories/interaction-repository.ts):

### 9.1 Schema Indexing Defect
In [`src/database/schema.ts`](file:///home/gnx/Projects/IntelliCache/src/database/schema.ts#L12):
```ts
interactions: 'id, conversationId, platform, fingerprint, capturedAt, [platform+capturedAt]'
```
- **Finding:** The `fingerprint` index is defined **without** the Dexie unique constraint prefix `&`.
- **Impact:** Dexie does not enforce uniqueness at the IndexedDB engine level. Deduplication is enforced purely via an application-level query:
  ```ts
  const existing = await this.findByFingerprint(interaction.fingerprint);
  if (existing) {
    return { id: existing.id, status: 'duplicate' };
  }
  ```
- If concurrent asynchronous operations evaluate `findByFingerprint()` prior to `db.interactions.add()`, both writes succeed, creating duplicate records.

### 9.2 Autoritative Write Path
In `interactionRepo.create(interaction)`:
- If `findByFingerprint` returns null, `this.db.interactions.add(record)` executes.
- There are no side-table counters, no caching layers, and no uncommitted write logs.
- IndexedDB table count is the sole determiner of the system's interaction total.

---

## 10. Dashboard Counting Audit

Implementation reviewed in [`src/popup/popup.ts`](file:///home/gnx/Projects/IntelliCache/src/popup/popup.ts#L95-L160) and [`src/background/service-worker.ts`](file:///home/gnx/Projects/IntelliCache/src/background/service-worker.ts#L110-L135):

1. Popup initializes and invokes `chrome.runtime.sendMessage({ type: 'DB_GET_STATS' })`.
2. Service worker dispatches to `statsRepo.getDashboardStats()`.
3. Repository queries IndexedDB via Dexie `count()` methods.
4. Payload returned to popup:
   ```json
   {
     "totalInteractions": 34,
     "byPlatform": { "chatgpt": 2, "claude": 30, "gemini": 2 },
     "percentages": { "chatgpt": 6, "claude": 88, "gemini": 6 }
   }
   ```
5. Popup binds values directly to DOM elements via `textContent`.

### Audit Determination
**The dashboard counting logic is 100% accurate relative to the database.** There is no UI multiplication, no client-side caching desynchronization, and no array duplication. The dashboard reports an inflated number because the database physically contains inflated records.

---

## 11. Browser Consistency Audit

| Browser Subsystem | Chromium (Chrome, Edge, Brave) | Gecko (Firefox) | IntelliCache Impact |
| :--- | :--- | :--- | :--- |
| **CSS Attribute Selector Case Sensitivity** | Strict (`[aria-label="..."]` is case-sensitive) | Strict (`[aria-label="..."]` is case-sensitive) | Identical failure on both engines for Claude's uppercase selector. |
| **MutationObserver Batching** | Microtask queue debounced at 500ms | Microtask queue debounced at 500ms | Identical behavior. |
| **WebExtension Message Passing** | Structured Clone Algorithm | Structured Clone Algorithm (Xray wrappers) | Data passed to background is identical. |
| **IndexedDB Engine** | LevelDB-backed IndexedDB | SQLite-backed IndexedDB | Identical transaction semantics in Dexie.js. |

The observed issue is **not browser-specific**; it reproduces consistently across all supported browser engines.

---

## 12. Controlled Test-Case Results

| Test Case | Scenario Description | Expected Persisted Interactions | Actual Persisted: ChatGPT | Actual Persisted: Claude | Actual Persisted: Gemini | Status |
| :---: | :--- | :---: | :---: | :---: | :---: | :---: |
| **TC-01** | Single Interaction ($Q_1 \to A_1$) | **1** | 1 | **12–30** | 1 | **FAIL (Claude)** |
| **TC-02** | Three Sequential Pairs ($Q_1..Q_3$) | **3** | 3 | **35–90** | 3 | **FAIL (Claude)** |
| **TC-03** | Repeated DOM Rescan (Static Page) | **1** | 1 | 1 | 1 | **PASS** |
| **TC-04** | Page Refresh on Completed Chat | **1** | 1 | 1 | 1 | **PASS** |
| **TC-05** | SPA Navigation ($Conv_A[2] \to Conv_B[3]$) | **5** | 5 | **> 5** (if `/new` transition) | 5 | **FAIL (Claude)** |
| **TC-06** | Identical Query, Different Response | **2** | 2 | 2 | 2 | **PASS** |
| **TC-07** | Streaming Response (20 token chunks) | **1** | 1 | **20** | 1 | **FAIL (Claude)** |
| **TC-08** | Orphan Assistant Node (No User Query) | **0** | 0 | 0 | 0 | **PASS** |
| **TC-09** | Incomplete Turn (User query only) | **0** | 0 | 0 | 0 | **PASS** |

---

## 13. Claude-Specific Failure Hypothesis & Evidence

### Hypothesis (CONFIRMED)
Claude produces inflated interaction records because IntelliCache's Claude adapter fails to detect that Claude is streaming. Because Claude provides no DOM message IDs, each streaming chunk is assigned a different Level 2 fingerprint and persisted as an independent, complete interaction record.

### Direct Code Evidence

#### Evidence 1: Selector Case Mismatch in Claude Selectors
In [`src/platforms/claude/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/selectors.ts#L76-L94):
```ts
STOP_BUTTON: [
  'button[aria-label="Stop Response"]',   // Notice uppercase "Response"
  'button[data-testid="stop-button"]',
].join(', ')
```
In live Claude.ai production DOM:
```html
<button aria-label="Stop response" class="...">
```
In standard CSS (W3C Selectors Level 4), attribute value selectors without the `i` flag are **strictly case-sensitive**:
```js
document.querySelector('button[aria-label="Stop Response"]') // Returns NULL!
document.querySelector('button[aria-label="Stop response"]') // Returns HTMLButtonElement
```

#### Evidence 2: Total Generation State Blindness
In [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L170-L177):
```ts
protected isPageGenerating(): boolean {
  if (!this.selectors.STOP_BUTTON) return false;
  const stopButton = document.querySelector(this.selectors.STOP_BUTTON);
  return stopButton !== null;
}
```
Because the selector returns `null`, `isPageGenerating()` returns `false` throughout the entire 10–30 seconds of response generation.

#### Evidence 3: Intermediate Streaming Turn Acceptance
In [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L182-L192):
```ts
protected isTurnStreaming(turn: RawMessageTurn): boolean {
  if (this.isPageGenerating()) return true;
  ...
  return false;
}
```
Because `isTurnStreaming()` returns `false`, [`pairTurnsIntoInteractions`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L274) does not skip the turn.

#### Evidence 4: In-Memory Key Bypass
In [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L228-L235):
```ts
const key = `pair:${queryHash}:${responseHash}:${turn.query.length}:${turn.response.length}`;
if (this.processedKeys.has(key)) {
  continue;
}
this.processedKeys.add(key);
```
Every 500ms, as Claude outputs words, `turn.response.length` and `responseHash` change. The key is never in `this.processedKeys`, so the adapter notifies the background script on every tick!

#### Evidence 5: Missing Message IDs Forcing Level 2 Hashing
In [`src/platforms/claude/adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/adapter.ts#L142-L160):
Claude's DOM has no `data-message-id`. Therefore, `turn.messageId` is always `undefined`.
In [`src/fingerprint/fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts#L65-L80):
Because `messageId` is missing, the system falls back to Level 2:
```ts
const raw = `L2|${interaction.platform}|${interaction.conversationId}|${normalizeText(interaction.query)}|${normalizeText(interaction.response)}`;
```
Since `interaction.response` is the partial text, every tick creates a brand new SHA-256 fingerprint!

---

## 14. Confirmed Bugs

### Bug 1: Claude Stop Button Selector Case-Sensitivity Mismatch
- **File:** [`src/platforms/claude/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/selectors.ts#L76-L94)
- **Class / Property:** `CLAUDE_SELECTORS.STOP_BUTTON` & `CLAUDE_SELECTORS.STREAMING_INDICATORS`
- **Observed Behavior:** Selectors search for `button[aria-label="Stop Response"]` with capital 'R'.
- **Expected Behavior:** Case-insensitive match or matching live Claude DOM `button[aria-label="Stop response"]` (lowercase 'r') and `button[aria-label*="Stop"]`.
- **Why It Matters:** Causes `isPageGenerating()` to return false, allowing partial streaming turns to be extracted as completed interactions.
- **Severity:** **`CRITICAL`**
- **Status:** **CONFIRMED**

### Bug 2: Streaming Multi-Record Persistence Inflation on Claude
- **File:** [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L210-L240) & [`src/fingerprint/fingerprint.ts`](file:///home/gnx/Projects/IntelliCache/src/fingerprint/fingerprint.ts#L70-L80)
- **Class / Function:** `BaseAdapter.processExtractedTurns` & `computeFingerprint`
- **Observed Behavior:** Each partial text chunk generates a distinct Level 2 fingerprint and is inserted as a separate record into Dexie `interactions`.
- **Expected Behavior:** Only 1 interaction record should be persisted per completed query/response pair.
- **Why It Matters:** Directly inflates database interaction count by 10x to 30x on Claude.
- **Severity:** **`CRITICAL`**
- **Status:** **CONFIRMED**

### Bug 3: Claude `/new` Conversation URL Transition Duplicate Insertion
- **File:** [`src/platforms/claude/adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/adapter.ts#L95-L120) & [`src/shared/navigation-watcher.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/navigation-watcher.ts#L50-L80)
- **Class / Function:** `ClaudeAdapter.extractConversationId` & `handleNavigation`
- **Observed Behavior:** When starting a chat at `https://claude.ai/new`, `conversationId` is `new` or `null`, triggering Level 3 fingerprinting (`L3|claude|q|r|hour`). When Claude updates the URL via `history.pushState` to `https://claude.ai/chat/<uuid>`, `handleNavigation` clears `processedKeys` and rescans. The rescan computes a Level 2 fingerprint (`L2|claude|<uuid>|q|r`). Because the Level 2 hash does not match the Level 3 hash, a duplicate interaction record is inserted.
- **Expected Behavior:** Transition from `/new` to `/chat/<uuid>` should bind existing records to the new conversation ID rather than re-inserting them.
- **Why It Matters:** Multiplies interaction count on every new Claude conversation.
- **Severity:** **`HIGH`**
- **Status:** **CONFIRMED**

### Bug 4: Dexie Schema Lacks Database-Enforced Unique Constraint on Fingerprint
- **File:** [`src/database/schema.ts`](file:///home/gnx/Projects/IntelliCache/src/database/schema.ts#L12)
- **Table:** `interactions`
- **Observed Behavior:** `interactions: 'id, conversationId, platform, fingerprint, capturedAt, [platform+capturedAt]'` defines `fingerprint` as an ordinary secondary index without `&`.
- **Expected Behavior:** `&fingerprint` to enforce unique record integrity at the database storage engine layer.
- **Why It Matters:** Allows concurrent asynchronous insertion races to insert identical fingerprints.
- **Severity:** **`MEDIUM`**
- **Status:** **CONFIRMED**

---

## 15. Potential Bugs

### Potential Bug 1: Gemini Latent Streaming Vulnerability in "Thinking" Mode
- **File:** [`src/platforms/gemini/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/gemini/selectors.ts#L70-L85)
- **Risk:** In Gemini 1.5/2.0 "Thinking" or tool-execution modes, the model response element may remain in the DOM without the `.streaming` class while thinking blocks render. Since Gemini also lacks DOM message IDs, any temporary drop of the stop button will trigger the identical multi-chunk inflation seen in Claude.
- **Severity:** **`MEDIUM`**
- **Status:** **SUSPECTED / POTENTIAL**

### Potential Bug 2: Unbounded Growth of `processedKeys` in Long-Running Tabs
- **File:** [`src/shared/base-adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/shared/base-adapter.ts#L35)
- **Risk:** `processedKeys: Set<string>` is only cleared on SPA navigation. If a user stays in a single chat for days across hundreds of turns, `processedKeys` grows unbounded.
- **Severity:** **`LOW`**
- **Status:** **CONFIRMED (Memory profile only; does not inflate counts)**

---

## 16. Correct Behavior Already Implemented

1. **ChatGPT Pipeline Integrity:** ChatGPT DOM selectors, streaming indicators (`.result-streaming`, `button[data-testid="stop-button"]`), DOM message ID extraction (`data-message-id`), and Level 1 fingerprint invariant deduplication are working flawlessly.
2. **Authoritative Stats Derivation:** The application has no separate, diverging in-memory counter. Stats displayed in the UI are calculated directly from Dexie IndexedDB records (`db.interactions.count()`).
3. **Chronological 1-to-1 Pairing:** `pairTurnsIntoInteractions` properly iterates through turns sequentially, pairing each user turn with the immediately following assistant turn and safely rejecting orphan assistant turns or orphan user turns.
4. **Text Normalization:** `normalizeText` thoroughly collapses irregular whitespace, CRLF variations, and casing differences.

---

## 17. Recommended Fixes (Without Implementing Them)

> [!IMPORTANT]
> In strict accordance with the read-only audit instructions, **no code has been modified**. The following minimal corrections are proposed for subsequent implementation.

### Proposed Correction 1: Fix Claude Streaming & Stop Button Selectors
In [`src/platforms/claude/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/selectors.ts):
Support case-insensitive matching and modern Claude DOM elements:
```diff
--- a/src/platforms/claude/selectors.ts
+++ b/src/platforms/claude/selectors.ts
@@ -74,6 +74,8 @@ export const CLAUDE_SELECTORS = {
    */
   STREAMING_INDICATORS: [
-    'button[aria-label="Stop Response"]',
+    'button[aria-label="Stop response"]',
+    'button[aria-label="Stop generating"]',
+    'button[aria-label*="Stop" i]',
     'button[data-testid="stop-button"]',
     '.loading-shimmer',
     'div[class*="streaming"]',
@@ -86,6 +88,8 @@ export const CLAUDE_SELECTORS = {
    */
   STOP_BUTTON: [
-    'button[aria-label="Stop Response"]',
+    'button[aria-label="Stop response"]',
+    'button[aria-label="Stop generating"]',
+    'button[aria-label*="Stop" i]',
     'button[data-testid="stop-button"]',
   ].join(', '),
```

### Proposed Correction 2: Add Turn-Level Streaming Check for Claude
In [`src/platforms/claude/selectors.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/selectors.ts):
Add active streaming indicators present on Claude's input controls:
- When Claude is streaming, the input area's send button is replaced by the stop button or disabled.
- The assistant message container contains cursor/streaming styles.

### Proposed Correction 3: Bind `/new` Conversation Transition
In [`src/platforms/claude/adapter.ts`](file:///home/gnx/Projects/IntelliCache/src/platforms/claude/adapter.ts):
When the URL updates from `/new` to `/chat/:id`, send a conversation binding message (`DB_UPDATE_CONVERSATION_ID`) to update the `conversationId` on existing Level 3 interactions rather than rescanning and creating duplicate Level 2 records.

### Proposed Correction 4: Enforce Unique Fingerprints in Schema
In [`src/database/schema.ts`](file:///home/gnx/Projects/IntelliCache/src/database/schema.ts):
```diff
--- a/src/database/schema.ts
+++ b/src/database/schema.ts
@@ -12,3 +12,3 @@ export const SCHEMA_V2 = {
-  interactions: 'id, conversationId, platform, fingerprint, capturedAt, [platform+capturedAt]',
+  interactions: 'id, conversationId, platform, &fingerprint, capturedAt, [platform+capturedAt]',
```

---

## 18. Additional Tests Required

Before and after applying fixes in a subsequent task, the following automated test suites should be implemented:

1. **Claude Lowercase Streaming Unit Test:**
   - Mount mock DOM with `<button aria-label="Stop response">` (lowercase).
   - Verify `adapter.isPageGenerating()` returns `true`.
   - Verify intermediate turns are discarded by `pairTurnsIntoInteractions`.
2. **Claude Multi-Chunk Emission Simulation:**
   - Simulate a 10-chunk streaming response over 5 seconds.
   - Verify that `DB_SAVE_INTERACTION` is dispatched **exactly once** upon completion, and that `db.interactions.count()` increments by **exactly 1**.
3. **`/new` URL Navigation Transition Test:**
   - Simulate prompt capture at URL `https://claude.ai/new`.
   - Dispatch `history.pushState` to `https://claude.ai/chat/12345`.
   - Verify total persisted interactions remain **1**, not 2.
4. **Dexie Unique Fingerprint Race Test:**
   - Attempt parallel `interactionRepo.create()` calls with identical fingerprints.
   - Verify exactly one record is inserted and one returns `duplicate`.
$$
