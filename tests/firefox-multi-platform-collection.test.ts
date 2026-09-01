// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import {
  createDbGetIntegrityReportMessage,
  createDbSaveInteractionMessage,
  createErrorResponse,
  createSuccessResponse,
  isExtensionMessage,
} from '../src/shared/messages'
import type {
  DbIntegrityReportData,
  DbStatsResponseData,
  ExtensionMessage,
  ExtensionResponse,
  PingResponseData,
} from '../src/shared/types'

describe('Firefox Multi-Platform Simultaneous Collection & Integrity Verification', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    // Simulate Firefox environment
    Object.defineProperty(globalThis, 'navigator', {
      value: {
        userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0',
      },
      configurable: true,
    })

    dbName = `firefox-multi-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(dbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  // Simulated background service worker / event page dispatcher
  async function handleBackgroundMessage(rawMessage: unknown): Promise<ExtensionResponse> {
    if (!isExtensionMessage(rawMessage)) {
      return createErrorResponse('Invalid extension message format')
    }

    const message = rawMessage as ExtensionMessage

    switch (message.type) {
      case 'PING': {
        const pingData: PingResponseData = {
          reply: 'PONG',
          echoTimestamp: message.timestamp,
          receivedFrom: message.sender,
        }
        return createSuccessResponse(pingData)
      }

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
          const statsData: DbStatsResponseData = {
            dbName,
            dbVersion: 1,
            interactionCount,
            conversationCount,
          }
          return createSuccessResponse(statsData)
        } catch (err) {
          return createErrorResponse(err instanceof Error ? err.message : 'Failed to get stats')
        }
      }

      case 'DB_GET_INTEGRITY_REPORT': {
        try {
          const [convReport, interactionReport] = await Promise.all([
            conversationRepo.getIntegrityReport(),
            interactionRepo.getIntegrityReport(),
          ])
          const reportData: DbIntegrityReportData = {
            conversations: convReport,
            interactions: interactionReport,
          }
          return createSuccessResponse(reportData)
        } catch (err) {
          return createErrorResponse(
            err instanceof Error ? err.message : 'Failed to generate integrity report'
          )
        }
      }

      default:
        return createErrorResponse('Unhandled message type')
    }
  }

  // ─── MULTI-TAB SIMULTANEOUS DATA COLLECTION ──────────────────────────────

  describe('Simultaneous Multi-Tab Collection (ChatGPT + Claude + Gemini in Firefox)', () => {
    it('handles concurrent interaction persistence from 3 simultaneous Firefox tabs', async () => {
      // Simulate 3 tabs generating responses simultaneously
      const tab1ChatGPT = handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: 'gpt-tab-1',
          message_id: 'gpt-msg-1',
          query: { text: 'Simulated prompt in ChatGPT tab' },
          response: { text: 'Simulated response from ChatGPT' },
          conversation_title: 'ChatGPT Tab 1',
        })
      )

      const tab2Claude = handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: 'claude-tab-2',
          message_id: 'claude-msg-2',
          query: { text: 'Simulated prompt in Claude tab' },
          response: { text: 'Simulated response from Claude' },
          conversation_title: 'Claude Tab 2',
        })
      )

      const tab3Gemini = handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'gemini-tab-3',
          message_id: 'gemini-msg-3',
          query: { text: 'Simulated prompt in Gemini tab' },
          response: { text: 'Simulated response from Gemini' },
          conversation_title: 'Gemini Tab 3',
        })
      )

      // Await all 3 concurrent dispatches
      const [res1, res2, res3] = await Promise.all([tab1ChatGPT, tab2Claude, tab3Gemini])

      expect(res1.success).toBe(true)
      expect(res2.success).toBe(true)
      expect(res3.success).toBe(true)

      // Verify IndexedDB state
      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(3)

      const gptInteractions = await interactionRepo.getByPlatform('chatgpt')
      const claudeInteractions = await interactionRepo.getByPlatform('claude')
      const geminiInteractions = await interactionRepo.getByPlatform('gemini')

      expect(gptInteractions).toHaveLength(1)
      expect(claudeInteractions).toHaveLength(1)
      expect(geminiInteractions).toHaveLength(1)

      expect(gptInteractions[0].conversation_id).toBe('chatgpt:gpt-tab-1')
      expect(claudeInteractions[0].conversation_id).toBe('claude:claude-tab-2')
      expect(geminiInteractions[0].conversation_id).toBe('gemini:gemini-tab-3')
    })

    it('rejects duplicate writes when multiple tabs open the same conversation', async () => {
      const convId = 'shared-thread-101'
      const turnInput = {
        platform: 'chatgpt' as const,
        conversation_id: convId,
        message_id: 'msg-shared-1',
        query: { text: 'What is the speed of light in vacuum?' },
        response: { text: 'The speed of light in vacuum is approximately 299,792,458 m/s.' },
      }

      // Tab A saves turn
      const tabA = await handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', turnInput)
      )
      expect(tabA.success).toBe(true)

      // Tab B in another Firefox window/tab scans the same turn
      const tabB = await handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', turnInput)
      )
      expect(tabB.success).toBe(false)
      expect(tabB.error).toContain('already exists')

      expect(await interactionRepo.count()).toBe(1)
      expect(await conversationRepo.count()).toBe(1)
    })
  })

  // ─── INTEGRITY REPORT IN FIREFOX ENVIRONMENT ─────────────────────────────

  describe('Integrity Report & Zero Duplicates Audit', () => {
    it('verifies DB_GET_INTEGRITY_REPORT reports 0 physical duplicate primary keys and 0 duplicate fingerprints', async () => {
      // Seed multi-platform interactions
      for (const platform of ['chatgpt', 'claude', 'gemini'] as const) {
        for (let t = 1; t <= 3; t++) {
          await handleBackgroundMessage(
            createDbSaveInteractionMessage('content-script', {
              platform,
              conversation_id: `thread-${platform}`,
              message_id: `turn-${platform}-${t}`,
              query: { text: `Query ${t} on ${platform}` },
              response: { text: `Response ${t} on ${platform}` },
            })
          )
        }
      }

      const reportRes = await handleBackgroundMessage(createDbGetIntegrityReportMessage('popup'))
      expect(reportRes.success).toBe(true)

      const reportData = reportRes.data as DbIntegrityReportData

      expect(reportData.conversations.total).toBe(3)
      expect(reportData.conversations.unique).toBe(3)
      expect(reportData.conversations.duplicates).toBe(0)

      expect(reportData.interactions.total).toBe(9)
      expect(reportData.interactions.uniqueFingerprints).toBe(9)
      expect(reportData.interactions.duplicateFingerprints).toBe(0)
      expect(reportData.interactions.uniqueIds).toBe(9)
      expect(reportData.interactions.duplicateIds).toBe(0)
    })
  })

  // ─── NEGATIVE & EDGE-CASE SCENARIOS ──────────────────────────────────────

  describe('Negative & Edge-Case Resilience in Firefox', () => {
    it('same prompt and response across different platforms produce distinct platform-namespaced records', async () => {
      const prompt = 'Hello, can you help me?'
      const response = 'Hello! Yes, I am here to help you.'

      // ChatGPT
      await handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'chatgpt',
          conversation_id: 'gpt-cross-test',
          query: { text: prompt },
          response: { text: response },
        })
      )

      // Claude
      await handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'claude',
          conversation_id: 'claude-cross-test',
          query: { text: prompt },
          response: { text: response },
        })
      )

      // Gemini
      await handleBackgroundMessage(
        createDbSaveInteractionMessage('content-script', {
          platform: 'gemini',
          conversation_id: 'gemini-cross-test',
          query: { text: prompt },
          response: { text: response },
        })
      )

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(3)

      const all = await interactionRepo.getAll()
      const fingerprints = new Set(all.map((i) => i.fingerprint))
      expect(fingerprints.size).toBe(3)
    })
  })
})
