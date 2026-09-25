import type { CreateInteractionInput } from '../database/types'
import { sendBrowserRuntimeMessage } from './browser'
import type {
  ContentScriptInitMessage,
  DbGetIntegrityReportMessage,
  DbGetInteractionMessage,
  DbGetStatsMessage,
  DbGetStorageMetricsMessage,
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
 * Creates a structured DB_GET_STORAGE_METRICS message to measure the storage
 * footprint of the collected dataset.
 */
export function createDbGetStorageMetricsMessage(
  sender: MessageSenderType
): DbGetStorageMetricsMessage {
  return {
    type: 'DB_GET_STORAGE_METRICS',
    sender,
    timestamp: Date.now(),
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
 * Prefer passing a machine-readable `code` so callers don't string-match `error` text.
 */
export function createErrorResponse(
  error: string,
  code?: ExtensionResponse['code']
): ExtensionResponse<never> {
  return {
    success: false,
    error,
    ...(code !== undefined ? { code } : {}),
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
  'DB_GET_STORAGE_METRICS',
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
  return (
    typeof candidate.type === 'string' &&
    VALID_MESSAGE_TYPES.has(candidate.type) &&
    typeof candidate.sender === 'string' &&
    VALID_SENDER_TYPES.has(candidate.sender) &&
    typeof candidate.timestamp === 'number'
  )
}

/**
 * Pure helper function to determine AI platform based on current URL.
 * Returns null when the URL does not match any supported AI platform,
 * or when the URL is malformed.
 */
export function detectPlatformFromUrl(url: string): SupportedPlatform | null {
  try {
    const parsed = new URL(url)
    const hostname = parsed.hostname.toLowerCase()

    if (
      hostname === 'chatgpt.com' ||
      hostname.endsWith('.chatgpt.com') ||
      hostname === 'chat.openai.com' ||
      hostname.endsWith('.chat.openai.com')
    ) {
      return 'chatgpt'
    }
    if (hostname === 'claude.ai' || hostname.endsWith('.claude.ai')) {
      return 'claude'
    }
    if (
      hostname === 'gemini.google.com' ||
      hostname.endsWith('.gemini.google.com') ||
      hostname.startsWith('gemini.google.')
    ) {
      return 'gemini'
    }
    return null
  } catch {
    return null
  }
}

/**
 * Dispatches a typed message to the extension runtime (Service Worker / Background).
 * Handles both Promise-based (Firefox) and callback-based (Chromium) runtimes safely,
 * classifying runtime errors and context invalidation.
 */
export const sendExtensionMessage = sendBrowserRuntimeMessage

// ─── Payload validation & sender trust ──────────────────────────────────────
// These guards run in the service worker before any IndexedDB access so that
// malformed or oversized payloads are rejected cheaply (quota-exhaustion DoS
// mitigation). They are pure functions and safe to unit test in isolation.

/** Maximum accepted characters for a single query/response text payload. */
export const MAX_INTERACTION_TEXT_CHARS = 200_000
/** Maximum accepted characters for conversation titles and init handshake fields. */
export const MAX_TITLE_CHARS = 500
/** Platforms accepted for persisted interactions. */
export const PERSISTABLE_PLATFORMS: ReadonlySet<string> = new Set(['chatgpt', 'claude', 'gemini'])

export interface PayloadValidationResult {
  ok: boolean
  reason?: string
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null
}

function isValidTextField(value: unknown, field: string): PayloadValidationResult {
  if (typeof value !== 'string') {
    return { ok: false, reason: `${field} must be a string` }
  }
  if (value.length > MAX_INTERACTION_TEXT_CHARS) {
    return {
      ok: false,
      reason: `${field} exceeds ${MAX_INTERACTION_TEXT_CHARS} characters (${value.length})`,
    }
  }
  return { ok: true }
}

/**
 * Validates a DB_SAVE_INTERACTION payload without touching IndexedDB.
 * Rejects missing shapes, wrong types, and oversized texts.
 */
export function validateDbSaveInteractionPayload(payload: unknown): PayloadValidationResult {
  if (!isRecord(payload)) {
    return { ok: false, reason: 'payload must be an object' }
  }
  if (typeof payload.platform !== 'string' || payload.platform.trim().length === 0) {
    return { ok: false, reason: 'platform is required and must be a non-empty string' }
  }
  if (!PERSISTABLE_PLATFORMS.has(payload.platform.trim().toLowerCase())) {
    return { ok: false, reason: `unsupported platform '${payload.platform}'` }
  }
  if (!isRecord(payload.query)) {
    return { ok: false, reason: 'query must be an object' }
  }
  const queryCheck = isValidTextField(payload.query.text, 'query.text')
  if (!queryCheck.ok) return queryCheck
  if (!isRecord(payload.response)) {
    return { ok: false, reason: 'response must be an object' }
  }
  const responseCheck = isValidTextField(payload.response.text, 'response.text')
  if (!responseCheck.ok) return responseCheck

  for (const field of ['conversation_id', 'message_id', 'user_message_id', 'unbound_id'] as const) {
    const v = payload[field]
    if (v !== undefined && v !== null && typeof v !== 'string') {
      return { ok: false, reason: `${field} must be a string, null, or omitted` }
    }
    if (typeof v === 'string' && v.length > 200) {
      return { ok: false, reason: `${field} exceeds 200 characters` }
    }
  }
  if (
    payload.capture_context !== undefined &&
    payload.capture_context !== 'on_load' &&
    payload.capture_context !== 'on_generate'
  ) {
    return { ok: false, reason: "capture_context must be 'on_load' or 'on_generate'" }
  }
  if (payload.observed_at !== undefined && typeof payload.observed_at !== 'string') {
    return { ok: false, reason: 'observed_at must be an ISO-8601 string' }
  }
  if (
    payload.conversation_title !== undefined &&
    payload.conversation_title !== null &&
    (typeof payload.conversation_title !== 'string' ||
      payload.conversation_title.length > MAX_TITLE_CHARS)
  ) {
    return {
      ok: false,
      reason: `conversation_title must be a string under ${MAX_TITLE_CHARS} chars`,
    }
  }
  return { ok: true }
}

/**
 * Validates a DB_GET_INTERACTION payload ({ id: string }).
 */
export function validateDbGetInteractionPayload(payload: unknown): PayloadValidationResult {
  if (!isRecord(payload) || typeof payload.id !== 'string' || payload.id.trim().length === 0) {
    return { ok: false, reason: 'id is required and must be a non-empty string' }
  }
  return { ok: true }
}

/**
 * Validates a CONTENT_SCRIPT_INITIALIZED payload ({ url, title }).
 * Applies length caps so PII-heavy URLs/titles can't bloat logs or storage.
 */
export function validateContentScriptInitPayload(payload: unknown): PayloadValidationResult {
  if (!isRecord(payload)) {
    return { ok: false, reason: 'payload must be an object' }
  }
  if (typeof payload.url !== 'string' || payload.url.length === 0) {
    return { ok: false, reason: 'url must be a non-empty string' }
  }
  if (payload.url.length > 2000) {
    return { ok: false, reason: 'url exceeds 2000 characters' }
  }
  if (
    payload.title !== undefined &&
    (typeof payload.title !== 'string' || payload.title.length > MAX_TITLE_CHARS)
  ) {
    return { ok: false, reason: `title must be a string under ${MAX_TITLE_CHARS} chars` }
  }
  return { ok: true }
}

export interface WriteSender {
  tab?: { url?: string }
  url?: string
}

/**
 * Determines whether a message sender is allowed to perform a database write.
 * Permissive by design (never breaks legit flows):
 * - Senders without tab context (popup, tests, service worker) are allowed.
 * - Content-script senders whose tab URL platform conflicts with the payload
 *   platform are rejected (cross-site spoof mitigation).
 */
export function isSenderAllowedForWrite(
  sender: WriteSender | undefined,
  payloadPlatform: string | undefined
): boolean {
  if (!sender) return true
  const tabUrl = sender.tab?.url ?? sender.url
  if (!tabUrl || !payloadPlatform) return true

  // Extension internal contexts (popup, side panel, options) are allowed
  if (
    tabUrl.startsWith('chrome-extension://') ||
    tabUrl.startsWith('moz-extension://') ||
    tabUrl.startsWith('extension://')
  ) {
    return true
  }

  const senderPlatform = detectPlatformFromUrl(tabUrl)
  // FIX-007: If sender is a web tab that is not a recognized AI platform, reject it
  if (senderPlatform === null) return false

  return senderPlatform === payloadPlatform.trim().toLowerCase()
}
