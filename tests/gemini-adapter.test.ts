// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import { getAdapterForUrl, registerAdapter } from '../src/platforms/registry'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('GeminiAdapter Integration & Lifecycle', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let adapter: GeminiAdapter
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-gemini-adapter-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
    adapter = new GeminiAdapter()

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
    expect(adapter.canHandle('https://gemini.google.com/app/123')).toBe(true)
    expect(adapter.canHandle('https://chatgpt.com/c/456')).toBe(false)
    expect(adapter.canHandle('https://claude.ai/chat/789')).toBe(false)

    registerAdapter(adapter)
    const resolved = getAdapterForUrl('https://gemini.google.com/app/test')
    expect(resolved).not.toBeNull()
    expect(resolved?.platform).toBe('gemini')
  })

  it('manages lifecycle state correctly (start, isObserving, stop)', () => {
    expect(adapter.isObserving()).toBe(false)
    adapter.start()
    expect(adapter.isObserving()).toBe(true)
    adapter.stop()
    expect(adapter.isObserving()).toBe(false)
  })

  it('extracts complete turns from DOM and persists interaction via service-worker database layer', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/gemini-conv-uuid-123'),
      writable: true,
    })
    document.title = 'Vector Quantization - Gemini'

    document.body.innerHTML = `
      <div>
        <button data-testid="model-selector">Gemini 1.5 Pro</button>
        <user-query data-message-id="u-gem-1">
          <div class="query-text">What is Product Quantization for vectors?</div>
        </user-query>
        <model-response data-message-id="a-gem-1">
          <div class="response-container">
            <p>Product Quantization compresses high-dimensional vectors by breaking them into sub-vectors and quantizing each independently.</p>
          </div>
        </model-response>
      </div>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored).toHaveLength(1)
    expect(stored[0].platform).toBe('gemini')
    expect(stored[0].conversation_id).toBe('gemini:gemini-conv-uuid-123')
    expect(stored[0].conversation_title).toBe('Vector Quantization')
    expect(stored[0].model.provider).toBe('google')
    expect(stored[0].model.name).toBe('Gemini 1.5 Pro')
    expect(stored[0].query.text).toBe('What is Product Quantization for vectors?')
    expect(stored[0].response.text).toBe(
      'Product Quantization compresses high-dimensional vectors by breaking them into sub-vectors and quantizing each independently.'
    )
    expect(stored[0].fingerprint_strategy).toBe('level_1')
  })

  it('prevents duplicate captures on repeated DOM processing passes', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/gemini-dedup-conv'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <user-query data-message-id="u-dedup">
          <div class="query-text">What is semantic caching?</div>
        </user-query>
        <model-response data-message-id="a-dedup">
          <div class="response-container">
            <p>It caches responses based on query meaning.</p>
          </div>
        </model-response>
      </div>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Repeat pass
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
  })

  it('does NOT persist interactions when page is actively streaming', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/gemini-stream-conv'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <user-query>
          <div class="query-text">Generate data pipeline</div>
        </user-query>
        <model-response class="loading">
          <mat-spinner></mat-spinner>
          <div>Pipeline loading...</div>
        </model-response>
        <button aria-label="Stop response">Stop</button>
      </div>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(0)
  })

  it('handles initial new chat flow: holds interaction until URL transitions to /app/{id} and persists with on_generate', async () => {
    // 1. Start at /
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/'),
      writable: true,
    })
    document.title = 'Gemini'

    document.body.innerHTML = ''
    adapter.start()

    // Initial pass: no turns yet, sets isInitialScan = false
    await adapter.processConversation()

    // Generation completes at /
    document.body.innerHTML = `
      <div>
        <user-query data-message-id="u-new-gem-1">
          <div class="query-text">Explain Bloom filters</div>
        </user-query>
        <model-response data-message-id="a-new-gem-1">
          <div class="response-container">
            <p>Bloom filters are space-efficient probabilistic data structures for set membership.</p>
          </div>
        </model-response>
      </div>
    `

    await adapter.processConversation()
    // Held in pending queue
    expect(await interactionRepo.count()).toBe(0)

    // URL transitions to /app/{uuid}
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/bloom-filter-uuid-999'),
      writable: true,
    })
    document.title = 'Bloom Filter Deep Dive - Gemini'

    adapter['handleDomMutation']()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored[0].conversation_id).toBe('gemini:bloom-filter-uuid-999')
    expect(stored[0].capture_context).toBe('on_generate')
  })

  it('persists with conversation_id: null if URL transition never occurs after bounded timeout', async () => {
    const fastAdapter = new GeminiAdapter({ newChatTimeoutMs: 30 })

    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <user-query data-message-id="u-timeout">
          <div class="query-text">Stateless query</div>
        </user-query>
        <model-response data-message-id="a-timeout">
          <div class="response-container">Stateless response</div>
        </model-response>
      </div>
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

  it('SPA navigation from conversation A to B marks B turns as on_load', async () => {
    // Conv A
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/conv-gemini-A'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <user-query data-message-id="u-A"><div class="query-text">Topic A</div></user-query>
        <model-response data-message-id="a-A"><div class="response-container">Response A</div></model-response>
      </div>
    `
    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
    const aRecord = (await interactionRepo.getAll())[0]
    expect(aRecord.capture_context).toBe('on_load')

    // Navigate to Conv B
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/conv-gemini-B'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <user-query data-message-id="u-B"><div class="query-text">Topic B</div></user-query>
        <model-response data-message-id="a-B"><div class="response-container">Response B</div></model-response>
      </div>
    `
    adapter['handleDomMutation']()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(2)
    const all = await interactionRepo.getAll()
    const bRecord = all.find((r) => r.message_id === 'a-B')!
    expect(bRecord.capture_context).toBe('on_load')
    expect(bRecord.conversation_id).toBe('gemini:conv-gemini-B')
  })
})
