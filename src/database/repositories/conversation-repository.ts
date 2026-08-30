/**
 * Conversation Repository
 * Encapsulates all data access and persistence logic for lightweight conversation records.
 */

import Dexie from 'dexie'
import { logger, toDiagnosticPlatform } from '../../diagnostics'
import { getDatabase, type IntelliCacheDB } from '../db'
import {
  DatabaseOperationError,
  namespaceConversationId,
  type Conversation,
  type CreateConversationInput,
} from '../types'

export interface ConversationIntegrityResult {
  total: number
  unique: number
  duplicates: number
  byPlatform: Record<string, { total: number; unique: number; duplicates: number }>
}

export class ConversationRepository {
  private db: IntelliCacheDB

  constructor(db?: IntelliCacheDB) {
    this.db = db ?? getDatabase()
  }

  /**
   * Creates a new conversation or updates an existing conversation's last_observed_at timestamp.
   * Preserves first_observed_at on subsequent updates.
   * Guarantees conversation identity is namespaced by platform (`${platform}:${raw_conversation_id}`).
   *
   * Implementation: Uses an atomic Dexie transaction with get + put to avoid TOCTOU races.
   * A concurrent second call for the same ID will wait for the transaction to commit before
   * running its own get, ensuring it sees the already-created record and performs an update
   * instead of a duplicate insert.
   */
  async createOrUpdate(input: CreateConversationInput | Conversation): Promise<Conversation> {
    const platformTag = toDiagnosticPlatform(input.platform)
    try {
      const platform = input.platform.trim().toLowerCase()
      const namespacedId = namespaceConversationId(platform, input.id)
      if (!namespacedId) {
        throw new Error('Conversation id cannot be empty')
      }

      const observedAt =
        'observed_at' in input && input.observed_at ? input.observed_at : new Date().toISOString()

      // Atomic read-modify-write via Dexie transaction.
      // This prevents a TOCTOU race where two concurrent saves for the same conversation
      // both see no existing record and both attempt add(), causing a ConstraintError.
      const result = await this.db.transaction('rw', this.db.conversations, async () => {
        const existing = await this.db.conversations.get(namespacedId)

        if (existing) {
          const newLastObservedAt =
            new Date(observedAt).getTime() > new Date(existing.last_observed_at).getTime()
              ? observedAt
              : existing.last_observed_at

          const updated: Conversation = {
            ...existing,
            title: input.title !== undefined ? (input.title ?? existing.title) : existing.title,
            last_observed_at: newLastObservedAt,
          }
          await this.db.conversations.put(updated)
          return { record: updated, wasCreated: false }
        }

        const firstObservedAt =
          'first_observed_at' in input && input.first_observed_at
            ? input.first_observed_at
            : observedAt
        const lastObservedAt =
          'last_observed_at' in input && input.last_observed_at
            ? input.last_observed_at
            : observedAt

        const newConversation: Conversation = {
          id: namespacedId,
          platform,
          title: input.title ?? null,
          first_observed_at: firstObservedAt,
          last_observed_at: lastObservedAt,
        }
        await this.db.conversations.put(newConversation)
        return { record: newConversation, wasCreated: true }
      })

      if (result.wasCreated) {
        logger.info(
          'Database',
          platformTag,
          `New conversation record created in IndexedDB (ID: ${namespacedId}, title: ${result.record.title ?? 'null'})`
        )
      } else {
        logger.debug(
          'Database',
          platformTag,
          `Conversation updated in IndexedDB (ID: ${namespacedId}, title: ${result.record.title ?? 'null'}, last_observed_at: ${result.record.last_observed_at})`
        )
      }

      return result.record
    } catch (error) {
      // If a ConstraintError somehow escapes the transaction (e.g. concurrent workers),
      // treat it as a harmless duplicate and re-read the existing record.
      if (
        error instanceof Dexie.ConstraintError ||
        (error &&
          typeof error === 'object' &&
          'name' in error &&
          (error as { name: string }).name === 'ConstraintError')
      ) {
        const platform = input.platform.trim().toLowerCase()
        const namespacedId = namespaceConversationId(platform, input.id)
        logger.debug(
          'Database',
          platformTag,
          `Duplicate conversation prevented (ID: ${namespacedId ?? input.id}, concurrent save detected — returning existing record)`
        )
        if (namespacedId) {
          const existing = await this.db.conversations.get(namespacedId)
          if (existing) return existing
        }
      }

      logger.error(
        'Database',
        platformTag,
        `Failed to create/update conversation (${input.id}): ${error instanceof Error ? error.message : String(error)}`
      )
      throw new DatabaseOperationError(`createOrUpdate conversation (${input.id})`, error)
    }
  }

