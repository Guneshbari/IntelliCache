// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import {
  extractAssistantResponseText,
  extractConversationIdFromUrl,
  extractConversationTitle,
  extractConversationTurns,
  extractMessageId,
  extractModelInfo,
  extractSourceTimestamp,
  extractUserQueryText,
  isPageGenerating,
  isTurnStreaming,
  pairTurnsIntoInteractions,
} from '../src/platforms/gemini/parser'
import type { RawMessageTurn } from '../src/platforms/types'

describe('Gemini URL & Conversation ID Parser', () => {
  it('extracts conversation ID from standard Gemini app URLs', () => {
    const url = 'https://gemini.google.com/app/6a8617f8ce4483ee'
    expect(extractConversationIdFromUrl(url)).toBe('6a8617f8ce4483ee')
  })

  it('extracts conversation ID from Gemini chat URLs', () => {
    const url = 'https://gemini.google.com/chat/abc-gemini-789'
    expect(extractConversationIdFromUrl(url)).toBe('abc-gemini-789')
  })

  it('returns null for root or non-conversation URLs', () => {
    expect(extractConversationIdFromUrl('https://gemini.google.com/')).toBeNull()
    expect(extractConversationIdFromUrl('https://gemini.google.com/app')).toBeNull()
    expect(extractConversationIdFromUrl('https://google.com/')).toBeNull()
    expect(extractConversationIdFromUrl('invalid-url')).toBeNull()
  })
})

describe('Gemini Conversation Title & Model Extraction', () => {
  it('extracts clean conversation title from page title with suffix', () => {
    document.title = 'Semantic Embedding Distillation - Gemini'
    expect(extractConversationTitle(document)).toBe('Semantic Embedding Distillation')

    document.title = 'Distributed Systems Guide - Google Gemini'
    expect(extractConversationTitle(document)).toBe('Distributed Systems Guide')

    document.title = 'Vector Search Architecture | Gemini'
    expect(extractConversationTitle(document)).toBe('Vector Search Architecture')
  })

  it('returns null for default generic page titles', () => {
    document.title = 'Gemini'
    expect(extractConversationTitle(document)).toBeNull()

    document.title = 'Google Gemini'
    expect(extractConversationTitle(document)).toBeNull()

    document.title = 'New chat - Gemini'
    expect(extractConversationTitle(document)).toBeNull()
  })

  it('extracts model info when model selector is present', () => {
    document.body.innerHTML = `
      <div>
        <button data-testid="model-selector">Gemini 1.5 Pro</button>
      </div>
    `
    const model = extractModelInfo(document)
    expect(model.provider).toBe('google')
    expect(model.name).toBe('Gemini 1.5 Pro')
  })

  it('returns null model name when selector is absent', () => {
    document.body.innerHTML = `<div><main></main></div>`
    const model = extractModelInfo(document)
    expect(model.provider).toBe('google')
    expect(model.name).toBeNull()
  })
})

describe('Gemini Text & Turn Extraction', () => {
  it('extracts message ID if present, otherwise returns null', () => {
    const div = document.createElement('div')
    expect(extractMessageId(div)).toBeNull()

    div.setAttribute('data-message-id', 'msg-gemini-555')
    expect(extractMessageId(div)).toBe('msg-gemini-555')
  })

  it('extracts source timestamp if present, otherwise returns null', () => {
    const div = document.createElement('div')
    expect(extractSourceTimestamp(div)).toBeNull()

    div.innerHTML = `<time datetime="2026-08-20T03:30:00Z">3:30 AM</time>`
    expect(extractSourceTimestamp(div)).toBe('2026-08-20T03:30:00Z')
  })

  it('extracts user query text and strips button controls', () => {
    const userTurn = document.createElement('user-query')
    userTurn.innerHTML = `
      <div class="query-text">
        What is the difference between cosine similarity and Euclidean distance?
        <button aria-label="Copy">Copy</button>
      </div>
    `
    const text = extractUserQueryText(userTurn)
    expect(text).toBe('What is the difference between cosine similarity and Euclidean distance?')
  })

  it('extracts assistant response text and formats code blocks', () => {
    const asstTurn = document.createElement('model-response')
    asstTurn.innerHTML = `
      <div class="response-container">
        <p>Cosine similarity measures the angle between vectors:</p>
        <pre><code class="language-python">import numpy as np
def cosine_sim(a, b):
    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))</code></pre>
        <button aria-label="Copy code">Copy</button>
      </div>
    `
    const text = extractAssistantResponseText(asstTurn)
    expect(text).toContain('Cosine similarity measures the angle between vectors:')
    expect(text).toContain(
      '```python\nimport numpy as np\ndef cosine_sim(a, b):\n    return np.dot(a, b) / (np.linalg.norm(a) * np.linalg.norm(b))\n```'
    )
    expect(text).not.toContain('Copy code')
  })

  it('extracts conversation turns from document in order', () => {
    document.body.innerHTML = `
      <user-query><div class="query-text">Hello Gemini</div></user-query>
      <model-response><div class="response-container">Hello User</div></model-response>
    `
    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[0].role).toBe('user')
    expect(turns[0].text).toBe('Hello Gemini')
    expect(turns[1].role).toBe('assistant')
    expect(turns[1].text).toBe('Hello User')
  })
})

