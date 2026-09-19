// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { DuplicateInteractionError } from '../src/database/types'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import { ClaudeAdapter } from '../src/platforms/claude/adapter'
import {
  isPageGenerating as isClaudeGenerating,
  isTurnStreaming as isClaudeTurnStreaming,
} from '../src/platforms/claude/parser'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import {
  isPageGenerating as isGeminiGenerating,
  isTurnStreaming as isGeminiTurnStreaming,
} from '../src/platforms/gemini/parser'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('Interaction Counting & Deduplication Regression Suite', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let claudeAdapter: ClaudeAdapter
  let geminiAdapter: GeminiAdapter
  let chatgptAdapter: ChatGPTAdapter
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-regression-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
    claudeAdapter = new ClaudeAdapter()
    geminiAdapter = new GeminiAdapter()
    chatgptAdapter = new ChatGPTAdapter()

    // Mock chrome runtime messaging to route DB_SAVE_INTERACTION to repository
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
                      err instanceof Error ? err.message : 'Failed to save interaction',
                      err instanceof DuplicateInteractionError ? 'DUPLICATE_INTERACTION' : undefined
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
    claudeAdapter.stop()
    geminiAdapter.stop()
    chatgptAdapter.stop()
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
  })

  // Test 1: Claude streaming selector
  it('Claude streaming selector: correctly recognizes lowercase and variant Stop response states', () => {
    // 1. Lowercase live Claude production aria-label
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message">Hello</div>
        <div data-testid="assistant-message"><div class="font-claude-message">Streaming...</div></div>
        <button aria-label="Stop response">Stop</button>
      </div>
    `
    expect(isClaudeGenerating(document)).toBe(true)
    const asstEl = document.querySelector('[data-testid="assistant-message"]')!
    expect(isClaudeTurnStreaming(asstEl, document)).toBe(true)

    // 2. Uppercase variation (backward compatibility)
    document.body.innerHTML = `
      <div>
        <button aria-label="Stop Response">Stop</button>
      </div>
    `
    expect(isClaudeGenerating(document)).toBe(true)

    // 3. Stop generating variation
    document.body.innerHTML = `
      <div>
        <button aria-label="Stop generating">Stop</button>
      </div>
    `
    expect(isClaudeGenerating(document)).toBe(true)

    // 4. Test-id stop button
    document.body.innerHTML = `
      <div>
        <button data-testid="stop-button">Stop</button>
      </div>
    `
    expect(isClaudeGenerating(document)).toBe(true)

    // 5. Idle state (send button visible, no stop button)
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message">Hello</div>
        <div data-testid="assistant-message"><div class="font-claude-message">Done!</div></div>
        <button aria-label="Send message">Send</button>
      </div>
    `
    expect(isClaudeGenerating(document)).toBe(false)
    const doneAsstEl = document.querySelector('[data-testid="assistant-message"]')!
    expect(isClaudeTurnStreaming(doneAsstEl, document)).toBe(false)
  })

  // Test 2: Claude incremental response (at least 20 response mutations before completion)
  it('Claude incremental response: 20 incremental DOM mutations during generation result in exactly ONE interaction record', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-stream-20-steps'),
      writable: true,
    })
    document.title = 'Distributed Caching - Claude'

    claudeAdapter.start()

    const query = 'Explain how distributed caching algorithms handle partition tolerance.'
    let responseAcc = ''

    // Simulate 20 incremental mutations with live Claude stop button present
    for (let step = 1; step <= 20; step++) {
      responseAcc += ` token_${step}`
      document.body.innerHTML = `
        <div>
          <div data-testid="user-message"><div>${query}</div></div>
          <div data-testid="assistant-message"><div class="font-claude-message">${responseAcc}</div></div>
          <button aria-label="Stop response">Stop</button>
        </div>
      `
      await claudeAdapter.processConversation()
      // Generation is active: zero records must be persisted
      expect(await interactionRepo.count()).toBe(0)
    }

    // Step 21: Generation completes, stop button disappears, idle send button appears
    responseAcc += ' [COMPLETED]'
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>${query}</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">${responseAcc}</div></div>
        <button aria-label="Send message">Send</button>
      </div>
    `

    await claudeAdapter.processConversation()

    // Exactly one interaction must exist in IndexedDB
    expect(await interactionRepo.count()).toBe(1)
    const records = await interactionRepo.getAll()
    expect(records).toHaveLength(1)
    expect(records[0].platform).toBe('claude')
    expect(records[0].query.text).toBe(query)
    expect(records[0].response.text).toBe(responseAcc.trim())
    expect(records[0].conversation_id).toBe('claude:claude-stream-20-steps')
  })

  // Test 3: Claude multiple interactions
  it('Claude multiple interactions: three sequential Claude queries with streaming mutations produce exactly 3 records', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-three-turns'),
      writable: true,
    })
    claudeAdapter.start()

    const turns = [
      { q: 'Query 1: What is LRU?', a: 'LRU discards the least recently used items first.' },
      { q: 'Query 2: What is LFU?', a: 'LFU discards items based on least frequency of access.' },
      { q: 'Query 3: What is ARC?', a: 'ARC dynamically balances between recency and frequency.' },
    ]

    for (let i = 0; i < turns.length; i++) {
      // Stream 5 chunks with Stop button
      for (let chunk = 1; chunk <= 5; chunk++) {
        let html = '<div>'
        for (let prev = 0; prev < i; prev++) {
          html += `
            <div data-testid="user-message"><div>${turns[prev].q}</div></div>
            <div data-testid="assistant-message"><div class="font-claude-message">${turns[prev].a}</div></div>
          `
        }
        html += `
          <div data-testid="user-message"><div>${turns[i].q}</div></div>
          <div data-testid="assistant-message"><div class="font-claude-message">${turns[i].a.slice(0, chunk * 5)}</div></div>
          <button aria-label="Stop response">Stop</button>
        </div>`
        document.body.innerHTML = html
        await claudeAdapter.processConversation()
      }

      // Complete current turn
      let completedHtml = '<div>'
      for (let prev = 0; prev <= i; prev++) {
        completedHtml += `
          <div data-testid="user-message"><div>${turns[prev].q}</div></div>
          <div data-testid="assistant-message"><div class="font-claude-message">${turns[prev].a}</div></div>
        `
      }
      completedHtml += '<button aria-label="Send message">Send</button></div>'
      document.body.innerHTML = completedHtml
      await claudeAdapter.processConversation()

      expect(await interactionRepo.count()).toBe(i + 1)
    }

    expect(await interactionRepo.count()).toBe(3)
  })

  // Test 4: Claude refresh
  it('Claude refresh: page refresh on completed conversation does not duplicate records', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-refresh-test'),
      writable: true,
    })
    claudeAdapter.start()

    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>Stable Question</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Stable Answer</div></div>
      </div>
    `
    await claudeAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Simulate page refresh: stop old adapter, create fresh adapter instance
    claudeAdapter.stop()
    const refreshedAdapter = new ClaudeAdapter()
    refreshedAdapter.start()

    // Rescan on fresh adapter
    await refreshedAdapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    refreshedAdapter.stop()
  })

  // Test 5: Claude navigation
  it('Claude navigation: SPA navigation from Conversation A to B and returning to A preserves exact counts', async () => {
    // 1. In Conversation A
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/conv-A'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>Topic A1</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Answer A1</div></div>
        <div data-testid="user-message"><div>Topic A2</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Answer A2</div></div>
      </div>
    `
    claudeAdapter.start()
    await claudeAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(2)

    // 2. Navigate to Conversation B
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/conv-B'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>Topic B1</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Answer B1</div></div>
      </div>
    `
    claudeAdapter.handleNavigation('https://claude.ai/chat/conv-A', 'https://claude.ai/chat/conv-B')
    await claudeAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(3)

    // 3. Navigate back to Conversation A
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/conv-A'),
      writable: true,
    })
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>Topic A1</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Answer A1</div></div>
        <div data-testid="user-message"><div>Topic A2</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Answer A2</div></div>
      </div>
    `
    claudeAdapter.handleNavigation('https://claude.ai/chat/conv-B', 'https://claude.ai/chat/conv-A')
    await claudeAdapter.processConversation()

    expect(await interactionRepo.count()).toBe(3)
  })

  // Test 6: Claude /new transition
  it('Claude /new transition: interaction captured during /new binds in-place when conversation UUID becomes available', async () => {
    // 1. Start at /new
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/new'),
      writable: true,
    })
    document.body.innerHTML = ''
    claudeAdapter.start()
    await claudeAdapter.processConversation()

    // Prompt submitted at /new
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message"><div>What is consensus in distributed systems?</div></div>
        <div data-testid="assistant-message"><div class="font-claude-message">Consensus algorithms allow nodes to agree on a state.</div></div>
      </div>
    `
    await claudeAdapter.processConversation()
    // Held in pending buffer
    expect(await interactionRepo.count()).toBe(0)

    // URL transitions to real conversation ID
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-consensus-uuid-999'),
      writable: true,
    })
    document.title = 'Consensus in Distributed Systems - Claude'

    claudeAdapter.handleNavigation(
      'https://claude.ai/new',
      'https://claude.ai/chat/claude-consensus-uuid-999'
    )
    await claudeAdapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const records = await interactionRepo.getAll()
    expect(records).toHaveLength(1)
    expect(records[0].conversation_id).toBe('claude:claude-consensus-uuid-999')
    expect(records[0].query.text).toBe('What is consensus in distributed systems?')

    // Subsequent rescan of the same page must not insert a duplicate
    await claudeAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
  })

  // Test 7: Fingerprint uniqueness
  it('Fingerprint uniqueness: concurrent persistence attempts with the same fingerprint result in exactly ONE physical record', async () => {
    const input = {
      platform: 'claude',
      conversation_id: 'conv-concurrent',
      query: { text: 'Concurrent prompt' },
      response: { text: 'Concurrent response' },
    }

    // Execute two parallel insertions
    const [result1, result2] = await Promise.allSettled([
      interactionRepo.create(input),
      interactionRepo.create(input),
    ])

    // Exactly one should succeed, and one should be rejected as DuplicateInteractionError
    const successes = [result1, result2].filter((r) => r.status === 'fulfilled')
    const duplicates = [result1, result2].filter(
      (r) => r.status === 'rejected' && r.reason instanceof DuplicateInteractionError
    )

    expect(successes).toHaveLength(1)
    expect(duplicates).toHaveLength(1)
    expect(await interactionRepo.count()).toBe(1)
  })

  // Test 8: Gemini streaming
  it('Gemini streaming: multiple DOM mutations during Gemini generation produce exactly one record upon completion', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/gemini-stream-test'),
      writable: true,
    })
    geminiAdapter.start()

    expect(
      isGeminiGenerating({
        querySelector: (sel: string) =>
          sel.includes('Stop response') ? document.createElement('button') : null,
      } as unknown as Document)
    ).toBe(true)

    // Streaming updates
    for (let step = 1; step <= 5; step++) {
      document.body.innerHTML = `
        <div>
          <user-query><div class="query-text">Explain Bloom filters</div></user-query>
          <model-response class="streaming"><div class="response-text">Chunk ${step}</div></model-response>
          <button aria-label="Stop response">Stop</button>
        </div>
      `
      const streamingEl = document.querySelector('model-response')!
      expect(isGeminiTurnStreaming(streamingEl, document)).toBe(true)
      await geminiAdapter.processConversation()
      expect(await interactionRepo.count()).toBe(0)
    }

    // Completion
    document.body.innerHTML = `
      <div>
        <user-query><div class="query-text">Explain Bloom filters</div></user-query>
        <model-response><div class="response-text">Bloom filters are space-efficient probabilistic data structures.</div></model-response>
      </div>
    `
    await geminiAdapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const records = await interactionRepo.getAll()
    expect(records[0].platform).toBe('gemini')
    expect(records[0].query.text).toBe('Explain Bloom filters')
  })

  // Test 9: ChatGPT regression
  it('ChatGPT regression: existing ChatGPT collection lifecycle continues to function correctly without regression', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/chatgpt-regression-test'),
      writable: true,
    })
    chatgptAdapter.start()

    document.body.innerHTML = `
      <div>
        <article data-testid="conversation-turn-1" data-message-author-role="user" data-message-id="cg-u1">
          <div class="whitespace-pre-wrap">What is Raft consensus?</div>
        </article>
        <article data-testid="conversation-turn-2" data-message-author-role="assistant" data-message-id="cg-a1">
          <div class="markdown"><p>Raft is a leader-based consensus algorithm.</p></div>
        </article>
      </div>
    `
    await chatgptAdapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = (await interactionRepo.getAll())[0]
    expect(stored.platform).toBe('chatgpt')
    expect(stored.message_id).toBe('cg-a1')
    expect(stored.user_message_id).toBe('cg-u1')
    expect(stored.fingerprint_strategy).toBe('level_1')

    // Rescan does not duplicate
    await chatgptAdapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)
  })
})
