/**
 * Storage Metrics for IntelliCache (measurement only).
 *
 * Estimates the storage footprint of the collected conversation/interaction
 * dataset:
 * - `logicalDatasetBytes`: UTF-8 byte estimate of the persisted records
 *   themselves (NOT exact physical disk usage — IndexedDB overhead, indexes,
 *   and WAL pages are not counted).
 * - `browserUsageBytes` / `browserQuotaBytes`: estimates from
 *   `navigator.storage.estimate()` for this extension origin (NOT exact
 *   filesystem usage).
 *
 * No filesystem or browser-profile access is performed here, and no caching,
 * filtering, or eviction logic lives in this module.
 *
 * Performance note: `collect()` reads the stored records to measure them.
 * This is intentional for Phase 1 and runs only on dashboard open / explicit
 * refresh (never in content-script hot paths). If datasets grow very large,
 * replace the full scan with maintained aggregate counters without changing
 * the returned `StorageMetrics` shape.
 */

import { utf8ByteLength } from '../shared/storage-format'
import { getDatabase, type IntelliCacheDB } from './db'
import { ConversationRepository } from './repositories/conversation-repository'
import { InteractionRepository } from './repositories/interaction-repository'
import type { Conversation, Interaction } from './types'

/**
 * Point-in-time storage footprint of the IntelliCache dataset.
 * Raw byte values are kept unrounded for calculations; format for display
 * with `formatBytes` from shared/storage-format.
 */
export interface StorageMetrics {
  /** Estimated UTF-8 bytes across all persisted interaction + conversation records. */
  logicalDatasetBytes: number
  /** Browser-reported storage usage for this extension origin, if supported. */
  browserUsageBytes: number | null
  /** Browser-reported storage quota for this extension origin, if supported. */
  browserQuotaBytes: number | null
  /** Total persisted interaction records. */
  interactionCount: number
  /** Total persisted conversation records. */
  conversationCount: number
  /** logicalDatasetBytes / interactionCount (0 when no interactions exist). */
  averageInteractionBytes: number
  /** UTF-8 bytes of stored query texts (subset of logicalDatasetBytes). */
  queryBytes: number
  /** UTF-8 bytes of stored response texts (subset of logicalDatasetBytes). */
  responseBytes: number
  /** UTF-8 bytes of all other persisted record fields. */
  metadataBytes: number
  /** ISO-8601 timestamp of when the measurement was taken. */
  measuredAt: string
}

/**
 * Per-interaction logical size split into query / response / metadata parts.
 */
export interface InteractionSizeBreakdown {
  totalBytes: number
  queryBytes: number
  responseBytes: number
  metadataBytes: number
}

/**
 * Estimates the logical UTF-8 size of one persisted interaction.
 * Covers every string field that exists on the v1 Interaction schema:
 * query/response texts plus identifiers, timestamps, fingerprint, model, and
 * title metadata. Numeric metric fields (characters/bytes/estimated_tokens)
 * are derived values with negligible footprint and are excluded by design.
 */
export function measureInteractionBytes(interaction: Interaction): InteractionSizeBreakdown {
  const queryBytes = utf8ByteLength(interaction.query?.text)
  const responseBytes = utf8ByteLength(interaction.response?.text)
  const metadataBytes =
    utf8ByteLength(interaction.id) +
    utf8ByteLength(interaction.fingerprint) +
    utf8ByteLength(interaction.fingerprint_strategy) +
    utf8ByteLength(interaction.platform) +
    utf8ByteLength(interaction.conversation_id) +
    utf8ByteLength(interaction.message_id) +
    utf8ByteLength(interaction.user_message_id) +
    utf8ByteLength(interaction.observed_at) +
    utf8ByteLength(interaction.source_timestamp) +
    utf8ByteLength(interaction.capture_context) +
    utf8ByteLength(interaction.conversation_title) +
    utf8ByteLength(interaction.model?.provider) +
    utf8ByteLength(interaction.model?.name) +
    utf8ByteLength(interaction.collector_version) +
    utf8ByteLength(String(interaction.schema_version))
  return {
    totalBytes: queryBytes + responseBytes + metadataBytes,
    queryBytes,
    responseBytes,
    metadataBytes,
  }
}

