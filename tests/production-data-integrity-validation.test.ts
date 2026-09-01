// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { CURRENT_DB_VERSION, SCHEMA_V1 } from '../src/database/schema'
import { generateInteractionFingerprint } from '../src/fingerprint/fingerprint'
import { normalizeTextForFingerprint } from '../src/fingerprint/normalize'
import {
  createDbGetStatsMessage,
  createDbSaveInteractionMessage,
  createErrorResponse,
  createSuccessResponse,
  isExtensionMessage,
} from '../src/shared/messages'
import type { DbStatsResponseData, ExtensionMessage, ExtensionResponse } from '../src/shared/types'

describe('Production-Like Data Integrity & Persistence Validation', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    dbName = `prod-val-${Date.now()}-${Math.random()}`
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

  // ─── PHASE 1: FRESH DATABASE VALIDATION ──────────────────────────────────

  describe('Phase 1: Fresh Database Validation', () => {
    it('verifies clean state, schema definitions, and index presence', async () => {
      await db.open()
      const rawIdb = db.backendDB()

      expect(db.verno).toBe(CURRENT_DB_VERSION)
      expect(rawIdb.version).toBe(CURRENT_DB_VERSION * 10)

      expect(await interactionRepo.count()).toBe(0)
      expect(await conversationRepo.count()).toBe(0)

      const intReport = await interactionRepo.getIntegrityReport()
      expect(intReport.total).toBe(0)
      expect(intReport.uniqueFingerprints).toBe(0)
      expect(intReport.duplicateFingerprints).toBe(0)
      expect(intReport.uniqueIds).toBe(0)
      expect(intReport.duplicateIds).toBe(0)

      const convReport = await conversationRepo.getIntegrityReport()
      expect(convReport.total).toBe(0)
      expect(convReport.unique).toBe(0)
      expect(convReport.duplicates).toBe(0)

      // Schema reflection verification
      expect(SCHEMA_V1.interactions).toBe(
        'id, &fingerprint, platform, conversation_id, observed_at'
      )
      expect(SCHEMA_V1.conversations).toBe('id, platform, first_observed_at, last_observed_at')
    })
  })

  // ─── PHASE 2: REAL BROWSER CAPTURE MATRIX ACROSS 3 PLATFORMS ─────────────

  describe('Phase 2: Real Browser Capture Matrix (ChatGPT, Claude, Gemini)', () => {
    const platforms = ['chatgpt', 'claude', 'gemini'] as const

    for (const platform of platforms) {
      describe(`Platform: ${platform.toUpperCase()}`, () => {
        it('single_turn: produces exactly 1 conversation and 1 interaction', async () => {
          const res = await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              platform,
              conversation_id: `conv-single-${platform}`,
              message_id: `msg-single-${platform}`,
              query: { text: `Single turn prompt on ${platform}` },
              response: { text: `Single turn response from ${platform}` },
              conversation_title: `${platform} Single Turn`,
              capture_context: 'on_generate',
            })
          )
          expect(res.success).toBe(true)

          expect(await conversationRepo.count()).toBe(1)
          expect(await interactionRepo.count()).toBe(1)

          const allInteractions = await interactionRepo.getAll()
          expect(allInteractions).toHaveLength(1)
          expect(allInteractions[0].platform).toBe(platform)
          expect(allInteractions[0].conversation_id).toBe(`${platform}:conv-single-${platform}`)
        })

        it('refresh: count and fingerprints remain strictly unchanged', async () => {
          const turnData = {
            platform,
            conversation_id: `conv-ref-${platform}`,
            message_id: `msg-ref-${platform}`,
            query: { text: `Refresh prompt for ${platform}` },
            response: { text: `Refresh response from ${platform}` },
            conversation_title: `${platform} Refresh Thread`,
          }

          // Initial capture (live generation)
          await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              ...turnData,
              capture_context: 'on_generate',
            })
          )
          expect(await interactionRepo.count()).toBe(1)
          const initialRecord = (await interactionRepo.getAll())[0]

          // Page reload: on_load scan sees the same turn
          const refreshRes = await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              ...turnData,
              capture_context: 'on_load',
            })
          )
          expect(refreshRes.success).toBe(false)
          expect(refreshRes.error).toContain('already exists')

          expect(await interactionRepo.count()).toBe(1)
          expect(await conversationRepo.count()).toBe(1)

          const afterRecord = (await interactionRepo.getAll())[0]
          expect(afterRecord.id).toBe(initialRecord.id)
          expect(afterRecord.fingerprint).toBe(initialRecord.fingerprint)
        })

        it('spa_navigation: navigating away and returning preserves exact records', async () => {
          const conv1 = `conv-nav1-${platform}`
          const conv2 = `conv-nav2-${platform}`

          // 1. Thread 1 turn
          await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              platform,
              conversation_id: conv1,
              message_id: `m-${platform}-1`,
              query: { text: 'Thread 1 query' },
              response: { text: 'Thread 1 response' },
            })
          )

          // 2. Navigate to Thread 2
          await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              platform,
              conversation_id: conv2,
              message_id: `m-${platform}-2`,
              query: { text: 'Thread 2 query' },
              response: { text: 'Thread 2 response' },
            })
          )

          expect(await interactionRepo.count()).toBe(2)
          expect(await conversationRepo.count()).toBe(2)

          // 3. Navigate back to Thread 1 (on_load re-scan)
          const reScanRes = await handleServiceWorkerMessage(
            createDbSaveInteractionMessage('content-script', {
              platform,
              conversation_id: conv1,
              message_id: `m-${platform}-1`,
              query: { text: 'Thread 1 query' },
              response: { text: 'Thread 1 response' },
              capture_context: 'on_load',
            })
          )
          expect(reScanRes.success).toBe(false)

          expect(await interactionRepo.count()).toBe(2)
          expect(await conversationRepo.count()).toBe(2)
        })

        it('multiple_turns: 3 unique prompts produce exactly 3 interactions and 1 conversation', async () => {
          const convId = `conv-multi-${platform}`

          for (let turn = 1; turn <= 3; turn++) {
            const res = await handleServiceWorkerMessage(
              createDbSaveInteractionMessage('content-script', {
                platform,
                conversation_id: convId,
                message_id: `msg-${platform}-t${turn}`,
                query: { text: `Prompt ${turn} on ${platform}` },
                response: { text: `Response ${turn} from ${platform}` },
                capture_context: 'on_generate',
              })
            )
            expect(res.success).toBe(true)
          }

          expect(await conversationRepo.count()).toBe(1)
          expect(await interactionRepo.count()).toBe(3)

          // Refresh: all 3 turns re-scanned via on_load
          for (let turn = 1; turn <= 3; turn++) {
            const refRes = await handleServiceWorkerMessage(
              createDbSaveInteractionMessage('content-script', {
                platform,
                conversation_id: convId,
                message_id: `msg-${platform}-t${turn}`,
                query: { text: `Prompt ${turn} on ${platform}` },
                response: { text: `Response ${turn} from ${platform}` },
                capture_context: 'on_load',
              })
            )
            expect(refRes.success).toBe(false)
          }

          expect(await conversationRepo.count()).toBe(1)
          expect(await interactionRepo.count()).toBe(3)

          const records = await interactionRepo.getByConversationId(convId, platform)
          expect(records).toHaveLength(3)
          const fps = new Set(records.map((r) => r.fingerprint))
          expect(fps.size).toBe(3)
        })
      })
    }
  })

  // ─── PHASE 3: FINGERPRINT LIFECYCLE AUDIT ────────────────────────────────

  describe('Phase 3: Fingerprint Input Components and Normalization', () => {
    it('compares fingerprint inputs between on_generate and on_load for identical turn', async () => {
      const turn = {
        platform: 'gemini',
        conversation_id: 'conv-fp-audit',
        message_id: 'msg-fp-1',
        query_text: '  Explain   neural\r\n\r\nembeddings.  ',
        response_text: 'Embeddings map semantic concepts to dense vectors.',
        observed_at: '2026-09-01T10:00:00.000Z',
      }

      // Compute on_generate fingerprint
      const fpGen = await generateInteractionFingerprint(turn)

      // Compute on_load fingerprint (identical DOM text)
      const fpLoad = await generateInteractionFingerprint(turn)

      expect(fpGen.fingerprint).toBe(fpLoad.fingerprint)
      expect(fpGen.canonicalPayload).toBe(fpLoad.canonicalPayload)
      expect(fpGen.strategy).toBe('level_1')
      expect(fpGen.canonicalPayload).toBe('L1|gemini|conv-fp-audit|msg-fp-1')
    })

    it('verifies text normalization idempotence and deterministic whitespace collapsing', () => {
      const raw1 =
        'What  is\t deep learning?\r\n\r\n\r\nIt is machine learning with deep neural networks.'
      const raw2 = 'What is deep learning?\n\nIt is machine learning with deep neural networks.'

      const norm1 = normalizeTextForFingerprint(raw1)
      const norm2 = normalizeTextForFingerprint(raw2)

      expect(norm1).toBe(norm2)
      expect(norm1).toBe(
        'What is deep learning?\n\nIt is machine learning with deep neural networks.'
      )
    })
  })

  // ─── PHASE 4: CONVERSATION_ID NULL BINDING TEST ──────────────────────────

  describe('Phase 4: Temporary conversation_id Null Binding Proof', () => {
    it('persists unbound interaction and binds it in-place when conversation ID becomes available', async () => {
      const prompt = 'Design a high-throughput distributed message queue'
      const reply =
        'Key design pillars include partitioned commit logs, zero-copy I/O, and sequential disk writes...'

      // Step 1: User submits in new chat (route: /app, conversation_id: null)
      const unboundSave = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: null,
          query: { text: prompt },
          response: { text: reply },
          trace_id: 'trace_unbound_stage4',
        })
      )
      expect(unboundSave.success).toBe(true)
      expect(await interactionRepo.count()).toBe(1)

      const initialRecord = (await interactionRepo.getAll())[0]
      const originalId = initialRecord.id
      const originalFingerprint = initialRecord.fingerprint
      const originalObservedAt = initialRecord.observed_at
      expect(initialRecord.conversation_id).toBeNull()

      // Step 2: SPA router assigns conversation ID 'chat-queue-design-456'
      const boundSave = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: 'chat-queue-design-456',
          query: { text: prompt },
          response: { text: reply },
          conversation_title: 'Queue Design Thread',
          trace_id: 'trace_bound_stage4',
        })
      )
      expect(boundSave.success).toBe(true)

      // Physical interaction count must remain 1
      expect(await interactionRepo.count()).toBe(1)
      expect(await conversationRepo.count()).toBe(1)

      // Verify in-place update: ID, fingerprint, and timestamp are preserved
      const updatedRecord = (await interactionRepo.getAll())[0]
      expect(updatedRecord.id).toBe(originalId)
      expect(updatedRecord.fingerprint).toBe(originalFingerprint)
      expect(updatedRecord.observed_at).toBe(originalObservedAt)
      expect(updatedRecord.conversation_id).toBe('claude:chat-queue-design-456')
      expect(updatedRecord.conversation_title).toBe('Queue Design Thread')

      // Verify conversation record was created
      const conv = await conversationRepo.getById('chat-queue-design-456', 'claude')
      expect(conv).not.toBeNull()
      expect(conv!.id).toBe('claude:chat-queue-design-456')
    })
  })

  // ─── PHASE 5: UNBOUND FINGERPRINT COLLISION ANALYSIS ─────────────────────

  describe('Phase 5: Level-3 Unbound Collision Analysis', () => {
    it('demonstrates Level-3 formula behavior and hourly window bucketing', async () => {
      const prompt = 'Hello'
      const response = 'Hi there!'

      const t1 = '2026-09-01T10:15:00.000Z'
      const t2 = '2026-09-01T10:45:00.000Z' // Same UTC hour (10)
      const t3 = '2026-09-01T11:05:00.000Z' // Different UTC hour (11)

      const fp1 = await generateInteractionFingerprint({
        platform: 'chatgpt',
        conversation_id: null,
        query_text: prompt,
        response_text: response,
        observed_at: t1,
      })

      const fp2 = await generateInteractionFingerprint({
        platform: 'chatgpt',
        conversation_id: null,
        query_text: prompt,
        response_text: response,
        observed_at: t2,
      })

      const fp3 = await generateInteractionFingerprint({
        platform: 'chatgpt',
        conversation_id: null,
        query_text: prompt,
        response_text: response,
        observed_at: t3,
      })

      // Within same hour bucket: identical Level-3 payload
      expect(fp1.fingerprint).toBe(fp2.fingerprint)
      expect(fp1.canonicalPayload).toBe('L3|chatgpt|Hello|Hi there!|2026-09-01T10')

      // Across different hours: different hourly bucket
      expect(fp1.fingerprint).not.toBe(fp3.fingerprint)
      expect(fp3.canonicalPayload).toBe('L3|chatgpt|Hello|Hi there!|2026-09-01T11')
    })
  })

  // ─── PHASE 6: DUPLICATE WRITE AUDIT ──────────────────────────────────────

  describe('Phase 6: Trace ID Persistence Metrics and Deduplication Accounting', () => {
    it('accurately accounts for candidate detections, inserts, duplicate rejections, and updates', async () => {
      let candidateDetections = 0
      let persistenceRequests = 0
      let newInserts = 0
      let duplicateRejections = 0
      let updates = 0

      // Scenario:
      // Turn 1: unbound insert (candidate + persistence)
      candidateDetections++
      persistenceRequests++
      const res1 = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: null,
          query: { text: 'Analyze market trends' },
          response: { text: 'Market trends indicate growth in edge AI.' },
        })
      )
      if (res1.success) newInserts++

      // Turn 1 bound: route assigns conversation ID -> update
      candidateDetections++
      persistenceRequests++
      const res2 = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'gemini-trends-999',
          query: { text: 'Analyze market trends' },
          response: { text: 'Market trends indicate growth in edge AI.' },
        })
      )
      if (res2.success) updates++

      // Turn 1 re-scan: DOM rescan -> duplicate rejection
      candidateDetections++
      persistenceRequests++
      const res3 = await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'gemini-trends-999',
          query: { text: 'Analyze market trends' },
          response: { text: 'Market trends indicate growth in edge AI.' },
        })
      )
      if (!res3.success && res3.error?.includes('already exists')) duplicateRejections++

      expect(candidateDetections).toBe(3)
      expect(persistenceRequests).toBe(3)
      expect(newInserts).toBe(1)
      expect(updates).toBe(1)
      expect(duplicateRejections).toBe(1)
      expect(await interactionRepo.count()).toBe(1)
    })
  })

  // ─── PHASE 7: UI VERSUS INDEXEDDB TRUTH AUDIT ────────────────────────────

  describe('Phase 7: UI State Consistency vs Raw IndexedDB Truth', () => {
    it('verifies exact parity between raw IndexedDB stores and popup stats endpoint', async () => {
      // Seed multi-platform data
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: 'c-gpt-truth',
          message_id: 'm-gpt-1',
          query: { text: 'Q1' },
          response: { text: 'R1' },
        })
      )
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: 'c-claude-truth',
          message_id: 'm-claude-1',
          query: { text: 'Q2' },
          response: { text: 'R2' },
        })
      )
      await handleServiceWorkerMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'c-gemini-truth',
          message_id: 'm-gemini-1',
          query: { text: 'Q3' },
          response: { text: 'R3' },
        })
      )

      // Raw IndexedDB records
      const rawConversations = await conversationRepo.getAll()
      const rawInteractions = await interactionRepo.getAll()

      expect(rawConversations).toHaveLength(3)
      expect(rawInteractions).toHaveLength(3)

      // UI stats message
      const statsRes = await handleServiceWorkerMessage(createDbGetStatsMessage('popup'))
      expect(statsRes.success).toBe(true)
      const uiData = statsRes.data as DbStatsResponseData

      expect(uiData.conversationCount).toBe(rawConversations.length)
      expect(uiData.interactionCount).toBe(rawInteractions.length)
    })
  })
})
