// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { CURRENT_DB_VERSION } from '../src/database/schema'
import type { Conversation, Interaction } from '../src/database/types'

describe('Forensic IndexedDB Integrity Audit', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    dbName = `forensic-audit-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(dbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  // ─── PHASE 2: INSPECT ACTUAL SCHEMA PROGRAMMATICALLY ─────────────────────

  describe('Phase 2: Programmatic Schema & Index Inspection', () => {
    it('verifies exact database version, object stores, keyPaths, and index definitions', async () => {
      await db.open()
      const rawIdb = db.backendDB()

      expect(rawIdb.name).toBe(dbName)
      expect(db.verno).toBe(CURRENT_DB_VERSION)
      // Dexie multiplies version by 10 for native IDB versioning (1.0 -> 10)
      expect(rawIdb.version).toBe(CURRENT_DB_VERSION * 10)
      expect(Array.from(rawIdb.objectStoreNames).sort()).toEqual(['conversations', 'interactions'])

      // Inspect interactions object store
      const tx = rawIdb.transaction(['interactions', 'conversations'], 'readonly')
      const interactionStore = tx.objectStore('interactions')

      expect(interactionStore.keyPath).toBe('id')
      expect(interactionStore.autoIncrement).toBe(false)

      const interactionIndexes = Array.from(interactionStore.indexNames).map((name) => {
        const idx = interactionStore.index(name)
        return {
          name: idx.name,
          keyPath: idx.keyPath,
          unique: idx.unique,
          multiEntry: idx.multiEntry,
        }
      })

      // Must have exactly: fingerprint (unique), platform, conversation_id, observed_at
      expect(interactionIndexes).toEqual(
        expect.arrayContaining([
          { name: 'fingerprint', keyPath: 'fingerprint', unique: true, multiEntry: false },
          { name: 'platform', keyPath: 'platform', unique: false, multiEntry: false },
          { name: 'conversation_id', keyPath: 'conversation_id', unique: false, multiEntry: false },
          { name: 'observed_at', keyPath: 'observed_at', unique: false, multiEntry: false },
        ])
      )
      expect(interactionIndexes).toHaveLength(4)

      // Inspect conversations object store
      const conversationStore = tx.objectStore('conversations')
      expect(conversationStore.keyPath).toBe('id')
      expect(conversationStore.autoIncrement).toBe(false)

      const conversationIndexes = Array.from(conversationStore.indexNames).map((name) => {
        const idx = conversationStore.index(name)
        return {
          name: idx.name,
          keyPath: idx.keyPath,
          unique: idx.unique,
          multiEntry: idx.multiEntry,
        }
      })

      expect(conversationIndexes).toEqual(
        expect.arrayContaining([
          { name: 'platform', keyPath: 'platform', unique: false, multiEntry: false },
          {
            name: 'first_observed_at',
            keyPath: 'first_observed_at',
            unique: false,
            multiEntry: false,
          },
          {
            name: 'last_observed_at',
            keyPath: 'last_observed_at',
            unique: false,
            multiEntry: false,
          },
        ])
      )
      expect(conversationIndexes).toHaveLength(3)
    })
  })

  // ─── PHASE 3 & 4: PHYSICAL OBJECT STORE READS & INDEX CONSISTENCY ─────────

  describe('Phase 3 & 4: Physical Reads & Index-to-Store Consistency Verification', () => {
    it('verifies ChatGPT, Claude, and Gemini records in physical store and across all indexes', async () => {
      // Seed data across all three platforms
      await conversationRepo.createOrUpdate({
        id: 'chatgpt-conv-1',
        platform: 'chatgpt',
        title: 'ChatGPT Thread',
        observed_at: '2026-08-30T10:00:00.000Z',
      })
      await conversationRepo.createOrUpdate({
        id: 'claude-conv-1',
        platform: 'claude',
        title: 'Claude Thread',
        observed_at: '2026-08-30T10:05:00.000Z',
      })
      await conversationRepo.createOrUpdate({
        id: 'b9d96f140f5edd1c',
        platform: 'gemini',
        title: 'Mic Check and System Startup',
        observed_at: '2026-08-30T10:10:00.000Z',
      })

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'chatgpt-conv-1',
        message_id: 'msg-gpt-1',
        query: { text: 'Hello ChatGPT' },
        response: { text: 'Hello! How can I help?' },
        observed_at: '2026-08-30T10:00:00.000Z',
      })
      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'claude-conv-1',
        message_id: 'msg-claude-1',
        query: { text: 'Hello Claude' },
        response: { text: 'Hello! How can I assist?' },
        observed_at: '2026-08-30T10:05:00.000Z',
      })
      await interactionRepo.create({
        platform: 'gemini',
        conversation_id: 'b9d96f140f5edd1c',
        message_id: 'msg-gemini-1',
        query: { text: 'Hello Gemini' },
        response: { text: 'Hello! What would you like to explore?' },
        observed_at: '2026-08-30T10:10:00.000Z',
      })

      // Physical reads using native IDB objectStore.getAll() without index
      const rawIdb = db.backendDB()
      const tx = rawIdb.transaction(['interactions', 'conversations'], 'readonly')

      const physicalConversations: Conversation[] = await new Promise((resolve) => {
        const req = tx.objectStore('conversations').getAll()
        req.onsuccess = () => resolve(req.result)
      })

      const physicalInteractions: Interaction[] = await new Promise((resolve) => {
        const req = tx.objectStore('interactions').getAll()
        req.onsuccess = () => resolve(req.result)
      })

      // Diagnostic assertions for conversations
      expect(physicalConversations).toHaveLength(3)
      const convPrimaryKeys = physicalConversations.map((c) => c.id)
      expect(new Set(convPrimaryKeys).size).toBe(3)
      expect(convPrimaryKeys).toEqual([
        'chatgpt:chatgpt-conv-1',
        'claude:claude-conv-1',
        'gemini:b9d96f140f5edd1c',
      ])

      // Diagnostic assertions for interactions
      expect(physicalInteractions).toHaveLength(3)
      const intPrimaryKeys = physicalInteractions.map((i) => i.id)
      expect(new Set(intPrimaryKeys).size).toBe(3)
      const intFingerprints = physicalInteractions.map((i) => i.fingerprint)
      expect(new Set(intFingerprints).size).toBe(3)

      // Phase 4: Compare every index against physical object store records
      const interactionStore = tx.objectStore('interactions')
      const platformIdx = interactionStore.index('platform')
      const convIdIdx = interactionStore.index('conversation_id')
      const observedAtIdx = interactionStore.index('observed_at')
      const fingerprintIdx = interactionStore.index('fingerprint')

      const interactionMissingReport = {
        platformIndexMissingRecords: [] as string[],
        conversationIdIndexMissingRecords: [] as string[],
        observedAtIndexMissingRecords: [] as string[],
        fingerprintIndexMissingRecords: [] as string[],
      }

      for (const record of physicalInteractions) {
        // Test platform index
        const platformMatch: Interaction[] = await new Promise((resolve) => {
          const req = platformIdx.getAll(record.platform)
          req.onsuccess = () => resolve(req.result)
        })
        if (!platformMatch.some((m) => m.id === record.id)) {
          interactionMissingReport.platformIndexMissingRecords.push(record.id)
        }

        // Test conversation_id index (if conversation_id is non-null)
        if (record.conversation_id !== null) {
          const convIdMatch: Interaction[] = await new Promise((resolve) => {
            const req = convIdIdx.getAll(record.conversation_id)
            req.onsuccess = () => resolve(req.result)
          })
          if (!convIdMatch.some((m) => m.id === record.id)) {
            interactionMissingReport.conversationIdIndexMissingRecords.push(record.id)
          }
        }

        // Test observed_at index
        const observedAtMatch: Interaction[] = await new Promise((resolve) => {
          const req = observedAtIdx.getAll(record.observed_at)
          req.onsuccess = () => resolve(req.result)
        })
        if (!observedAtMatch.some((m) => m.id === record.id)) {
          interactionMissingReport.observedAtIndexMissingRecords.push(record.id)
        }

        // Test fingerprint index
        const fingerprintMatch: Interaction | undefined = await new Promise((resolve) => {
          const req = fingerprintIdx.get(record.fingerprint)
          req.onsuccess = () => resolve(req.result)
        })
        if (!fingerprintMatch || fingerprintMatch.id !== record.id) {
          interactionMissingReport.fingerprintIndexMissingRecords.push(record.id)
        }
      }

      expect(interactionMissingReport.platformIndexMissingRecords).toEqual([])
      expect(interactionMissingReport.conversationIdIndexMissingRecords).toEqual([])
      expect(interactionMissingReport.observedAtIndexMissingRecords).toEqual([])
      expect(interactionMissingReport.fingerprintIndexMissingRecords).toEqual([])

      // Conversations index verification
      const conversationStore = tx.objectStore('conversations')
      const convPlatformIdx = conversationStore.index('platform')
      const firstObservedIdx = conversationStore.index('first_observed_at')
      const lastObservedIdx = conversationStore.index('last_observed_at')

      const convMissingReport = {
        platformIndexMissingRecords: [] as string[],
        firstObservedAtIndexMissingRecords: [] as string[],
        lastObservedAtIndexMissingRecords: [] as string[],
      }

      for (const record of physicalConversations) {
        const pMatch: Conversation[] = await new Promise((resolve) => {
          const req = convPlatformIdx.getAll(record.platform)
          req.onsuccess = () => resolve(req.result)
        })
        if (!pMatch.some((m) => m.id === record.id)) {
          convMissingReport.platformIndexMissingRecords.push(record.id)
        }

        const foMatch: Conversation[] = await new Promise((resolve) => {
          const req = firstObservedIdx.getAll(record.first_observed_at)
          req.onsuccess = () => resolve(req.result)
        })
        if (!foMatch.some((m) => m.id === record.id)) {
          convMissingReport.firstObservedAtIndexMissingRecords.push(record.id)
        }

        const loMatch: Conversation[] = await new Promise((resolve) => {
          const req = lastObservedIdx.getAll(record.last_observed_at)
          req.onsuccess = () => resolve(req.result)
        })
        if (!loMatch.some((m) => m.id === record.id)) {
          convMissingReport.lastObservedAtIndexMissingRecords.push(record.id)
        }
      }

      expect(convMissingReport.platformIndexMissingRecords).toEqual([])
      expect(convMissingReport.firstObservedAtIndexMissingRecords).toEqual([])
      expect(convMissingReport.lastObservedAtIndexMissingRecords).toEqual([])
    })
  })

  // ─── PHASE 8: CHECK DUPLICATE CONVERSATIONS (GEMINI SPECIFIC CASE) ────────

  describe('Phase 8: Gemini Conversation Record Verification', () => {
    it('guarantees gemini:b9d96f140f5edd1c produces exactly one physical record across repeated updates', async () => {
      const geminiId = 'b9d96f140f5edd1c'
      const title = 'Mic Check and System Startup'

      // First observation
      const record1 = await conversationRepo.createOrUpdate({
        id: geminiId,
        platform: 'gemini',
        title,
        observed_at: '2026-08-30T10:00:00.000Z',
      })

      // Second observation (e.g. from page refresh or second interaction in same conversation)
      const record2 = await conversationRepo.createOrUpdate({
        id: geminiId,
        platform: 'gemini',
        title,
        observed_at: '2026-08-30T10:05:00.000Z',
      })

      // Third observation
      const record3 = await conversationRepo.createOrUpdate({
        id: geminiId,
        platform: 'gemini',
        title,
        observed_at: '2026-08-30T10:10:00.000Z',
      })

      expect(record1.id).toBe(`gemini:${geminiId}`)
      expect(record2.id).toBe(`gemini:${geminiId}`)
      expect(record3.id).toBe(`gemini:${geminiId}`)

      // Physical record count must be exactly 1
      const count = await conversationRepo.count()
      expect(count).toBe(1)

      const allPhysical = await conversationRepo.getAll()
      expect(allPhysical).toHaveLength(1)
      expect(allPhysical[0].id).toBe(`gemini:${geminiId}`)
      expect(allPhysical[0].first_observed_at).toBe('2026-08-30T10:00:00.000Z')
      expect(allPhysical[0].last_observed_at).toBe('2026-08-30T10:10:00.000Z')
    })
  })

  // ─── PHASE 9: CHECK CLAUDE INDEXES ────────────────────────────────────────

  describe('Phase 9: Claude Interaction Index Verification', () => {
    it('ensures Claude records appear in all 4 indexes when conversationId is present', async () => {
      const claudeInteraction = await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'claude-chat-xyz',
        message_id: 'claude-msg-123',
        query: { text: 'What is artifact generation?' },
        response: { text: 'Artifacts are standalone documents or code.' },
        observed_at: '2026-08-30T12:00:00.000Z',
      })

      // Test retrieving via platform index
      const byPlatform = await db.interactions.where('platform').equals('claude').toArray()
      expect(byPlatform.some((i) => i.id === claudeInteraction.id)).toBe(true)

      // Test retrieving via conversation_id index
      const byConvId = await db.interactions
        .where('conversation_id')
        .equals('claude:claude-chat-xyz')
        .toArray()
      expect(byConvId.some((i) => i.id === claudeInteraction.id)).toBe(true)

      // Test retrieving via observed_at index
      const byObservedAt = await db.interactions
        .where('observed_at')
        .equals('2026-08-30T12:00:00.000Z')
        .toArray()
      expect(byObservedAt.some((i) => i.id === claudeInteraction.id)).toBe(true)

      // Test retrieving via fingerprint index
      const byFingerprint = await interactionRepo.getByFingerprint(claudeInteraction.fingerprint)
      expect(byFingerprint).not.toBeNull()
      expect(byFingerprint!.id).toBe(claudeInteraction.id)
    })

    it('handles Claude interaction with null conversationId (unbound/new-chat)', async () => {
      const unboundInteraction = await interactionRepo.create({
        platform: 'claude',
        conversation_id: null,
        message_id: 'claude-msg-unbound',
        query: { text: 'Standalone query' },
        response: { text: 'Standalone response' },
        observed_at: '2026-08-30T12:30:00.000Z',
      })

      expect(unboundInteraction.conversation_id).toBeNull()

      // Platform index must still index it
      const byPlatform = await db.interactions.where('platform').equals('claude').toArray()
      expect(byPlatform.some((i) => i.id === unboundInteraction.id)).toBe(true)

      // Fingerprint index must still index it
      const byFp = await interactionRepo.getByFingerprint(unboundInteraction.fingerprint)
      expect(byFp).not.toBeNull()
      expect(byFp!.id).toBe(unboundInteraction.id)

      // Observed_at index must still index it
      const byObs = await db.interactions
        .where('observed_at')
        .equals('2026-08-30T12:30:00.000Z')
        .toArray()
      expect(byObs.some((i) => i.id === unboundInteraction.id)).toBe(true)
    })
  })

  // ─── PHASE 13 & 15: DATABASE INVARIANTS & CLEAN DB / REFRESH TESTING ───────

  describe('Phase 15: Clean Database, Refresh, Multi-Turn, and Cross-Platform Tests', () => {
    it('clean database test: ChatGPT, Claude, and Gemini generate 1 response each', async () => {
      expect(await interactionRepo.count()).toBe(0)
      expect(await conversationRepo.count()).toBe(0)

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'c-gpt',
        message_id: 'm-gpt',
        query: { text: 'Q1' },
        response: { text: 'R1' },
      })
      await conversationRepo.createOrUpdate({ id: 'c-gpt', platform: 'chatgpt' })

      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'c-claude',
        message_id: 'm-claude',
        query: { text: 'Q2' },
        response: { text: 'R2' },
      })
      await conversationRepo.createOrUpdate({ id: 'c-claude', platform: 'claude' })

      await interactionRepo.create({
        platform: 'gemini',
        conversation_id: 'c-gemini',
        message_id: 'm-gemini',
        query: { text: 'Q3' },
        response: { text: 'R3' },
      })
      await conversationRepo.createOrUpdate({ id: 'c-gemini', platform: 'gemini' })

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(3)

      const intReport = await interactionRepo.getIntegrityReport()
      expect(intReport.total).toBe(3)
      expect(intReport.duplicateFingerprints).toBe(0)
      expect(intReport.byPlatform['chatgpt'].total).toBe(1)
      expect(intReport.byPlatform['claude'].total).toBe(1)
      expect(intReport.byPlatform['gemini'].total).toBe(1)
    })

    it('refresh test: refreshing all 3 platforms keeps physical counts unchanged', async () => {
      const gptInput = {
        platform: 'chatgpt',
        conversation_id: 'c-gpt-ref',
        message_id: 'm-gpt-ref',
        query: { text: 'Q-ref-gpt' },
        response: { text: 'R-ref-gpt' },
      }
      const claudeInput = {
        platform: 'claude',
        conversation_id: 'c-claude-ref',
        message_id: 'm-claude-ref',
        query: { text: 'Q-ref-claude' },
        response: { text: 'R-ref-claude' },
      }
      const geminiInput = {
        platform: 'gemini',
        conversation_id: 'c-gemini-ref',
        message_id: 'm-gemini-ref',
        query: { text: 'Q-ref-gemini' },
        response: { text: 'R-ref-gemini' },
      }

      await interactionRepo.create(gptInput)
      await conversationRepo.createOrUpdate({ id: 'c-gpt-ref', platform: 'chatgpt' })
      await interactionRepo.create(claudeInput)
      await conversationRepo.createOrUpdate({ id: 'c-claude-ref', platform: 'claude' })
      await interactionRepo.create(geminiInput)
      await conversationRepo.createOrUpdate({ id: 'c-gemini-ref', platform: 'gemini' })

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(3)

      // Simulate refresh for all 3
      await expect(
        interactionRepo.create({ ...gptInput, capture_context: 'on_load' as const })
      ).rejects.toThrow()
      await conversationRepo.createOrUpdate({ id: 'c-gpt-ref', platform: 'chatgpt' })

      await expect(
        interactionRepo.create({ ...claudeInput, capture_context: 'on_load' as const })
      ).rejects.toThrow()
      await conversationRepo.createOrUpdate({ id: 'c-claude-ref', platform: 'claude' })

      await expect(
        interactionRepo.create({ ...geminiInput, capture_context: 'on_load' as const })
      ).rejects.toThrow()
      await conversationRepo.createOrUpdate({ id: 'c-gemini-ref', platform: 'gemini' })

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(3)
    })

    it('multiple interaction test: 3 turns in one conversation produce 3 interactions and 1 conversation', async () => {
      const convId = 'multi-turn-conv'
      await conversationRepo.createOrUpdate({
        id: convId,
        platform: 'chatgpt',
        title: 'Multi Turn',
      })

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-1',
        query: { text: 'Turn 1 query' },
        response: { text: 'Turn 1 response' },
      })
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-2',
        query: { text: 'Turn 2 query' },
        response: { text: 'Turn 2 response' },
      })
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: convId,
        message_id: 'turn-3',
        query: { text: 'Turn 3 query' },
        response: { text: 'Turn 3 response' },
      })

      expect(await interactionRepo.count()).toBe(3)
      expect(await conversationRepo.count()).toBe(1)

      const convInteractions = await interactionRepo.getByConversationId(convId, 'chatgpt')
      expect(convInteractions).toHaveLength(3)
    })

    it('cross platform test: identical query text across ChatGPT, Claude, and Gemini remain distinct', async () => {
      const sameQuery = 'Explain quantum computing in simple terms'
      const sameResponse =
        'Quantum computing uses qubits that can exist in multiple states simultaneously.'

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-gpt-cross',
        message_id: 'msg-1',
        query: { text: sameQuery },
        response: { text: sameResponse },
      })
      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'conv-claude-cross',
        message_id: 'msg-1',
        query: { text: sameQuery },
        response: { text: sameResponse },
      })
      await interactionRepo.create({
        platform: 'gemini',
        conversation_id: 'conv-gemini-cross',
        message_id: 'msg-1',
        query: { text: sameQuery },
        response: { text: sameResponse },
      })

      expect(await interactionRepo.count()).toBe(3)

      const gptRecords = await db.interactions.where('platform').equals('chatgpt').toArray()
      const claudeRecords = await db.interactions.where('platform').equals('claude').toArray()
      const geminiRecords = await db.interactions.where('platform').equals('gemini').toArray()

      expect(gptRecords).toHaveLength(1)
      expect(claudeRecords).toHaveLength(1)
      expect(geminiRecords).toHaveLength(1)

      // Fingerprints must all be unique because platform is hashed into the payload
      const fingerprints = [
        gptRecords[0].fingerprint,
        claudeRecords[0].fingerprint,
        geminiRecords[0].fingerprint,
      ]
      expect(new Set(fingerprints).size).toBe(3)
    })
  })
})
