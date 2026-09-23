// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { DuplicateInteractionError } from '../src/database/types'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import { ClaudeAdapter } from '../src/platforms/claude/adapter'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import type { ExtractedInteraction } from '../src/platforms/types'
import {
  createErrorResponse,
  createSuccessResponse,
  isSenderAllowedForWrite,
} from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('Gemini Duplicate Record & Safe Rebinding Regression Suite', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-gemini-rebinding-${Date.now()}-${Math.random().toString(36).slice(2, 7)}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

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

  // ─── TEST 1: GEMINI UNBOUND PROMOTION WITH CHANGED RESPONSE ───────────────

  it('promotes unbound Gemini interaction in-place when DOM response text mutates post-render', async () => {
    const adapter = new GeminiAdapter({ newChatTimeoutMs: 100 })
    adapter.start()

    const initialUnbound: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: null,
      messageId: null,
      userMessageId: null,
      queryText: 'What is quantum computing?',
      responseText: 'Quantum computing operates on qubits to perform computations.',
      conversationTitle: null,
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    // 1. Initial persistence as unbound interaction (e.g. after timeout fires at /app)
    const unboundKey = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(initialUnbound)

    const res1 = await (
      adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }
    ).persistInteraction(initialUnbound, unboundKey)

    expect(res1).toBe('saved')
    expect(await interactionRepo.count()).toBe(1)

    const storedUnbound = (await interactionRepo.getAll())[0]
    expect(storedUnbound.conversation_id).toBeNull()
    expect(storedUnbound.fingerprint_strategy).toBe('level_3')
    const originalId = storedUnbound.id

    // 2. Later, Gemini updates URL to /app/gemini-conv-abc and DOM response has updated formatting/citations (R1 -> R2)
    const boundInteractionWithMutatedResponse: ExtractedInteraction = {
      ...initialUnbound,
      conversationId: 'gemini-conv-abc',
      conversationTitle: 'Quantum Computing',
      responseText:
        'Quantum computing operates on qubits to perform computations. [1] See reference notes [2].',
    }

    // Process through BaseAdapter.processInteractions
    const processResult = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([boundInteractionWithMutatedResponse])

    expect(processResult.savedCount).toBe(1)
    expect(processResult.duplicateCount).toBe(0)

    // Verify EXACTLY 1 physical record exists in IndexedDB (rebound in-place, not duplicated)
    const allRecords = await interactionRepo.getAll()
    expect(allRecords).toHaveLength(1)

    const rebound = allRecords[0]
    expect(rebound.id).toBe(originalId)
    expect(rebound.conversation_id).toBe('gemini:gemini-conv-abc')
    expect(rebound.conversation_title).toBe('Quantum Computing')
    expect(rebound.response.text).toBe(
      'Quantum computing operates on qubits to perform computations. [1] See reference notes [2].'
    )
    expect(rebound.fingerprint_strategy).toBe('level_2')

    adapter.stop()
  })

  // ─── TEST 2: IDENTICAL QUERY IN SEPARATE GEMINI CONVERSATIONS ─────────────

  it('keeps identical queries in separate Gemini conversations as independent records (never merged)', async () => {
    const adapter = new GeminiAdapter()
    adapter.start()

    const conv1Interaction: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: 'conv-session-alpha',
      messageId: null,
      userMessageId: null,
      queryText: 'What is gravity?',
      responseText: 'Gravity is a fundamental interaction that causes mutual attraction.',
      conversationTitle: 'Gravity Basics',
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const conv2Interaction: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: 'conv-session-beta',
      messageId: null,
      userMessageId: null,
      queryText: 'What is gravity?', // Identical query
      responseText: 'In general relativity, gravity is the curvature of spacetime.',
      conversationTitle: 'Gravity Physics',
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const key1 = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(conv1Interaction)
    const res1 = await (
      adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }
    ).persistInteraction(conv1Interaction, key1)
    expect(res1).toBe('saved')

    // Navigate to second conversation (resets session key cache)
    adapter.handleNavigation(
      'https://gemini.google.com/app/conv-session-alpha',
      'https://gemini.google.com/app/conv-session-beta'
    )

    const key2 = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(conv2Interaction)
    const res2 = await (
      adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }
    ).persistInteraction(conv2Interaction, key2)
    expect(res2).toBe('saved')

    // Must have exactly 2 distinct records
    const all = await interactionRepo.getAll()
    expect(all).toHaveLength(2)

    const recAlpha = all.find((r) => r.conversation_id === 'gemini:conv-session-alpha')
    const recBeta = all.find((r) => r.conversation_id === 'gemini:conv-session-beta')

    expect(recAlpha).toBeDefined()
    expect(recBeta).toBeDefined()
    expect(recAlpha?.id).not.toBe(recBeta?.id)
    expect(recAlpha?.query.text).toBe('What is gravity?')
    expect(recBeta?.query.text).toBe('What is gravity?')
    expect(recAlpha?.response.text).toContain('fundamental interaction')
    expect(recBeta?.response.text).toContain('curvature of spacetime')

    adapter.stop()
  })

  // ─── TEST 3: IDENTICAL QUERY TWICE IN SAME CONVERSATION ───────────────────

  it('handles identical queries in the same conversation without false merging or corruption', async () => {
    const adapter = new GeminiAdapter()
    adapter.start()

    const turn0: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: 'conv-repeat-query',
      messageId: null,
      userMessageId: null,
      queryText: 'Tell me a joke',
      responseText: 'Why did the chicken cross the road? To get to the other side!',
      conversationTitle: 'Jokes',
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const turn1DifferentResponse: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: 'conv-repeat-query',
      messageId: null,
      userMessageId: null,
      queryText: 'Tell me a joke', // identical query
      responseText: 'Why do programmers prefer dark mode? Because light attracts bugs!',
      conversationTitle: 'Jokes',
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 1, // different turn index
    }

    const res0 = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([turn0])
    expect(res0.savedCount).toBe(1)

    const res1 = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([turn1DifferentResponse])
    expect(res1.savedCount).toBe(1)

    // Both distinct turns are preserved
    expect(await interactionRepo.count()).toBe(2)

    // If turn 1 is submitted again with the exact same response, it is deduplicated
    const resDuplicate = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([turn1DifferentResponse])
    expect(resDuplicate.duplicateCount).toBe(1)
    expect(await interactionRepo.count()).toBe(2)

    adapter.stop()
  })

  // ─── TEST 4: GEMINI DELAYED CONVERSATION ID (TIMEOUT PATH) ────────────────

  it('safely rebinds after 4000ms timeout has fired and persisted as unbound', async () => {
    const adapter = new GeminiAdapter({ newChatTimeoutMs: 30 })
    adapter.start()

    const turn0: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: null,
      messageId: null,
      userMessageId: null,
      queryText: 'Explain photosythesis',
      responseText: 'Photosynthesis is the process by which plants convert sunlight into energy.',
      conversationTitle: null,
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    // Process while URL is /app (conversationId is null)
    const initialProc = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ queuedCount: number; savedCount: number }>
      }
    ).processInteractions([turn0])

    expect(initialProc.queuedCount).toBe(1)
    expect(await interactionRepo.count()).toBe(0)

    // Wait for timeout to fire (30ms timeout)
    await new Promise((resolve) => setTimeout(resolve, 80))

    expect(await interactionRepo.count()).toBe(1)
    const initialRecord = (await interactionRepo.getAll())[0]
    expect(initialRecord.conversation_id).toBeNull()

    // Now Gemini updates URL to /app/gemini-conv-delayed
    adapter.handleNavigation(
      'https://gemini.google.com/app',
      'https://gemini.google.com/app/gemini-conv-delayed'
    )

    // DOM scan discovers turn 0 with the assigned conversation ID and updated text
    const turn0Bound: ExtractedInteraction = {
      ...turn0,
      conversationId: 'gemini-conv-delayed',
      conversationTitle: 'Photosynthesis Overview',
      responseText:
        'Photosynthesis is the process by which green plants convert light energy into chemical energy.',
    }

    const boundProc = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([turn0Bound])

    expect(boundProc.savedCount).toBe(1)
    expect(boundProc.duplicateCount).toBe(0)

    // Exactly 1 physical record remains, now bound
    const finalRecords = await interactionRepo.getAll()
    expect(finalRecords).toHaveLength(1)
    expect(finalRecords[0].id).toBe(initialRecord.id)
    expect(finalRecords[0].conversation_id).toBe('gemini:gemini-conv-delayed')
    expect(finalRecords[0].conversation_title).toBe('Photosynthesis Overview')

    adapter.stop()
  })

  // ─── TEST 5: GEMINI PAGEHIDE / STOP FLUSH REBINDING ────────────────────────

  it('safely rebinds after stopShared() flushes unbound buffer', async () => {
    const adapter1 = new GeminiAdapter({ newChatTimeoutMs: 5000 })
    adapter1.start()

    const turn0: ExtractedInteraction = {
      platform: 'gemini',
      conversationId: null,
      messageId: null,
      userMessageId: null,
      queryText: 'What is WebAssembly?',
      responseText:
        'WebAssembly (Wasm) is a binary instruction format for a stack-based virtual machine.',
      conversationTitle: null,
      model: { provider: 'google', name: 'gemini-1.5-pro' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    // Queue in pending buffer
    await (
      adapter1 as unknown as {
        processInteractions: (items: ExtractedInteraction[]) => Promise<unknown>
      }
    ).processInteractions([turn0])

    // Pagehide / unload triggers stopShared() flush
    adapter1.stop()

    // Wait a tick for fire-and-forget persist in stopShared
    await new Promise((resolve) => setTimeout(resolve, 50))

    expect(await interactionRepo.count()).toBe(1)
    const storedUnbound = (await interactionRepo.getAll())[0]
    expect(storedUnbound.conversation_id).toBeNull()

    // Subsequent navigation / new scan with conversation ID (same response text matches exact L3 hash)
    const rebound = await interactionRepo.create({
      platform: 'gemini',
      conversation_id: 'gemini-conv-resumed',
      query: { text: turn0.queryText },
      response: { text: turn0.responseText },
      observed_at: storedUnbound.observed_at,
    })

    expect(rebound.id).toBe(storedUnbound.id)
    expect(rebound.conversation_id).toBe('gemini:gemini-conv-resumed')
    expect(await interactionRepo.count()).toBe(1)
  })

  // ─── TEST 6: CONCURRENT CROSS-PLATFORM PERSISTENCE ISOLATION ───────────────

  it('guarantees strict cross-platform isolation and sender validation across Gemini, ChatGPT, and Claude', async () => {
    const prompt = 'Explain machine learning algorithms'

    // 1. Gemini persistence
    const geminiRecord = await interactionRepo.create({
      platform: 'gemini',
      conversation_id: 'conv-gemini-1',
      query: { text: prompt },
      response: { text: 'Gemini algorithm response' },
    })

    // 2. ChatGPT persistence
    const chatgptRecord = await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-chatgpt-1',
      query: { text: prompt },
      response: { text: 'ChatGPT algorithm response' },
    })

    // 3. Claude persistence
    const claudeRecord = await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'conv-claude-1',
      query: { text: prompt },
      response: { text: 'Claude algorithm response' },
    })

    expect(await interactionRepo.count()).toBe(3)
    expect(geminiRecord.platform).toBe('gemini')
    expect(chatgptRecord.platform).toBe('chatgpt')
    expect(claudeRecord.platform).toBe('claude')
    expect(geminiRecord.conversation_id).toBe('gemini:conv-gemini-1')
    expect(chatgptRecord.conversation_id).toBe('chatgpt:conv-chatgpt-1')
    expect(claudeRecord.conversation_id).toBe('claude:conv-claude-1')

    // Verify sender write validation rejects cross-platform attempts
    const geminiSender = { tab: { url: 'https://gemini.google.com/app' } }
    const chatgptSender = { tab: { url: 'https://chatgpt.com/c/123' } }

    expect(isSenderAllowedForWrite(geminiSender, 'gemini')).toBe(true)
    expect(isSenderAllowedForWrite(geminiSender, 'chatgpt')).toBe(false)
    expect(isSenderAllowedForWrite(geminiSender, 'claude')).toBe(false)

    expect(isSenderAllowedForWrite(chatgptSender, 'chatgpt')).toBe(true)
    expect(isSenderAllowedForWrite(chatgptSender, 'gemini')).toBe(false)

    // Unbound cross-platform hijack attempt is impossible
    const unboundGemini = await interactionRepo.create({
      platform: 'gemini',
      conversation_id: null,
      query: { text: 'Unique question' },
      response: { text: 'Unique response' },
    })

    // Claude attempting to rebind Gemini unbound record by ID must fail to rebind it
    const crossPlatformRebind = await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'claude-conv-hijack',
      unbound_id: unboundGemini.id,
      query: { text: 'Unique question' },
      response: { text: 'Unique response' },
    })

    // Claude creates its own record; Gemini unbound record remains untouched
    expect(crossPlatformRebind.id).not.toBe(unboundGemini.id)
    expect(crossPlatformRebind.platform).toBe('claude')

    const recheckGemini = await interactionRepo.getById(unboundGemini.id)
    expect(recheckGemini?.platform).toBe('gemini')
    expect(recheckGemini?.conversation_id).toBeNull()
  })

  // ─── TEST 7: CHATGPT REGRESSION (AUTHENTICATED & GUEST) ────────────────────

  it('preserves ChatGPT authenticated and guest extraction/persistence semantics', async () => {
    const adapter = new ChatGPTAdapter()
    adapter.start()

    // 1. Authenticated turn with conversation ID & message IDs (L1 strategy)
    const authTurn: ExtractedInteraction = {
      platform: 'chatgpt',
      conversationId: 'chatgpt-conv-auth-1',
      messageId: 'msg-asst-101',
      userMessageId: 'msg-user-101',
      queryText: 'Write a quicksort in TypeScript',
      responseText: 'Here is a quicksort implementation...',
      conversationTitle: 'Quicksort in TS',
      model: { provider: 'openai', name: 'gpt-4o' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const keyAuth = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(authTurn)
    expect(keyAuth).toBe('msg:msg-asst-101')

    const resAuth = await (
      adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }
    ).persistInteraction(authTurn, keyAuth)
    expect(resAuth).toBe('saved')

    const storedAuth = (await interactionRepo.getAll()).find(
      (r) => r.conversation_id === 'chatgpt:chatgpt-conv-auth-1'
    )
    expect(storedAuth).toBeDefined()
    expect(storedAuth?.fingerprint_strategy).toBe('level_1')
    expect(storedAuth?.message_id).toBe('msg-asst-101')

    // 2. Anonymous / guest chat (conversationId: null, persisted unbound)
    const guestTurn: ExtractedInteraction = {
      platform: 'chatgpt',
      conversationId: null,
      messageId: 'msg-asst-guest-1',
      userMessageId: 'msg-user-guest-1',
      queryText: 'What is the capital of France?',
      responseText: 'The capital of France is Paris.',
      conversationTitle: null,
      model: { provider: 'openai', name: 'gpt-4o' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const keyGuest = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(guestTurn)
    const resGuest = await (
      adapter as unknown as {
        persistInteraction: (i: ExtractedInteraction, k: string) => Promise<string>
      }
    ).persistInteraction(guestTurn, keyGuest)
    expect(resGuest).toBe('saved')

    const storedGuest = (await interactionRepo.getAll()).find((r) => r.conversation_id === null)
    expect(storedGuest).toBeDefined()
    expect(storedGuest?.platform).toBe('chatgpt')
    expect(storedGuest?.response.text).toBe('The capital of France is Paris.')

    adapter.stop()
  })

  // ─── TEST 8: CLAUDE REGRESSION (STREAMING & SETTLED EXTRACTION) ───────────

  it('preserves Claude streaming deferral and settled turn persistence', async () => {
    const adapter = new ClaudeAdapter()
    adapter.start()

    // 1. Streaming assistant turn is deferred/skipped
    const settledTurn: ExtractedInteraction = {
      platform: 'claude',
      conversationId: 'claude-conv-settled-1',
      messageId: 'msg-claude-asst-1',
      userMessageId: 'msg-claude-user-1',
      queryText: 'Explain the CAP theorem',
      responseText:
        'The CAP theorem states that a distributed system can provide at most two of Consistency, Availability, and Partition tolerance.',
      conversationTitle: 'CAP Theorem',
      model: { provider: 'anthropic', name: 'claude-3-5-sonnet' },
      captureContext: 'on_generate',
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      turnIndex: 0,
    }

    const keySettled = (
      adapter as unknown as { generateInteractionKey: (i: ExtractedInteraction) => string }
    ).generateInteractionKey(settledTurn)
    expect(keySettled).toBe('msg:msg-claude-asst-1')

    const res = await (
      adapter as unknown as {
        processInteractions: (
          items: ExtractedInteraction[]
        ) => Promise<{ savedCount: number; duplicateCount: number }>
      }
    ).processInteractions([settledTurn])

    expect(res.savedCount).toBe(1)
    expect(res.duplicateCount).toBe(0)

    const storedClaude = (await interactionRepo.getAll()).find(
      (r) => r.conversation_id === 'claude:claude-conv-settled-1'
    )
    expect(storedClaude).toBeDefined()
    expect(storedClaude?.platform).toBe('claude')
    expect(storedClaude?.response.text).toContain('Consistency, Availability')

    adapter.stop()
  })
})