  /**
   * Retrieves a conversation record by its unique ID.
   * If platform is provided, ensures namespaced conversation ID lookup.
   */
  async getById(id: string, platform?: string): Promise<Conversation | null> {
    try {
      const lookupId = platform ? (namespaceConversationId(platform, id) ?? id) : id
      const record = await this.db.conversations.get(lookupId)
      return record ?? null
    } catch (error) {
      throw new DatabaseOperationError(`getById conversation (${id})`, error)
    }
  }

  /**
   * Retrieves all conversations belonging to a specific AI platform.
   */
  async getByPlatform(platform: string): Promise<Conversation[]> {
    try {
      return await this.db.conversations
        .where('platform')
        .equals(platform.toLowerCase())
        .sortBy('last_observed_at')
    } catch (error) {
      throw new DatabaseOperationError(`getByPlatform (${platform})`, error)
    }
  }

  /**
   * Counts the total number of conversations in IndexedDB without loading records into memory.
   */
  async count(): Promise<number> {
    try {
      return await this.db.conversations.count()
    } catch (error) {
      throw new DatabaseOperationError('count conversations', error)
    }
  }

  /**
   * Retrieves all conversations from IndexedDB.
   */
  async getAll(): Promise<Conversation[]> {
    try {
      return await this.db.conversations.toArray()
    } catch (error) {
      throw new DatabaseOperationError('getAll conversations', error)
    }
  }

  /**
   * Development-only: Performs a full data-integrity scan of the conversations store.
   * Reports physical record count, unique primary keys, and per-platform statistics.
   * Because the `id` field is the primary key and is deterministic (platform:conversationId),
   * physical duplicates cannot exist in Dexie; this verifies that invariant holds.
   */
  async getIntegrityReport(): Promise<ConversationIntegrityResult> {
    try {
      const all = await this.db.conversations.toArray()
      const idSet = new Set<string>()
      const byPlatform: Record<string, { total: number; ids: Set<string> }> = {}

      for (const conv of all) {
        idSet.add(conv.id)
        if (!byPlatform[conv.platform]) {
          byPlatform[conv.platform] = { total: 0, ids: new Set() }
        }
        byPlatform[conv.platform].total++
        byPlatform[conv.platform].ids.add(conv.id)
      }

      const total = all.length
      const unique = idSet.size
      const duplicates = total - unique

      const byPlatformResult: ConversationIntegrityResult['byPlatform'] = {}
      for (const [platform, { total: pt, ids }] of Object.entries(byPlatform)) {
        const pu = ids.size
        byPlatformResult[platform] = { total: pt, unique: pu, duplicates: pt - pu }
      }

      logger.debug(
        'Database',
        'CORE',
        `[Database integrity check] Conversations: total=${total}, unique=${unique}, duplicates=${duplicates}`
      )

      return { total, unique, duplicates, byPlatform: byPlatformResult }
    } catch (error) {
      throw new DatabaseOperationError('getIntegrityReport conversations', error)
    }
  }

  /**
   * Deletes a conversation by its primary ID.
   * Returns true if a record was deleted, false if no record existed.
   */
  async deleteById(id: string, platform?: string): Promise<boolean> {
    try {
      const targetId = platform ? (namespaceConversationId(platform, id) ?? id) : id
      const exists = await this.db.conversations.get(targetId)
      if (!exists) {
        return false
      }
      await this.db.conversations.delete(targetId)
      return true
    } catch (error) {
      throw new DatabaseOperationError(`deleteById conversation (${id})`, error)
    }
  }
}
