/**
 * Database schema definitions, constants, and index specifications for IntelliCache.
 */

export const DB_NAME = 'intelliCache'
export const CURRENT_DB_VERSION = 1
export const CURRENT_COLLECTOR_VERSION = '0.1.0'
export const CURRENT_EXTENSION_VERSION = '0.1.0'

/**
 * Dexie table index schema definitions.
 *
 * `interactions`:
 * - `id`: Primary key
 * - `&fingerprint`: Unique index for fast deduplication lookup and constraint enforcement
 * - `platform`: Indexed for filtering interactions by AI platform
 * - `conversation_id`: Indexed for grouping interactions by conversation thread
 * - `observed_at`: Indexed for time-series sorting, range queries, and analysis
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
