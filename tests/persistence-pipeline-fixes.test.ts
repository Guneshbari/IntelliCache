// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import {
  DatabaseOperationError,
  DuplicateInteractionError,
  namespaceConversationId,
} from '../src/database/types'
import { generateInteractionFingerprint } from '../src/fingerprint/fingerprint'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import { extractConversationTitle } from '../src/platforms/chatgpt/parser'
import { pairTurnsIntoInteractions } from '../src/platforms/shared/parser-utils'
import type { RawMessageTurn } from '../src/platforms/types'
import {
  createErrorResponse,
  createSuccessResponse,
  isSenderAllowedForWrite,
  type WriteSender,
} from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'
import type { ExtractedInteraction } from '../src/platforms/types'

describe('Persistence Pipeline, Identity, Deduplication & SPA Race Fixes (FIX-001 - FIX-008)', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-pipeline-fixes-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

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
                  if (err instanceof DuplicateInteractionError) {
                    callback(createErrorResponse(err.message, 'DUPLICATE_INTERACTION'))
                  } else {
                    callback(
                      createErrorResponse(
                        err instanceof Error ? err.message : 'Failed to save interaction'
                      )
                    )
                  }
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
    await db.delete()
    await closeDatabase()
    document.body.innerHTML = ''
    document.title = ''
    globalThis.chrome = originalChrome
  })

  // ─── FIX-001: ELIMINATE UNSAFE QUERY-ONLY REBINDING ────────────────────────

  describe('FIX-001: Safe Unbound Interaction Rebinding', () => {
    it('never rebinds an unbound interaction using query.text alone when prompt is identical', async () => {
      // Step 1: Create an unbound interaction with a common prompt "Hello"
      const unbound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: null,
        query: { text: 'Hello' },
        response: { text: 'Hello! How can I help you today?' },
        observed_at: '2026-09-01T10:00:00.000Z',
      })
      expect(unbound.conversation_id).toBeNull()

      // Step 2: In a distinct conversation A, user sends the same prompt "Hello" but gets a different response
      const convAInteraction = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-a-123',
        query: { text: 'Hello' },
        response: { text: 'Greetings from Conversation A.' },
        observed_at: '2026-09-01T10:05:00.000Z',
      })

      // Must be separate records: unbound was NOT hijacked or mutated into Conversation A
      expect(convAInteraction.id).not.toBe(unbound.id)
      expect(convAInteraction.conversation_id).toBe('chatgpt:conv-a-123')

      const unboundCheck = await interactionRepo.getById(unbound.id)
      expect(unboundCheck?.conversation_id).toBeNull()
      expect(unboundCheck?.response.text).toBe('Hello! How can I help you today?')
      expect(await interactionRepo.count()).toBe(2)
    })

    it('rebinds an unbound interaction when deterministic evidence (message_id) matches', async () => {
      // Step 1: Create unbound with deterministic message_id
      const unbound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: null,
        message_id: 'msg-unique-123',
        query: { text: 'What is Raft?' },
        response: { text: 'Raft is a consensus algorithm.' },
        observed_at: '2026-09-01T10:00:00.000Z',
      })
      expect(unbound.conversation_id).toBeNull()

      // Step 2: Later, conversation ID arrives with the exact same message_id
      const bound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'chat-raft-uuid',
        message_id: 'msg-unique-123',
        query: { text: 'What is Raft?' },
        response: { text: 'Raft is a consensus algorithm.' },
        observed_at: '2026-09-01T10:00:00.000Z',
      })

      // Must update in-place
      expect(bound.id).toBe(unbound.id)
      expect(bound.conversation_id).toBe('chatgpt:chat-raft-uuid')
      expect(await interactionRepo.count()).toBe(1)
    })

    it('rebinds an unbound interaction when deterministic evidence (user_message_id) matches', async () => {
      const unbound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: null,
        user_message_id: 'user-turn-xyz',
        query: { text: 'What is Paxos?' },
        response: { text: 'Paxos is a consensus protocol.' },
        observed_at: '2026-09-01T10:00:00.000Z',
      })

      const bound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'chat-paxos-uuid',
        user_message_id: 'user-turn-xyz',
        query: { text: 'What is Paxos?' },
        response: { text: 'Paxos is a consensus protocol.' },
        observed_at: '2026-09-01T10:00:00.000Z',
      })

      expect(bound.id).toBe(unbound.id)
      expect(bound.conversation_id).toBe('chatgpt:chat-paxos-uuid')
      expect(await interactionRepo.count()).toBe(1)
    })
  })

  // ─── FIX-002: RECOMPUTE FINGERPRINT ON PERSISTED IDENTITY CHANGE ───────────

  describe('FIX-002: Fingerprint Recomputation on Rebinding', () => {
    it('recomputes fingerprint from level_3 to level_2 when conversation_id is bound', async () => {
      const unbound = await interactionRepo.create({
        platform: 'claude',
        conversation_id: null,
        query: { text: 'Explain Bloom filters' },
        response: { text: 'Bloom filters are probabilistic data structures.' },
        observed_at: '2026-09-01T12:00:00.000Z',
      })
      expect(unbound.fingerprint_strategy).toBe('level_3')
      const originalFp = unbound.fingerprint

      // Bind to conversation
      const bound = await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'conv-bloom-999',
        query: { text: 'Explain Bloom filters' },
        response: { text: 'Bloom filters are probabilistic data structures.' },
        observed_at: '2026-09-01T12:00:00.000Z',
      })

      expect(bound.id).toBe(unbound.id)
      expect(bound.conversation_id).toBe('claude:conv-bloom-999')
      // Old level_3 fingerprint must NOT be retained
      expect(bound.fingerprint).not.toBe(originalFp)
      expect(bound.fingerprint_strategy).toBe('level_2')

      const expectedFp = await generateInteractionFingerprint({
        platform: 'claude',
        conversation_id: 'claude:conv-bloom-999',
        query_text: 'Explain Bloom filters',
        response_text: 'Bloom filters are probabilistic data structures.',
        observed_at: '2026-09-01T12:00:00.000Z',
      })
      expect(bound.fingerprint).toBe(expectedFp.fingerprint)

      // Verified in IndexedDB
      const stored = await interactionRepo.getById(unbound.id)
      expect(stored?.fingerprint).toBe(expectedFp.fingerprint)
      expect(stored?.fingerprint_strategy).toBe('level_2')
    })

    it('rejects rebinding if target fingerprint collision already exists in database', async () => {
      // Existing bound interaction
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-target',
        message_id: 'msg-target-1',
        query: { text: 'Collision test' },
        response: { text: 'Target response' },
      })

      // Unbound interaction with same message_id
      const unbound = await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: null,
        message_id: 'msg-target-1',
        query: { text: 'Collision test' },
        response: { text: 'Target response' },
      })

      // Rebinding would produce fingerprint of existing bound record
      await expect(
        interactionRepo.create({
          platform: 'chatgpt',
          conversation_id: 'conv-target',
          message_id: 'msg-target-1',
          query: { text: 'Collision test' },
          response: { text: 'Target response' },
        })
      ).rejects.toThrow(DuplicateInteractionError)

      expect(unbound.id).toBeDefined()
    })
  })

  // ─── FIX-003: SPA NAVIGATION RACE CONDITION ────────────────────────────────

  describe('FIX-003: SPA Navigation Race Prevention', () => {
    it('defers extraction when DOM still represents Conversation A after URL changes to Conversation B', async () => {
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/c/conversation-a'),
        writable: true,
      })
      document.title = 'Conversation A - ChatGPT'

      // DOM contains Conversation A turns
      document.body.innerHTML = `
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user">
            <div>What is MapReduce?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-mr-1">
            <div class="markdown">MapReduce processes large datasets in parallel.</div>
          </div>
        </article>
      `

      const adapter = new ChatGPTAdapter()
      adapter.start()

      // Initial scan processes Conversation A
      await adapter.processConversation()

      // Simulate SPA navigation: URL updates to Conversation B, but DOM has NOT updated yet
      Object.defineProperty(window, 'location', {
        value: new URL('https://chatgpt.com/c/conversation-b'),
        writable: true,
      })
      document.title = 'Conversation B - ChatGPT'

      adapter.handleNavigation(
        'https://chatgpt.com/c/conversation-a',
        'https://chatgpt.com/c/conversation-b'
      )

      // An immediate processConversation pass fires while DOM STILL has Conversation A's messages
      await adapter.processConversation()

      // In-flight or deferred checks: verify that Conversation A messages were NEVER saved under Conversation B
      // (Any calls to persistInteraction with conv B would show up here)
      const bInteractions = await interactionRepo.getByConversationId(
        'conversation-b',
        'chatgpt'
      )
      expect(bInteractions).toHaveLength(0)

      // Now SPA finishes rendering Conversation B DOM
      document.body.innerHTML = `
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user">
            <div>What is Bigtable?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-bt-1">
            <div class="markdown">Bigtable is a distributed storage system for structured data.</div>
          </div>
        </article>
      `

      await adapter.processConversation()

      const bUpdated = await interactionRepo.getByConversationId(
        'conversation-b',
        'chatgpt'
      )
      expect(bUpdated).toHaveLength(1)
      expect(bUpdated[0].query.text).toBe('What is Bigtable?')
      expect(bUpdated[0].response.text).toContain('Bigtable is a distributed storage system')

      adapter.stop()
    })
  })

  // ─── FIX-004: PREVENT UNBOUND TIMEOUT LOCKOUT ──────────────────────────────

  describe('FIX-004: In-Memory Unbound Key Lockout Prevention', () => {
    it('does not permanently lock out an interaction when it was initially persisted as unbound', async () => {
      const adapter = new ChatGPTAdapter({ newChatTimeoutMs: 100 })
      adapter.start()

      const unboundInteraction: ExtractedInteraction = {
        platform: 'chatgpt',
        conversationId: null,
        messageId: 'asst-timeout-msg-1',
        userMessageId: 'user-timeout-msg-1',
        queryText: 'Explain virtual DOM',
        responseText: 'Virtual DOM is an in-memory representation of real DOM.',
        conversationTitle: null,
        model: { provider: 'openai', name: 'gpt-4o' },
        captureContext: 'on_generate',
        observedAt: new Date().toISOString(),
        sourceTimestamp: null,
        turnIndex: 0,
      }

      // 1. Persisted as unbound (e.g. after timeout)
      const key = (adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string })
        .generateInteractionKey(unboundInteraction)

      const res1 = await (adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }).persistInteraction(unboundInteraction, key)
      expect(res1).toBe('saved')
      expect(await interactionRepo.count()).toBe(1)

      const initialStored = (await interactionRepo.getAll())[0]
      expect(initialStored.conversation_id).toBeNull()

      // 2. Real conversation ID becomes available later (same turn arrives with conversationId)
      const boundInteraction: ExtractedInteraction = {
        ...unboundInteraction,
        conversationId: 'assigned-chat-uuid',
        conversationTitle: 'Virtual DOM',
      }

      // BaseAdapter processInteractions should NOT skip it as duplicate because conversationId !== null
      const processResult = await (adapter as unknown as {
        processInteractions: (items: ExtractedInteraction[]) => Promise<{ savedCount: number; duplicateCount: number }>
      }).processInteractions([boundInteraction])

      expect(processResult.savedCount).toBe(1)
      expect(processResult.duplicateCount).toBe(0)

      // Total count remains 1 (rebound in-place)
      expect(await interactionRepo.count()).toBe(1)
      const finalStored = (await interactionRepo.getAll())[0]
      expect(finalStored.conversation_id).toBe('chatgpt:assigned-chat-uuid')
      expect(finalStored.conversation_title).toBe('Virtual DOM')

      adapter.stop()
    })
  })

  // ─── FIX-005: REGENERATION DEDUPLICATION ───────────────────────────────────

  describe('FIX-005: Regeneration Deduplication', () => {
    it('includes response hash in turn key so regenerated responses are not silently discarded', () => {
      const adapter = new ChatGPTAdapter()

      const turn1Response1: ExtractedInteraction = {
        platform: 'chatgpt',
        conversationId: 'conv-regen-1',
        messageId: null, // turn index fallback
        userMessageId: null,
        queryText: 'Write a haiku about computers',
        responseText: 'Metal and silicon\nThinking in ones and zeros\nQuiet machine dreams',
        conversationTitle: 'Haiku',
        model: { provider: 'openai', name: 'gpt-4o' },
        captureContext: 'on_generate',
        observedAt: new Date().toISOString(),
        sourceTimestamp: null,
        turnIndex: 0,
      }

      const turn1Response2: ExtractedInteraction = {
        ...turn1Response1,
        responseText: 'Circuits pulse with light\nData flows like silver streams\nFuture comes alive',
      }

      const keyGen = (adapter as unknown as {
        generateInteractionKey: (i: ExtractedInteraction) => string
      })

      const key1 = keyGen.generateInteractionKey(turn1Response1)
      const key2 = keyGen.generateInteractionKey(turn1Response2)

      // Keys must differ because response text differs
      expect(key1).not.toBe(key2)
      expect(key1).toMatch(/^turn:0:[a-f0-9]+:[a-f0-9]+$/)
      expect(key2).toMatch(/^turn:0:[a-f0-9]+:[a-f0-9]+$/)
    })
  })

  // ─── FIX-006: REJECT FOREIGN PLATFORM PREFIXES ────────────────────────────

  describe('FIX-006: Foreign Platform Prefix Rejection', () => {
    it('rejects foreign platform prefixes in namespaceConversationId', () => {
      // Matching platform prefixes are accepted and normalized
      expect(namespaceConversationId('chatgpt', 'chatgpt:12345')).toBe('chatgpt:12345')
      expect(namespaceConversationId('chatgpt', '12345')).toBe('chatgpt:12345')

      // Foreign prefixes must be rejected (return null)
      expect(namespaceConversationId('chatgpt', 'claude:12345')).toBeNull()
      expect(namespaceConversationId('chatgpt', 'gemini:12345')).toBeNull()
      expect(namespaceConversationId('claude', 'chatgpt:12345')).toBeNull()
      expect(namespaceConversationId('gemini', 'claude:12345')).toBeNull()
    })

    it('rejects foreign conversation_id in interaction repository and fails closed', async () => {
      await expect(
        interactionRepo.create({
          platform: 'chatgpt',
          conversation_id: 'claude:foreign-uuid',
          query: { text: 'Cross-platform injection' },
          response: { text: 'Should be rejected' },
        })
      ).rejects.toThrow(DatabaseOperationError)

      expect(await interactionRepo.count()).toBe(0)
    })
  })

  // ─── FIX-007: REJECT UNAPPROVED SENDER TABS ────────────────────────────────

  describe('FIX-007: Tab Sender Origin Validation', () => {
    it('accepts writes from known AI platform tabs matching the payload platform', () => {
      const senderChatGPT: WriteSender = { tab: { url: 'https://chatgpt.com/c/123' } }
      expect(isSenderAllowedForWrite(senderChatGPT, 'chatgpt')).toBe(true)

      const senderClaude: WriteSender = { tab: { url: 'https://claude.ai/chat/456' } }
      expect(isSenderAllowedForWrite(senderClaude, 'claude')).toBe(true)

      const senderGemini: WriteSender = { tab: { url: 'https://gemini.google.com/app/789' } }
      expect(isSenderAllowedForWrite(senderGemini, 'gemini')).toBe(true)
    })

    it('accepts writes from internal extension contexts without tabs (popup/options)', () => {
      expect(isSenderAllowedForWrite(undefined, 'chatgpt')).toBe(true)
      const senderExt: WriteSender = { url: 'chrome-extension://abcdef/popup.html' }
      expect(isSenderAllowedForWrite(senderExt, 'chatgpt')).toBe(true)
      const senderMozExt: WriteSender = { url: 'moz-extension://abcdef/popup.html' }
      expect(isSenderAllowedForWrite(senderMozExt, 'claude')).toBe(true)
    })

    it('rejects writes from third-party / unknown web tabs', () => {
      const maliciousSender: WriteSender = { tab: { url: 'https://malicious-website.com/chat' } }
      expect(isSenderAllowedForWrite(maliciousSender, 'chatgpt')).toBe(false)
      expect(isSenderAllowedForWrite(maliciousSender, 'claude')).toBe(false)
      expect(isSenderAllowedForWrite(maliciousSender, 'gemini')).toBe(false)

      const googleSender: WriteSender = { tab: { url: 'https://google.com/search' } }
      expect(isSenderAllowedForWrite(googleSender, 'gemini')).toBe(false)
    })

    it('rejects writes where sender platform conflicts with payload platform', () => {
      const chatgptSender: WriteSender = { tab: { url: 'https://chatgpt.com/c/123' } }
      // ChatGPT tab attempting to write for Claude
      expect(isSenderAllowedForWrite(chatgptSender, 'claude')).toBe(false)
    })
  })

  // ─── FIX-008: TITLE PRESERVATION & GENERIC TITLE FILTERING ─────────────────

  describe('FIX-008: Conversation Title Preservation & Tagline Filtering', () => {
    it('preserves existing non-empty title when subsequent update passes null or empty title', async () => {
      // Step 1: Create conversation with valid title
      const initial = await conversationRepo.createOrUpdate({
        id: 'thread-alpha',
        platform: 'chatgpt',
        title: 'Project Alpha Architecture',
      })
      expect(initial.title).toBe('Project Alpha Architecture')

      // Step 2: Subsequent turn passes null title (e.g. DOM title not yet updated)
      const afterNull = await conversationRepo.createOrUpdate({
        id: 'thread-alpha',
        platform: 'chatgpt',
        title: null,
      })
      expect(afterNull.title).toBe('Project Alpha Architecture')

      // Step 3: Subsequent turn passes whitespace-only title
      const afterEmpty = await conversationRepo.createOrUpdate({
        id: 'thread-alpha',
        platform: 'chatgpt',
        title: '   ',
      })
      expect(afterEmpty.title).toBe('Project Alpha Architecture')

      // Step 4: Meaningful title update is applied
      const updated = await conversationRepo.createOrUpdate({
        id: 'thread-alpha',
        platform: 'chatgpt',
        title: 'Project Alpha Final Design',
      })
      expect(updated.title).toBe('Project Alpha Final Design')
    })

    it('filters generic marketing/landing titles in extractConversationTitle', () => {
      const doc = document.implementation.createHTMLDocument()

      doc.title = 'ChatGPT: Chat, Work, Create & Code with AI'
      expect(extractConversationTitle(doc)).toBeNull()

      doc.title = 'ChatGPT: Chat, Work, Create'
      expect(extractConversationTitle(doc)).toBeNull()

      doc.title = 'ChatGPT'
      expect(extractConversationTitle(doc)).toBeNull()

      doc.title = 'New chat'
      expect(extractConversationTitle(doc)).toBeNull()

      doc.title = 'Distributed Systems Basics - ChatGPT'
      expect(extractConversationTitle(doc)).toBe('Distributed Systems Basics')
    })
  })

  // ─── CONSECUTIVE USER MESSAGES PRESERVATION ────────────────────────────────

  describe('Consecutive User Messages Preservation', () => {
    it('merges consecutive user turns instead of discarding earlier user turns', () => {
      const turns: RawMessageTurn[] = [
        {
          role: 'user',
          element: document.createElement('div'),
          text: 'Part 1: Here is the problem context.',
          messageId: 'u-1',
          sourceTimestamp: '2026-09-01T10:00:00Z',
          isStreaming: false,
        },
        {
          role: 'user',
          element: document.createElement('div'),
          text: 'Part 2: What is the recommended fix?',
          messageId: 'u-2',
          sourceTimestamp: '2026-09-01T10:00:05Z',
          isStreaming: false,
        },
        {
          role: 'assistant',
          element: document.createElement('div'),
          text: 'The recommended fix is to use exponential backoff.',
          messageId: 'a-1',
          sourceTimestamp: '2026-09-01T10:00:10Z',
          isStreaming: false,
        },
      ]

      const interactions = pairTurnsIntoInteractions('chatgpt', turns, {
        conversationId: 'conv-consecutive',
        title: 'Consecutive Messages',
        model: { provider: 'openai', name: 'gpt-4o' },
      })

      expect(interactions).toHaveLength(1)
      expect(interactions[0].queryText).toContain('Part 1: Here is the problem context.')
      expect(interactions[0].queryText).toContain('Part 2: What is the recommended fix?')
      expect(interactions[0].queryText).toBe(
        'Part 1: Here is the problem context.\n\nPart 2: What is the recommended fix?'
      )
      expect(interactions[0].responseText).toBe('The recommended fix is to use exponential backoff.')
    })
  })
})
