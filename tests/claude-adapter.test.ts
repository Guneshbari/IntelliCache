// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { ClaudeAdapter } from '../src/platforms/claude/adapter'
import { getAdapterForUrl, registerAdapter } from '../src/platforms/registry'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('ClaudeAdapter Integration & Lifecycle', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let adapter: ClaudeAdapter
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-claude-adapter-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
    adapter = new ClaudeAdapter()

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
    expect(adapter.canHandle('https://claude.ai/chat/123')).toBe(true)
    expect(adapter.canHandle('https://chatgpt.com/c/456')).toBe(false)
    expect(adapter.canHandle('https://gemini.google.com/app/789')).toBe(false)

    registerAdapter(adapter)
    const resolved = getAdapterForUrl('https://claude.ai/chat/test')
    expect(resolved).not.toBeNull()
    expect(resolved?.platform).toBe('claude')
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
      value: new URL('https://claude.ai/chat/claude-conv-uuid-123'),
      writable: true,
    })
    document.title = 'Vector Indexing Optimization - Claude'

    document.body.innerHTML = `
      <div>
        <button data-testid="model-selector-trigger">Claude 3.5 Sonnet</button>
        <div data-testid="user-message" data-message-id="u-msg-1">
          <div>How do HNSW vector indexes work?</div>
        </div>
        <div data-testid="assistant-message" data-message-id="a-msg-1">
          <div class="font-claude-message">
            <p>HNSW builds multi-layer graphs for approximate nearest neighbor search.</p>
          </div>
        </div>
      </div>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored).toHaveLength(1)
    expect(stored[0].platform).toBe('claude')
    expect(stored[0].conversation_id).toBe('claude:claude-conv-uuid-123')
    expect(stored[0].conversation_title).toBe('Vector Indexing Optimization')
    expect(stored[0].model.provider).toBe('claude')
    expect(stored[0].model.name).toBe('Claude 3.5 Sonnet')
    expect(stored[0].query.text).toBe('How do HNSW vector indexes work?')
    expect(stored[0].response.text).toBe(
      'HNSW builds multi-layer graphs for approximate nearest neighbor search.'
    )
    expect(stored[0].fingerprint_strategy).toBe('level_1')
  })

  it('prevents duplicate captures on repeated DOM processing passes', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-dedup-conv'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <div data-testid="user-message" data-message-id="u-dedup">
          <div>What is semantic caching?</div>
        </div>
        <div data-testid="assistant-message" data-message-id="a-dedup">
          <div class="font-claude-message">
            <p>It caches responses based on query meaning.</p>
          </div>
        </div>
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
      value: new URL('https://claude.ai/chat/claude-stream-conv'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <div data-testid="user-message">
          <div>Tell me a story</div>
        </div>
        <div data-testid="assistant-message" class="streaming">
          <div class="font-claude-message">Once upon a time...</div>
        </div>
        <button aria-label="Stop Response">Stop</button>
      </div>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(0)
  })

  it('handles initial new chat flow: holds interaction until URL transitions to /chat/{id} and persists with on_generate', async () => {
    // 1. Start at /new
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/new'),
      writable: true,
    })
    document.title = 'Claude'

    document.body.innerHTML = ''
    adapter.start()

    // Initial pass: no turns yet, sets isInitialScan = false
    await adapter.processConversation()

    // Generation completes at /new
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message" data-message-id="u-new-1">
          <div>Explain consistency hashing</div>
        </div>
        <div data-testid="assistant-message" data-message-id="a-new-1">
          <div class="font-claude-message">
            <p>Consistent hashing maps keys and nodes to a circular hash ring.</p>
          </div>
        </div>
      </div>
    `

    await adapter.processConversation()
    // Held in pending queue
    expect(await interactionRepo.count()).toBe(0)

    // URL transitions to /chat/{uuid}
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-ring-uuid-888'),
      writable: true,
    })
    document.title = 'Consistent Hashing Guide - Claude'

    adapter['handleDomMutation']()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = await interactionRepo.getAll()
    expect(stored[0].conversation_id).toBe('claude:claude-ring-uuid-888')
    expect(stored[0].capture_context).toBe('on_generate')
  })

  it('persists with conversation_id: null if URL transition never occurs after bounded timeout', async () => {
    const fastAdapter = new ClaudeAdapter({ newChatTimeoutMs: 30 })

    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/new'),
      writable: true,
    })

    document.body.innerHTML = `
      <div>
        <div data-testid="user-message" data-message-id="u-timeout">
          <div>Stateless single query</div>
        </div>
        <div data-testid="assistant-message" data-message-id="a-timeout">
          <div class="font-claude-message">Stateless response</div>
        </div>
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
      value: new URL('https://claude.ai/chat/conv-A'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message" data-message-id="u-A"><div>Topic A</div></div>
        <div data-testid="assistant-message" data-message-id="a-A"><div class="font-claude-message">Response A</div></div>
      </div>
    `
    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
    const aRecord = (await interactionRepo.getAll())[0]
    expect(aRecord.capture_context).toBe('on_load')

    // Navigate to Conv B
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/conv-B'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message" data-message-id="u-B"><div>Topic B</div></div>
        <div data-testid="assistant-message" data-message-id="a-B"><div class="font-claude-message">Response B</div></div>
      </div>
    `
    adapter.handleNavigation('https://claude.ai/chat/conv-A', 'https://claude.ai/chat/conv-B')
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(2)
    const all = await interactionRepo.getAll()
    const bRecord = all.find((r) => r.message_id === 'a-B')!
    expect(bRecord.capture_context).toBe('on_load')
    expect(bRecord.conversation_id).toBe('claude:conv-B')
  })
})
