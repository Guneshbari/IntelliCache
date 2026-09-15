/**
 * Interaction Repository
 * Encapsulates all data access and persistence logic for AI query/response interactions.
 */

import Dexie from 'dexie'
import { logger, toDiagnosticPlatform } from '../../diagnostics'
import { generateInteractionFingerprint } from '../../fingerprint/fingerprint'
import { MAX_INTERACTION_TEXT_CHARS } from '../../shared/messages'
import { getDatabase, type IntelliCacheDB } from '../db'
import { calculateTextMetrics } from '../metrics'
import { CURRENT_COLLECTOR_VERSION } from '../schema'
import {
  DatabaseOperationError,
  DuplicateInteractionError,
  namespaceConversationId,
  type CreateInteractionInput,
  type FingerprintStrategy,
  type Interaction,
} from '../types'

const FINGERPRINT_HEX_RE = /^[0-9a-f]{64}$/i
const VALID_STRATEGIES: ReadonlySet<string> = new Set(['level_1', 'level_2', 'level_3'])

function isValidPrecomputedFingerprint(value: unknown): value is string {
  return typeof value === 'string' && FINGERPRINT_HEX_RE.test(value)
}

export class InteractionRepository {
  private db: IntelliCacheDB

  constructor(db?: IntelliCacheDB) {
    this.db = db ?? getDatabase()
  }

