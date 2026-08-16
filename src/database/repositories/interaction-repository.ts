/**
 * Interaction Repository
 * Encapsulates all data access and persistence logic for AI query/response interactions.
 */

import Dexie from 'dexie'
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
      } else {
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
      }
      computedFingerprint = fingerprint

      // Check for duplicate fingerprint before insertion
      const existing = await this.db.interactions.where('fingerprint').equals(fingerprint).first()
      if (existing) {
        throw new DuplicateInteractionError(
          fingerprint,
          `Interaction with fingerprint '${fingerprint}' already exists (ID: ${existing.id}).`
        )
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
      } catch (addError) {
        if (
          (addError &&
            typeof addError === 'object' &&
            'name' in addError &&
            addError.name === 'ConstraintError') ||
          addError instanceof Dexie.ConstraintError
        ) {
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
        throw new DuplicateInteractionError(
          computedFingerprint,
          `Interaction with fingerprint '${computedFingerprint}' already exists (constraint violation).`
        )
      }
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
}
