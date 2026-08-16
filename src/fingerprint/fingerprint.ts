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

/**
 * Computes a SHA-256 hexadecimal hash using the Web Crypto API.
 */
export async function sha256(input: string): Promise<string> {
  const data = textEncoder.encode(input)
  const hashBuffer = await crypto.subtle.digest('SHA-256', data)
  const hashArray = Array.from(new Uint8Array(hashBuffer))
  return hashArray.map((b) => b.toString(16).padStart(2, '0')).join('')
}

/**
 * Formats a timestamp into an ISO-8601 hourly bucket string (UTC).
 * E.g., "2026-08-17T03:26:18.123Z" -> "2026-08-17T03"
 */
export function getHourlyBucket(isoString: string): string {
  try {
    const date = new Date(isoString)
    if (isNaN(date.getTime())) {
      return new Date().toISOString().slice(0, 13)
    }
    return date.toISOString().slice(0, 13)
  } catch {
    return new Date().toISOString().slice(0, 13)
  }
}

/**
 * Generates a deterministic SHA-256 fingerprint for an AI query/response interaction.
 */
export async function generateInteractionFingerprint(
  input: FingerprintInput
): Promise<FingerprintResult> {
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
