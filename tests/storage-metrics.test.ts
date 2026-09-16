import { readFileSync } from 'node:fs'
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import {
  measureConversationBytes,
  measureInteractionBytes,
  StorageMetricsService,
} from '../src/database/storage-metrics'
import type { Conversation, Interaction } from '../src/database/types'
import { createDbGetStorageMetricsMessage, isExtensionMessage } from '../src/shared/messages'
import { formatBytes, utf8ByteLength } from '../src/shared/storage-format'

function utf8(text: string): number {
  return new TextEncoder().encode(text).byteLength
}

describe('utf8ByteLength', () => {
  it('measures ASCII text as one byte per character', () => {
    expect(utf8ByteLength('Hello, world!')).toBe(13)
    expect(utf8ByteLength('intelliCache')).toBe(12)
  })

  it('measures multibyte Unicode text in UTF-8 bytes, not UTF-16 units', () => {
    // 🧠 = 4 UTF-8 bytes (2 UTF-16 units); 你 = 3 UTF-8 bytes each
    expect(utf8ByteLength('🧠')).toBe(4)
    expect(utf8ByteLength('你好')).toBe(6)
    expect(utf8ByteLength('🧠 AI 助手: 你好')).toBe(utf8('🧠 AI 助手: 你好'))
    expect(utf8ByteLength('café')).toBe(5)
  })

  it('returns 0 for empty strings', () => {
    expect(utf8ByteLength('')).toBe(0)
  })

  it('handles null, undefined, and non-string values safely', () => {
    expect(utf8ByteLength(null)).toBe(0)
    expect(utf8ByteLength(undefined)).toBe(0)
    expect(utf8ByteLength(12345)).toBe(0)
    expect(utf8ByteLength({ text: 'hi' })).toBe(0)
    expect(utf8ByteLength(['a'])).toBe(0)
  })

  it('matches TextEncoder output for mixed content', () => {
    const samples = [
      'SELECT * FROM interactions;',
      '```python\ndef fib(n):\n    return n\n```',
      '日本語テスト 🎉 mixed 123',
      'a'.repeat(1000),
    ]
    for (const s of samples) {
      expect(utf8ByteLength(s)).toBe(utf8(s))
    }
  })
})

describe('formatBytes', () => {
  it('formats byte counts below 1 KiB as bytes', () => {
    expect(formatBytes(0)).toBe('0 B')
    expect(formatBytes(1)).toBe('1 B')
    expect(formatBytes(512)).toBe('512 B')
    expect(formatBytes(1023)).toBe('1023 B')
  })

  it('formats kibibytes with binary units', () => {
    expect(formatBytes(1024)).toBe('1 KiB')
    expect(formatBytes(1536)).toBe('1.5 KiB')
    expect(formatBytes(212992)).toBe('208 KiB')
  })

  it('formats mebibytes and gibibytes', () => {
    expect(formatBytes(1024 * 1024)).toBe('1 MiB')
    expect(formatBytes(5 * 1024 * 1024)).toBe('5 MiB')
    expect(formatBytes(2 * 1024 * 1024 * 1024)).toBe('2 GiB')
    expect(formatBytes(1.25 * 1024 * 1024 * 1024)).toBe('1.3 GiB')
  })

  it('renders non-finite or negative input as 0 B', () => {
    expect(formatBytes(NaN)).toBe('0 B')
    expect(formatBytes(Infinity)).toBe('0 B')
    expect(formatBytes(-5)).toBe('0 B')
  })
})

