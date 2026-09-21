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

    it('scrapes ChatGPT guest interaction without URL conversation ID immediately into IndexedDB and groups turns under guest ID', async () => {
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
      expect(firstInteraction.conversation_id).toMatch(/^chatgpt:guest_chatgpt_\d+_[a-z0-9]+$/)
      expect(firstInteraction.query.text).toBe('How does Redis handle pub/sub?')
      expect(firstInteraction.response.text).toContain('Redis pub/sub delivers messages')
      expect(firstInteraction.conversation_title).toBe('How does Redis handle pub/sub?')

      // Conversation entity must be recorded with title fallback
      const convIdRaw = firstInteraction.conversation_id!.replace(/^chatgpt:/, '')
      const conv = await conversationRepo.getById(convIdRaw, 'chatgpt')
      expect(conv).not.toBeNull()
      expect(conv?.title).toBe('How does Redis handle pub/sub?')

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

      // Turns in the same guest session must share the same guest conversation ID
      expect(secondInteraction.conversation_id).toBe(firstInteraction.conversation_id)
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
      expect(firstInteraction.response.text).toContain('consensus algorithm designed for understandability')
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
      const secondInteraction = updatedInteractions.find((i) => i.message_id === 'gemini-guest-a-2')!

      // Must share the same guest conversation ID
      expect(secondInteraction.conversation_id).toBe(firstInteraction.conversation_id)
    })
  })
})
