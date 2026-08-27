import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, getDatabase, IntelliCacheDB } from '../src/database/db'
import { CURRENT_DB_VERSION, DB_NAME } from '../src/database/schema'

describe('Dexie Database Initialization & Schema', () => {
  let db: IntelliCacheDB

  beforeEach(() => {
    db = new IntelliCacheDB(`test-db-${Date.now()}-${Math.random()}`)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  it('initializes with correct schema version and store tables', async () => {
    await db.open()

    expect(db.verno).toBe(CURRENT_DB_VERSION)
    expect(db.name).toContain('test-db')
    expect(db.interactions).toBeDefined()
    expect(db.conversations).toBeDefined()

    const tableNames = db.tables.map((t) => t.name)
    expect(tableNames).toContain('interactions')
    expect(tableNames).toContain('conversations')
  })

  it('can be opened and closed repeatedly without corruption', async () => {
    await db.open()
    expect(db.isOpen()).toBe(true)

    db.close()
    expect(db.isOpen()).toBe(false)

    await db.open()
    expect(db.isOpen()).toBe(true)
  })

  it('returns shared singleton instance via getDatabase', () => {
    const db1 = getDatabase(DB_NAME)
    const db2 = getDatabase(DB_NAME)

    expect(db1).toBe(db2)
    expect(db1.name).toBe(DB_NAME)
  })
})
