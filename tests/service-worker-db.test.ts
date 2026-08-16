import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { CURRENT_DB_VERSION, DB_NAME } from '../src/database/schema'
import type { Interaction } from '../src/database/types'
import {
  createDbGetInteractionMessage,
  createDbGetStatsMessage,
  createDbSaveInteractionMessage,
  createErrorResponse,
  createSuccessResponse,
  isExtensionMessage,
  sendExtensionMessage,
} from '../src/shared/messages'
import type { DbStatsResponseData, ExtensionMessage, ExtensionResponse } from '../src/shared/types'

describe('Service Worker Async Database Operations & Messaging', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  const originalChrome = globalThis.chrome

  beforeEach(() => {
    const testDbName = `test-sw-db-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
  })

  // Simulated service worker listener logic matching src/background/service-worker.ts
  function handleServiceWorkerMessage(
    rawMessage: unknown,
    sendResponse: (response: ExtensionResponse) => void
  ): boolean {
    if (!isExtensionMessage(rawMessage)) {
      sendResponse(createErrorResponse('Invalid extension message format'))
      return false
    }

    const message = rawMessage as ExtensionMessage

    switch (message.type) {
      case 'DB_GET_STATS': {
        void (async () => {
          try {
            const [interactionCount, conversationCount] = await Promise.all([
              interactionRepo.count(),
              conversationRepo.count(),
            ])
            sendResponse(
              createSuccessResponse<DbStatsResponseData>({
                dbName: DB_NAME,
                dbVersion: CURRENT_DB_VERSION,
                interactionCount,
                conversationCount,
              })
            )
          } catch (err) {
            sendResponse(
              createErrorResponse(
                err instanceof Error ? err.message : 'Failed to retrieve database stats'
              )
            )
          }
        })()
        return true
      }

      case 'DB_SAVE_INTERACTION': {
        void (async () => {
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
            sendResponse(createSuccessResponse(created))
          } catch (err) {
            sendResponse(
              createErrorResponse(err instanceof Error ? err.message : 'Failed to save interaction')
            )
          }
        })()
        return true
      }

      case 'DB_GET_INTERACTION': {
        void (async () => {
          try {
            const interaction = await interactionRepo.getById(message.payload.id)
            sendResponse(createSuccessResponse(interaction))
          } catch (err) {
            sendResponse(
              createErrorResponse(
                err instanceof Error ? err.message : 'Failed to retrieve interaction'
              )
            )
          }
        })()
        return true
      }

      default: {
        sendResponse(createErrorResponse('Unhandled message type'))
        return false
      }
    }
  }

  function setupMockChromeRuntime() {
    globalThis.chrome = {
      runtime: {
        lastError: undefined,
        sendMessage: vi.fn((message: unknown, callback: (response: unknown) => void) => {
          handleServiceWorkerMessage(message, callback)
        }),
      },
    } as unknown as typeof chrome
  }

  it('handles DB_GET_STATS asynchronously and returns valid ExtensionResponse envelope', async () => {
    setupMockChromeRuntime()

    const statsMsg = createDbGetStatsMessage('popup')
    const response = await sendExtensionMessage<typeof statsMsg, DbStatsResponseData>(statsMsg)

    expect(response.success).toBe(true)
    expect(response.data).toBeDefined()
    expect(response.data?.dbName).toBe(DB_NAME)
    expect(response.data?.dbVersion).toBe(CURRENT_DB_VERSION)
    expect(response.data?.interactionCount).toBe(0)
    expect(response.data?.conversationCount).toBe(0)
  })

  it('handles DB_SAVE_INTERACTION asynchronously and saves interaction + conversation record', async () => {
    setupMockChromeRuntime()

    const saveMsg = createDbSaveInteractionMessage('content-script', {
      platform: 'chatgpt',
      conversation_id: 'conv-sw-test-1',
      message_id: 'msg-sw-test-1',
      query: { text: 'Test query via service worker' },
      response: { text: 'Test response via service worker' },
      conversation_title: 'SW Test Conversation',
    })

    const response = await sendExtensionMessage<typeof saveMsg, Interaction>(saveMsg)

    expect(response.success).toBe(true)
    expect(response.data).toBeDefined()
    expect(response.data?.platform).toBe('chatgpt')
    expect(response.data?.fingerprint).toHaveLength(64)
    expect(response.data?.fingerprint_strategy).toBe('level_1')
    expect(response.data?.conversation_id).toBe('chatgpt:conv-sw-test-1')

    // Verify conversation was also recorded with namespaced ID
    const savedConv = await conversationRepo.getById('conv-sw-test-1', 'chatgpt')
    expect(savedConv).not.toBeNull()
    expect(savedConv?.id).toBe('chatgpt:conv-sw-test-1')
    expect(savedConv?.title).toBe('SW Test Conversation')

    // Verify stats updated
    expect(await interactionRepo.count()).toBe(1)
    expect(await conversationRepo.count()).toBe(1)
  })

  it('handles DB_GET_INTERACTION to retrieve a saved interaction by ID', async () => {
    setupMockChromeRuntime()

    const created = await interactionRepo.create({
      platform: 'claude',
      query: { text: 'Query to get' },
      response: { text: 'Response to get' },
    })

    const getMsg = createDbGetInteractionMessage('popup', created.id)
    const response = await sendExtensionMessage<typeof getMsg, Interaction | null>(getMsg)

    expect(response.success).toBe(true)
    expect(response.data).toEqual(created)
  })

  it('returns structured error envelope when attempting duplicate interaction insertion', async () => {
    setupMockChromeRuntime()

    const payload = {
      platform: 'gemini',
      conversation_id: 'conv-dup',
      message_id: 'msg-dup',
      query: { text: 'Same query' },
      response: { text: 'Same response' },
    }

    const saveMsg1 = createDbSaveInteractionMessage('content-script', payload)
    const res1 = await sendExtensionMessage(saveMsg1)
    expect(res1.success).toBe(true)

    // Attempting duplicate
    const saveMsg2 = createDbSaveInteractionMessage('content-script', payload)
    const res2 = await sendExtensionMessage(saveMsg2)

    expect(res2.success).toBe(false)
    expect(res2.error).toContain('already exists')
    expect(res2.data).toBeUndefined()
  })
})
