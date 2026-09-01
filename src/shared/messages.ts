import type { CreateInteractionInput } from '../database/types'
import type {
  ContentScriptInitMessage,
  DbGetIntegrityReportMessage,
  DbGetInteractionMessage,
  DbGetStatsMessage,
  DbSaveInteractionMessage,
  ExtensionMessage,
  ExtensionResponse,
  GetStatusMessage,
  MessageSenderType,
  PingMessage,
  SupportedPlatform,
} from './types'

/**
 * Creates a structured PING message.
 */
export function createPingMessage(sender: MessageSenderType, text?: string): PingMessage {
  return {
    type: 'PING',
    sender,
    timestamp: Date.now(),
    payload: text ? { text } : undefined,
  }
}

/**
 * Creates a structured GET_STATUS message.
 */
export function createGetStatusMessage(sender: MessageSenderType): GetStatusMessage {
  return {
    type: 'GET_STATUS',
    sender,
    timestamp: Date.now(),
  }
}

/**
 * Creates a message broadcasted when a content script initializes.
 */
export function createContentScriptInitMessage(
  url: string,
  title: string
): ContentScriptInitMessage {
  return {
    type: 'CONTENT_SCRIPT_INITIALIZED',
    sender: 'content-script',
    timestamp: Date.now(),
    payload: {
      url,
      title,
    },
  }
}

/**
 * Creates a structured DB_GET_STATS message to query record counts and version.
 */
export function createDbGetStatsMessage(sender: MessageSenderType): DbGetStatsMessage {
  return {
    type: 'DB_GET_STATS',
    sender,
    timestamp: Date.now(),
  }
}

/**
 * Creates a structured DB_SAVE_INTERACTION message.
 */
export function createDbSaveInteractionMessage(
  sender: MessageSenderType,
  payload: CreateInteractionInput
): DbSaveInteractionMessage {
  return {
    type: 'DB_SAVE_INTERACTION',
    sender,
    timestamp: Date.now(),
    payload,
  }
}

/**
 * Creates a structured DB_GET_INTERACTION message.
 */
export function createDbGetInteractionMessage(
  sender: MessageSenderType,
  id: string
): DbGetInteractionMessage {
  return {
    type: 'DB_GET_INTERACTION',
    sender,
    timestamp: Date.now(),
    payload: { id },
  }
}

/**
 * Creates a development-only DB_GET_INTEGRITY_REPORT message.
 * Triggers a full database integrity scan in the service worker.
 */
export function createDbGetIntegrityReportMessage(
  sender: MessageSenderType
): DbGetIntegrityReportMessage {
  return {
    type: 'DB_GET_INTEGRITY_REPORT',
    sender,
    timestamp: Date.now(),
  }
}

/**
 * Wraps a successful response payload in the standard response envelope.
 */
export function createSuccessResponse<T>(data: T): ExtensionResponse<T> {
  return {
    success: true,
    data,
    timestamp: Date.now(),
  }
}

/**
 * Wraps an error string in the standard response envelope.
 */
export function createErrorResponse(error: string): ExtensionResponse<never> {
  return {
    success: false,
    error,
    timestamp: Date.now(),
  }
}

const VALID_MESSAGE_TYPES = new Set([
  'PING',
  'GET_STATUS',
  'CONTENT_SCRIPT_INITIALIZED',
  'DB_GET_STATS',
  'DB_SAVE_INTERACTION',
  'DB_GET_INTERACTION',
  'DB_GET_INTEGRITY_REPORT',
])

const VALID_SENDER_TYPES = new Set(['popup', 'content-script', 'service-worker'])

/**
 * Type guard to check if an arbitrary object is a valid ExtensionMessage.
 */
export function isExtensionMessage(value: unknown): value is ExtensionMessage {
  if (typeof value !== 'object' || value === null) {
    return false
  }

  const candidate = value as Record<string, unknown>
  const hasValidType = typeof candidate.type === 'string' && VALID_MESSAGE_TYPES.has(candidate.type)
  const hasValidSender =
    typeof candidate.sender === 'string' && VALID_SENDER_TYPES.has(candidate.sender)
  const hasValidTimestamp = typeof candidate.timestamp === 'number'

  return hasValidType && hasValidSender && hasValidTimestamp
}

/**
 * Pure helper function to determine AI platform based on current URL.
 * Ready for future platform-specific adapters in Step 3.
 */
export function detectPlatformFromUrl(url: string): SupportedPlatform {
  try {
    const parsed = new URL(url)
    const hostname = parsed.hostname.toLowerCase()

    if (
      hostname === 'chatgpt.com' ||
      hostname.endsWith('.chatgpt.com') ||
      hostname === 'chat.openai.com'
    ) {
      return 'chatgpt'
    }
    if (hostname === 'claude.ai' || hostname.endsWith('.claude.ai')) {
      return 'claude'
    }
    if (hostname === 'gemini.google.com') {
      return 'gemini'
    }
    return 'unknown'
  } catch {
    return 'unknown'
  }
}

import { sendBrowserRuntimeMessage } from './browser'

/**
 * Dispatches a typed message to the extension runtime (Service Worker / Background).
 * Handles both Promise-based (Firefox) and callback-based (Chromium) runtimes safely,
 * classifying runtime errors and context invalidation.
 */
export async function sendExtensionMessage<M extends ExtensionMessage, R = unknown>(
  message: M
): Promise<ExtensionResponse<R>> {
  return sendBrowserRuntimeMessage<M, R>(message)
}
