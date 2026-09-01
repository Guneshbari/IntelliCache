// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import {
  createDbGetStatsMessage,
  createDbSaveInteractionMessage,
  createErrorResponse,
  createSuccessResponse,
  isExtensionMessage,
} from '../src/shared/messages'
import type { DbStatsResponseData, ExtensionMessage, ExtensionResponse } from '../src/shared/types'

describe('End-to-End Interaction Lifecycle Tracing & Correctness Verification', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    dbName = `trace-test-${Date.now()}-${Math.random()}`
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

  // ─── STAGE 4: ON_LOAD VS ON_GENERATE PERSISTENCE ─────────────────────────

  describe('Stage 4: on_load vs on_generate Capture Convergence', () => {
    const platforms = ['chatgpt', 'claude', 'gemini'] as const

    for (const platform of platforms) {
      it(`${platform}: on_generate followed by on_load produces exactly one physical interaction`, async () => {
        const traceId1 = `trace_gen_${platform}_1`
        const traceId2 = `trace_load_${platform}_2`

        const turnData = {
          platform,
          conversation_id: `conv-${platform}-turn1`,
          message_id: `msg-${platform}-1`,
          query: { text: `Query for ${platform}` },
          response: { text: `Response from ${platform}` },
        }

        // 1. Captured live via on_generate
        const resGen = await handleServiceWorkerMessage(
          createDbSaveInteractionMessage('content-script', {
            ...turnData,
            capture_context: 'on_generate',
            trace_id: traceId1,
          })
        )
        expect(resGen.success).toBe(true)
        expect(await interactionRepo.count()).toBe(1)
        expect(await conversationRepo.count()).toBe(1)

        // 2. Captured later via historical on_load scan
        const resLoad = await handleServiceWorkerMessage(
          createDbSaveInteractionMessage('content-script', {
            ...turnData,
            capture_context: 'on_load',
            trace_id: traceId2,
          })
        )
        // Duplicate detection prevents second record
        expect(resLoad.success).toBe(false)
        expect(resLoad.error).toContain('already exists')

        // Physical count remains strictly 1
        expect(await interactionRepo.count()).toBe(1)
        expect(await conversationRepo.count()).toBe(1)
      })
    }
  })

  // ─── STAGE 5: DOM RESCAN TEST ─────────────────────────────────────────────

  describe('Stage 5: Repeated DOM Rescans (5+ Scans)', () => {
    it('repeated DOM rescans (6 passes) produce exactly one physical record', async () => {
      const turnInput = {
        platform: 'chatgpt',
        conversation_id: 'conv-rescan-test',
        message_id: 'msg-rescan-1',
        query: { text: 'How do index cursors work in IndexedDB?' },
        response: { text: 'Index cursors iterate over keyed entries in an object store.' },
      }

      // Pass 1: initial insertion
      const res1 = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          ...turnInput,
          trace_id: 'trace_rescan_1',
        })
      )
      expect(res1.success).toBe(true)

      // Passes 2 through 6: simulated MutationObserver re-scans
      for (let pass = 2; pass <= 6; pass++) {
        const res = await handleServiceWorkerMessage(
          createDbSaveInteractionMessage('content-script', {
            ...turnInput,
            trace_id: `trace_rescan_${pass}`,
          })
        )
        expect(res.success).toBe(false)
        expect(res.error).toContain('already exists')
      }

      expect(await interactionRepo.count()).toBe(1)
      expect(await conversationRepo.count()).toBe(1)
    })
  })

  // ─── STAGE 6: REFRESH TEST ───────────────────────────────────────────────

  describe('Stage 6: Refresh Persistence Stability', () => {
    const platforms = ['chatgpt', 'claude', 'gemini'] as const

    for (const platform of platforms) {
      it(`${platform}: page refresh retains exact single physical record and fingerprint`, async () => {
        const turn = {
          platform,
          conversation_id: `conv-ref-${platform}`,
          message_id: `msg-ref-${platform}`,
          query: { text: `Refresh test query for ${platform}` },
          response: { text: `Refresh test response from ${platform}` },
          conversation_title: `${platform} Thread`,
        }

        // Before refresh
        const saveRes = await handleServiceWorkerMessage(
          createDbSaveInteractionMessage('content-script', turn)
        )
        expect(saveRes.success).toBe(true)
        expect(await interactionRepo.count()).toBe(1)

        const initialRecord = (await interactionRepo.getAll())[0]
        const initialFp = initialRecord.fingerprint
        const initialId = initialRecord.id

        // Page refresh: adapter initializes and scans DOM with on_load
        const refreshRes = await handleServiceWorkerMessage(
          createDbSaveInteractionMessage('content-script', {
            ...turn,
            capture_context: 'on_load',
            trace_id: `trace_refresh_${platform}`,
          })
        )
        expect(refreshRes.success).toBe(false)

        // Verify count and record properties are strictly unchanged
        expect(await interactionRepo.count()).toBe(1)
        const afterRefreshRecord = (await interactionRepo.getAll())[0]
        expect(afterRefreshRecord.id).toBe(initialId)
        expect(afterRefreshRecord.fingerprint).toBe(initialFp)
      })
    }
  })

  // ─── STAGE 7: NAVIGATION TEST ────────────────────────────────────────────

  describe('Stage 7: SPA Navigation Away and Back', () => {
    it('navigating between conversations preserves exact record counts without duplication', async () => {
      const convA = 'conv-spa-A'
      const convB = 'conv-spa-B'

      // 1. Thread A: Turn 1
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: convA,
          message_id: 'turn-a1',
          query: { text: 'Thread A query 1' },
          response: { text: 'Thread A response 1' },
        })
      )

      // 2. Navigate to Thread B: Turn 1
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: convB,
          message_id: 'turn-b1',
          query: { text: 'Thread B query 1' },
          response: { text: 'Thread B response 1' },
        })
      )

      expect(await interactionRepo.count()).toBe(2)
      expect(await conversationRepo.count()).toBe(2)

      // 3. Navigate back to Thread A: on_load scan re-evaluates Thread A Turn 1
      const reVisitA = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: convA,
          message_id: 'turn-a1',
          query: { text: 'Thread A query 1' },
          response: { text: 'Thread A response 1' },
          capture_context: 'on_load',
        })
      )
      expect(reVisitA.success).toBe(false)
      expect(await interactionRepo.count()).toBe(2)
      expect(await conversationRepo.count()).toBe(2)

      // 4. In Thread A, generate Turn 2
      const turnA2 = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: convA,
          message_id: 'turn-a2',
          query: { text: 'Thread A query 2' },
          response: { text: 'Thread A response 2' },
          capture_context: 'on_generate',
        })
      )
      expect(turnA2.success).toBe(true)

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(2)

      const threadAInteractions = await interactionRepo.getByConversationId(convA, 'claude')
      expect(threadAInteractions).toHaveLength(2)
    })
  })

  // ─── STAGE 8: TEMPORARY CONVERSATION ID RESOLUTION ────────────────────────

  describe('Stage 8: Temporary Conversation ID Resolution & In-Place Binding', () => {
    it('unbound interaction (conversation_id: null) binds to conversation in-place without duplicate', async () => {
      const prompt = 'Analyze architectural trade-offs of micro frontends'
      const reply = 'Micro frontends provide team autonomy at the cost of operational complexity...'

      // Step 1: User queries in new chat (route: /app or /new, conversation_id is null)
      const unboundSave = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: null,
          query: { text: prompt },
          response: { text: reply },
          trace_id: 'trace_unbound_initial',
        })
      )
      expect(unboundSave.success).toBe(true)
      expect(await interactionRepo.count()).toBe(1)

      const initialRecord = (await interactionRepo.getAll())[0]
      expect(initialRecord.conversation_id).toBeNull()
      const originalFp = initialRecord.fingerprint
      const originalId = initialRecord.id
      const originalObservedAt = initialRecord.observed_at

      // Step 2: Router assigns conversation ID 'gemini-arch-123'
      // Adapter flushes or re-scans with resolved conversation ID
      const boundSave = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'gemini-arch-123',
          query: { text: prompt },
          response: { text: reply },
          conversation_title: 'Micro Frontends Trade-offs',
          trace_id: 'trace_bound_resolution',
        })
      )
      expect(boundSave.success).toBe(true)

      // Physical interaction count must remain exactly 1
      expect(await interactionRepo.count()).toBe(1)
      expect(await conversationRepo.count()).toBe(1)

      // Verify in-place update preservation
      const updatedRecord = (await interactionRepo.getAll())[0]
      expect(updatedRecord.id).toBe(originalId)
      expect(updatedRecord.fingerprint).toBe(originalFp)
      expect(updatedRecord.observed_at).toBe(originalObservedAt)
      expect(updatedRecord.conversation_id).toBe('gemini:gemini-arch-123')
      expect(updatedRecord.conversation_title).toBe('Micro Frontends Trade-offs')

      // Verify conversation record was created
      const conv = await conversationRepo.getById('gemini-arch-123', 'gemini')
      expect(conv).not.toBeNull()
      expect(conv!.id).toBe('gemini:gemini-arch-123')
    })
  })

  // ─── STAGE 9 & 10: UI RETRIEVAL AND THREE-PLATFORM CONSISTENCY ───────────

  describe('Stage 9 & 10: UI Retrieval Consistency across All 3 Platforms', () => {
    it('UI stats response matches physical IndexedDB counts for ChatGPT, Claude, and Gemini', async () => {
      // 1. ChatGPT interaction
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: 'c-gpt',
          message_id: 'm-gpt',
          query: { text: 'Prompt 1' },
          response: { text: 'Reply 1' },
        })
      )

      // 2. Claude interaction
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: 'c-claude',
          message_id: 'm-claude',
          query: { text: 'Prompt 2' },
          response: { text: 'Reply 2' },
        })
      )

      // 3. Gemini interaction
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'c-gemini',
          message_id: 'm-gemini',
          query: { text: 'Prompt 3' },
          response: { text: 'Reply 3' },
        })
      )

      // Check direct IndexedDB count
      const rawInteractionCount = await interactionRepo.count()
      const rawConversationCount = await conversationRepo.count()
      expect(rawInteractionCount).toBe(3)
      expect(rawConversationCount).toBe(3)

      // Query UI stats endpoint
      const statsResponse = await handleServiceWorkerMessage(createDbGetStatsMessage('popup'))
      expect(statsResponse.success).toBe(true)
      const data = statsResponse.data as DbStatsResponseData

      expect(data.interactionCount).toBe(rawInteractionCount)
      expect(data.conversationCount).toBe(rawConversationCount)
    })
  })
})