/**
 * Estimates the logical UTF-8 size of one persisted conversation record.
 * Covers every field on the Conversation model: id, platform, title,
 * first_observed_at, last_observed_at.
 */
export function measureConversationBytes(conversation: Conversation): number {
  return (
    utf8ByteLength(conversation.id) +
    utf8ByteLength(conversation.platform) +
    utf8ByteLength(conversation.title) +
    utf8ByteLength(conversation.first_observed_at) +
    utf8ByteLength(conversation.last_observed_at)
  )
}

/** Raw shape returned by `navigator.storage.estimate()` (fields optional per browser). */
export interface StorageEstimate {
  usage?: unknown
  quota?: unknown
}

/**
 * Injectable browser-storage probe. The default calls
 * `navigator.storage.estimate()`; tests and non-DOM contexts inject stubs.
 */
export type StorageEstimateProvider = () => Promise<StorageEstimate | null | undefined>

/**
 * Default provider reading the Storage API estimate for this extension origin.
 * Returns null when the API is missing or throws (unsupported browsers,
 * restricted contexts) — never rejects.
 */
export async function defaultStorageEstimateProvider(): Promise<StorageEstimate | null> {
  try {
    const storage = (globalThis as { navigator?: Navigator }).navigator?.storage
    if (!storage || typeof storage.estimate !== 'function') {
      return null
    }
    return (await storage.estimate()) ?? null
  } catch {
    return null
  }
}

function toFiniteBytes(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null
}

export interface StorageMetricsServiceOptions {
  db?: IntelliCacheDB
  interactionRepository?: InteractionRepository
  conversationRepository?: ConversationRepository
  estimateProvider?: StorageEstimateProvider
}

/**
 * Reads persisted counts, measures logical UTF-8 dataset size, and attaches
 * the browser-reported storage estimate into a single typed result.
 * Read-only: never writes, deletes, or migrates records.
 */
export class StorageMetricsService {
  private readonly interactionRepo: InteractionRepository
  private readonly conversationRepo: ConversationRepository
  private readonly estimateProvider: StorageEstimateProvider

  constructor(options: StorageMetricsServiceOptions = {}) {
    const db = options.db ?? getDatabase()
    this.interactionRepo =
      options.interactionRepository ?? new InteractionRepository(options.db ?? db)
    this.conversationRepo =
      options.conversationRepository ?? new ConversationRepository(options.db ?? db)
    this.estimateProvider = options.estimateProvider ?? defaultStorageEstimateProvider
  }

  async collect(): Promise<StorageMetrics> {
    const [interactions, conversations, estimate] = await Promise.all([
      this.interactionRepo.getAll(),
      this.conversationRepo.getAll(),
      Promise.resolve()
        .then(() => this.estimateProvider())
        .catch(() => null),
    ])

    let queryBytes = 0
    let responseBytes = 0
    let metadataBytes = 0
    for (const interaction of interactions) {
      const breakdown = measureInteractionBytes(interaction)
      queryBytes += breakdown.queryBytes
      responseBytes += breakdown.responseBytes
      metadataBytes += breakdown.metadataBytes
    }
    for (const conversation of conversations) {
      metadataBytes += measureConversationBytes(conversation)
    }

    const logicalDatasetBytes = queryBytes + responseBytes + metadataBytes
    const interactionCount = interactions.length
    const conversationCount = conversations.length

    return {
      logicalDatasetBytes,
      browserUsageBytes: toFiniteBytes(estimate?.usage),
      browserQuotaBytes: toFiniteBytes(estimate?.quota),
      interactionCount,
      conversationCount,
      averageInteractionBytes: interactionCount > 0 ? logicalDatasetBytes / interactionCount : 0,
      queryBytes,
      responseBytes,
      metadataBytes,
      measuredAt: new Date().toISOString(),
    }
  }
}
