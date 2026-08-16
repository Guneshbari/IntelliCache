/**
 * Data contracts and schema interfaces for the IntelliCache Local Data Layer.
 * Defines Version-1 Interaction and Conversation domain models.
 */

import type { FingerprintStrategy } from '../fingerprint/fingerprint'

export type { FingerprintStrategy }

/**
 * Model provider and architecture metadata.
 */
export interface InteractionModel {
  provider: string | null
  name: string | null
}

/**
 * Text payload metrics (character count, UTF-8 byte count, estimated tokens).
 */
export interface InteractionTextMetrics {
  text: string
  characters: number
  bytes: number
  estimated_tokens: number | null
}

/**
 * Primary research unit: a single query-response interaction with an AI platform.
 * Version 1 Schema.
 */
export interface Interaction {
  schema_version: 1
  id: string
  fingerprint: string
  fingerprint_strategy: FingerprintStrategy
  platform: string
  conversation_id: string | null
  message_id: string | null
  observed_at: string // ISO-8601 string
  model: InteractionModel
  query: InteractionTextMetrics
  response: InteractionTextMetrics
  conversation_title: string | null
  collector_version: string
}

/**
 * Generates a deterministic namespaced conversation identifier (`${platform}:${raw_conversation_id}`).
 * Returns null if the provided conversationId is null, undefined, or empty.
 */
export function namespaceConversationId(
  platform: string,
  conversationId?: string | null
): string | null {
  if (!conversationId) {
    return null
  }
  const normPlatform = platform.trim().toLowerCase()
  const trimmedId = conversationId.trim()
  if (!trimmedId) {
    return null
  }
  const prefix = `${normPlatform}:`
  if (trimmedId.toLowerCase().startsWith(prefix)) {
    return `${normPlatform}:${trimmedId.slice(prefix.length)}`
  }
  return `${prefix}${trimmedId}`
}

/**
 * Input payload for creating a new Interaction record.
 * Metrics and fingerprint will be computed automatically if not pre-populated.
 */
export interface CreateInteractionInput {
  id?: string
  fingerprint?: string
  fingerprint_strategy?: FingerprintStrategy
  platform: string
  conversation_id?: string | null
  message_id?: string | null
  observed_at?: string // ISO-8601 string; defaults to Date.now()
  model?: {
    provider?: string | null
    name?: string | null
  }
  query: {
    text: string
    estimated_tokens?: number | null
  }
  response: {
    text: string
    estimated_tokens?: number | null
  }
  conversation_title?: string | null
  collector_version?: string
}

/**
 * Lightweight conversation metadata.
 */
export interface Conversation {
  id: string
  platform: string
  title: string | null
  first_observed_at: string // ISO-8601 string
  last_observed_at: string // ISO-8601 string
}

/**
 * Input payload for creating or updating a Conversation record.
 */
export interface CreateConversationInput {
  id: string
  platform: string
  title?: string | null
  observed_at?: string // ISO-8601 string; defaults to current time
}

/**
 * Custom domain error thrown when attempting to insert an interaction
 * with an existing fingerprint.
 */
export class DuplicateInteractionError extends Error {
  public readonly fingerprint: string

  constructor(fingerprint: string, message?: string) {
    super(message ?? `Interaction with fingerprint '${fingerprint}' already exists in database.`)
    this.name = 'DuplicateInteractionError'
    this.fingerprint = fingerprint
  }
}

/**
 * Custom domain error for unexpected database execution failures.
 */
export class DatabaseOperationError extends Error {
  public readonly originalError?: unknown

  constructor(operation: string, originalError?: unknown) {
    const detail = originalError instanceof Error ? originalError.message : String(originalError)
    super(`Database operation '${operation}' failed: ${detail}`)
    this.name = 'DatabaseOperationError'
    this.originalError = originalError
  }
}
