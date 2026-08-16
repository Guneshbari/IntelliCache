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
})
