// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import {
  extractConversationTurns as extractChatGPTTurns,
  isChatGPTGuestSession,
} from '../src/platforms/chatgpt/parser'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import {
  extractConversationTurns as extractGeminiTurns,
  isGeminiGuestSession,
} from '../src/platforms/gemini/parser'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('Guest / Logged-out Chat Scraping for ChatGPT & Gemini', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-guest-scraping-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

    // Mock chrome runtime routing to local database
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
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
    document.body.innerHTML = ''
    document.title = ''
  })

  describe('ChatGPT Guest Scraping', () => {
    let chatgptAdapter: ChatGPTAdapter

    beforeEach(() => {
      chatgptAdapter = new ChatGPTAdapter({ newChatTimeoutMs: 5000 })
    })

    afterEach(() => {
      chatgptAdapter.stop()
    })

    it('identifies unauthenticated guest session using login buttons and absence of user profile', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <nav>
          <a href="/login" data-testid="login-button">Log in</a>
          <button data-testid="signup-button">Sign up</button>
        </nav>
      `
      expect(isChatGPTGuestSession(container)).toBe(true)

      // When user profile is present, it is not guest
      const loggedInContainer = document.createElement('div')
      loggedInContainer.innerHTML = `
        <nav>
          <button data-testid="profile-button" aria-label="Open user menu">User</button>
        </nav>
      `
      expect(isChatGPTGuestSession(loggedInContainer)).toBe(false)
    })

    it('Guest account menu classification: Guest DOM containing button[aria-label*="Open account menu"] is not classified as authenticated', () => {
      const guestContainer = document.createElement('div')
      guestContainer.innerHTML = `
        <header>
          <button>Log in</button>
          <button>Sign up</button>
        </header>
        <div class="sidebar-footer">
          <button aria-label="Open account menu">
            <span>Log in or sign up</span>
          </button>
        </div>
      `
      expect(isChatGPTGuestSession(guestContainer)).toBe(true)
    })

    it('Authenticated profile classification: Authenticated DOM containing real profile indicator is classified as authenticated', () => {
      const authenticatedContainer = document.createElement('div')
      authenticatedContainer.innerHTML = `
        <div class="sidebar-footer">
          <button aria-label="Open account menu">
            <div class="avatar-user" data-testid="user-avatar">User Name</div>
          </button>
        </div>
      `
      expect(isChatGPTGuestSession(authenticatedContainer)).toBe(false)

      const profileBtnContainer = document.createElement('div')
      profileBtnContainer.innerHTML = `
        <button data-testid="profile-button" aria-label="User profile">John Doe</button>
      `
      expect(isChatGPTGuestSession(profileBtnContainer)).toBe(false)
    })

    it('extracts turns in guest mode even when data-testid attributes differ and speaker headings are used', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <article>
          <h6 class="sr-only">You said:</h6>
          <div class="whitespace-pre-wrap">Explain merge sort in Python</div>
        </article>
        <article>
          <h6 class="sr-only">ChatGPT said:</h6>
          <div class="markdown">Merge sort is a divide-and-conquer algorithm with O(n log n) complexity.</div>
        </article>
      `

      const turns = extractChatGPTTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toContain('Explain merge sort in Python')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('Merge sort is a divide-and-conquer algorithm')
    })

    it('Guest production DOM: extracts turns and scrapes guest interaction when article[data-testid] and role attributes are completely absent', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/'),
        writable: true,
      })
      document.title = 'ChatGPT'

      document.body.innerHTML = `
        <header>
          <button>Log in</button>
          <button>Sign up</button>
        </header>
        <div class="sidebar">
          <button aria-label="Open account menu">Log in or sign up</button>
        </div>
        <main>
          <div class="group/conversation-turn w-full">
            <h6 class="sr-only">You said:</h6>
            <div class="whitespace-pre-wrap">What is a Bloom filter?</div>
          </div>
          <div class="group/conversation-turn w-full">
            <h6 class="sr-only">ChatGPT said:</h6>
            <div class="markdown prose">A Bloom filter is a space-efficient probabilistic data structure.</div>
          </div>
        </main>
      `

      const turns = extractChatGPTTurns(document.body)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('What is a Bloom filter?')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('probabilistic data structure')

      chatgptAdapter.start()
      await chatgptAdapter.processConversation()

      // Immediately persisted without waiting for /c/<uuid>
      expect(await interactionRepo.count()).toBe(1)
      const stored = (await interactionRepo.getAll())[0]
      expect(stored.platform).toBe('chatgpt')
      expect(stored.conversation_id).toBeNull()
      expect(stored.fingerprint_strategy).toBe('level_3')
      expect(stored.query.text).toBe('What is a Bloom filter?')
      expect(stored.response.text).toContain('space-efficient probabilistic data structure')
    })

    it('Guest streaming: One guest response undergoing 20+ mutations results in exactly one interaction', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/'),
        writable: true,
      })

      document.body.innerHTML = `
        <header><button>Log in</button></header>
        <button aria-label="Stop generating">Stop</button>
        <main>
          <div class="group/conversation-turn">
            <div class="whitespace-pre-wrap">Explain raft consensus</div>
          </div>
          <div class="group/conversation-turn">
            <div class="result-streaming markdown">Raft is a</div>
          </div>
        </main>
      `

      chatgptAdapter.start()

      // Simulate 20 streaming mutation ticks
      for (let i = 0; i < 20; i++) {
        const md = document.querySelector('.result-streaming.markdown')
        if (md)
          md.textContent = `Raft is a consensus algorithm that manages replicated logs (tick ${i}).`
        await chatgptAdapter.processConversation()
      }

      // No interactions should be persisted while actively generating
      expect(await interactionRepo.count()).toBe(0)

      // Streaming completes: stop button removed and result-streaming class removed
      const stopBtn = document.querySelector('button[aria-label="Stop generating"]')
      stopBtn?.remove()
      const asstTurn = document.querySelector('.result-streaming')
      asstTurn?.classList.remove('result-streaming')
      if (asstTurn)
        asstTurn.textContent = 'Raft is a consensus algorithm that manages replicated logs.'

      await chatgptAdapter.processConversation()

      expect(await interactionRepo.count()).toBe(1)
      const stored = (await interactionRepo.getAll())[0]
      expect(stored.query.text).toBe('Explain raft consensus')
      expect(stored.response.text).toBe(
        'Raft is a consensus algorithm that manages replicated logs.'
      )
    })

    it('Guest multiple interactions: Three consecutive guest queries in the same session persist exactly three interactions', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/'),
        writable: true,
      })

      document.body.innerHTML = `
        <header><button>Log in</button><button>Sign up</button></header>
        <main id="chat-stream">
          <div class="group/conversation-turn">
            <div class="whitespace-pre-wrap">Turn 1: What is ACID?</div>
          </div>
          <div class="group/conversation-turn">
            <div class="markdown">ACID stands for Atomicity, Consistency, Isolation, and Durability.</div>
          </div>
        </main>
      `

      chatgptAdapter.start()
      await chatgptAdapter.processConversation()
      expect(await interactionRepo.count()).toBe(1)

      const stream = document.getElementById('chat-stream')!
      stream.innerHTML += `
        <div class="group/conversation-turn">
          <div class="whitespace-pre-wrap">Turn 2: What is BASE?</div>
        </div>
        <div class="group/conversation-turn">
          <div class="markdown">BASE stands for Basically Available, Soft state, and Eventual consistency.</div>
        </div>
      `
      await chatgptAdapter.processConversation()
      expect(await interactionRepo.count()).toBe(2)

      stream.innerHTML += `
        <div class="group/conversation-turn">
          <div class="whitespace-pre-wrap">Turn 3: What is CAP theorem?</div>
        </div>
        <div class="group/conversation-turn">
          <div class="markdown">CAP theorem states that a distributed system can deliver at most two of Consistency, Availability, Partition tolerance.</div>
        </div>
      `
      await chatgptAdapter.processConversation()
      expect(await interactionRepo.count()).toBe(3)

      const all = await interactionRepo.getAll()
      expect(all.map((i) => i.query.text)).toEqual([
        'Turn 1: What is ACID?',
        'Turn 2: What is BASE?',
        'Turn 3: What is CAP theorem?',
      ])
      for (const item of all) {
        expect(item.conversation_id).toBeNull()
        expect(item.fingerprint_strategy).toBe('level_3')
      }

      // Guest refresh: refreshing page creates 0 duplicates
      const refreshedAdapter = new ChatGPTAdapter({ newChatTimeoutMs: 5000 })
      refreshedAdapter.start()
      await refreshedAdapter.processConversation()
      refreshedAdapter.stop()

      expect(await interactionRepo.count()).toBe(3)
    })

    it('Authenticated new chat: query starting at / with profile button buffers and binds to real /c/<uuid> on navigation', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/'),
        writable: true,
      })
      document.title = 'ChatGPT'

      document.body.innerHTML = `
        <nav>
          <button data-testid="profile-button" aria-label="User profile">Logged In User</button>
        </nav>
        <main>
          <div class="group/conversation-turn">
            <div class="whitespace-pre-wrap">Explain zero-copy I/O</div>
          </div>
          <div class="group/conversation-turn">
            <div class="markdown">Zero-copy I/O avoids CPU copies between kernel and user space.</div>
          </div>
        </main>
      `

      chatgptAdapter.start()
      await chatgptAdapter.processConversation()

      // Authenticated session is buffered in pendingUnboundInteractions waiting for /c/<uuid>
      expect(await interactionRepo.count()).toBe(0)

      // SPA navigation to assigned conversation URL occurs
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/c/zerocopy-assigned-uuid'),
        writable: true,
      })
      document.title = 'Zero-Copy I/O - ChatGPT'

      chatgptAdapter.handleNavigation(
        'https://chatgpt.com/',
        'https://chatgpt.com/c/zerocopy-assigned-uuid'
      )
      await new Promise((resolve) => setTimeout(resolve, 50))

      // Exactly 1 interaction created, bound to real conversation ID
      expect(await interactionRepo.count()).toBe(1)
      const stored = (await interactionRepo.getAll())[0]
      expect(stored.conversation_id).toBe('chatgpt:zerocopy-assigned-uuid')
      expect(stored.conversation_title).toBe('Zero-Copy I/O')
      expect(stored.query.text).toBe('Explain zero-copy I/O')
    })

    it('scrapes ChatGPT guest interaction without URL conversation ID immediately into IndexedDB using content-based fallback identity', async () => {
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
            <div data-message-author-role="user" data-message-id="guest-u-1">
              <div>How does Redis handle pub/sub?</div>
            </div>
          </article>
          <article data-testid="conversation-turn-2">
            <div data-message-author-role="assistant" data-message-id="guest-a-1">
              <div class="markdown">Redis pub/sub delivers messages to channels in real time without persistence.</div>
            </div>
          </article>
        </main>
      `

      chatgptAdapter.start()
      await chatgptAdapter.processConversation()

      // Should immediately persist without waiting for unbound delay
      expect(await interactionRepo.count()).toBe(1)
      const allInteractions = await interactionRepo.getAll()
      const firstInteraction = allInteractions[0]

      expect(firstInteraction.platform).toBe('chatgpt')
      expect(firstInteraction.conversation_id).toBeNull()
      expect(firstInteraction.fingerprint_strategy).toBe('level_3')
      expect(firstInteraction.message_id).toBe('guest-a-1')
      expect(firstInteraction.query.text).toBe('How does Redis handle pub/sub?')
      expect(firstInteraction.response.text).toContain('Redis pub/sub delivers messages')

      // Multi-turn in the same guest session: append turn 2
      const main = document.querySelector('main')!
      main.innerHTML += `
        <article data-testid="conversation-turn-3">
          <div data-message-author-role="user" data-message-id="guest-u-2">
            <div>Does Redis Streams provide persistence?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-4">
          <div data-message-author-role="assistant" data-message-id="guest-a-2">
            <div class="markdown">Yes, Redis Streams persists entries like an append-only log.</div>
          </div>
        </article>
      `

      await chatgptAdapter.processConversation()

      expect(await interactionRepo.count()).toBe(2)
      const updatedInteractions = await interactionRepo.getAll()
      const secondInteraction = updatedInteractions.find((i) => i.message_id === 'guest-a-2')!

      // Turns in the anonymous session fall back to content-based identity without fabricating session IDs
      expect(secondInteraction.conversation_id).toBeNull()
      expect(secondInteraction.fingerprint_strategy).toBe('level_3')

      // Refreshing the anonymous conversation does NOT create duplicate records
      const refreshedAdapter = new ChatGPTAdapter({ newChatTimeoutMs: 5000 })
      refreshedAdapter.start()
      await refreshedAdapter.processConversation()
      refreshedAdapter.stop()

      expect(await interactionRepo.count()).toBe(2)
    })
  })

  describe('Gemini Guest Scraping', () => {
    let geminiAdapter: GeminiAdapter

    beforeEach(() => {
      geminiAdapter = new GeminiAdapter({ newChatTimeoutMs: 5000 })
    })

    afterEach(() => {
      geminiAdapter.stop()
    })

    it('identifies unauthenticated Gemini session using Google sign-in indicators and absence of profile', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <header>
          <a href="https://accounts.google.com/ServiceLogin?service=wise" aria-label="Sign in">Sign in</a>
        </header>
      `
      expect(isGeminiGuestSession(container)).toBe(true)

      const loggedInContainer = document.createElement('div')
      loggedInContainer.innerHTML = `
        <header>
          <a href="https://myaccount.google.com" aria-label="Google Account: User">Account</a>
        </header>
      `
      expect(isGeminiGuestSession(loggedInContainer)).toBe(false)
    })

    it('extracts turns in guest mode using query and response container selectors', () => {
      const container = document.createElement('div')
      container.innerHTML = `
        <div class="user-query-container" data-query-id="g-q-1">
          <div class="query-content">Explain quicksort partitioning</div>
        </div>
        <div class="response-container" data-response-id="g-r-1">
          <div class="markdown">Lomuto and Hoare are two common partitioning schemes for quicksort.</div>
        </div>
      `

      const turns = extractGeminiTurns(container)
      expect(turns).toHaveLength(2)
      expect(turns[0].role).toBe('user')
      expect(turns[0].text).toBe('Explain quicksort partitioning')
      expect(turns[1].role).toBe('assistant')
      expect(turns[1].text).toContain('Lomuto and Hoare are two common partitioning schemes')
    })

    it('scrapes Gemini guest interaction without URL conversation ID immediately into IndexedDB and groups turns under guest ID', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://gemini.google.com/app'),
        writable: true,
      })
      document.title = 'Google Gemini'

      document.body.innerHTML = `
        <header>
          <a href="https://accounts.google.com/ServiceLogin" aria-label="Sign in">Sign in</a>
        </header>
        <main>
          <user-query data-message-id="gemini-guest-u-1">
            <div class="query-text">What is Raft consensus?</div>
          </user-query>
          <model-response data-message-id="gemini-guest-a-1">
            <div class="response-container">
              <div class="markdown">Raft is a consensus algorithm designed for understandability, using leader election and log replication.</div>
            </div>
          </model-response>
        </main>
      `

      geminiAdapter.start()
      await geminiAdapter.processConversation()

      // Immediately persisted without 5s delay
      expect(await interactionRepo.count()).toBe(1)
      const allInteractions = await interactionRepo.getAll()
      const firstInteraction = allInteractions[0]

      expect(firstInteraction.platform).toBe('gemini')
      expect(firstInteraction.conversation_id).toMatch(/^gemini:guest_gemini_\d+_[a-z0-9]+$/)
      expect(firstInteraction.query.text).toBe('What is Raft consensus?')
      expect(firstInteraction.response.text).toContain(
        'consensus algorithm designed for understandability'
      )
      expect(firstInteraction.conversation_title).toBe('What is Raft consensus?')

      // Verify conversation record
      const convIdRaw = firstInteraction.conversation_id!.replace(/^gemini:/, '')
      const conv = await conversationRepo.getById(convIdRaw, 'gemini')
      expect(conv).not.toBeNull()
      expect(conv?.title).toBe('What is Raft consensus?')

      // Multi-turn in the same guest session: append second turn
      const main = document.querySelector('main')!
      main.innerHTML += `
        <user-query data-message-id="gemini-guest-u-2">
          <div class="query-text">How does leader election work in Raft?</div>
        </user-query>
        <model-response data-message-id="gemini-guest-a-2">
          <div class="response-container">
            <div class="markdown">Nodes use randomized election timeouts to transition from follower to candidate.</div>
          </div>
        </model-response>
      `

      await geminiAdapter.processConversation()

      expect(await interactionRepo.count()).toBe(2)
      const updatedInteractions = await interactionRepo.getAll()
      const secondInteraction = updatedInteractions.find(
        (i) => i.message_id === 'gemini-guest-a-2'
      )!

      // Must share the same guest conversation ID
      expect(secondInteraction.conversation_id).toBe(firstInteraction.conversation_id)
    })
  })
})