  /**
   * Persists a new interaction into IndexedDB.
   * Computes metrics and deterministic fingerprint automatically if not provided.
   * Throws DuplicateInteractionError if an interaction with the same fingerprint exists.
   */
  async create(input: CreateInteractionInput | Interaction): Promise<Interaction> {
    if (!input || typeof input !== 'object') {
      throw new DatabaseOperationError(
        'create interaction',
        new Error('Invalid interaction input: payload must be an object')
      )
    }
    if (!input.platform || typeof input.platform !== 'string') {
      throw new DatabaseOperationError(
        'create interaction',
        new Error('Invalid interaction input: platform is required and must be a string')
      )
    }
    if (!input.query || typeof input.query.text !== 'string') {
      throw new DatabaseOperationError(
        'create interaction',
        new Error('Invalid interaction input: query.text is required and must be a string')
      )
    }
    if (!input.response || typeof input.response.text !== 'string') {
      throw new DatabaseOperationError(
        'create interaction',
        new Error('Invalid interaction input: response.text is required and must be a string')
      )
    }
    if (input.query.text.length > MAX_INTERACTION_TEXT_CHARS) {
      throw new DatabaseOperationError(
        'create interaction',
        new Error(
          `Invalid interaction input: query.text exceeds ${MAX_INTERACTION_TEXT_CHARS} characters`
        )
      )
    }
    if (input.response.text.length > MAX_INTERACTION_TEXT_CHARS) {
      throw new DatabaseOperationError(
        'create interaction',
        new Error(
          `Invalid interaction input: response.text exceeds ${MAX_INTERACTION_TEXT_CHARS} characters`
        )
      )
    }

    const platformTag = toDiagnosticPlatform(input.platform)
    const traceId =
      'trace_id' in input && input.trace_id
        ? input.trace_id
        : `trace_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
    try {
      const observedAt =
        'observed_at' in input && input.observed_at ? input.observed_at : new Date().toISOString()
      const id =
        'id' in input && input.id
          ? input.id
          : typeof globalThis.crypto?.randomUUID === 'function'
            ? globalThis.crypto.randomUUID()
            : `int_${Date.now()}_${Math.random().toString(36).slice(2, 11)}`
      const platform = input.platform.trim().toLowerCase()
      const namespacedConvId = namespaceConversationId(platform, input.conversation_id)

      // Calculate fingerprint and strategy.
      // Pre-computed fingerprints are accepted only when well-formed (64 hex chars
      // with a known strategy); anything else is recomputed so a compromised or
      // buggy caller cannot force false-dedup or bypass dedup.
      let fingerprint: string
      let fingerprintStrategy: FingerprintStrategy
      if (
        'fingerprint' in input &&
        isValidPrecomputedFingerprint(input.fingerprint) &&
        (input.fingerprint_strategy === undefined ||
          VALID_STRATEGIES.has(input.fingerprint_strategy))
      ) {
        fingerprint = input.fingerprint.toLowerCase()
        fingerprintStrategy = (input.fingerprint_strategy ?? 'level_1') as FingerprintStrategy
        logger.debug(
          'Database',
          platformTag,
          `Using pre-computed fingerprint: ${fingerprint.slice(0, 16)}... (strategy: ${fingerprintStrategy})`
        )
      } else {
        logger.debug(
          'Database',
          platformTag,
          `Starting fingerprint generation (convId: ${input.conversation_id ?? 'null'}, messageId: ${input.message_id ?? 'null'})`
        )
        const fpResult = await generateInteractionFingerprint({
          platform,
          conversation_id: input.conversation_id,
          message_id: input.message_id,
          query_text: input.query.text,
          response_text: input.response.text,
          observed_at: observedAt,
        })
        fingerprint = fpResult.fingerprint
        fingerprintStrategy = fpResult.strategy
        logger.info(
          'Lifecycle',
          platformTag,
          `fingerprint trace=${traceId} (strategy=${fingerprintStrategy}, fp=${fingerprint.slice(0, 16)}...)`
        )
      }

      // Check for duplicate fingerprint before insertion
      logger.info(
        'Database',
        platformTag,
        `fingerprint-check trace=${traceId} (fp=${fingerprint.slice(0, 16)}...)`
      )
      const existing = await this.db.interactions.where('fingerprint').equals(fingerprint).first()
      if (existing) {
        // If existing record was unbound and new input provides conversation_id, bind it
        if (existing.conversation_id === null && namespacedConvId !== null) {
          existing.conversation_id = namespacedConvId
          if (input.conversation_title) existing.conversation_title = input.conversation_title
          if (input.message_id) existing.message_id = input.message_id.trim()
          if (input.user_message_id) existing.user_message_id = input.user_message_id.trim()
          await this.db.interactions.put(existing)
          logger.info(
            'Database',
            platformTag,
            `updated trace=${traceId} (bound existing ID: ${existing.id} -> ${namespacedConvId})`
          )
          logger.info(
            'Lifecycle',
            platformTag,
            `conversation-bound trace=${traceId} (interaction ID: ${existing.id} -> ${namespacedConvId})`
          )
          return existing
        }

        logger.info(
          'Database',
          platformTag,
          `duplicate trace=${traceId} (existing ID: ${existing.id}, fp=${fingerprint.slice(0, 16)}...)`
        )
        throw new DuplicateInteractionError(
          fingerprint,
          `Interaction with fingerprint '${fingerprint}' already exists (ID: ${existing.id}).`
        )
      }

      // Check if an unbound fallback (Level 3) interaction exists for the same content
      if (namespacedConvId !== null) {
        const l3FpResult = await generateInteractionFingerprint({
          platform,
          conversation_id: null,
          message_id: null,
          query_text: input.query.text,
          response_text: input.response.text,
          observed_at: observedAt,
        })
        const existingUnbound = await this.db.interactions
          .where('fingerprint')
          .equals(l3FpResult.fingerprint)
          .first()
        if (existingUnbound) {
          if (existingUnbound.conversation_id === null) {
            existingUnbound.conversation_id = namespacedConvId
            if (input.conversation_title)
              existingUnbound.conversation_title = input.conversation_title
            if (input.message_id) existingUnbound.message_id = input.message_id.trim()
            if (input.user_message_id)
              existingUnbound.user_message_id = input.user_message_id.trim()
            await this.db.interactions.put(existingUnbound)
            logger.info(
              'Database',
              platformTag,
              `updated trace=${traceId} (bound existing unbound ID: ${existingUnbound.id} -> ${namespacedConvId})`
            )
            logger.info(
              'Lifecycle',
              platformTag,
              `conversation-bound trace=${traceId} (interaction ID: ${existingUnbound.id} -> ${namespacedConvId})`
            )
            return existingUnbound
          } else if (existingUnbound.conversation_id === namespacedConvId) {
            logger.info(
              'Database',
              platformTag,
              `duplicate trace=${traceId} (existing ID: ${existingUnbound.id}, fp=${existingUnbound.fingerprint.slice(0, 16)}...)`
            )
            throw new DuplicateInteractionError(
              fingerprint,
              `Interaction with fingerprint '${fingerprint}' already exists (ID: ${existingUnbound.id}).`
            )
          }
        }
      }

      // Construct full canonical interaction entity.
      // Metrics are always recomputed from raw text so callers cannot inject
      // inconsistent characters/bytes; only estimated_tokens is inherited.
      const interaction: Interaction = {
        schema_version: 1,
        id,
        fingerprint,
        fingerprint_strategy: fingerprintStrategy,
        platform,
        conversation_id: namespacedConvId,
        message_id: input.message_id?.trim() ?? null,
        user_message_id: input.user_message_id?.trim() ?? null,
        observed_at: observedAt,
        source_timestamp: input.source_timestamp?.trim() ?? null,
        capture_context: input.capture_context ?? 'on_generate',
        model: {
          provider: input.model?.provider ?? null,
          name: input.model?.name ?? null,
        },
        query: calculateTextMetrics(input.query.text, input.query.estimated_tokens),
        response: calculateTextMetrics(input.response.text, input.response.estimated_tokens),
        conversation_title: input.conversation_title ?? null,
        collector_version: input.collector_version ?? CURRENT_COLLECTOR_VERSION,
      }

      try {
        await this.db.interactions.add(interaction)
        logger.info(
          'Database',
          platformTag,
          `inserted trace=${traceId} (ID: ${id}, fp=${fingerprint.slice(0, 16)}..., queryChars: ${interaction.query.characters}, responseChars: ${interaction.response.characters})`
        )
      } catch (addError) {
        if (
          (addError &&
            typeof addError === 'object' &&
            'name' in addError &&
            addError.name === 'ConstraintError') ||
          addError instanceof Dexie.ConstraintError
        ) {
          logger.info(
            'Database',
            platformTag,
            `ConstraintError caught during insertion: fingerprint '${fingerprint.slice(0, 16)}...' already exists.`
          )
          throw new DuplicateInteractionError(
            fingerprint,
            `Interaction with fingerprint '${fingerprint}' already exists (constraint violation).`
          )
        }
        throw addError
      }

      return interaction
    } catch (error) {
      if (error instanceof DuplicateInteractionError) {
        throw error
      }
      logger.error(
        'Database',
        platformTag,
        `Failed to persist interaction into IndexedDB: ${error instanceof Error ? error.message : String(error)}`
      )
      throw new DatabaseOperationError('create interaction', error)
    }
  }

  /**
   * Retrieves an interaction by its unique primary ID.
   */
  async getById(id: string): Promise<Interaction | null> {
    try {
      const record = await this.db.interactions.get(id)
      return record ?? null
    } catch (error) {
      throw new DatabaseOperationError(`getById (${id})`, error)
    }
  }

  /**
   * Retrieves an interaction by its unique SHA-256 fingerprint.
   */
  async getByFingerprint(fingerprint: string): Promise<Interaction | null> {
    try {
      const record = await this.db.interactions.where('fingerprint').equals(fingerprint).first()
      return record ?? null
    } catch (error) {
      throw new DatabaseOperationError(`getByFingerprint (${fingerprint})`, error)
    }
  }

  /**
   * Retrieves all interactions belonging to a specific conversation ID.
   * If platform is provided, ensures namespaced conversation ID lookup.
   * Uses the [conversation_id+observed_at] compound index when available
   * (schema v2) with a single-field fallback for v1 databases.
   */
  async getByConversationId(
    conversationId: string,
    platform?: string,
    options?: { limit?: number }
  ): Promise<Interaction[]> {
    try {
      const targetId = platform
        ? (namespaceConversationId(platform, conversationId) ?? conversationId)
        : conversationId
      const limit = options?.limit
      try {
        let query = this.db.interactions
          .where('[conversation_id+observed_at]')
          .between([targetId, Dexie.minKey], [targetId, Dexie.maxKey])
        const rows = await (limit !== undefined ? query.limit(limit).toArray() : query.toArray())
        return rows
      } catch {
        const rows = await this.db.interactions
          .where('conversation_id')
          .equals(targetId)
          .sortBy('observed_at')
        return limit !== undefined ? rows.slice(0, limit) : rows
      }
    } catch (error) {
      throw new DatabaseOperationError(`getByConversationId (${conversationId})`, error)
    }
  }

  /**
   * Retrieves all interactions belonging to a specific platform.
   * Uses the [platform+observed_at] compound index when available (schema v2).
   */
  async getByPlatform(platform: string, options?: { limit?: number }): Promise<Interaction[]> {
    try {
      const normalized = platform.trim().toLowerCase()
      const limit = options?.limit
      try {
        let query = this.db.interactions
          .where('[platform+observed_at]')
          .between([normalized, Dexie.minKey], [normalized, Dexie.maxKey])
        return await (limit !== undefined ? query.limit(limit).toArray() : query.toArray())
      } catch {
        const rows = await this.db.interactions
          .where('platform')
          .equals(normalized)
          .sortBy('observed_at')
        return limit !== undefined ? rows.slice(0, limit) : rows
      }
    } catch (error) {
      throw new DatabaseOperationError(`getByPlatform (${platform})`, error)
    }
  }

  /**
   * Counts the number of interactions belonging to a specific platform.
   */
  async countByPlatform(platform: string): Promise<number> {
    try {
      const normalized = platform.trim().toLowerCase()
      return await this.db.interactions.where('platform').equals(normalized).count()
    } catch (error) {
      throw new DatabaseOperationError(`countByPlatform (${platform})`, error)
    }
  }

  /**
   * Retrieves the most recent interactions ordered by observed_at descending.
   * Limit is clamped to [1, 100] to bound structured-clone cost over the
   * runtime message channel.
   */
  async getRecent(limit: number = 20): Promise<Interaction[]> {
    try {
      const safeLimit = Math.min(Math.max(Math.floor(limit) || 20, 1), 100)
      return await this.db.interactions.orderBy('observed_at').reverse().limit(safeLimit).toArray()
    } catch (error) {
      throw new DatabaseOperationError(`getRecent (${limit})`, error)
    }
  }

  /**
   * Counts the total number of interactions in IndexedDB without loading records into memory.
   */
  async count(): Promise<number> {
    try {
      return await this.db.interactions.count()
    } catch (error) {
      throw new DatabaseOperationError('count interactions', error)
    }
  }

  /**
   * Deletes an interaction by its primary ID.
   * Returns true if a record was deleted, false if no record existed.
   */
  async deleteById(id: string): Promise<boolean> {
    try {
      const exists = await this.db.interactions.get(id)
      if (!exists) {
        return false
      }
      await this.db.interactions.delete(id)
      return true
    } catch (error) {
      throw new DatabaseOperationError(`deleteById (${id})`, error)
    }
  }

  /**
   * Retrieves interactions with pagination support for testing or export.
   */
  async getAll(options?: { limit?: number; offset?: number }): Promise<Interaction[]> {
    try {
      let query = this.db.interactions.orderBy('observed_at')
      if (options?.offset !== undefined) {
        query = query.offset(options.offset)
      }
      if (options?.limit !== undefined) {
        query = query.limit(options.limit)
      }
      return await query.toArray()
    } catch (error) {
      throw new DatabaseOperationError('getAll interactions', error)
    }
  }

  /**
   * Development-only: Performs a full data-integrity scan of the interactions store.
   * Reports total records, unique fingerprints, and per-platform statistics.
   * Because `&fingerprint` is a unique index, physical fingerprint duplicates cannot
   * exist in Dexie; this method verifies that invariant holds and surfaces any anomalies.
   */
  async getIntegrityReport(): Promise<{
    total: number
    uniqueFingerprints: number
    duplicateFingerprints: number
    uniqueIds: number
    duplicateIds: number
    byPlatform: Record<
      string,
      {
        total: number
        uniqueFingerprints: number
        duplicateFingerprints: number
      }
    >
  }> {
    try {
      let total = 0
      const fingerprintSet = new Set<string>()
      const idSet = new Set<string>()
      const byPlatform: Record<string, { total: number; fingerprints: Set<string> }> = {}

      await this.db.interactions.each((interaction) => {
        total++
        fingerprintSet.add(interaction.fingerprint)
        idSet.add(interaction.id)
        const platform = interaction.platform
        if (!byPlatform[platform]) {
          byPlatform[platform] = { total: 0, fingerprints: new Set() }
        }
        byPlatform[platform].total++
        byPlatform[platform].fingerprints.add(interaction.fingerprint)
      })
      const uniqueFingerprints = fingerprintSet.size
      const duplicateFingerprints = total - uniqueFingerprints
      const uniqueIds = idSet.size
      const duplicateIds = total - uniqueIds

      const byPlatformResult: Record<
        string,
        { total: number; uniqueFingerprints: number; duplicateFingerprints: number }
      > = {}

      for (const [platform, { total: pt, fingerprints }] of Object.entries(byPlatform)) {
        const uf = fingerprints.size
        byPlatformResult[platform] = {
          total: pt,
          uniqueFingerprints: uf,
          duplicateFingerprints: pt - uf,
        }
      }

      logger.debug(
        'Database',
        'CORE',
        `[Database integrity check] Interactions: total=${total}, uniqueFingerprints=${uniqueFingerprints}, duplicateFingerprints=${duplicateFingerprints}, uniqueIds=${uniqueIds}, duplicateIds=${duplicateIds}`
      )

      return {
        total,
        uniqueFingerprints,
        duplicateFingerprints,
        uniqueIds,
        duplicateIds,
        byPlatform: byPlatformResult,
      }
    } catch (error) {
      throw new DatabaseOperationError('getIntegrityReport interactions', error)
    }
  }
}
