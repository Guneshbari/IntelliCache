// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import { getAdapterForUrl, registerAdapter } from '../src/platforms/registry'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('ChatGPTAdapter Integration & Lifecycle', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let adapter: ChatGPTAdapter
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-chatgpt-adapter-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
    adapter = new ChatGPTAdapter()

    // Mock chrome runtime to route DB_SAVE_INTERACTION to repository
    globalThis.chrome = {
      runtime: {
        lastError: undefined,
        sendMessage: vi.fn(
          (rawMessage: unknown, callback: (response: ExtensionResponse) => void) => {
            const msg = rawMessage as DbSaveInteractionMessage
            if (msg.type === 'DB_SAVE_INTERACTION') {
              void (async () => {
                try {
                  const created = await interactionRepo.create(msg.payload)
                  if (created.conversation_id) {
                    await conversationRepo.createOrUpdate({
                      id: created.conversation_id,
                      platform: created.platform,
                      title: created.conversation_title,
                      observed_at: created.observed_at,
                    })
                  }
                  callback(createSuccessResponse(created))
                } catch (err) {
                  callback(
                    createErrorResponse(
                      err instanceof Error ? err.message : 'Failed to save interaction'
                    )
                  )
                }
              })()
            } else {
              callback(createSuccessResponse({}))
            }
          }
        ),
      },
    } as unknown as typeof chrome
  })

  afterEach(async () => {
    adapter.stop()
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
  })

  it('correctly handles platform detection and registry lookup', () => {
    expect(adapter.canHandle('https://chatgpt.com/c/123')).toBe(true)
    expect(adapter.canHandle('https://chat.openai.com/c/456')).toBe(true)
    expect(adapter.canHandle('https://claude.ai/chat/789')).toBe(false)
    expect(adapter.canHandle('https://google.com')).toBe(false)

    registerAdapter(adapter)
    const resolved = getAdapterForUrl('https://chatgpt.com/c/test')
    expect(resolved).not.toBeNull()
    expect(resolved?.platform).toBe('chatgpt')
  })

  it('manages lifecycle state correctly (start, isObserving, stop)', () => {
    expect(adapter.isObserving()).toBe(false)

    adapter.start()
    expect(adapter.isObserving()).toBe(true)

    // Idempotent start
    adapter.start()
    expect(adapter.isObserving()).toBe(true)

    adapter.stop()
    expect(adapter.isObserving()).toBe(false)
  })

  it('extracts complete turns from DOM and persists interaction via service-worker database layer', async () => {
    // Setup simulated ChatGPT URL and DOM
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/6789abcd-0000-0000-0000-000000000001'),
      writable: true,
    })
    document.title = 'Vector Search Exploration - ChatGPT'

    document.body.innerHTML = `
      <main>
        <button data-testid="model-switcher-dropdown-button">GPT-4o</button>
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user" data-message-id="usr-msg-101">
            <div class="whitespace-pre-wrap">How do vector embeddings work?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-msg-101">
            <div class="markdown prose">
              <p>Vector embeddings represent semantic concepts as dense numeric vectors in high-dimensional space.</p>
              <button data-testid="copy-turn-action-button">Copy</button>
            </div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    // Verify interaction in DB
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)

    const stored = await interactionRepo.getByConversationId(
      'chatgpt:6789abcd-0000-0000-0000-000000000001'
    )
    expect(stored).toHaveLength(1)
    expect(stored[0].platform).toBe('chatgpt')
    expect(stored[0].conversation_id).toBe('chatgpt:6789abcd-0000-0000-0000-000000000001')
    expect(stored[0].message_id).toBe('asst-msg-101')
    expect(stored[0].query.text).toBe('How do vector embeddings work?')
    expect(stored[0].query.characters).toBe(30)
    expect(stored[0].response.text).toBe(
      'Vector embeddings represent semantic concepts as dense numeric vectors in high-dimensional space.'
    )
    expect(stored[0].model.name).toBe('GPT-4o')
    expect(stored[0].conversation_title).toBe('Vector Search Exploration')
    expect(stored[0].fingerprint_strategy).toBe('level_1')
  })

  it('prevents duplicate captures on repeated DOM processing passes', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/conv-dedup-test'),
      writable: true,
    })

    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-dedup">
          <div>What is LRU?</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-dedup">
          <div class="markdown">Least Recently Used cache eviction.</div>
        </div>
      </article>
    `

    adapter.start()

    // Pass 1: Persists 1 record
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Pass 2: Identical DOM, must NOT insert again
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
  })

  it('does NOT persist interactions when page is actively streaming', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/conv-streaming-test'),
      writable: true,
    })

    document.body.innerHTML = `
      <article>
        <div data-message-author-role="user">
          <div>Write a poem</div>
        </div>
      </article>
      <article>
        <div data-message-author-role="assistant" class="result-streaming">
          <div class="markdown">Roses are red...</div>
        </div>
      </article>
      <button data-testid="stop-button">Stop generating</button>
    `

    adapter.start()
    await adapter.processConversation()

    // Should NOT persist while stop button or streaming class is active
    expect(await interactionRepo.count()).toBe(0)
  })

  it('handles initial new chat flow: holds interaction until URL transitions to /c/{id} and persists once (Priority 2)', async () => {
    // 1. Start at root new-chat URL
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })
    document.title = 'ChatGPT'

    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-newchat-1">
          <div>What is semantic caching?</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-newchat-1">
          <div class="markdown">Semantic caching matches queries by semantic similarity.</div>
        </div>
      </article>
    `

    adapter.start()
    await adapter.processConversation()

    // Immediately after generation at /, should be held in pending queue, NOT yet persisted with null ID
    expect(await interactionRepo.count()).toBe(0)

    // 2. ChatGPT updates URL to /c/{uuid} and title to conversation title
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/real-new-conv-uuid-999'),
      writable: true,
    })
    document.title = 'Semantic Caching Intro - ChatGPT'

    // Trigger mutation event / conversation pass
    await adapter.processConversation()

    // 3. Verify exactly 1 interaction was persisted with the real namespaced conversation ID
    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored).toHaveLength(1)
    expect(stored[0].conversation_id).toBe('chatgpt:real-new-conv-uuid-999')
    expect(stored[0].conversation_title).toBe('Semantic Caching Intro')
    expect(stored[0].user_message_id).toBe('u-newchat-1')
    expect(stored[0].message_id).toBe('a-newchat-1')

    // 4. Repeated passes must NOT duplicate
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
  })

  it('persists with conversation_id: null if URL transition never occurs after bounded timeout (Priority 2)', async () => {
    const fastAdapter = new ChatGPTAdapter({ newChatTimeoutMs: 30 })

    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-timeout">
          <div>Stateless single query</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-timeout">
          <div class="markdown">Stateless response</div>
        </div>
      </article>
    `

    fastAdapter.start()
    await fastAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(0)

    // Wait for fast timeout (30ms)
    await new Promise((resolve) => setTimeout(resolve, 80))

    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored[0].conversation_id).toBeNull()
    expect(stored[0].fingerprint_strategy).toBe('level_3')

    fastAdapter.stop()
  })

  it('assigns on_load for historical interactions on start and on_generate for new live interactions (Priority 3)', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/historical-conv-1'),
      writable: true,
    })

    // Step 1: Initial page load with existing turn
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-hist-1">
          <div>Old Query</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-hist-1">
          <div class="markdown">Old Response</div>
        </div>
      </article>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const histRecord = (await interactionRepo.getAll())[0]
    expect(histRecord.capture_context).toBe('on_load')

    // Step 2: User asks new question (live generation)
    document.body.innerHTML += `
      <article data-testid="conversation-turn-2">
        <div data-message-author-role="user" data-message-id="u-live-2">
          <div>New Live Query</div>
        </div>
      </article>
      <article data-testid="conversation-turn-3">
        <div data-message-author-role="assistant" data-message-id="a-live-2">
          <div class="markdown">New Live Response</div>
        </div>
      </article>
    `

    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(2)

    const all = await interactionRepo.getAll()
    expect(all[1].capture_context).toBe('on_generate')
  })

  it('persists regenerated assistant responses as distinct entries while preserving user_message_id (Priority 7)', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/conv-regen-integration'),
      writable: true,
    })

    // Generation 1
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-regen-prompt">
          <div>Tell me a fact</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-regen-v1">
          <div class="markdown">Honey never spoils.</div>
        </div>
      </article>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // User clicks regenerate -> Assistant turn replaced in DOM with new text & message ID
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-regen-prompt">
          <div>Tell me a fact</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-regen-v2">
          <div class="markdown">Octopuses have three hearts.</div>
        </div>
      </article>
    `

    await adapter.processConversation()

    // Both distinct responses preserved
    expect(await interactionRepo.count()).toBe(2)
    const records = await interactionRepo.getAll()
    expect(records[0].user_message_id).toBe('u-regen-prompt')
    expect(records[0].message_id).toBe('a-regen-v1')
    expect(records[0].response.text).toBe('Honey never spoils.')

    expect(records[1].user_message_id).toBe('u-regen-prompt')
    expect(records[1].message_id).toBe('a-regen-v2')
    expect(records[1].response.text).toBe('Octopuses have three hearts.')
  })

  // ─── capture_context Regression Tests (Bug: new-chat generation was on_load) ───

  it('REGRESSION: new chat via URL transition (/ → /c/{id}) must produce capture_context = on_generate', async () => {
    // Start at / (new chat page)
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })
    document.title = 'ChatGPT'
    document.body.innerHTML = ''

    adapter.start()

    // Initial 100ms pass: no turns yet, sets isInitialScan = false
    await adapter.processConversation()

    // User sends message; DOM updates with completed user + assistant turns
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-ctx-1">
          <div>IntelliCache acceptance test: explain binary search in three sentences.</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-ctx-1">
          <div class="markdown">Binary search repeatedly halves a sorted array to find a target.</div>
        </div>
      </article>
    `

    // Debounce fires while URL still at / → interaction goes into pending queue
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(0) // still pending

    // ChatGPT assigns conversation ID: URL changes from / to /c/{uuid}
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/binary-search-conv-001'),
      writable: true,
    })
    document.title = 'Binary Search Explanation - ChatGPT'

    // Simulate the MutationObserver firing due to URL change.
    // handleDomMutation() must NOT reset isInitialScan to true for this / → /c/{id} transition.
    // It should flush the pending interaction with the real conversation ID.
    adapter['handleDomMutation']()

    // Drive the processing pass directly.
    // flushPendingWithConversationId() was already called inside handleDomMutation()
    // so the interaction is already enqueued for persistence; processConversation()
    // completes any remaining work synchronously.
    await adapter.processConversation()

    // Verify: exactly 1 interaction, classified as on_generate (not on_load)
    expect(await interactionRepo.count()).toBe(1)
    const records = await interactionRepo.getAll()
    expect(records[0].capture_context).toBe('on_generate')
    expect(records[0].conversation_id).toBe('chatgpt:binary-search-conv-001')
    expect(records[0].message_id).toBe('a-ctx-1')
    expect(records[0].user_message_id).toBe('u-ctx-1')
  })

  it('REGRESSION: existing conversation loaded directly still produces capture_context = on_load', async () => {
    // Start directly at an existing conversation URL (e.g. a bookmark or shared link)
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/existing-conv-historical'),
      writable: true,
    })
    document.title = 'Old Conversation - ChatGPT'

    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-hist-load">
          <div>Historical question from yesterday</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-hist-load">
          <div class="markdown">Historical answer from yesterday.</div>
        </div>
      </article>
    `

    adapter.start() // isInitialScan = true on start
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const records = await interactionRepo.getAll()
    // Loaded from pre-existing page → must be on_load
    expect(records[0].capture_context).toBe('on_load')
    expect(records[0].conversation_id).toBe('chatgpt:existing-conv-historical')
  })

  it('REGRESSION: SPA navigation from existing conversation A to B marks B turns as on_load, not on_generate', async () => {
    // ── Conversation A ──
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/spa-conv-A'),
      writable: true,
    })
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-spa-A">
          <div>Question in conversation A</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-spa-A">
          <div class="markdown">Answer in conversation A.</div>
        </div>
      </article>
    `

    adapter.start() // isInitialScan = true
    await adapter.processConversation() // processes A as on_load, isInitialScan = false

    expect(await interactionRepo.count()).toBe(1)
    const aRecord = (await interactionRepo.getAll())[0]
    expect(aRecord.capture_context).toBe('on_load')

    // ── Navigate to Conversation B (SPA: /c/A → /c/B) ──
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/spa-conv-B'),
      writable: true,
    })
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="u-spa-B">
          <div>Question in conversation B</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="a-spa-B">
          <div class="markdown">Answer in conversation B.</div>
        </div>
      </article>
    `

    // Simulate the MutationObserver detecting the SPA navigation (/c/A → /c/B).
    // handleDomMutation() updates lastObservedUrl and sets isInitialScan = true
    // (because /c/A → /c/B is true SPA navigation between existing conversations).
    adapter['handleDomMutation']()

    // Drive the processing pass directly (same pattern as all other adapter tests).
    // The debounce timer from handleDomMutation would fire after 200ms but afterEach
    // cancels it via adapter.stop(). processConversation() is the unit under test.
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(2)
    const all = await interactionRepo.getAll()
    const bRecord = all.find((r) => r.message_id === 'a-spa-B')!
    // Navigation to pre-existing conversation B → discovered turns must be on_load
    expect(bRecord.capture_context).toBe('on_load')
    expect(bRecord.conversation_id).toBe('chatgpt:spa-conv-B')
  })
})
