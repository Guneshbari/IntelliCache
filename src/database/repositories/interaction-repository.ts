/**
 * Interaction Repository
 * Encapsulates all data access and persistence logic for AI query/response interactions.
 */

import Dexie from 'dexie'
import { logger, toDiagnosticPlatform } from '../../diagnostics'
import { generateInteractionFingerprint } from '../../fingerprint/fingerprint'
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
    let computedFingerprint = ''
    const platformTag = toDiagnosticPlatform(input.platform)
    try {
      const observedAt =
        'observed_at' in input && input.observed_at ? input.observed_at : new Date().toISOString()
      const id = 'id' in input && input.id ? input.id : crypto.randomUUID()
      const platform = input.platform.trim().toLowerCase()
      const namespacedConvId = namespaceConversationId(platform, input.conversation_id)

      // Calculate fingerprint and strategy
      let fingerprint: string
      let fingerprintStrategy: FingerprintStrategy
      if ('fingerprint' in input && input.fingerprint) {
        fingerprint = input.fingerprint
        fingerprintStrategy = input.fingerprint_strategy ?? 'level_1'
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
          `Fingerprint generated: ${fingerprint.slice(0, 16)}... (strategy: ${fingerprintStrategy})`
        )
      }
      computedFingerprint = fingerprint

      // Check for duplicate fingerprint before insertion
      const existing = await this.db.interactions.where('fingerprint').equals(fingerprint).first()
      if (existing) {
        logger.info(
          'Database',
          platformTag,
          `Existing fingerprint found: '${fingerprint.slice(0, 16)}...' already exists in DB (ID: ${existing.id}).`
        )

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
            `Existing interaction updated: ID ${existing.id} bound to conversation ${namespacedConvId}`
          )
          logger.info(
            'Lifecycle',
            platformTag,
            `Conversation ID resolved: interaction ID ${existing.id} -> ${namespacedConvId}`
          )
          return existing
        }

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
        if (existingUnbound && existingUnbound.conversation_id === null) {
          logger.info(
            'Database',
            platformTag,
            `Existing fingerprint found: unbound interaction '${existingUnbound.fingerprint.slice(0, 16)}...' in DB (ID: ${existingUnbound.id}).`
          )
          existingUnbound.conversation_id = namespacedConvId
          if (input.conversation_title)
            existingUnbound.conversation_title = input.conversation_title
          if (input.message_id) existingUnbound.message_id = input.message_id.trim()
          if (input.user_message_id) existingUnbound.user_message_id = input.user_message_id.trim()
          await this.db.interactions.put(existingUnbound)
          logger.info(
            'Database',
            platformTag,
            `Existing interaction updated: unbound interaction ${existingUnbound.id} bound to conversation ${namespacedConvId}`
          )
          logger.info(
            'Lifecycle',
            platformTag,
            `Conversation ID resolved: interaction ID ${existingUnbound.id} -> ${namespacedConvId}`
          )
          return existingUnbound
        }
      }

      // Construct full canonical interaction entity
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
        query:
          'characters' in input.query && 'bytes' in input.query
            ? (input.query as Interaction['query'])
            : calculateTextMetrics(input.query.text, input.query.estimated_tokens),
        response:
          'characters' in input.response && 'bytes' in input.response
            ? (input.response as Interaction['response'])
            : calculateTextMetrics(input.response.text, input.response.estimated_tokens),
        conversation_title: input.conversation_title ?? null,
        collector_version: input.collector_version ?? CURRENT_COLLECTOR_VERSION,
      }

      try {
        await this.db.interactions.add(interaction)
        logger.info(
          'Database',
          platformTag,
          `New interaction inserted: ID ${id}, fingerprint ${fingerprint.slice(0, 16)}..., queryChars: ${interaction.query.characters}, responseChars: ${interaction.response.characters}`
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
      if (
        (error &&
          typeof error === 'object' &&
          'name' in error &&
          error.name === 'ConstraintError') ||
        error instanceof Dexie.ConstraintError
      ) {
        logger.info(
          'Database',
          platformTag,
          `ConstraintError caught during insertion: fingerprint '${computedFingerprint.slice(0, 16)}...' already exists.`
        )
        throw new DuplicateInteractionError(
          computedFingerprint,
          `Interaction with fingerprint '${computedFingerprint}' already exists (constraint violation).`
        )
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
   */
  async getByConversationId(conversationId: string, platform?: string): Promise<Interaction[]> {
    try {
      const targetId = platform
        ? (namespaceConversationId(platform, conversationId) ?? conversationId)
        : conversationId
      return await this.db.interactions
        .where('conversation_id')
        .equals(targetId)
        .sortBy('observed_at')
    } catch (error) {
      throw new DatabaseOperationError(`getByConversationId (${conversationId})`, error)
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
      const all = await this.db.interactions.toArray()
      const fingerprintSet = new Set<string>()
      const idSet = new Set<string>()
      const byPlatform: Record<string, { total: number; fingerprints: Set<string> }> = {}

      for (const interaction of all) {
        fingerprintSet.add(interaction.fingerprint)
        idSet.add(interaction.id)
        const platform = interaction.platform
        if (!byPlatform[platform]) {
          byPlatform[platform] = { total: 0, fingerprints: new Set() }
        }
        byPlatform[platform].total++
        byPlatform[platform].fingerprints.add(interaction.fingerprint)
      }

      const total = all.length
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
