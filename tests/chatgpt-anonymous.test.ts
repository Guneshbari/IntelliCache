// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import {
  extractConversationIdFromUrl,
  extractConversationTurns,
} from '../src/platforms/chatgpt/parser'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('ChatGPT Collection: Authenticated & Anonymous Support', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let adapter: ChatGPTAdapter
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-chatgpt-anon-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

    // Wire chrome.runtime.sendMessage to test repositories
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

    adapter = new ChatGPTAdapter({ mutationDebounceMs: 10, newChatTimeoutMs: 100 })
  })

  afterEach(async () => {
    adapter.stop()
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
    document.body.innerHTML = ''
    document.title = ''
  })

  it('Authenticated ChatGPT regression: Existing authenticated ChatGPT interaction behavior remains unchanged', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/68b8e05c-1234-4567-89ab-cdef01234567'),
      writable: true,
    })
    document.title = 'Quantum Computing Basics - ChatGPT'

    document.body.innerHTML = `
      <nav>
        <button data-testid="profile-button" aria-label="User profile">Account</button>
      </nav>
      <main>
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user" data-message-id="auth-user-msg-1">
            <div class="whitespace-pre-wrap">What is quantum superposition?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="auth-asst-msg-1">
            <div class="markdown">Superposition is a fundamental principle of quantum mechanics where a system exists in multiple states simultaneously.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = (await interactionRepo.getAll())[0]
    expect(stored.platform).toBe('chatgpt')
    expect(stored.conversation_id).toBe('chatgpt:68b8e05c-1234-4567-89ab-cdef01234567')
    expect(stored.message_id).toBe('auth-asst-msg-1')
    expect(stored.user_message_id).toBe('auth-user-msg-1')
    expect(stored.fingerprint_strategy).toBe('level_1')
    expect(stored.query.text).toBe('What is quantum superposition?')
    expect(stored.response.text).toContain('Superposition is a fundamental principle')

    // Conversation entity is recorded for authenticated conversation with ID
    const conv = await conversationRepo.getById('68b8e05c-1234-4567-89ab-cdef01234567', 'chatgpt')
    expect(conv).not.toBeNull()
    expect(conv?.title).toBe('Quantum Computing Basics')
  })

  it('Anonymous ChatGPT extraction: ChatGPT exposes a query and response without requiring login and is persisted', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })
    document.title = 'ChatGPT'

    document.body.innerHTML = `
      <header>
        <button data-testid="login-button">Log in</button>
        <button data-testid="signup-button">Sign up</button>
      </header>
      <main>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="anon-u-1">
            <div class="whitespace-pre-wrap">How do B-trees differ from binary search trees?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-id="anon-a-1">
            <div class="markdown">B-trees are multi-way self-balancing trees optimized for disk storage systems with multiple keys per node.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = (await interactionRepo.getAll())[0]
    expect(stored.platform).toBe('chatgpt')
    expect(stored.conversation_id).toBeNull() // Not fabricated!
    expect(stored.fingerprint_strategy).toBe('level_3') // Content-based fallback
    expect(stored.message_id).toBe('anon-a-1')
    expect(stored.query.text).toBe('How do B-trees differ from binary search trees?')
    expect(stored.response.text).toContain('B-trees are multi-way self-balancing trees')
  })

  it('Anonymous multiple interactions: Three anonymous ChatGPT queries receive responses and exactly three are persisted', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <nav>
        <a href="/login" data-testid="login-button">Log in</a>
      </nav>
      <main id="chat-thread">
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="anon-u-1">
            <div class="whitespace-pre-wrap">Question 1: What is DNS?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-id="anon-a-1">
            <div class="markdown">DNS translates human-readable domain names into IP addresses.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Interaction 2
    const thread = document.getElementById('chat-thread')!
    thread.innerHTML += `
      <article data-testid="conversation-turn-3">
        <div data-message-author-role="user" data-message-id="anon-u-2">
          <div class="whitespace-pre-wrap">Question 2: What is TCP?</div>
        </div>
      </article>
      <article data-testid="conversation-turn-4">
        <div data-message-author-role="assistant" data-message-id="anon-a-2">
          <div class="markdown">TCP is a connection-oriented transmission protocol providing reliable byte streams.</div>
        </div>
      </article>
    `
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(2)

    // Interaction 3
    thread.innerHTML += `
      <article data-testid="conversation-turn-5">
        <div data-message-author-role="user" data-message-id="anon-u-3">
          <div class="whitespace-pre-wrap">Question 3: What is UDP?</div>
        </div>
      </article>
      <article data-testid="conversation-turn-6">
        <div data-message-author-role="assistant" data-message-id="anon-a-3">
          <div class="markdown">UDP is a connectionless protocol prioritizing low latency over delivery guarantees.</div>
        </div>
      </article>
    `
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(3)
    const all = await interactionRepo.getAll()
    expect(all.map((i) => i.query.text)).toEqual([
      'Question 1: What is DNS?',
      'Question 2: What is TCP?',
      'Question 3: What is UDP?',
    ])
    for (const record of all) {
      expect(record.conversation_id).toBeNull()
      expect(record.fingerprint_strategy).toBe('level_3')
    }
  })

  it('Anonymous streaming: One anonymous response changing through multiple DOM mutations persists exactly once', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    // Turn with active generation (stop button and streaming class)
    document.body.innerHTML = `
      <header>
        <button data-testid="login-button">Log in</button>
      </header>
      <main>
        <button data-testid="stop-button" aria-label="Stop generating">Stop</button>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="stream-u-1">
            <div class="whitespace-pre-wrap">Explain event loops in Node.js</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div class="result-streaming" data-message-author-role="assistant" data-message-id="stream-a-1">
            <div class="markdown">The event loop is a single-threaded loop that</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(0) // Not persisted while streaming

    // Mutation 2: Partial streaming chunk
    const asstText = document.querySelector('.markdown')!
    asstText.textContent =
      'The event loop is a single-threaded loop that orchestrates asynchronous operations using phases like timers, poll, and check.'
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(0) // Still streaming

    // Mutation 3: Generation complete (stop button removed, streaming class removed)
    document.querySelector('button[data-testid="stop-button"]')?.remove()
    document.querySelector('.result-streaming')?.classList.remove('result-streaming')
    asstText.textContent =
      'The event loop is a single-threaded loop that orchestrates asynchronous operations using phases like timers, pending callbacks, poll, check, and close callbacks.'

    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1) // Persisted once upon completion

    // Subsequent DOM mutations do NOT create duplicate entries
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    const stored = (await interactionRepo.getAll())[0]
    expect(stored.response.text).toContain('phases like timers, pending callbacks')
  })

  it('Anonymous refresh: Refreshing the anonymous conversation does not create duplicate interactions', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <header>
        <a href="/auth/login" data-testid="login-button">Log in</a>
      </header>
      <main>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="refresh-u-1">
            <div class="whitespace-pre-wrap">What is CAP theorem?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-id="refresh-a-1">
            <div class="markdown">CAP theorem states that a distributed system can deliver at most two of Consistency, Availability, and Partition tolerance.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Simulate page refresh: stop old adapter, instantiate and start new adapter
    adapter.stop()
    const freshAdapter = new ChatGPTAdapter({ mutationDebounceMs: 10, newChatTimeoutMs: 100 })
    freshAdapter.start()
    await freshAdapter.processConversation()
    freshAdapter.stop()

    expect(await interactionRepo.count()).toBe(1) // Zero duplicates created on refresh
  })

  it('Anonymous navigation: Navigating between anonymous ChatGPT conversations collects interactions without duplicates', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <header>
        <button data-testid="login-button">Log in</button>
      </header>
      <main id="chat">
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="nav-u-1">
            <div class="whitespace-pre-wrap">First conversation topic</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-id="nav-a-1">
            <div class="markdown">Response to first topic.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()
    expect(await interactionRepo.count()).toBe(1)

    // Navigate to a new conversation (e.g. shared link or reset)
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/anon-share-conv-2'),
      writable: true,
    })
    document.title = 'Second Topic - ChatGPT'
    document.getElementById('chat')!.innerHTML = `
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="user" data-message-id="nav-u-2">
          <div class="whitespace-pre-wrap">Second conversation topic</div>
        </div>
      </article>
      <article data-testid="conversation-turn-2">
        <div data-message-author-role="assistant" data-message-id="nav-a-2">
          <div class="markdown">Response to second topic.</div>
        </div>
      </article>
    `

    adapter.handleNavigation('https://chatgpt.com/', 'https://chatgpt.com/c/anon-share-conv-2')
    await new Promise((resolve) => setTimeout(resolve, 30))
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(2)
    const records = await interactionRepo.getAll()
    expect(records.find((r) => r.query.text === 'First conversation topic')).toBeDefined()
    expect(records.find((r) => r.query.text === 'Second conversation topic')).toBeDefined()
  })

  it('Missing conversation ID: Anonymous ChatGPT DOM provides no stable conversation ID falls back to content-based fingerprinting strategy', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    document.body.innerHTML = `
      <header>
        <button data-testid="login-button">Log in</button>
      </header>
      <main>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user">
            <div class="whitespace-pre-wrap">What is Raft consensus?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant">
            <div class="markdown">Raft is an understandable consensus algorithm for replicated logs.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = (await interactionRepo.getAll())[0]
    expect(stored.conversation_id).toBeNull()
    expect(stored.fingerprint_strategy).toBe('level_3') // Level 3 content-based strategy
    expect(stored.fingerprint).toHaveLength(64) // Valid SHA-256 hex
  })

  it('Missing message ID: Anonymous ChatGPT DOM provides no stable message ID does not crash and uses fallback identity', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/'),
      writable: true,
    })

    // Turn elements without data-message-id attribute
    document.body.innerHTML = `
      <header>
        <button data-testid="login-button">Log in</button>
      </header>
      <main>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user">
            <div class="whitespace-pre-wrap">Explain Bloom filters</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant">
            <div class="markdown">A Bloom filter is a space-efficient probabilistic data structure used to test set membership.</div>
          </div>
        </article>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    expect(await interactionRepo.count()).toBe(1)
    const stored = (await interactionRepo.getAll())[0]
    expect(stored.message_id).toBeNull()
    expect(stored.user_message_id).toBeNull()
    expect(stored.query.text).toBe('Explain Bloom filters')
    expect(stored.response.text).toContain('Bloom filter is a space-efficient probabilistic')
    expect(stored.fingerprint_strategy).toBe('level_3')
  })

  describe('Modern Production Guest DOM & /uc/<uuid> Scraping', () => {
    // Test 1: Guest URL verification
    it('Guest URL: /uc/<uuid> extracts the UUID, /c/<uuid> extracts the UUID, no regression for root /', () => {
      expect(extractConversationIdFromUrl('https://chatgpt.com/uc/abc-123')).toBe('abc-123')
      expect(
        extractConversationIdFromUrl('https://chatgpt.com/uc/6ab22d47-35f8-83ea-9121-4ba79c816f57')
      ).toBe('6ab22d47-35f8-83ea-9121-4ba79c816f57')
      expect(
        extractConversationIdFromUrl('https://chatgpt.com/g/g-p-custom/uc/guest-conv-999')
      ).toBe('guest-conv-999')
      expect(extractConversationIdFromUrl('https://chatgpt.com/c/abc-123')).toBe('abc-123')
      expect(
        extractConversationIdFromUrl('https://chatgpt.com/c/68b8e05c-1234-4567-89ab-cdef01234567')
      ).toBe('68b8e05c-1234-4567-89ab-cdef01234567')
      expect(extractConversationIdFromUrl('https://chatgpt.com/')).toBeNull()
      expect(extractConversationIdFromUrl('https://chatgpt.com/auth/login')).toBeNull()
    })

    // Test R: Sibling heuristic finds only .prose assistant containers (real-browser failure mode)
    // In production OL/LI guest DOM: the sibling heuristic matches .prose inside assistant LI
    // elements only, yielding 0 user turns + N assistant turns. This MUST fall through to
    // extractGuestTurnsFromCopyAnchors rather than producing empty interactions.
    it('R. Sibling-heuristic-only-assistant bypass: when sibling heuristic produces 0 user turns + 2 assistant turns, copy-anchor extraction runs instead', () => {
      const container = document.createElement('div')
      // Simulates the production scenario: .prose class in assistant LI but NO user markers
      // that the sibling heuristic recognizes. User text is in an unlabelled BUTTON.
      container.innerHTML = `
        <main>
          <ol>
            <li>
              <h4>You said:</h4>
              <div>
                <button>What is 8?</button>
                <div><button aria-label="Copy message">Copy</button></div>
              </div>
            </li>
            <li>
              <h4>ChatGPT said:</h4>
              <div>
                <div class="prose"><p>8</p></div>
                <div>
                  <button aria-label="Copy response">Copy</button>
                </div>
              </div>
            </li>
          </ol>
        </main>
      `

      const turns = extractConversationTurns(container)
      // Must extract 2 turns (1 user + 1 assistant), not 0 pairs
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toContain('What is 8')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('8')
      // Headings must be stripped
      expect(turns[0].text).not.toContain('You said')
      expect(turns[1].text).not.toContain('ChatGPT said')
    })

    // Test A: Copy response detection
    it('A. Copy response detection: button[aria-label="Copy response"] is recognized as the assistant turn anchor', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">What is a Bloom filter?</div>
            </div>
            <div>
              <div data-assistant-markdown="">
                <p>A Bloom filter is a space-efficient probabilistic data structure.</p>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('What is a Bloom filter?')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('space-efficient probabilistic data structure')
    })

    // Test B: Copy message must NOT be mistaken for assistant anchor
    it('B. Copy message must NOT be mistaken for assistant anchor: button[aria-label="Copy message"] on user turn is not treated as assistant', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">Tell me a joke</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown="">
                <p>Why do programmers prefer dark mode? Because light attracts bugs.</p>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('Tell me a joke')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('Why do programmers prefer dark mode?')
      // Ensure button texts are stripped
      expect(turns[0].text).not.toContain('Copy')
      expect(turns[1].text).not.toContain('Copy')
    })

    // Test C: data-assistant-markdown extraction
    it('C. data-assistant-markdown extraction: extracts markdown/text cleanly from div[data-assistant-markdown]', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">Explain B-trees</div>
            </div>
            <div>
              <div data-assistant-markdown="" class="prose">
                <p>B-trees keep data sorted with logarithmic lookup.</p>
                <pre><code>SELECT * FROM index_table;</code></pre>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('B-trees keep data sorted with logarithmic lookup.')
      expect(turns[1].text).toContain('SELECT * FROM index_table;')
    })

    // Test D: Unrelated bare article must NOT block guest fallback
    it('D. Unrelated bare article must NOT block guest fallback: presence of shell <article> without message role is ignored', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <aside>
          <!-- Unrelated bare article that caused shadowing in production -->
          <article class="sidebar-promo">
            <h3>Try ChatGPT Plus</h3>
            <p>Upgrade for more features</p>
          </article>
        </aside>
        <main>
          <!-- Production guest layout with no article, no testid, no author role -->
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">How does DNS resolution work?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown="">
                <p>DNS translates domain names into IP addresses through recursive resolvers and root servers.</p>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('How does DNS resolution work?')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('DNS translates domain names into IP addresses')
    })

    // Test E: Single guest interaction
    it('E. Single guest interaction: exactly one user turn and one assistant turn extracted from production guest DOM', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <div class="flex flex-col">
            <div class="user-row">
              <div class="whitespace-pre-wrap">What is HTTP/3?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div class="assistant-row">
              <div data-assistant-markdown="">
                <p>HTTP/3 is the third major version of the Hypertext Transfer Protocol, using QUIC over UDP.</p>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('What is HTTP/3?')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('HTTP/3 is the third major version')
    })

    // Test F: Multiple guest interactions
    it('F. Multiple guest interactions: sequential user/assistant pairs extracted in DOM order with Copy response anchors', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <div class="flex flex-col">
            <!-- Turn 1 -->
            <div>
              <div class="whitespace-pre-wrap">Turn 1: What is TCP?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown=""><p>TCP is a reliable connection-oriented protocol.</p></div>
              <button aria-label="Copy response">Copy</button>
            </div>
            <!-- Turn 2 -->
            <div>
              <div class="whitespace-pre-wrap">Turn 2: What is UDP?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown=""><p>UDP is a connectionless protocol prioritizing speed.</p></div>
              <button aria-label="Copy response">Copy</button>
            </div>
            <!-- Turn 3 -->
            <div>
              <div class="whitespace-pre-wrap">Turn 3: What is QUIC?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown=""><p>QUIC is a multiplexed transport protocol over UDP.</p></div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(6)

      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('Turn 1: What is TCP?')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('TCP is a reliable connection-oriented protocol.')

      expect(turns[2].role).toBe('user')
      expect(turns[2].text).toBe('Turn 2: What is UDP?')
      expect(turns[3].role).toBe('assistant')
      expect(turns[3].text).toContain('UDP is a connectionless protocol prioritizing speed.')

      expect(turns[4].role).toBe('user')
      expect(turns[4].text).toBe('Turn 3: What is QUIC?')
      expect(turns[5].role).toBe('assistant')
      expect(turns[5].text).toContain('QUIC is a multiplexed transport protocol over UDP.')
    })

    // Test G: Streaming before Copy response exists
    it('G. Streaming: assistant response without Copy response button is not treated as completed, becomes extractable once Copy response appears', () => {
      const container = document.createElement('div')
      // While generating, stop button exists and copy response button has not mounted
      container.innerHTML = `
        <main>
          <button aria-label="Stop generating">Stop</button>
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">Generate an essay</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown="" class="result-streaming">
                <p>Generating essay intro...</p>
              </div>
            </div>
          </div>
        </main>
      `

      // With stop button present and no Copy response button on assistant,
      // the incomplete stream must not be extracted
      const streamingTurns = extractConversationTurns(container)
      expect(streamingTurns.filter((t) => t.role === 'assistant' && !t.isStreaming)).toHaveLength(0)

      // Once generation completes, stop button is removed and Copy response appears
      container.querySelector('button[aria-label="Stop generating"]')?.remove()
      const asstRow = container.querySelector('.flex-col > div:last-child')!
      asstRow.querySelector('.result-streaming')?.classList.remove('result-streaming')
      asstRow.innerHTML = `
        <div data-assistant-markdown="">
          <p>This is the fully completed essay response.</p>
        </div>
        <button aria-label="Copy response">Copy</button>
      `

      const completedTurns = extractConversationTurns(container)
      expect(completedTurns).toHaveLength(2)
      expect(completedTurns[0].role).toBe('user')
      expect(completedTurns[0].text).toBe('Generate an essay')
      expect(completedTurns[1].role).toBe('assistant')
      expect(completedTurns[1].text).toContain('fully completed essay response')
    })

    // Test H: Authenticated regression
    it('H. Authenticated regression: existing /c/<uuid> extraction with article and role attributes continues to work unchanged', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <article data-testid="conversation-turn-1">
            <div data-message-author-role="user" data-message-id="auth-u-1">
              <div class="whitespace-pre-wrap">Authenticated user query</div>
            </div>
          </article>
          <article data-testid="conversation-turn-2">
            <div data-message-author-role="assistant" data-message-id="auth-a-1">
              <div class="markdown prose">Authenticated assistant response</div>
            </div>
          </article>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].messageId).toBe('auth-u-1')
      expect(turns[0].text).toBe('Authenticated user query')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].messageId).toBe('auth-a-1')
      expect(turns[1].text).toContain('Authenticated assistant response')
    })

    // Test I: Repeated scans / deduplication
    it('I. Repeated scans: running extraction repeatedly over the same guest DOM produces stable results with zero duplicate database records', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/uc/repeat-scan-uuid-123'),
        writable: true,
      })

      document.body.innerHTML = `
        <header><button>Log in</button></header>
        <aside>
          <!-- Unrelated article in sidebar -->
          <article><div>Unrelated promo</div></article>
        </aside>
        <main>
          <div class="flex flex-col">
            <div>
              <div class="whitespace-pre-wrap">Idempotency query test</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown=""><p>Idempotency response test</p></div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      adapter.start()
      await adapter.processConversation()
      expect(await interactionRepo.count()).toBe(1)

      // Run second and third processing passes on exact same DOM
      await adapter.processConversation()
      await adapter.processConversation()
      expect(await interactionRepo.count()).toBe(1) // Zero duplicate records created
    })

    // Test: Adapter integration with /uc/<uuid>
    it('Adapter integration: guest /uc/<uuid> DOM with Copy response and data-assistant-markdown persists with chatgpt:<uuid>', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/uc/6ab2500b-afd8-83ea-8c2d-0123f1a99be7'),
        writable: true,
      })
      document.title = 'Bloom Filters Explained - ChatGPT'

      document.body.innerHTML = `
        <header>
          <button>Log in</button>
          <button>Sign up</button>
        </header>
        <aside>
          <article><div>Unrelated shell article</div></article>
        </aside>
        <main class="relative h-full w-full flex-1 overflow-auto">
          <div class="flex flex-col text-sm md:pb-9">
            <div>
              <div class="whitespace-pre-wrap">What is an LSM-tree?</div>
              <button aria-label="Copy message">Copy</button>
            </div>
            <div>
              <div data-assistant-markdown="">
                <p>A Log-Structured Merge-tree is an append-only storage structure optimized for write-heavy workloads.</p>
              </div>
              <button aria-label="Copy response">Copy</button>
            </div>
          </div>
        </main>
      `

      adapter.start()
      await adapter.processConversation()

      // Confirm interaction was persisted via chrome.runtime.sendMessage -> DB_SAVE_INTERACTION
      expect(await interactionRepo.count()).toBe(1)
      const stored = (await interactionRepo.getAll())[0]
      expect(stored.platform).toBe('chatgpt')
      expect(stored.conversation_id).toBe('chatgpt:6ab2500b-afd8-83ea-8c2d-0123f1a99be7')
      expect(stored.query.text).toBe('What is an LSM-tree?')
      expect(stored.response.text).toContain('Log-Structured Merge-tree is an append-only storage')

      // Confirm conversation entity was also created with the UUID
      const conv = await conversationRepo.getById('6ab2500b-afd8-83ea-8c2d-0123f1a99be7', 'chatgpt')
      expect(conv).not.toBeNull()
      expect(conv?.id).toBe('chatgpt:6ab2500b-afd8-83ea-8c2d-0123f1a99be7')
    })
    // Test P: Production OL/LI DOM (real-browser confirmed structure)
    it('P. Production OL/LI DOM: guest conversations using OL/LI turn structure with H4 said-headings and atomic CSS extract correctly', () => {
      // This mirrors the exact DOM structure revealed by dump_chains.mjs on the real production
      // ChatGPT guest page (Chromium 144, 2026-09-22).
      // Each turn is an <LI> in an <OL>. User text is in a <BUTTON> (no aria-label).
      // H4 "You said:" / "ChatGPT said:" headings separate the turn roles.
      const container = document.createElement('div')
      container.innerHTML = `
        <main class="xg3ptho xnpn0wm">
          <div class="xjp7ctv">
            <div class="xdmi676 x78zum5">
              <ol class="xrvj5dj xxhr3t xh8yej3">
                <li class="x10ukxgv xrvj5dj">
                  <div class="xuk3077 xhez8tq">
                    <h4>You said:</h4>
                    <div class="xjbqb8w x1yt6v20">
                      <button>What is a content-addressable store?</button>
                      <div class="xjbqb8w">
                        <button aria-label="Copy message">Copy</button>
                      </div>
                    </div>
                  </div>
                </li>
                <li class="x10ukxgv xrvj5dj">
                  <div class="xjp7ctv">
                    <h4>ChatGPT said:</h4>
                    <div class="xjp7ctv">
                      <div>A content-addressable store retrieves data by its cryptographic hash rather than by location.</div>
                      <div class="xzq6wi8 x6s0dn4">
                        <div>A content-addressable store retrieves data by its cryptographic hash rather than by location.</div>
                        <div class="xzq6wi8">
                          <button aria-label="Copy response">Copy</button>
                        </div>
                      </div>
                    </div>
                  </div>
                </li>
              </ol>
            </div>
          </div>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toContain('content-addressable store')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('content-addressable store retrieves data')
      // Headings must be stripped
      expect(turns[0].text).not.toContain('You said')
      expect(turns[1].text).not.toContain('ChatGPT said')
    })

    // Test Q: Production OL/LI with multiple turns
    it('Q. Production OL/LI multi-turn: two user/assistant LI pairs in a single OL both extracted in order', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <main>
          <ol>
            <li>
              <h4>You said:</h4>
              <div>
                <button>Turn 1 question about TCP</button>
                <div><button aria-label="Copy message">Copy</button></div>
              </div>
            </li>
            <li>
              <h4>ChatGPT said:</h4>
              <div>
                <div>TCP is a reliable connection-oriented protocol.</div>
                <div>
                  <button aria-label="Copy response">Copy</button>
                </div>
              </div>
            </li>
            <li>
              <h4>You said:</h4>
              <div>
                <button>Turn 2 question about UDP</button>
                <div><button aria-label="Copy message">Copy</button></div>
              </div>
            </li>
            <li>
              <h4>ChatGPT said:</h4>
              <div>
                <div>UDP is a connectionless low-latency protocol.</div>
                <div>
                  <button aria-label="Copy response">Copy</button>
                </div>
              </div>
            </li>
          </ol>
        </main>
      `

      const turns = extractConversationTurns(container)
      expect(turns).toHaveLength(4)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toContain('TCP')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('TCP is a reliable connection-oriented protocol')
      expect(turns[2].role).toBe('user')
      expect(turns[2].text).toContain('UDP')
      expect(turns[3].role).toBe('assistant')
      expect(turns[3].text).toContain('UDP is a connectionless low-latency protocol')
    })
  })
})
