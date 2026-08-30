// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { generateInteractionFingerprint } from '../src/fingerprint/fingerprint'
import {
  createDbGetStatsMessage,
  createDbSaveInteractionMessage,
  createErrorResponse,
  createSuccessResponse,
  isExtensionMessage,
} from '../src/shared/messages'
import type { DbStatsResponseData, ExtensionMessage, ExtensionResponse } from '../src/shared/types'

describe('End-to-End Interaction Lifecycle & UI Retrieval Correctness', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    dbName = `lifecycle-test-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(dbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  // Simulated service worker message handler matching background/service-worker.ts
  async function handleServiceWorkerMessage(rawMessage: unknown): Promise<ExtensionResponse> {
    if (!isExtensionMessage(rawMessage)) {
      return createErrorResponse('Invalid message format')
    }
    const message = rawMessage as ExtensionMessage

    switch (message.type) {
      case 'DB_SAVE_INTERACTION': {
        try {
          const created = await interactionRepo.create(message.payload)
          if (created.conversation_id) {
            await conversationRepo.createOrUpdate({
              id: created.conversation_id,
              platform: created.platform,
              title: created.conversation_title,
              observed_at: created.observed_at,
            })
          }
          return createSuccessResponse(created)
        } catch (err) {
          return createErrorResponse(
            err instanceof Error ? err.message : 'Failed to save interaction'
          )
        }
      }

      case 'DB_GET_STATS': {
        try {
          const [interactionCount, conversationCount] = await Promise.all([
            interactionRepo.count(),
            conversationRepo.count(),
          ])
          const data: DbStatsResponseData = {
            dbName,
            dbVersion: 1,
            interactionCount,
            conversationCount,
          }
          return createSuccessResponse(data)
        } catch (err) {
          return createErrorResponse(err instanceof Error ? err.message : 'Failed to get stats')
        }
      }

      default:
        return createErrorResponse('Unhandled message type')
    }
  }

  // ─── 1. ON_LOAD VS ON_GENERATE PERSISTENCE ───────────────────────────────

  it('same interaction captured by on_generate and then on_load produces exactly one physical interaction', async () => {
    const input = {
      platform: 'chatgpt',
      conversation_id: 'conv-100',
      message_id: 'msg-100',
      query: { text: 'What is vector search?' },
      response: { text: 'Vector search indexes embeddings.' },
    }

    // 1. Live generation: captured with capture_context: 'on_generate'
    const liveSave = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        ...input,
        capture_context: 'on_generate',
      })
    )
    expect(liveSave.success).toBe(true)
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)

    // 2. Later on_load scan: sees the same completed turn pair from DOM
    const onLoadSave = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        ...input,
        capture_context: 'on_load',
      })
    )
    // Duplicate rejection prevents second record
    expect(onLoadSave.success).toBe(false)
    expect(onLoadSave.error).toContain('already exists')
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)
  })

  // ─── 2. DOM RESCAN DEDUPLICATION ─────────────────────────────────────────

  it('same interaction captured after DOM rescan produces exactly one physical interaction', async () => {
    const input = {
      platform: 'claude',
      conversation_id: 'conv-claude-rescan',
      message_id: 'msg-claude-rescan',
      query: { text: 'Explain constitutional AI' },
      response: { text: 'Constitutional AI aligns models using principle-based feedback.' },
    }

    // First scan pass
    const res1 = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', input)
    )
    expect(res1.success).toBe(true)

    // Second scan pass (MutationObserver re-trigger)
    const res2 = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', input)
    )
    expect(res2.success).toBe(false)
    expect(res2.error).toContain('already exists')

    // Third scan pass (manual trigger)
    const res3 = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', input)
    )
    expect(res3.success).toBe(false)

    expect(await interactionRepo.count()).toBe(1)
  })

  // ─── 3. PAGE REFRESH DEDUPLICATION ───────────────────────────────────────

  it('same interaction captured after page refresh produces exactly one physical interaction', async () => {
    const input = {
      platform: 'gemini',
      conversation_id: 'conv-gemini-refresh',
      message_id: 'msg-gemini-refresh',
      query: { text: 'What are Gemini multimodal capabilities?' },
      response: { text: 'Gemini natively understands text, code, images, audio, and video.' },
      conversation_title: 'Multimodal Research',
    }

    // Persist before refresh
    const saveBefore = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', input)
    )
    expect(saveBefore.success).toBe(true)
    expect(await interactionRepo.count()).toBe(1)

    // Simulate page refresh: content script re-injects, scans DOM with capture_context: 'on_load'
    const saveAfterRefresh = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        ...input,
        capture_context: 'on_load',
      })
    )
    expect(saveAfterRefresh.success).toBe(false)
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)
  })

  // ─── 4. TEMPORARY NULL CONVERSATION_ID RESOLUTION ─────────────────────────

  it('temporary null conversation_id can later be resolved without creating a duplicate', async () => {
    const prompt = 'Plan a trip to Tokyo'
    const reply = 'Here is a 5-day itinerary for Tokyo...'

    // 1. Interaction created in new-chat route before URL assignment (conversation_id: null)
    const unboundInput = {
      platform: 'chatgpt',
      conversation_id: null,
      query: { text: prompt },
      response: { text: reply },
    }

    const unboundSave = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', unboundInput)
    )
    expect(unboundSave.success).toBe(true)
    expect(await interactionRepo.count()).toBe(1)

    const unboundRecord = await interactionRepo.getAll()
    expect(unboundRecord[0].conversation_id).toBeNull()
    const originalInteractionId = unboundRecord[0].id
    const originalFingerprint = unboundRecord[0].fingerprint

    // 2. Later, URL router assigns conversation ID 'chat-tokyo-123'
    // The adapter rescan or flush sends the interaction with conversation_id: 'chat-tokyo-123'
    const boundInput = {
      platform: 'chatgpt',
      conversation_id: 'chat-tokyo-123',
      query: { text: prompt },
      response: { text: reply },
      conversation_title: 'Tokyo Trip Plan',
    }

    const boundSave = await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', boundInput)
    )
    expect(boundSave.success).toBe(true)

    // Must still have exactly ONE physical interaction
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)

    // Verify the existing interaction was updated in-place
    const updatedRecord = await interactionRepo.getById(originalInteractionId)
    expect(updatedRecord).not.toBeNull()
    expect(updatedRecord!.id).toBe(originalInteractionId)
    expect(updatedRecord!.fingerprint).toBe(originalFingerprint)
    expect(updatedRecord!.conversation_id).toBe('chatgpt:chat-tokyo-123')
    expect(updatedRecord!.conversation_title).toBe('Tokyo Trip Plan')

    // Verify conversation record exists
    const conv = await conversationRepo.getById('chat-tokyo-123', 'chatgpt')
    expect(conv).not.toBeNull()
    expect(conv!.id).toBe('chatgpt:chat-tokyo-123')
  })

  // ─── 5. FINGERPRINT STABILITY ACROSS CONVERSATION ID RESOLUTION ───────────

  it('fingerprint remains stable when conversation_id is resolved on existing unbound record', async () => {
    const input = {
      platform: 'claude',
      query_text: 'Explain quantum entanglement',
      response_text: 'Quantum entanglement is a physical phenomenon...',
      observed_at: '2026-08-30T15:00:00.000Z',
    }

    // Generate Level 3 fingerprint for unbound interaction
    const fpUnbound = await generateInteractionFingerprint({
      platform: input.platform,
      conversation_id: null,
      message_id: null,
      query_text: input.query_text,
      response_text: input.response_text,
      observed_at: input.observed_at,
    })

    expect(fpUnbound.strategy).toBe('level_3')
    expect(fpUnbound.fingerprint).toHaveLength(64)

    // Save unbound
    const created = await interactionRepo.create({
      platform: input.platform,
      conversation_id: null,
      query: { text: input.query_text },
      response: { text: input.response_text },
      observed_at: input.observed_at,
    })
    expect(created.fingerprint).toBe(fpUnbound.fingerprint)

    // Bind to conversation
    const bound = await interactionRepo.create({
      platform: input.platform,
      conversation_id: 'claude-conv-quantum',
      query: { text: input.query_text },
      response: { text: input.response_text },
      observed_at: input.observed_at,
    })

    expect(bound.id).toBe(created.id)
    expect(bound.fingerprint).toBe(created.fingerprint)
    expect(bound.conversation_id).toBe('claude:claude-conv-quantum')
    expect(await interactionRepo.count()).toBe(1)
  })

  // ─── 6, 7, 8. UI STATS MATCH INDEXEDDB COUNTS PER PLATFORM ────────────────

  it('ChatGPT UI count matches IndexedDB count', async () => {
    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-gpt-1',
      message_id: 'm1',
      query: { text: 'Q1' },
      response: { text: 'R1' },
    })
    await conversationRepo.createOrUpdate({ id: 'conv-gpt-1', platform: 'chatgpt' })

    const statsResponse = await handleServiceWorkerMessage(createDbGetStatsMessage('popup'))
    expect(statsResponse.success).toBe(true)
    const stats = statsResponse.data as DbStatsResponseData

    const directInteractionCount = await interactionRepo.count()
    const directConversationCount = await conversationRepo.count()

    expect(stats.interactionCount).toBe(directInteractionCount)
    expect(stats.conversationCount).toBe(directConversationCount)
    expect(stats.interactionCount).toBe(1)
    expect(stats.conversationCount).toBe(1)
  })

  it('Claude UI count matches IndexedDB count', async () => {
    await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'conv-claude-1',
      message_id: 'm2',
      query: { text: 'Q2' },
      response: { text: 'R2' },
    })
    await conversationRepo.createOrUpdate({ id: 'conv-claude-1', platform: 'claude' })

    const statsResponse = await handleServiceWorkerMessage(createDbGetStatsMessage('popup'))
    expect(statsResponse.success).toBe(true)
    const stats = statsResponse.data as DbStatsResponseData

    expect(stats.interactionCount).toBe(await interactionRepo.count())
    expect(stats.conversationCount).toBe(await conversationRepo.count())
    expect(stats.interactionCount).toBe(1)
  })

  it('Gemini UI count matches IndexedDB count', async () => {
    await interactionRepo.create({
      platform: 'gemini',
      conversation_id: 'conv-gemini-1',
      message_id: 'm3',
      query: { text: 'Q3' },
      response: { text: 'R3' },
    })
    await conversationRepo.createOrUpdate({ id: 'conv-gemini-1', platform: 'gemini' })

    const statsResponse = await handleServiceWorkerMessage(createDbGetStatsMessage('popup'))
    expect(statsResponse.success).toBe(true)
    const stats = statsResponse.data as DbStatsResponseData

    expect(stats.interactionCount).toBe(await interactionRepo.count())
    expect(stats.conversationCount).toBe(await conversationRepo.count())
    expect(stats.interactionCount).toBe(1)
  })

  // ─── 9. THREE LEGITIMATE TURNS REMAIN THREE ACROSS REFRESH & NAVIGATION ───

  it('three legitimate turns remain exactly three after refresh and navigation', async () => {
    const convId = 'multi-turn-thread'
    await conversationRepo.createOrUpdate({
      id: convId,
      platform: 'chatgpt',
      title: 'Full Multi-turn Conversation',
    })

    // Turn 1
    await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-1',
        query: { text: 'Step 1: Introduction' },
        response: { text: 'Here is step 1.' },
        capture_context: 'on_generate',
      })
    )

    // Turn 2
    await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-2',
        query: { text: 'Step 2: Analysis' },
        response: { text: 'Here is step 2.' },
        capture_context: 'on_generate',
      })
    )

    // Turn 3
    await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-3',
        query: { text: 'Step 3: Conclusion' },
        response: { text: 'Here is step 3.' },
        capture_context: 'on_generate',
      })
    )

    expect(await interactionRepo.count()).toBe(3)
    expect(await conversationRepo.count()).toBe(1)

    // Simulate page refresh: all 3 turns re-scanned with on_load
    for (let i = 1; i <= 3; i++) {
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: convId,
          message_id: `turn-${i}`,
          query: {
            text: `Step ${i}: ${i === 1 ? 'Introduction' : i === 2 ? 'Analysis' : 'Conclusion'}`,
          },
          response: { text: `Here is step ${i}.` },
          capture_context: 'on_load',
        })
      )
    }

    expect(await interactionRepo.count()).toBe(3)
    expect(await conversationRepo.count()).toBe(1)

    // Simulate SPA navigation away to another thread and back
    const otherConvId = 'other-thread'
    await conversationRepo.createOrUpdate({ id: otherConvId, platform: 'chatgpt' })
    await handleServiceWorkerMessage(
      createDbSaveInteractionMessage('content-script', {
        platform: 'chatgpt',
        conversation_id: otherConvId,
        message_id: 'other-turn-1',
        query: { text: 'Other conversation query' },
        response: { text: 'Other conversation response' },
      })
    )

    expect(await interactionRepo.count()).toBe(4)
    expect(await conversationRepo.count()).toBe(2)

    // Navigate back to original thread and re-scan
    for (let i = 1; i <= 3; i++) {
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: convId,
          message_id: `turn-${i}`,
          query: {
            text: `Step ${i}: ${i === 1 ? 'Introduction' : i === 2 ? 'Analysis' : 'Conclusion'}`,
          },
          response: { text: `Here is step ${i}.` },
          capture_context: 'on_load',
        })
      )
    }

    // Target conversation still has exactly 3 interactions
    const originalInteractions = await interactionRepo.getByConversationId(convId, 'chatgpt')
    expect(originalInteractions).toHaveLength(3)

    // Total database interaction count is 4 (3 original + 1 other)
    expect(await interactionRepo.count()).toBe(4)
    expect(await conversationRepo.count()).toBe(2)
  })
})