describe('record size measurement', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository

  beforeEach(() => {
    const name = `storage-unit-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(name)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  it('splits interaction size into query, response, and metadata parts', async () => {
    const created = await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-1',
      query: { text: 'What is semantic caching?' },
      response: { text: 'Semantic caching stores responses by meaning. 🧠' },
      conversation_title: 'Caching Q&A',
    })

    const breakdown = measureInteractionBytes(created)
    expect(breakdown.queryBytes).toBe(utf8('What is semantic caching?'))
    expect(breakdown.responseBytes).toBe(utf8('Semantic caching stores responses by meaning. 🧠'))
    expect(breakdown.metadataBytes).toBeGreaterThan(0)
    expect(breakdown.totalBytes).toBe(
      breakdown.queryBytes + breakdown.responseBytes + breakdown.metadataBytes
    )
    // Metadata must include identifiers beyond the raw texts
    expect(breakdown.metadataBytes).toBeGreaterThan(utf8(created.id))
  })

  it('handles interactions with null nullable fields', async () => {
    const created = await interactionRepo.create({
      platform: 'claude',
      query: { text: 'hi' },
      response: { text: 'hello' },
    })
    expect(created.conversation_id).toBeNull()
    const breakdown = measureInteractionBytes(created)
    expect(breakdown.queryBytes).toBe(2)
    expect(breakdown.responseBytes).toBe(5)
    expect(breakdown.totalBytes).toBe(
      breakdown.queryBytes + breakdown.responseBytes + breakdown.metadataBytes
    )
  })

  it('measures conversation records across all persisted fields', async () => {
    const conv = await conversationRepo.createOrUpdate({
      id: 'chatgpt:conv-size-1',
      platform: 'chatgpt',
      title: 'Sizing convo ✅',
      observed_at: '2026-09-16T10:00:00.000Z',
    })
    const bytes = measureConversationBytes(conv)
    expect(bytes).toBe(
      utf8(conv.id) +
        utf8(conv.platform) +
        utf8(conv.title ?? '') +
        utf8(conv.first_observed_at) +
        utf8(conv.last_observed_at)
    )
    expect(bytes).toBeGreaterThan(0)
  })
})

describe('StorageMetricsService', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository

  beforeEach(() => {
    const name = `storage-svc-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(name)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
  })

  function serviceWithEstimate(estimate: unknown) {
    return new StorageMetricsService({
      db,
      estimateProvider: async () => estimate as { usage?: unknown; quota?: unknown },
    })
  }

  it('returns zeros with null browser values on an empty database', async () => {
    const metrics = await serviceWithEstimate(null).collect()
    expect(metrics.interactionCount).toBe(0)
    expect(metrics.conversationCount).toBe(0)
    expect(metrics.logicalDatasetBytes).toBe(0)
    expect(metrics.averageInteractionBytes).toBe(0)
    expect(metrics.queryBytes).toBe(0)
    expect(metrics.responseBytes).toBe(0)
    expect(metrics.metadataBytes).toBe(0)
    expect(metrics.browserUsageBytes).toBeNull()
    expect(metrics.browserQuotaBytes).toBeNull()
    expect(Number.isNaN(Date.parse(metrics.measuredAt))).toBe(false)
  })

  it('reflects persisted interactions and conversations with consistent totals', async () => {
    const a = await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'conv-a',
      query: { text: 'Query A' },
      response: { text: 'Response A is a bit longer.' },
      conversation_title: 'Thread A',
    })
    const b = await interactionRepo.create({
      platform: 'gemini',
      conversation_id: 'conv-b',
      query: { text: 'Query B with unicode ✅' },
      response: { text: 'Response B' },
    })
    await conversationRepo.createOrUpdate({
      id: a.conversation_id ?? 'chatgpt:conv-a',
      platform: 'chatgpt',
      title: 'Thread A',
      observed_at: a.observed_at,
    })

    const metrics = await serviceWithEstimate({ usage: 4096, quota: 10485760 }).collect()
    expect(metrics.interactionCount).toBe(2)
    expect(metrics.conversationCount).toBe(1)

    const expectedInteractionBytes =
      measureInteractionBytes(a).totalBytes + measureInteractionBytes(b).totalBytes
    const conv: Conversation | null = await conversationRepo.getById('conv-a', 'chatgpt')
    expect(conv).not.toBeNull()
    const expectedLogical =
      expectedInteractionBytes + measureConversationBytes(conv as Conversation)
    expect(metrics.logicalDatasetBytes).toBe(expectedLogical)
    expect(metrics.queryBytes + metrics.responseBytes + metrics.metadataBytes).toBe(
      metrics.logicalDatasetBytes
    )
    expect(metrics.averageInteractionBytes).toBe(metrics.logicalDatasetBytes / 2)
    expect(metrics.browserUsageBytes).toBe(4096)
    expect(metrics.browserQuotaBytes).toBe(10485760)
  })

  it('does not grow when a duplicate interaction is rejected', async () => {
    const payload = {
      platform: 'claude',
      conversation_id: 'conv-dup-size',
      message_id: 'msg-1',
      query: { text: 'Same query' },
      response: { text: 'Same response' },
    }
    await interactionRepo.create(payload)
    const before = await serviceWithEstimate(null).collect()

    await expect(interactionRepo.create(payload)).rejects.toThrow(/already exists/)
    const after = await serviceWithEstimate(null).collect()

    expect(after.interactionCount).toBe(before.interactionCount)
    expect(after.logicalDatasetBytes).toBe(before.logicalDatasetBytes)
  })

  it('grows when a new interaction is added and shrinks on deletion', async () => {
    await interactionRepo.create({
      platform: 'chatgpt',
      query: { text: 'First' },
      response: { text: 'First response' },
    })
    const baseline = await serviceWithEstimate(null).collect()

    const second: Interaction = await interactionRepo.create({
      platform: 'claude',
      query: { text: 'Second query, longer than the first' },
      response: { text: 'Second response' },
    })
    const grown = await serviceWithEstimate(null).collect()
    expect(grown.interactionCount).toBe(baseline.interactionCount + 1)
    expect(grown.logicalDatasetBytes).toBeGreaterThan(baseline.logicalDatasetBytes)

    await interactionRepo.deleteById(second.id)
    const shrunk = await serviceWithEstimate(null).collect()
    expect(shrunk.interactionCount).toBe(baseline.interactionCount)
    expect(shrunk.logicalDatasetBytes).toBe(baseline.logicalDatasetBytes)
  })

  it('returns null browser values when the estimate provider throws or is missing', async () => {
    const throwing = new StorageMetricsService({
      db,
      estimateProvider: async () => {
        throw new Error('storage.estimate denied')
      },
    })
    const a = await throwing.collect()
    expect(a.browserUsageBytes).toBeNull()
    expect(a.browserQuotaBytes).toBeNull()

    const missing = new StorageMetricsService({
      db,
      estimateProvider: async () => undefined,
    })
    const b = await missing.collect()
    expect(b.browserUsageBytes).toBeNull()
    expect(b.browserQuotaBytes).toBeNull()
  })

  it('ignores non-finite browser estimate values', async () => {
    const metrics = await serviceWithEstimate({ usage: NaN, quota: 'lots' }).collect()
    expect(metrics.browserUsageBytes).toBeNull()
    expect(metrics.browserQuotaBytes).toBeNull()
  })
})

