/**
 * Dexie.js Database Wrapper for IntelliCache.
 * Provides typed access to interactions and conversations tables.
 */

import Dexie, { type Table } from 'dexie'
import { CURRENT_DB_VERSION, DB_NAME, SCHEMA_V1, SCHEMA_V2 } from './schema'
import type { Conversation, Interaction } from './types'

export class IntelliCacheDB extends Dexie {
  interactions!: Table<Interaction, string>
  conversations!: Table<Conversation, string>

  constructor(dbName: string = DB_NAME) {
    super(dbName)
    // v1 baseline retained so existing installs upgrade through Dexie's
    // versioned migration path instead of a destructive recreate.
    this.version(1).stores(SCHEMA_V1)
    if (CURRENT_DB_VERSION >= 2) {
      this.version(2).stores(SCHEMA_V2).upgrade(() => {})
    }
  }
}

let dbInstance: IntelliCacheDB | null = null

/**
 * Retrieves the shared database singleton, or creates a new instance.
 */
export function getDatabase(dbName: string = DB_NAME): IntelliCacheDB {
  if (!dbInstance || dbInstance.name !== dbName) {
    dbInstance = new IntelliCacheDB(dbName)
  }
  return dbInstance
}

/**
 * Closes and resets the singleton instance (useful for test isolation).
 */
export async function closeDatabase(): Promise<void> {
  if (dbInstance) {
    dbInstance.close()
    dbInstance = null
  }
}
