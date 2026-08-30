// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { DuplicateInteractionError } from '../src/database/types'

describe('Deduplication Invariant Tests — Data Integrity Audit', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository

  beforeEach(() => {
    const testDbName = `test-dedup-audit-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  // ─── CONVERSATION DEDUPLICATION ───────────────────────────────────────────

  describe('Conversation: one record per platform+conversationId', () => {
    it('creates exactly one record on first save', async () => {
      await conversationRepo.createOrUpdate({
        id: 'conv-abc',
        platform: 'chatgpt',
        title: 'First Thread',
      })
      expect(await conversationRepo.count()).toBe(1)
    })

    it('does NOT create a second record on refresh (same id, same platform)', async () => {
      await conversationRepo.createOrUpdate({
        id: 'conv-abc',
        platform: 'chatgpt',
        title: 'First Thread',
        observed_at: '2026-08-30T10:00:00.000Z',
      })
      await conversationRepo.createOrUpdate({
        id: 'conv-abc',
        platform: 'chatgpt',
        title: 'First Thread',
        observed_at: '2026-08-30T10:05:00.000Z',
      })
      expect(await conversationRepo.count()).toBe(1)
    })

    it('updates last_observed_at on refresh without creating a new record', async () => {
      await conversationRepo.createOrUpdate({
        id: 'conv-abc',
        platform: 'chatgpt',
        title: 'Thread',
        observed_at: '2026-08-30T10:00:00.000Z',
      })
      const updated = await conversationRepo.createOrUpdate({
        id: 'conv-abc',
        platform: 'chatgpt',
        title: 'Thread',
        observed_at: '2026-08-30T11:00:00.000Z',
      })
      expect(await conversationRepo.count()).toBe(1)
      expect(updated.first_observed_at).toBe('2026-08-30T10:00:00.000Z')
      expect(updated.last_observed_at).toBe('2026-08-30T11:00:00.000Z')
    })

    it('preserves first_observed_at across repeated updates', async () => {
      await conversationRepo.createOrUpdate({
        id: 'conv-stable',
        platform: 'claude',
        observed_at: '2026-08-30T08:00:00.000Z',
      })
      await conversationRepo.createOrUpdate({
        id: 'conv-stable',
        platform: 'claude',
        observed_at: '2026-08-30T09:00:00.000Z',
      })
      await conversationRepo.createOrUpdate({
        id: 'conv-stable',
        platform: 'claude',
        observed_at: '2026-08-30T10:00:00.000Z',
      })
      const final = await conversationRepo.getById('conv-stable', 'claude')
      expect(final).not.toBeNull()
      expect(final!.first_observed_at).toBe('2026-08-30T08:00:00.000Z')
      expect(final!.last_observed_at).toBe('2026-08-30T10:00:00.000Z')
      expect(await conversationRepo.count()).toBe(1)
    })

    it('isolates conversations across platforms with the same raw ID', async () => {
      await conversationRepo.createOrUpdate({ id: 'shared-id', platform: 'chatgpt' })
      await conversationRepo.createOrUpdate({ id: 'shared-id', platform: 'claude' })
      await conversationRepo.createOrUpdate({ id: 'shared-id', platform: 'gemini' })
      expect(await conversationRepo.count()).toBe(3)
    })

    it('handles concurrent saves for the same ID without creating duplicates', async () => {
      // Simulate two adapter scans arriving at the same time for the same conversation
      await Promise.all([
        conversationRepo.createOrUpdate({
          id: 'race-conv',
          platform: 'gemini',
          observed_at: '2026-08-30T10:00:00.000Z',
        }),
        conversationRepo.createOrUpdate({
          id: 'race-conv',
          platform: 'gemini',
          observed_at: '2026-08-30T10:00:01.000Z',
        }),
      ])
      expect(await conversationRepo.count()).toBe(1)
    })

    it('stores correct namespaced primary key (platform:id)', async () => {
      await conversationRepo.createOrUpdate({ id: 'raw-id', platform: 'gemini' })
      const record = await conversationRepo.getById('raw-id', 'gemini')
      expect(record).not.toBeNull()
      expect(record!.id).toBe('gemini:raw-id')
    })
  })

  // ─── INTERACTION DEDUPLICATION ────────────────────────────────────────────

  describe('Interaction: one record per fingerprint (IndexedDB unique constraint)', () => {
    it('creates exactly one interaction for a new query/response pair', async () => {
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-1',
        message_id: 'msg-1',
        query: { text: 'What is RAG?' },
        response: { text: 'RAG is Retrieval-Augmented Generation.' },
      })
      expect(await interactionRepo.count()).toBe(1)
    })

    it('rejects a duplicate (same fingerprint) with DuplicateInteractionError', async () => {
      const input = {
        platform: 'claude',
        conversation_id: 'conv-dup',
        message_id: 'msg-dup',
        query: { text: 'Tell me about embeddings.' },
        response: { text: 'Embeddings map text to vectors.' },
      }
      await interactionRepo.create(input)
      await expect(interactionRepo.create(input)).rejects.toBeInstanceOf(DuplicateInteractionError)
      expect(await interactionRepo.count()).toBe(1)
    })

    it('does NOT create a new record when the same interaction is re-observed on page refresh', async () => {
      // Simulates on_load during a page refresh re-seeing the same interaction
      const baseInput = {
        platform: 'gemini',
        conversation_id: 'conv-refresh',
        message_id: 'msg-refresh',
        query: { text: 'Explain transformers.' },
        response: { text: 'Transformers use self-attention mechanisms.' },
      }
      await interactionRepo.create({ ...baseInput, capture_context: 'on_load' as const })
      await expect(
        interactionRepo.create({ ...baseInput, capture_context: 'on_load' as const })
      ).rejects.toBeInstanceOf(DuplicateInteractionError)
      expect(await interactionRepo.count()).toBe(1)
    })

    it('allows two different interactions in the same conversation', async () => {
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-multi',
        message_id: 'msg-turn-1',
        query: { text: 'What is GPT?' },
        response: { text: 'GPT stands for Generative Pre-trained Transformer.' },
      })
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-multi',
        message_id: 'msg-turn-2',
        query: { text: 'How does fine-tuning work?' },
        response: { text: 'Fine-tuning adjusts a model on task-specific data.' },
      })
      expect(await interactionRepo.count()).toBe(2)
    })

    it('does not conflate interactions with same text across different platforms', async () => {
      // Same query/response text on different platforms should have different fingerprints
      // because platform is part of the hash payload at all levels
      const queryText = 'What is the capital of France?'
      const responseText = 'Paris.'
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-chatgpt',
        message_id: 'msg-cross-1',
        query: { text: queryText },
        response: { text: responseText },
      })
      await interactionRepo.create({
        platform: 'gemini',
        conversation_id: 'conv-gemini',
        message_id: 'msg-cross-2',
        query: { text: queryText },
        response: { text: responseText },
      })
      expect(await interactionRepo.count()).toBe(2)
    })

    it('does not conflate interactions with same text across different conversations (Level 1)', async () => {
      // Level 1: Platform+ConvId+MsgId — different conversation IDs mean different fingerprints
      const queryText = 'What is fine-tuning?'
      const responseText = 'Fine-tuning adapts a model to a specific domain.'
      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'conv-a',
        message_id: 'msg-shared',
        query: { text: queryText },
        response: { text: responseText },
      })
      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'conv-b',
        message_id: 'msg-shared',
        query: { text: queryText },
        response: { text: responseText },
      })
      expect(await interactionRepo.count()).toBe(2)
    })

    it('handles concurrent saves for the same interaction: exactly one succeeds', async () => {
      const input = {
        platform: 'gemini',
        conversation_id: 'conv-concurrent',
        message_id: 'msg-concurrent',
        query: { text: 'Concurrent query' },
        response: { text: 'Concurrent response' },
      }
      const results = await Promise.allSettled([
        interactionRepo.create(input),
        interactionRepo.create(input),
      ])
      const succeeded = results.filter((r) => r.status === 'fulfilled').length
      const failed = results.filter((r) => r.status === 'rejected').length
      expect(succeeded).toBe(1)
      expect(failed).toBe(1)
      expect(await interactionRepo.count()).toBe(1)
    })
  })

  // ─── INTEGRITY REPORT ─────────────────────────────────────────────────────

  describe('Integrity report: accurate counts and zero duplicates', () => {
    it('interaction integrity report shows 0 duplicates when all records are unique', async () => {
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'c1',
        message_id: 'm1',
        query: { text: 'Q1' },
        response: { text: 'R1' },
      })
      await interactionRepo.create({
        platform: 'claude',
        conversation_id: 'c2',
        message_id: 'm2',
        query: { text: 'Q2' },
        response: { text: 'R2' },
      })
      await interactionRepo.create({
        platform: 'gemini',
        conversation_id: 'c3',
        message_id: 'm3',
        query: { text: 'Q3' },
        response: { text: 'R3' },
      })

      const report = await interactionRepo.getIntegrityReport()
      expect(report.total).toBe(3)
      expect(report.uniqueFingerprints).toBe(3)
      expect(report.duplicateFingerprints).toBe(0)
      expect(report.uniqueIds).toBe(3)
      expect(report.duplicateIds).toBe(0)
      expect(report.byPlatform['chatgpt']).toEqual({
        total: 1,
        uniqueFingerprints: 1,
        duplicateFingerprints: 0,
      })
    })

    it('conversation integrity report shows 0 duplicates across platforms', async () => {
      await conversationRepo.createOrUpdate({ id: 'conv-a', platform: 'chatgpt' })
      await conversationRepo.createOrUpdate({ id: 'conv-b', platform: 'chatgpt' })
      await conversationRepo.createOrUpdate({ id: 'conv-a', platform: 'claude' })
      // Simulate a page refresh (same conversation seen again)
      await conversationRepo.createOrUpdate({ id: 'conv-a', platform: 'chatgpt' })

      const report = await conversationRepo.getIntegrityReport()
      expect(report.total).toBe(3) // conv-a (chatgpt), conv-b (chatgpt), conv-a (claude)
      expect(report.unique).toBe(3)
      expect(report.duplicates).toBe(0)
    })

    it('interaction integrity report shows 0 duplicates after multiple refresh simulations', async () => {
      const input1 = {
        platform: 'chatgpt',
        conversation_id: 'conv-refresh',
        message_id: 'msg-1',
        query: { text: 'First query' },
        response: { text: 'First response' },
      }
      const input2 = {
        platform: 'chatgpt',
        conversation_id: 'conv-refresh',
        message_id: 'msg-2',
        query: { text: 'Second query' },
        response: { text: 'Second response' },
      }
      await interactionRepo.create(input1)
      await interactionRepo.create(input2)
      // Simulate on_load refresh attempts
      await expect(interactionRepo.create(input1)).rejects.toBeInstanceOf(DuplicateInteractionError)
      await expect(interactionRepo.create(input2)).rejects.toBeInstanceOf(DuplicateInteractionError)

      const report = await interactionRepo.getIntegrityReport()
      expect(report.total).toBe(2)
      expect(report.duplicateFingerprints).toBe(0)
    })

    it('integrity report is empty when database is empty', async () => {
      const interactionReport = await interactionRepo.getIntegrityReport()
      expect(interactionReport.total).toBe(0)
      expect(interactionReport.uniqueFingerprints).toBe(0)
      expect(interactionReport.duplicateFingerprints).toBe(0)

      const convReport = await conversationRepo.getIntegrityReport()
      expect(convReport.total).toBe(0)
      expect(convReport.unique).toBe(0)
      expect(convReport.duplicates).toBe(0)
    })
  })

  // ─── END-TO-END REFRESH SIMULATION ────────────────────────────────────────

  describe('End-to-end: refresh simulation across all platforms', () => {
    it('ChatGPT: refresh does not increase interaction count', async () => {
      const chatgptInput = {
        platform: 'chatgpt',
        conversation_id: 'chatgpt-conv-e2e',
        message_id: 'chatgpt-msg-e2e',
        query: { text: 'What is a neural network?' },
        response: { text: 'A neural network is a computational model.' },
      }
      await interactionRepo.create(chatgptInput)
      expect(await interactionRepo.count()).toBe(1)

      // Simulate page refresh: on_load sees the same interaction
      await expect(
        interactionRepo.create({ ...chatgptInput, capture_context: 'on_load' as const })
      ).rejects.toBeInstanceOf(DuplicateInteractionError)
      expect(await interactionRepo.count()).toBe(1)
    })

    it('Claude: refresh does not increase interaction count', async () => {
      const claudeInput = {
        platform: 'claude',
        conversation_id: 'claude-conv-e2e',
        message_id: 'claude-msg-e2e',
        query: { text: 'What is constitutional AI?' },
        response: {
          text: 'Constitutional AI is a training approach from Anthropic.',
        },
      }
      await interactionRepo.create(claudeInput)
      expect(await interactionRepo.count()).toBe(1)

      await expect(
        interactionRepo.create({ ...claudeInput, capture_context: 'on_load' as const })
      ).rejects.toBeInstanceOf(DuplicateInteractionError)
      expect(await interactionRepo.count()).toBe(1)
    })

    it('Gemini: refresh does not increase interaction count', async () => {
      const geminiInput = {
        platform: 'gemini',
        conversation_id: 'gemini-conv-e2e',
        message_id: 'gemini-msg-e2e',
        query: { text: 'What is multimodal AI?' },
        response: { text: 'Multimodal AI processes text, images, and audio.' },
      }
      await interactionRepo.create(geminiInput)
      expect(await interactionRepo.count()).toBe(1)

      await expect(
        interactionRepo.create({ ...geminiInput, capture_context: 'on_load' as const })
      ).rejects.toBeInstanceOf(DuplicateInteractionError)
      expect(await interactionRepo.count()).toBe(1)
    })

    it('new legitimate response in same conversation increases count by exactly one', async () => {
      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-growing',
        message_id: 'msg-turn-a',
        query: { text: 'Turn A query' },
        response: { text: 'Turn A response' },
      })
      expect(await interactionRepo.count()).toBe(1)

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-growing',
        message_id: 'msg-turn-b',
        query: { text: 'Turn B query' },
        response: { text: 'Turn B response' },
      })
      expect(await interactionRepo.count()).toBe(2)

      await interactionRepo.create({
        platform: 'chatgpt',
        conversation_id: 'conv-growing',
        message_id: 'msg-turn-c',
        query: { text: 'Turn C query' },
        response: { text: 'Turn C response' },
      })
      expect(await interactionRepo.count()).toBe(3)
    })

    it('conversation count does not increase on ChatGPT refresh', async () => {
      await conversationRepo.createOrUpdate({
        id: 'chatgpt-conv-refresh-test',
        platform: 'chatgpt',
        title: 'My Conversation',
      })
      expect(await conversationRepo.count()).toBe(1)

      await conversationRepo.createOrUpdate({
        id: 'chatgpt-conv-refresh-test',
        platform: 'chatgpt',
        title: 'My Conversation',
      })
      expect(await conversationRepo.count()).toBe(1)
    })

    it('conversation count does not increase on Claude refresh', async () => {
      await conversationRepo.createOrUpdate({ id: 'claude-conv-refresh', platform: 'claude' })
      expect(await conversationRepo.count()).toBe(1)
      await conversationRepo.createOrUpdate({ id: 'claude-conv-refresh', platform: 'claude' })
      await conversationRepo.createOrUpdate({ id: 'claude-conv-refresh', platform: 'claude' })
      expect(await conversationRepo.count()).toBe(1)
    })

    it('conversation count does not increase on Gemini refresh', async () => {
      await conversationRepo.createOrUpdate({ id: 'gemini-conv-refresh', platform: 'gemini' })
      expect(await conversationRepo.count()).toBe(1)
      await conversationRepo.createOrUpdate({ id: 'gemini-conv-refresh', platform: 'gemini' })
      expect(await conversationRepo.count()).toBe(1)
    })
  })
})