describe('DB_GET_STORAGE_METRICS messaging', () => {
  it('creates a valid storage-metrics message accepted by the type guard', () => {
    const msg = createDbGetStorageMetricsMessage('popup')
    expect(msg.type).toBe('DB_GET_STORAGE_METRICS')
    expect(msg.sender).toBe('popup')
    expect(typeof msg.timestamp).toBe('number')
    expect(isExtensionMessage(msg)).toBe(true)
  })
})

describe('dashboard storage card contract', () => {
  const html = readFileSync(new URL('../src/popup/index.html', import.meta.url), 'utf8')
  const popup = readFileSync(new URL('../src/popup/popup.ts', import.meta.url), 'utf8')

  it('exposes all required storage element hooks', () => {
    for (const id of [
      'storage-dataset-val',
      'storage-usage-val',
      'storage-quota-val',
      'storage-interactions-val',
      'storage-conversations-val',
      'storage-avg-val',
      'storage-query-val',
      'storage-response-val',
      'storage-metadata-val',
      'storage-refresh-btn',
    ]) {
      expect(html).toContain(`id="${id}"`)
    }
  })

  it('uses estimate wording and never claims exact disk usage', () => {
    expect(html).toContain('Estimated dataset size')
    expect(html).toContain('Browser-reported usage')
    expect(html).toContain('Browser-reported quota')
    expect(html).toContain('Average interaction size')
    expect(html).not.toMatch(/exact disk usage/i)
  })

  it('popup controller requests and renders storage metrics', () => {
    expect(popup).toContain('createDbGetStorageMetricsMessage')
    expect(popup).toContain('storage-refresh-btn')
    expect(popup).toContain('renderStorageMetrics')
    expect(popup).not.toMatch(/exact disk usage/i)
  })
})