describe('Gemini Generation & Streaming Detection', () => {
  it('detects page is generating when stop button is present', () => {
    document.body.innerHTML = `
      <div>
        <button aria-label="Stop response">Stop</button>
      </div>
    `
    expect(isPageGenerating(document)).toBe(true)
  })

  it('detects page is generating when spinner/loading indicator is present', () => {
    document.body.innerHTML = `
      <div>
        <mat-spinner></mat-spinner>
      </div>
    `
    expect(isPageGenerating(document)).toBe(true)
  })

  it('detects page is idle when no streaming indicators exist', () => {
    document.body.innerHTML = `
      <div>
        <button aria-label="Send message">Send</button>
      </div>
    `
    expect(isPageGenerating(document)).toBe(false)
  })

  it('marks individual turn as streaming if loading or animating', () => {
    const turn = document.createElement('div')
    turn.classList.add('loading')
    expect(isTurnStreaming(turn)).toBe(true)
  })
})

describe('Gemini Turn Pairing Logic', () => {
  it('pairs sequential User and Assistant turns into an interaction with platform = gemini', () => {
    const userTurn: RawMessageTurn = {
      role: 'user',
      element: document.createElement('user-query'),
      text: 'Explain zero-shot classification.',
      messageId: 'u-gem-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const asstTurn: RawMessageTurn = {
      role: 'assistant',
      element: document.createElement('model-response'),
      text: 'Zero-shot classification classifies text without explicit fine-tuning on those classes.',
      messageId: 'a-gem-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const interactions = pairTurnsIntoInteractions([userTurn, asstTurn], {
      conversationId: 'gemini-conv-999',
      title: 'Zero Shot Classification',
      model: { provider: 'google', name: 'Gemini 1.5 Flash' },
      captureContext: 'on_generate',
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].platform).toBe('gemini')
    expect(interactions[0].conversationId).toBe('gemini-conv-999')
    expect(interactions[0].userMessageId).toBe('u-gem-1')
    expect(interactions[0].messageId).toBe('a-gem-1')
    expect(interactions[0].queryText).toBe('Explain zero-shot classification.')
    expect(interactions[0].responseText).toBe(
      'Zero-shot classification classifies text without explicit fine-tuning on those classes.'
    )
    expect(interactions[0].model.provider).toBe('google')
    expect(interactions[0].model.name).toBe('Gemini 1.5 Flash')
    expect(interactions[0].captureContext).toBe('on_generate')
  })

  it('does NOT pair turns when assistant is streaming', () => {
    const userTurn: RawMessageTurn = {
      role: 'user',
      element: document.createElement('user-query'),
      text: 'Write a poem',
      messageId: 'u-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const streamingAsstTurn: RawMessageTurn = {
      role: 'assistant',
      element: document.createElement('model-response'),
      text: 'The river flows...',
      messageId: 'a-1',
      sourceTimestamp: null,
      isStreaming: true,
    }

    const interactions = pairTurnsIntoInteractions([userTurn, streamingAsstTurn], {
      conversationId: 'conv-123',
      title: 'Poem',
      model: { provider: 'google', name: null },
    })

    expect(interactions).toHaveLength(0)
  })
})
