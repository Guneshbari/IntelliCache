import { describe, expect, it, vi } from 'vitest'
import {
  generateInteractionFingerprint,
  getHourlyBucket,
  sha256,
} from '../src/fingerprint/fingerprint'
import { normalizeTextForFingerprint } from '../src/fingerprint/normalize'

describe('Fingerprint Normalization Utility', () => {
  it('normalizes whitespace, newlines, and unicode composition deterministically', () => {
    const rawInput = '  Hello   World!\r\n\r\n\r\n\r\nThis   is a test.  \t\n'
    const normalized = normalizeTextForFingerprint(rawInput)

    expect(normalized).toBe('Hello World!\n\nThis is a test.')
  })

  it('does not mutate the original input variable', () => {
    const original = '   Original Text   '
    const copy = `${original}`
    normalizeTextForFingerprint(original)

    expect(original).toBe(copy)
  })

  it('handles empty and whitespace-only strings', () => {
    expect(normalizeTextForFingerprint('')).toBe('')
    expect(normalizeTextForFingerprint('   \n\t  ')).toBe('')
  })
})

describe('SHA-256 Hashing and Web Crypto API Integration', () => {
  it('computes exact SHA-256 hex strings for standard known test vectors', async () => {
    // "hello world"
    const hashHelloWorld = await sha256('hello world')
    expect(hashHelloWorld).toBe('b94d27b9934d3e08a52e52d7da7dabfac484efe37a5380ee9088f7ace2efcde9')

    // Empty string NIST test vector
    const hashEmpty = await sha256('')
    expect(hashEmpty).toBe('e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855')

    // Standard phrase NIST test vector
    const hashFox = await sha256('The quick brown fox jumps over the lazy dog')
    expect(hashFox).toBe('d7a8fbb307d7809469ca9abcb0082e4f8d5651e46d3cdb762d02d0bf37c9e592')
  })

  it('genuinely exercises crypto.subtle.digest via Web Crypto API', async () => {
    const digestSpy = vi.spyOn(crypto.subtle, 'digest')
    const hash = await sha256('test crypto execution')

    expect(digestSpy).toHaveBeenCalledWith('SHA-256', expect.any(Uint8Array))
    expect(hash).toHaveLength(64)
    digestSpy.mockRestore()
  })
})

describe('3-Tier Interaction Fingerprinting Strategy', () => {
  const baseInput = {
    platform: 'chatgpt',
    conversation_id: 'conv-12345',
    message_id: 'msg-67890',
    query_text: 'What is vector search?',
    response_text: 'Vector search indexes embeddings to find semantically similar items.',
    observed_at: '2026-08-17T03:30:00.000Z',
  }

  describe('Level 1 Strategy (Platform + Conversation ID + Message ID)', () => {
    it('uses level_1 when platform, conversation_id, and message_id are all available', async () => {
      const result = await generateInteractionFingerprint(baseInput)

      expect(result.strategy).toBe('level_1')
      expect(result.canonicalPayload).toBe('L1|chatgpt|conv-12345|msg-67890')
      expect(result.fingerprint).toHaveLength(64)
    })

    it('produces identical fingerprints for identical inputs', async () => {
      const res1 = await generateInteractionFingerprint(baseInput)
      const res2 = await generateInteractionFingerprint(baseInput)

      expect(res1.fingerprint).toBe(res2.fingerprint)
    })

    it('changes fingerprint when platform changes', async () => {
      const res1 = await generateInteractionFingerprint(baseInput)
      const res2 = await generateInteractionFingerprint({ ...baseInput, platform: 'claude' })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })

    it('changes fingerprint when conversation_id changes', async () => {
      const res1 = await generateInteractionFingerprint(baseInput)
      const res2 = await generateInteractionFingerprint({
        ...baseInput,
        conversation_id: 'conv-different',
      })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })

    it('changes fingerprint when message_id changes', async () => {
      const res1 = await generateInteractionFingerprint(baseInput)
      const res2 = await generateInteractionFingerprint({
        ...baseInput,
        message_id: 'msg-different',
      })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })
  })

  describe('Level 2 Strategy (Platform + Conversation ID + Content)', () => {
    const level2Input = {
      platform: 'claude',
      conversation_id: 'conv-abc-999',
      message_id: null,
      query_text: 'Explain LRU cache',
      response_text: 'LRU discards the least recently used items first.',
      observed_at: '2026-08-17T03:30:00.000Z',
    }

    it('uses level_2 when message_id is null/missing but conversation_id exists', async () => {
      const result = await generateInteractionFingerprint(level2Input)

      expect(result.strategy).toBe('level_2')
      expect(result.canonicalPayload).toBe(
        'L2|claude|conv-abc-999|Explain LRU cache|LRU discards the least recently used items first.'
      )
    })

    it('changes fingerprint when query text changes in Level 2', async () => {
      const res1 = await generateInteractionFingerprint(level2Input)
      const res2 = await generateInteractionFingerprint({
        ...level2Input,
        query_text: 'Explain LFU cache',
      })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })

    it('changes fingerprint when response text changes in Level 2', async () => {
      const res1 = await generateInteractionFingerprint(level2Input)
      const res2 = await generateInteractionFingerprint({
        ...level2Input,
        response_text: 'LFU discards the least frequently used items first.',
      })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })
  })

  describe('Level 3 Strategy (Platform + Content + Hourly Bucket)', () => {
    const level3Input = {
      platform: 'gemini',
      conversation_id: null,
      message_id: null,
      query_text: 'Quick question about algorithms',
      response_text: 'Here is an answer about algorithms.',
      observed_at: '2026-08-17T03:15:00.000Z',
    }

    it('uses level_3 when conversation_id and message_id are null', async () => {
      const result = await generateInteractionFingerprint(level3Input)

      expect(result.strategy).toBe('level_3')
      expect(result.canonicalPayload).toBe(
        'L3|gemini|Quick question about algorithms|Here is an answer about algorithms.|2026-08-17T03'
      )
    })

    it('produces identical fingerprints for queries in the same hourly bucket', async () => {
      const res1 = await generateInteractionFingerprint({
        ...level3Input,
        observed_at: '2026-08-17T03:10:00.000Z',
      })
      const res2 = await generateInteractionFingerprint({
        ...level3Input,
        observed_at: '2026-08-17T03:55:00.000Z',
      })

      expect(res1.fingerprint).toBe(res2.fingerprint)
    })

    it('produces different fingerprints for queries across different hour buckets', async () => {
      const res1 = await generateInteractionFingerprint({
        ...level3Input,
        observed_at: '2026-08-17T03:00:00.000Z',
      })
      const res2 = await generateInteractionFingerprint({
        ...level3Input,
        observed_at: '2026-08-17T05:00:00.000Z',
      })

      expect(res1.fingerprint).not.toBe(res2.fingerprint)
    })

    it('formats hourly buckets correctly', () => {
      expect(getHourlyBucket('2026-08-17T14:45:22.000Z')).toBe('2026-08-17T14')
    })
  })
})
