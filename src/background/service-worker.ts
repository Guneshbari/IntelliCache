/**
 * IntelliCache Collector - Manifest V3 Background Service Worker
 *
 * NOTE: Manifest V3 service workers are ephemeral. Persistent application state
 * is kept exclusively in IndexedDB (via Dexie.js repositories).
 */

import { ConversationRepository } from '../database/repositories/conversation-repository'
import { InteractionRepository } from '../database/repositories/interaction-repository'
import { CURRENT_DB_VERSION, CURRENT_EXTENSION_VERSION, DB_NAME } from '../database/schema'
import { createErrorResponse, createSuccessResponse, isExtensionMessage } from '../shared/messages'
import type {
  DbStatsResponseData,
  ExtensionMessage,
  ExtensionResponse,
  PingResponseData,
  StatusResponseData,
} from '../shared/types'

const EXTENSION_NAME = 'IntelliCache Collector'
const EXTENSION_VERSION = CURRENT_EXTENSION_VERSION
const workerStartTime = Date.now()

// Initialize repositories (singleton database)
const interactionRepo = new InteractionRepository()
const conversationRepo = new ConversationRepository()

console.log(
  `[IntelliCache Background] Service worker active. DB: '${DB_NAME}' (v${CURRENT_DB_VERSION}). Started at: ${new Date(workerStartTime).toISOString()}`
)

// Lifecycle: Extension installed or updated
chrome.runtime.onInstalled.addListener((details) => {
  console.log(`[IntelliCache Background] Extension installed/updated. Reason: ${details.reason}`)
})

// Central Message Dispatcher
chrome.runtime.onMessage.addListener(
  (
    rawMessage: unknown,
    _sender: chrome.runtime.MessageSender,
    sendResponse: (response: ExtensionResponse) => void
  ): boolean => {
    if (!isExtensionMessage(rawMessage)) {
      console.warn('[IntelliCache Background] Received malformed message:', rawMessage)
      sendResponse(createErrorResponse('Invalid extension message format'))
      return false
    }

    const message = rawMessage as ExtensionMessage

    switch (message.type) {
      case 'PING': {
        const pingData: PingResponseData = {
          reply: 'PONG',
          echoTimestamp: message.timestamp,
          receivedFrom: message.sender,
        }
        sendResponse(createSuccessResponse(pingData))
        return false
      }

      case 'GET_STATUS': {
        const statusData: StatusResponseData = {
          extensionName: EXTENSION_NAME,
          version: EXTENSION_VERSION,
          serviceWorkerStatus: 'active',
          manifestVersion: 3,
          uptimeMs: Date.now() - workerStartTime,
        }
        sendResponse(createSuccessResponse(statusData))
        return false
      }

      case 'CONTENT_SCRIPT_INITIALIZED': {
        console.log(
          `[IntelliCache Background] Content script initialized on: ${message.payload.url} ("${message.payload.title}")`
        )
        sendResponse(
          createSuccessResponse({
            acknowledged: true,
            workerActiveTime: Date.now(),
          })
        )
        return false
      }

      case 'DB_GET_STATS': {
        // Asynchronous database queries: return true to keep the message channel open
        void (async () => {
          try {
            const [interactionCount, conversationCount] = await Promise.all([
              interactionRepo.count(),
              conversationRepo.count(),
            ])
            const statsData: DbStatsResponseData = {
              dbName: DB_NAME,
              dbVersion: CURRENT_DB_VERSION,
              interactionCount,
              conversationCount,
            }
            sendResponse(createSuccessResponse(statsData))
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
        // Asynchronous database persistence: return true
        void (async () => {
          try {
            const created = await interactionRepo.create(message.payload)
            // If conversation_id is provided, also record/update the conversation
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
        // Asynchronous database query: return true
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
        sendResponse(createErrorResponse(`Unhandled message type`))
        return false
      }
    }
  }
)
