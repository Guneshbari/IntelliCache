import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { DuplicateInteractionError } from '../src/database/types'

describe('InteractionRepository', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository

  beforeEach(() => {
    const testDbName = `test-repo-interaction-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  it('creates an interaction with auto-generated ID, metrics, namespaced conversation ID, and level_1 fingerprint strategy', async () => {
    const created = await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-001',
      message_id: 'msg-001',
      query: { text: 'How does semantic caching work?' },
      response: { text: 'It caches based on embedding similarity rather than exact text keys.' },
      conversation_title: 'Semantic Cache Chat',
    })

    expect(created.id).toBeDefined()
    expect(created.schema_version).toBe(1)
    expect(created.fingerprint).toHaveLength(64)
    expect(created.fingerprint_strategy).toBe('level_1')
    expect(created.platform).toBe('chatgpt')
    expect(created.conversation_id).toBe('chatgpt:conv-001')
    expect(created.message_id).toBe('msg-001')
    expect(created.query.characters).toBe(31)
    expect(created.query.bytes).toBe(31)
    expect(created.query.estimated_tokens).toBeNull()
    expect(created.response.characters).toBe(68)
    expect(created.response.bytes).toBe(68)
    expect(created.conversation_title).toBe('Semantic Cache Chat')
  })

  it('correctly determines and persists level_2 and level_3 fingerprint strategies', async () => {
    // Level 2: Conversation ID present, message_id null
    const createdL2 = await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'conv-002',
      query: { text: 'Explain LRU cache' },
      response: { text: 'Discards least recently used items.' },
    })
    expect(createdL2.fingerprint_strategy).toBe('level_2')
    expect(createdL2.conversation_id).toBe('claude:conv-002')

    // Level 3: No conversation_id or message_id
    const createdL3 = await interactionRepo.create({
      platform: 'gemini',
      query: { text: 'Stateless query' },
      response: { text: 'Stateless response' },
    })
    expect(createdL3.fingerprint_strategy).toBe('level_3')
    expect(createdL3.conversation_id).toBeNull()
  })

  it('retrieves an interaction by ID and by fingerprint', async () => {
    const created = await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'conv-002',
      query: { text: 'What is a vector database?' },
      response: { text: 'A database specialized in storing high-dimensional vector embeddings.' },
    })

    const byId = await interactionRepo.getById(created.id)
    expect(byId).toEqual(created)

    const byFp = await interactionRepo.getByFingerprint(created.fingerprint)
    expect(byFp).toEqual(created)

    const nonExistent = await interactionRepo.getById('non-existent-id')
    expect(nonExistent).toBeNull()
  })

  it('retrieves interactions by conversation_id sorted by observed_at', async () => {
    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-group-1',
      observed_at: '2026-08-17T01:00:00.000Z',
      query: { text: 'Query 1' },
      response: { text: 'Response 1' },
    })

    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-group-1',
      observed_at: '2026-08-17T02:00:00.000Z',
      query: { text: 'Query 2' },
      response: { text: 'Response 2' },
    })

    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-other',
      query: { text: 'Other Query' },
      response: { text: 'Other Response' },
    })

    // Query using raw id + platform or namespaced id directly
    const results = await interactionRepo.getByConversationId('conv-group-1', 'chatgpt')
    expect(results).toHaveLength(2)
    expect(results[0].query.text).toBe('Query 1')
    expect(results[1].query.text).toBe('Query 2')

    const resultsDirect = await interactionRepo.getByConversationId('chatgpt:conv-group-1')
    expect(resultsDirect).toHaveLength(2)
  })

  it('counts total interactions without loading entire dataset into memory', async () => {
    expect(await interactionRepo.count()).toBe(0)

    await interactionRepo.create({
      platform: 'chatgpt',
      query: { text: 'Q1' },
      response: { text: 'R1' },
      observed_at: '2026-08-17T01:00:00.000Z',
    })
    await interactionRepo.create({
      platform: 'claude',
      query: { text: 'Q2' },
      response: { text: 'R2' },
      observed_at: '2026-08-17T02:00:00.000Z',
    })

    expect(await interactionRepo.count()).toBe(2)
  })

  it('rejects duplicate interaction fingerprints with DuplicateInteractionError via pre-check', async () => {
    const input = {
      platform: 'chatgpt',
      conversation_id: 'conv-duplicate-test',
      message_id: 'msg-duplicate-test',
      query: { text: 'Duplicate query' },
      response: { text: 'Duplicate response' },
    }

    await interactionRepo.create(input)

    await expect(interactionRepo.create(input)).rejects.toThrow(DuplicateInteractionError)
  })

  it('converts Dexie ConstraintError to DuplicateInteractionError during concurrent/direct insert', async () => {
    const input = {
      platform: 'chatgpt',
      conversation_id: 'conv-concurrent-test',
      message_id: 'msg-concurrent-test',
      query: { text: 'Concurrent duplicate query' },
      response: { text: 'Concurrent duplicate response' },
    }

    // Run two simultaneous create promises to test race condition handling
    const [res1, res2] = await Promise.allSettled([
      interactionRepo.create(input),
      interactionRepo.create(input),
    ])

    const successCount = [res1, res2].filter((r) => r.status === 'fulfilled').length
    const rejectedErrors = [res1, res2]
      .filter((r): r is PromiseRejectedResult => r.status === 'rejected')
      .map((r) => r.reason)

    expect(successCount).toBe(1)
    expect(rejectedErrors).toHaveLength(1)
    expect(rejectedErrors[0]).toBeInstanceOf(DuplicateInteractionError)
  })

  it('deletes an interaction by ID and returns boolean success status', async () => {
    const created = await interactionRepo.create({
      platform: 'gemini',
      query: { text: 'To delete' },
      response: { text: 'Will be deleted' },
    })

    expect(await interactionRepo.count()).toBe(1)
    const deleted = await interactionRepo.deleteById(created.id)
    expect(deleted).toBe(true)
    expect(await interactionRepo.count()).toBe(0)

    const deleteAgain = await interactionRepo.deleteById(created.id)
    expect(deleteAgain).toBe(false)
  })

  it('supports paginated retrieval using getAll including offset: 0', async () => {
    for (let i = 1; i <= 5; i++) {
      await interactionRepo.create({
        platform: 'chatgpt',
        observed_at: `2026-08-17T0${i}:00:00.000Z`,
        query: { text: `Query ${i}` },
        response: { text: `Response ${i}` },
      })
    }

    // Test explicit offset: 0
    const page1 = await interactionRepo.getAll({ limit: 2, offset: 0 })
    expect(page1).toHaveLength(2)
    expect(page1[0].query.text).toBe('Query 1')
    expect(page1[1].query.text).toBe('Query 2')

    const page2 = await interactionRepo.getAll({ limit: 2, offset: 2 })
    expect(page2).toHaveLength(2)
    expect(page2[0].query.text).toBe('Query 3')
  })
})

describe('ConversationRepository', () => {
  let db: IntelliCacheDB
  let conversationRepo: ConversationRepository

  beforeEach(() => {
    const testDbName = `test-repo-conv-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  it('creates a new conversation with namespaced ID, first_observed_at, and last_observed_at', async () => {
    const conv = await conversationRepo.createOrUpdate({
      id: 'conv-test-100',
      platform: 'Claude',
      title: 'Initial Title',
      observed_at: '2026-08-17T01:00:00.000Z',
    })

    expect(conv.id).toBe('claude:conv-test-100')
    expect(conv.platform).toBe('claude')
    expect(conv.title).toBe('Initial Title')
    expect(conv.first_observed_at).toBe('2026-08-17T01:00:00.000Z')
    expect(conv.last_observed_at).toBe('2026-08-17T01:00:00.000Z')
  })

  it('isolates cross-platform conversations sharing identical raw IDs without collision or overwrite', async () => {
    const sharedRawId = 'shared-conversation-uuid'

    const chatgptConv = await conversationRepo.createOrUpdate({
      id: sharedRawId,
      platform: 'chatgpt',
      title: 'ChatGPT Thread',
      observed_at: '2026-08-17T01:00:00.000Z',
    })

    const claudeConv = await conversationRepo.createOrUpdate({
      id: sharedRawId,
      platform: 'claude',
      title: 'Claude Thread',
      observed_at: '2026-08-17T02:00:00.000Z',
    })

    expect(chatgptConv.id).toBe(`chatgpt:${sharedRawId}`)
    expect(claudeConv.id).toBe(`claude:${sharedRawId}`)
    expect(await conversationRepo.count()).toBe(2)

    const fetchedChatGPT = await conversationRepo.getById(sharedRawId, 'chatgpt')
    expect(fetchedChatGPT?.title).toBe('ChatGPT Thread')
    expect(fetchedChatGPT?.platform).toBe('chatgpt')

    const fetchedClaude = await conversationRepo.getById(sharedRawId, 'claude')
    expect(fetchedClaude?.title).toBe('Claude Thread')
    expect(fetchedClaude?.platform).toBe('claude')
  })

  it('updates an existing conversation: updates last_observed_at and preserves first_observed_at', async () => {
    await conversationRepo.createOrUpdate({
      id: 'conv-test-200',
      platform: 'ChatGPT',
      title: null,
      observed_at: '2026-08-17T01:00:00.000Z',
    })

    const updated = await conversationRepo.createOrUpdate({
      id: 'conv-test-200',
      platform: 'ChatGPT',
      title: 'Updated Conversation Title',
      observed_at: '2026-08-17T03:00:00.000Z',
    })

    expect(updated.id).toBe('chatgpt:conv-test-200')
    expect(updated.first_observed_at).toBe('2026-08-17T01:00:00.000Z')
    expect(updated.last_observed_at).toBe('2026-08-17T03:00:00.000Z')
    expect(updated.title).toBe('Updated Conversation Title')

    const count = await conversationRepo.count()
    expect(count).toBe(1)
  })

  it('retrieves conversations by platform', async () => {
    await conversationRepo.createOrUpdate({
      id: 'conv-chatgpt-1',
      platform: 'chatgpt',
      title: 'ChatGPT 1',
    })
    await conversationRepo.createOrUpdate({
      id: 'conv-chatgpt-2',
      platform: 'chatgpt',
      title: 'ChatGPT 2',
    })
    await conversationRepo.createOrUpdate({
      id: 'conv-claude-1',
      platform: 'claude',
      title: 'Claude 1',
    })

    const chatgptConvs = await conversationRepo.getByPlatform('chatgpt')
    expect(chatgptConvs).toHaveLength(2)

    const claudeConvs = await conversationRepo.getByPlatform('claude')
    expect(claudeConvs).toHaveLength(1)
  })

  it('deletes conversation by ID with optional platform resolution', async () => {
    await conversationRepo.createOrUpdate({
      id: 'conv-del',
      platform: 'gemini',
    })

    expect(await conversationRepo.count()).toBe(1)
    const deleted = await conversationRepo.deleteById('conv-del', 'gemini')
    expect(deleted).toBe(true)
    expect(await conversationRepo.count()).toBe(0)
  })
})
