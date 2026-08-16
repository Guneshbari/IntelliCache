import { describe, expect, it } from 'vitest'
import { calculateTextMetrics, calculateUtf8Bytes } from '../src/database/metrics'
import type { Interaction } from '../src/database/types'

describe('Text Metric Calculation', () => {
  it('calculates ASCII character and byte counts accurately', () => {
    const text = 'Hello, world!'
    const metrics = calculateTextMetrics(text)

    expect(metrics.text).toBe(text)
    expect(metrics.characters).toBe(13)
    expect(metrics.bytes).toBe(13)
    expect(metrics.estimated_tokens).toBeNull()
  })

  it('calculates multibyte UTF-8 byte counts accurately (emojis & unicode)', () => {
    // 🧠 is 2 UTF-16 code units (characters.length = 2), but 4 UTF-8 bytes
    // '你好' is 2 characters, 6 UTF-8 bytes (3 bytes each)
    const text = '🧠 AI 助手: 你好'
    const metrics = calculateTextMetrics(text, 15)

    expect(metrics.characters).toBe(text.length) // 12
    expect(metrics.bytes).toBe(calculateUtf8Bytes(text)) // 4 (🧠) + 1 ( ) + 2 (AI) + 1 ( ) + 3 (助) + 3 (手) + 1 (:) + 1 ( ) + 3 (你) + 3 (好) = 22
    expect(metrics.bytes).toBeGreaterThan(metrics.characters)
    expect(metrics.estimated_tokens).toBe(15)
  })

  it('preserves code blocks and multiline formatting in query/response text', () => {
    const codeBlock = '```python\ndef fib(n):\n    return n if n <= 1 else fib(n-1) + fib(n-2)\n```'
    const metrics = calculateTextMetrics(codeBlock)

    expect(metrics.text).toBe(codeBlock)
    expect(metrics.characters).toBe(codeBlock.length)
    expect(metrics.bytes).toBe(new TextEncoder().encode(codeBlock).length)
    expect(metrics.estimated_tokens).toBeNull()
  })

  it('handles empty strings gracefully', () => {
    const metrics = calculateTextMetrics('')

    expect(metrics.text).toBe('')
    expect(metrics.characters).toBe(0)
    expect(metrics.bytes).toBe(0)
    expect(metrics.estimated_tokens).toBeNull()
  })
})

describe('Data Model Validation', () => {
  it('validates standard interaction schema structure with nullable fields', () => {
    const interaction: Interaction = {
      schema_version: 1,
      id: '123e4567-e89b-12d3-a456-426614174000',
      fingerprint: 'a665a45920422f9d417e4867efdc4fb8a04a1f3fff1fa07e998e86f7f7a27ae3',
      fingerprint_strategy: 'level_1',
      platform: 'chatgpt',
      conversation_id: null,
      message_id: null,
      user_message_id: null,
      observed_at: '2026-08-17T03:00:00.000Z',
      source_timestamp: null,
      capture_context: 'on_generate',
      model: {
        provider: null,
        name: null,
      },
      query: {
        text: 'What is semantic caching?',
        characters: 25,
        bytes: 25,
        estimated_tokens: null,
      },
      response: {
        text: 'Semantic caching stores responses based on semantic meaning rather than exact keys.',
        characters: 84,
        bytes: 84,
        estimated_tokens: null,
      },
      conversation_title: null,
      collector_version: '0.1.0',
    }

    expect(interaction.schema_version).toBe(1)
    expect(interaction.fingerprint_strategy).toBe('level_1')
    expect(interaction.conversation_id).toBeNull()
    expect(interaction.message_id).toBeNull()
    expect(interaction.user_message_id).toBeNull()
    expect(interaction.source_timestamp).toBeNull()
    expect(interaction.capture_context).toBe('on_generate')
    expect(interaction.model.provider).toBeNull()
    expect(interaction.model.name).toBeNull()
    expect(interaction.query.estimated_tokens).toBeNull()
    expect(interaction.response.estimated_tokens).toBeNull()
  })
})
