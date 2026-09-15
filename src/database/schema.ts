/**
 * Database schema definitions, constants, and index specifications for IntelliCache.
 */

export const DB_NAME = 'intelliCache'
export const CURRENT_DB_VERSION = 2
export const CURRENT_COLLECTOR_VERSION = '0.1.0'

/**
 * Dexie table index schema definitions.
 *
 * `interactions`:
 * - `id`: Primary key
 * - `&fingerprint`: Unique index for fast deduplication lookup and constraint enforcement
 * - `platform`: Indexed for filtering interactions by AI platform
 * - `conversation_id`: Indexed for grouping interactions by conversation thread
 * - `observed_at`: Indexed for time-series sorting, range queries, and analysis
 * - `[platform+observed_at]`, `[conversation_id+observed_at]`: Compound indexes so
 *   filtered+sorted queries use the index instead of in-memory sort (v2 addition)
 *
 * `conversations`:
 * - `id`: Primary key
 * - `platform`: Indexed for platform filtering
 * - `first_observed_at`: Indexed for chronological ordering
 * - `last_observed_at`: Indexed for recency sorting
 */
export const SCHEMA_V1 = {
  interactions: 'id, &fingerprint, platform, conversation_id, observed_at',
  conversations: 'id, platform, first_observed_at, last_observed_at',
} as const

/**
 * Version 2 adds compound indexes for the two hot query shapes
 * (`where(platform).sortBy(observed_at)`, `where(conversation_id).sortBy(observed_at)`).
 * Additive-only: Dexie auto-upgrades v1 databases without data loss.
 */
export const SCHEMA_V2 = {
  interactions:
    'id, &fingerprint, platform, conversation_id, observed_at, [platform+observed_at], [conversation_id+observed_at]',
  conversations: 'id, platform, first_observed_at, last_observed_at',
} as const
