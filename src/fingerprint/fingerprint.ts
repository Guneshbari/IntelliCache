/**
 * Deterministic Interaction Fingerprint Generator using Web Crypto API (SHA-256).
 *
 * Implements a 3-tier hierarchical fingerprinting strategy:
 * - Level 1: Platform + Conversation ID + Message ID (Direct platform identifiers)
 * - Level 2: Platform + Conversation ID + Normalized Query + Normalized Response (Conversation content fallback)
 * - Level 3: Platform + Normalized Query + Normalized Response + Observed Hour Bucket (Stateless fallback)
 *
 * Note: Level 3 acts as a deterministic deduplication aid for ephemeral or standalone queries.
 */

import { normalizeTextForFingerprint } from './normalize'

export type FingerprintStrategy = 'level_1' | 'level_2' | 'level_3'

export interface FingerprintInput {
  platform: string
  conversation_id?: string | null
  message_id?: string | null
  query_text: string
  response_text: string
  observed_at?: string // ISO-8601 string
}

export interface FingerprintResult {
  fingerprint: string
  strategy: FingerprintStrategy
  canonicalPayload: string
}

const textEncoder = new TextEncoder()

const byteToHex: string[] = []
for (let i = 0; i < 256; i++) {
  byteToHex.push(i.toString(16).padStart(2, '0'))
}

/**
 * Computes a SHA-256 hexadecimal hash using the Web Crypto API.
 * Requires a secure context (https:// or extension pages); throws a descriptive
 * error otherwise instead of surfacing a bare ReferenceError.
 */
export async function sha256(input: string): Promise<string> {
  const subtle = globalThis.crypto?.subtle
  if (!subtle) {
    throw new Error(
      'Web Crypto API (crypto.subtle) is unavailable in this context; SHA-256 fingerprinting requires a secure context.'
    )
  }
  const data = textEncoder.encode(input)
  const hashBuffer = await subtle.digest('SHA-256', data)
  const bytes = new Uint8Array(hashBuffer)
  let hex = ''
  for (let i = 0; i < bytes.length; i++) {
    hex += byteToHex[bytes[i]]
  }
  return hex
}

/**
 * Sentinel bucket for unparseable timestamps. Using a fixed sentinel (instead of
 * the current hour) keeps invalid-timestamp records deterministic and prevents
 * them from colliding with valid same-hour records.
 */
export const INVALID_TIMESTAMP_BUCKET = 'invalid-timestamp'

/**
 * Formats a timestamp into an ISO-8601 hourly bucket string (UTC).
 * E.g., "2026-08-17T03:26:18.123Z" -> "2026-08-17T03"
 * Returns INVALID_TIMESTAMP_BUCKET for unparseable input.
 */
export function getHourlyBucket(isoString: string): string {
  const date = new Date(isoString)
  if (Number.isNaN(date.getTime())) {
    return INVALID_TIMESTAMP_BUCKET
  }
  return date.toISOString().slice(0, 13)
}

/**
 * Generates a deterministic SHA-256 fingerprint for an AI query/response interaction.
 *
 * Canonical payload formats are version-1 stable (`L1|…`, `L2|…`, `L3|…`) so existing
 * IndexedDB records keep deduplicating after upgrades. Do not change field order,
 * delimiters, or level semantics without a schema/fingerprint version bump.
 *
 * Security note: fingerprints are unsalted deterministic hashes (brute-forceable for
 * short predictable prompts). Safe while storage stays local-only; add a per-install
 * salt before any sync/export feature.
 */
export async function generateInteractionFingerprint(
  input: FingerprintInput
): Promise<FingerprintResult> {
  if (!input || typeof input !== 'object') {
    throw new Error('generateInteractionFingerprint: input must be an object')
  }
  if (typeof input.platform !== 'string' || input.platform.trim().length === 0) {
    throw new Error('generateInteractionFingerprint: platform must be a non-empty string')
  }
  const platform = input.platform.trim().toLowerCase()
  const conversationId = input.conversation_id?.trim() || null
  const messageId = input.message_id?.trim() || null

  // Level 1 Strategy: Platform + Conversation ID + Message ID
  if (conversationId && messageId) {
    const canonicalPayload = `L1|${platform}|${conversationId}|${messageId}`
    const fingerprint = await sha256(canonicalPayload)
    return {
      fingerprint,
      strategy: 'level_1',
      canonicalPayload,
    }
  }

  const normalizedQuery = normalizeTextForFingerprint(input.query_text)
  const normalizedResponse = normalizeTextForFingerprint(input.response_text)

  // Level 2 Strategy: Platform + Conversation ID + Query + Response
  if (conversationId) {
    const canonicalPayload = `L2|${platform}|${conversationId}|${normalizedQuery}|${normalizedResponse}`
    const fingerprint = await sha256(canonicalPayload)
    return {
      fingerprint,
      strategy: 'level_2',
      canonicalPayload,
    }
  }

  // Level 3 Strategy: Platform + Query + Response + Hourly Bucket
  const hourBucket = getHourlyBucket(input.observed_at ?? new Date().toISOString())
  const canonicalPayload = `L3|${platform}|${normalizedQuery}|${normalizedResponse}|${hourBucket}`
  const fingerprint = await sha256(canonicalPayload)

  return {
    fingerprint,
    strategy: 'level_3',
    canonicalPayload,
  }
}
