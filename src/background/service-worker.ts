/**
 * IntelliCache Collector - Manifest V3 Background Service Worker
 *
 * NOTE: Manifest V3 service workers are ephemeral. Persistent application state
 * is kept exclusively in IndexedDB (via Dexie.js repositories).
 */

import { ConversationRepository } from '../database/repositories/conversation-repository'
import { InteractionRepository } from '../database/repositories/interaction-repository'
import { CURRENT_DB_VERSION, CURRENT_EXTENSION_VERSION, DB_NAME } from '../database/schema'
import { DatabaseOperationError, DuplicateInteractionError } from '../database/types'
import { logger, toDiagnosticPlatform } from '../diagnostics'
import {
  createErrorResponse,
  createSuccessResponse,
  detectPlatformFromUrl,
  isExtensionMessage,
} from '../shared/messages'
import type {
  BaseMessage,
  DbIntegrityReportData,
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

logger.info(
  'Background',
  'CORE',
  `Service worker active. DB: '${DB_NAME}' (v${CURRENT_DB_VERSION}). Started at: ${new Date(workerStartTime).toISOString()}`
)

// Lifecycle: Extension installed or updated
chrome.runtime.onInstalled.addListener((details) => {
  logger.info('Background', 'CORE', `Extension installed/updated. Reason: ${details.reason}`)
})

// Central Message Dispatcher
chrome.runtime.onMessage.addListener(
  (
    rawMessage: unknown,
    _sender: chrome.runtime.MessageSender,
    sendResponse: (response: ExtensionResponse) => void
  ): boolean => {
    if (!isExtensionMessage(rawMessage)) {
      logger.warn('Background', 'CORE', 'Received malformed extension message (invalid format).')
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
        logger.debug('Background', 'CORE', `Handled PING from ${message.sender}`)
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
        logger.debug('Background', 'CORE', 'Handled GET_STATUS request')
        sendResponse(createSuccessResponse(statusData))
        return false
      }

      case 'CONTENT_SCRIPT_INITIALIZED': {
        const platformTag = toDiagnosticPlatform(detectPlatformFromUrl(message.payload.url))
        logger.info(
          'Background',
          platformTag,
          `Content script initialized on: ${message.payload.url} ("${message.payload.title}")`
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
            logger.debug(
              'Background',
              'CORE',
              `Retrieved DB stats: ${interactionCount} interactions, ${conversationCount} conversations`
            )
            sendResponse(createSuccessResponse(statsData))
          } catch (err) {
            logger.error(
              'Background',
              'CORE',
              `Failed to retrieve database stats: ${err instanceof Error ? err.message : String(err)}`
            )
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
        const platformTag = toDiagnosticPlatform(message.payload.platform)
        logger.info(
          'Background',
          platformTag,
          `Received DB_SAVE_INTERACTION (conversationId: ${message.payload.conversation_id ?? 'null'}, captureContext: ${message.payload.capture_context ?? 'on_generate'}, queryChars: ${message.payload.query.text.length}, responseChars: ${message.payload.response.text.length})`
        )

        // Asynchronous database persistence: return true
        void (async () => {
          try {
            logger.debug('Background', platformTag, 'Starting database persistence operation...')
            const created = await interactionRepo.create(message.payload)

            // If conversation_id is provided, also record/update the conversation
            if (created.conversation_id) {
              logger.debug(
                'Background',
                platformTag,
                `Recording/updating conversation metadata for '${created.conversation_id}'...`
              )
              await conversationRepo.createOrUpdate({
                id: created.conversation_id,
                platform: created.platform,
                title: created.conversation_title,
                observed_at: created.observed_at,
              })
            }

            logger.info(
              'Background',
              platformTag,
              `Interaction persisted successfully (ID: ${created.id}, fingerprint: ${created.fingerprint.slice(0, 16)}..., strategy: ${created.fingerprint_strategy})`
            )
            sendResponse(createSuccessResponse(created))
          } catch (err) {
            if (err instanceof DuplicateInteractionError) {
              logger.info(
                'Background',
                platformTag,
                `Duplicate interaction detected: ${err.message}`
              )
            } else if (err instanceof DatabaseOperationError) {
              logger.error(
                'Background',
                platformTag,
                `Structured database operation error: ${err.message}`
              )
            } else {
              logger.error(
                'Background',
                platformTag,
                `Unexpected error saving interaction: ${err instanceof Error ? err.message : String(err)}`
              )
            }
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
            logger.error(
              'Background',
              'CORE',
              `Failed to retrieve interaction (${message.payload.id}): ${err instanceof Error ? err.message : String(err)}`
            )
            sendResponse(
              createErrorResponse(
                err instanceof Error ? err.message : 'Failed to retrieve interaction'
              )
            )
          }
        })()
        return true
      }

      case 'DB_GET_INTEGRITY_REPORT': {
        // Development-only: full database integrity scan
        void (async () => {
          try {
            const [convReport, interactionReport] = await Promise.all([
              conversationRepo.getIntegrityReport(),
              interactionRepo.getIntegrityReport(),
            ])

            const reportData: DbIntegrityReportData = {
              conversations: convReport,
              interactions: interactionReport,
            }

            logger.info(
              'Background',
              'CORE',
              `[Database integrity check] Conversations: total=${convReport.total}, unique=${convReport.unique}, duplicates=${convReport.duplicates} | Interactions: total=${interactionReport.total}, uniqueFingerprints=${interactionReport.uniqueFingerprints}, duplicateFingerprints=${interactionReport.duplicateFingerprints}`
            )

            sendResponse(createSuccessResponse(reportData))
          } catch (err) {
            logger.error(
              'Background',
              'CORE',
              `Failed to generate integrity report: ${err instanceof Error ? err.message : String(err)}`
            )
            sendResponse(
              createErrorResponse(
                err instanceof Error ? err.message : 'Failed to generate integrity report'
              )
            )
          }
        })()
        return true
      }

      default: {
        logger.warn(
          'Background',
          'CORE',
          `Unhandled message type received: ${(message as BaseMessage).type}`
        )
        sendResponse(createErrorResponse('Unhandled message type'))
        return false
      }
    }
  }
)
