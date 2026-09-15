/**
 * IntelliCache Collector - Manifest V3 Background Service Worker
 *
 * NOTE: Manifest V3 service workers are ephemeral. Persistent application state
 * is kept exclusively in IndexedDB (via Dexie.js repositories).
 */

import { ConversationRepository } from '../database/repositories/conversation-repository'
import { InteractionRepository } from '../database/repositories/interaction-repository'
import { CURRENT_COLLECTOR_VERSION, CURRENT_DB_VERSION, DB_NAME } from '../database/schema'
import { DatabaseOperationError, DuplicateInteractionError } from '../database/types'
import { logger, redactUrlForLog, toDiagnosticPlatform } from '../diagnostics'
import {
  addRuntimeMessageListener,
  onRuntimeInstalled,
  type WebExtensionSender,
} from '../shared/browser'
import {
  createErrorResponse,
  createSuccessResponse,
  detectPlatformFromUrl,
  isExtensionMessage,
  isSenderAllowedForWrite,
  validateContentScriptInitPayload,
  validateDbGetInteractionPayload,
  validateDbSaveInteractionPayload,
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
const workerStartTime = Date.now()

/** Minimum interval between full integrity scans (full-table scan DoS mitigation). */
const INTEGRITY_REPORT_MIN_INTERVAL_MS = 5000
let lastIntegrityReportAt = 0

/** Recent-interaction window returned with stats (bounded for channel cost). */
const STATS_RECENT_LIMIT = 20

/**
 * Truncates an error message for the response channel so database internals
 * (which may embed content snippets) stay out of logs and popups.
 */
function toResponseError(err: unknown, fallback: string): string {
  const raw = err instanceof Error ? err.message : String(err ?? fallback)
  const text = raw || fallback
  return text.length > 500 ? `${text.slice(0, 500)}…` : text
}

// Initialize repositories (singleton database)
const interactionRepo = new InteractionRepository()
const conversationRepo = new ConversationRepository()

logger.info(
  'Background',
  'CORE',
  `Service worker active. DB: '${DB_NAME}' (v${CURRENT_DB_VERSION}). Started at: ${new Date(workerStartTime).toISOString()}`
)

// Lifecycle: Extension installed or updated
onRuntimeInstalled((details) => {
  logger.info('Background', 'CORE', `Extension installed/updated. Reason: ${details.reason}`)
})

// Central Message Dispatcher
addRuntimeMessageListener(
  (
    rawMessage: unknown,
    sender: WebExtensionSender,
    sendResponse: (response: ExtensionResponse) => void
  ): boolean => {
    if (!isExtensionMessage(rawMessage)) {
      logger.warn('Background', 'CORE', 'Received malformed extension message (invalid format).')
      sendResponse(createErrorResponse('Invalid extension message format', 'INVALID_FORMAT'))
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
          version: CURRENT_COLLECTOR_VERSION,
          serviceWorkerStatus: 'active',
          manifestVersion: 3,
          uptimeMs: Date.now() - workerStartTime,
        }
        logger.debug('Background', 'CORE', 'Handled GET_STATUS request')
        sendResponse(createSuccessResponse(statusData))
        return false
      }

      case 'CONTENT_SCRIPT_INITIALIZED': {
        const initCheck = validateContentScriptInitPayload(message.payload)
        if (!initCheck.ok) {
          logger.warn(
            'Background',
            'CORE',
            `Content script init rejected: ${initCheck.reason ?? 'invalid payload'}`
          )
          sendResponse(createErrorResponse('Invalid init payload', 'VALIDATION_ERROR'))
          return false
        }
        const platformTag = toDiagnosticPlatform(detectPlatformFromUrl(message.payload.url))
        // Log redacted URL + title length only: raw URLs may carry tokens and
        // titles may embed conversation content.
        const titlePreview =
          typeof message.payload.title === 'string' && message.payload.title.length > 80
            ? `${message.payload.title.slice(0, 80)}…`
            : (message.payload.title ?? '')
        logger.info(
          'Background',
          platformTag,
          `Content script initialized on: ${redactUrlForLog(message.payload.url)} (title ${message.payload.title?.length ?? 0} chars: "${titlePreview}")`
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
            const [
              interactionCount,
              conversationCount,
              chatgptCount,
              claudeCount,
              geminiCount,
              recentInteractions,
            ] = await Promise.all([
              interactionRepo.count(),
              conversationRepo.count(),
              interactionRepo.countByPlatform('chatgpt'),
              interactionRepo.countByPlatform('claude'),
              interactionRepo.countByPlatform('gemini'),
              interactionRepo.getRecent(STATS_RECENT_LIMIT),
            ])
            const statsData: DbStatsResponseData = {
              dbName: DB_NAME,
              dbVersion: CURRENT_DB_VERSION,
              interactionCount,
              conversationCount,
              platformCounts: {
                chatgpt: chatgptCount,
                claude: claudeCount,
                gemini: geminiCount,
              },
              recentInteractions,
            }
            logger.debug(
              'Background',
              'CORE',
              `Retrieved DB stats: ${interactionCount} interactions (${chatgptCount} ChatGPT, ${claudeCount} Claude, ${geminiCount} Gemini), ${conversationCount} conversations`
            )
            sendResponse(createSuccessResponse(statsData))
          } catch (err) {
            logger.error('Background', 'CORE', 'Failed to retrieve database stats.')
            sendResponse(
              createErrorResponse(
                toResponseError(err, 'Failed to retrieve database stats'),
                'DB_ERROR'
              )
            )
          }
        })()
        return true
      }

      case 'DB_SAVE_INTERACTION': {
        const saveCheck = validateDbSaveInteractionPayload(message.payload)
        if (!saveCheck.ok) {
          logger.warn(
            'Background',
            'CORE',
            `DB_SAVE_INTERACTION rejected: ${saveCheck.reason ?? 'invalid payload'}`
          )
          sendResponse(
            createErrorResponse(
              `Invalid interaction payload: ${saveCheck.reason ?? 'invalid payload'}`,
              'VALIDATION_ERROR'
            )
          )
          return false
        }

        if (!isSenderAllowedForWrite(sender, (message.payload as { platform?: string }).platform)) {
          logger.warn(
            'Background',
            'CORE',
            'DB_SAVE_INTERACTION rejected: sender tab platform conflicts with payload platform'
          )
          sendResponse(
            createErrorResponse(
              'Sender is not allowed to save for this platform',
              'UNTRUSTED_SENDER'
            )
          )
          return false
        }

        const platformTag = toDiagnosticPlatform(message.payload.platform)
        const queryLen = message.payload.query?.text?.length ?? 0
        const respLen = message.payload.response?.text?.length ?? 0
        logger.info(
          'Background',
          platformTag,
          `Received DB_SAVE_INTERACTION (conversationId: ${message.payload.conversation_id ?? 'null'}, captureContext: ${message.payload.capture_context ?? 'on_generate'}, queryChars: ${queryLen}, responseChars: ${respLen})`
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
              `Interaction persisted successfully (ID: ${created.id}, fingerprint: ${typeof created.fingerprint === 'string' ? created.fingerprint.slice(0, 16) : 'n/a'}..., strategy: ${created.fingerprint_strategy})`
            )
            sendResponse(createSuccessResponse(created))
          } catch (err) {
            if (err instanceof DuplicateInteractionError) {
              logger.info(
                'Background',
                platformTag,
                `Duplicate interaction detected: ${err.fingerprint.slice(0, 16)}...`
              )
              sendResponse(
                createErrorResponse(
                  toResponseError(err, 'Duplicate interaction'),
                  'DUPLICATE_INTERACTION'
                )
              )
            } else if (err instanceof DatabaseOperationError) {
              logger.error('Background', platformTag, 'Structured database operation error.')
              sendResponse(
                createErrorResponse(toResponseError(err, 'Database operation failed'), 'DB_ERROR')
              )
            } else {
              logger.error('Background', platformTag, 'Unexpected error saving interaction.')
              sendResponse(
                createErrorResponse(toResponseError(err, 'Failed to save interaction'), 'DB_ERROR')
              )
            }
          }
        })()
        return true
      }

      case 'DB_GET_INTERACTION': {
        const getCheck = validateDbGetInteractionPayload(message.payload)
        if (!getCheck.ok) {
          logger.warn('Background', 'CORE', `DB_GET_INTERACTION rejected: ${getCheck.reason}`)
          sendResponse(createErrorResponse('Missing or invalid interaction ID', 'VALIDATION_ERROR'))
          return false
        }

        // Asynchronous database query: return true
        void (async () => {
          try {
            const interaction = await interactionRepo.getById(message.payload.id)
            sendResponse(createSuccessResponse(interaction))
          } catch (err) {
            logger.error('Background', 'CORE', 'Failed to retrieve interaction.')
            sendResponse(
              createErrorResponse(
                toResponseError(err, 'Failed to retrieve interaction'),
                'DB_ERROR'
              )
            )
          }
        })()
        return true
      }

      case 'DB_GET_INTEGRITY_REPORT': {
        // Full-table scan: rate-limit to prevent accidental or malicious DoS.
        const now = Date.now()
        if (now - lastIntegrityReportAt < INTEGRITY_REPORT_MIN_INTERVAL_MS) {
          logger.warn('Background', 'CORE', 'Integrity report rate-limited.')
          sendResponse(
            createErrorResponse('Integrity report rate-limited, try again shortly', 'RATE_LIMITED')
          )
          return false
        }
        lastIntegrityReportAt = now
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
            logger.error('Background', 'CORE', 'Failed to generate integrity report.')
            sendResponse(
              createErrorResponse(
                toResponseError(err, 'Failed to generate integrity report'),
                'DB_ERROR'
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
        sendResponse(createErrorResponse('Unhandled message type', 'INVALID_FORMAT'))
        return false
      }
    }
  }
)
