/**
 * Conversation Repository
 * Encapsulates all data access and persistence logic for lightweight conversation records.
 */

import { getDatabase, type IntelliCacheDB } from '../db'
import {
  DatabaseOperationError,
  namespaceConversationId,
  type Conversation,
  type CreateConversationInput,
} from '../types'

export class ConversationRepository {
  private db: IntelliCacheDB

  constructor(db?: IntelliCacheDB) {
    this.db = db ?? getDatabase()
  }

  /**
   * Creates a new conversation or updates an existing conversation's last_observed_at timestamp.
   * Preserves first_observed_at on subsequent updates.
   * Guarantees conversation identity is namespaced by platform (`${platform}:${raw_conversation_id}`).
   */
  async createOrUpdate(input: CreateConversationInput | Conversation): Promise<Conversation> {
    try {
      const platform = input.platform.trim().toLowerCase()
      const namespacedId = namespaceConversationId(platform, input.id)
      if (!namespacedId) {
        throw new Error('Conversation id cannot be empty')
      }

      const observedAt =
        'observed_at' in input && input.observed_at ? input.observed_at : new Date().toISOString()
      const existing = await this.db.conversations.get(namespacedId)

      if (existing) {
        const updated: Conversation = {
          ...existing,
          title: input.title !== undefined ? input.title : existing.title,
          last_observed_at:
            new Date(observedAt).getTime() > new Date(existing.last_observed_at).getTime()
              ? observedAt
              : existing.last_observed_at,
        }
        await this.db.conversations.put(updated)
        return updated
      }

      const firstObservedAt =
        'first_observed_at' in input && input.first_observed_at
          ? input.first_observed_at
          : observedAt
      const lastObservedAt =
        'last_observed_at' in input && input.last_observed_at ? input.last_observed_at : observedAt

      const newConversation: Conversation = {
        id: namespacedId,
        platform,
        title: input.title ?? null,
        first_observed_at: firstObservedAt,
        last_observed_at: lastObservedAt,
      }

      await this.db.conversations.add(newConversation)
      return newConversation
    } catch (error) {
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
