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
} from '../src/platforms/claude/parser'
import type { RawMessageTurn } from '../src/platforms/types'

describe('Claude URL & Conversation ID Parser', () => {
  it('extracts UUID conversation ID from standard Claude chat URLs', () => {
    const url = 'https://claude.ai/chat/6a8617f8-ce44-83ee-b5b6-72eb43d13516'
    expect(extractConversationIdFromUrl(url)).toBe('6a8617f8-ce44-83ee-b5b6-72eb43d13516')
  })

  it('extracts conversation ID from project chat Claude URLs', () => {
    const url = 'https://claude.ai/project/proj_12345/chat/abc-def-789'
    expect(extractConversationIdFromUrl(url)).toBe('abc-def-789')
  })

  it('returns null for root or non-conversation URLs', () => {
    expect(extractConversationIdFromUrl('https://claude.ai/')).toBeNull()
    expect(extractConversationIdFromUrl('https://claude.ai/new')).toBeNull()
    expect(extractConversationIdFromUrl('https://claude.ai/chats')).toBeNull()
    expect(extractConversationIdFromUrl('invalid-url')).toBeNull()
  })
})

describe('Claude Conversation Title & Model Extraction', () => {
  it('extracts clean conversation title from page title with suffix', () => {
    document.title = 'Distributed Caching Strategies - Claude'
    expect(extractConversationTitle(document)).toBe('Distributed Caching Strategies')

    document.title = 'Semantic Search Pipeline | Claude'
    expect(extractConversationTitle(document)).toBe('Semantic Search Pipeline')
  })

  it('returns null for default generic page titles', () => {
    document.title = 'Claude'
    expect(extractConversationTitle(document)).toBeNull()

    document.title = 'New chat - Claude'
    expect(extractConversationTitle(document)).toBeNull()

    document.title = 'Untitled | Claude'
    expect(extractConversationTitle(document)).toBeNull()
  })

  it('extracts model info when selector trigger is present', () => {
    document.body.innerHTML = `
      <div>
        <button data-testid="model-selector-trigger">Claude 3.5 Sonnet</button>
      </div>
    `
    const model = extractModelInfo(document)
    expect(model.provider).toBe('claude')
    expect(model.name).toBe('Claude 3.5 Sonnet')
  })

  it('returns null model name when selector is absent', () => {
    document.body.innerHTML = `<div><main></main></div>`
    const model = extractModelInfo(document)
    expect(model.provider).toBe('claude')
    expect(model.name).toBeNull()
  })
})

describe('Claude Text & Turn Extraction', () => {
  it('extracts message ID if present, otherwise returns null gracefully', () => {
    const div = document.createElement('div')
    expect(extractMessageId(div)).toBeNull()

    div.setAttribute('data-message-id', 'msg-claude-100')
    expect(extractMessageId(div)).toBe('msg-claude-100')
  })

  it('extracts source timestamp if present, otherwise returns null', () => {
    const div = document.createElement('div')
    expect(extractSourceTimestamp(div)).toBeNull()

    div.innerHTML = `<time datetime="2026-08-20T03:00:00Z">3:00 AM</time>`
    expect(extractSourceTimestamp(div)).toBe('2026-08-20T03:00:00Z')
  })

  it('extracts user query text and strips button controls', () => {
    const userTurn = document.createElement('div')
    userTurn.setAttribute('data-testid', 'user-message')
    userTurn.innerHTML = `
      <div>
        How does LRU eviction differ from LFU eviction?
        <button aria-label="Copy">Copy</button>
        <button aria-label="Edit">Edit</button>
      </div>
    `
    const text = extractUserQueryText(userTurn)
    expect(text).toBe('How does LRU eviction differ from LFU eviction?')
  })

  it('extracts assistant response text and formats code blocks', () => {
    const asstTurn = document.createElement('div')
    asstTurn.setAttribute('data-testid', 'assistant-message')
    asstTurn.innerHTML = `
      <div class="font-claude-message">
        <p>Here is an example in Python:</p>
        <pre><code class="language-python">def get_cached(key):
    return cache.get(key)</code></pre>
        <button aria-label="Copy">Copy</button>
      </div>
    `
    const text = extractAssistantResponseText(asstTurn)
    expect(text).toContain('Here is an example in Python:')
    expect(text).toContain('```python\ndef get_cached(key):\n    return cache.get(key)\n```')
    expect(text).not.toContain('Copy')
  })

  it('extracts conversation turns from document in order', () => {
    document.body.innerHTML = `
      <div data-testid="user-message"><div>Hello Claude</div></div>
      <div data-testid="assistant-message"><div class="font-claude-message">Hello User</div></div>
    `
    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[0].role).toBe('user')
    expect(turns[0].text).toBe('Hello Claude')
    expect(turns[1].role).toBe('assistant')
    expect(turns[1].text).toBe('Hello User')
  })
})

describe('Claude Generation & Streaming Detection', () => {
  it('detects page is generating when stop button is present', () => {
    document.body.innerHTML = `
      <div>
        <button aria-label="Stop Response">Stop</button>
      </div>
    `
    expect(isPageGenerating(document)).toBe(true)
  })

  it('detects page is generating when streaming indicator is present', () => {
    document.body.innerHTML = `
      <div>
        <div data-testid="streaming-indicator">Generating...</div>
      </div>
    `
    expect(isPageGenerating(document)).toBe(true)
  })

  it('detects page is idle when no streaming indicators exist', () => {
    document.body.innerHTML = `
      <div>
        <button aria-label="Send">Send</button>
      </div>
    `
    expect(isPageGenerating(document)).toBe(false)
  })

  it('marks individual turn as streaming if streaming indicator is inside it', () => {
    const turn = document.createElement('div')
    turn.classList.add('streaming')
    expect(isTurnStreaming(turn)).toBe(true)
  })
})

describe('Claude Turn Pairing Logic', () => {
  it('pairs sequential User and Assistant turns into an interaction with platform = claude', () => {
    const userTurn: RawMessageTurn = {
      role: 'user',
      element: document.createElement('div'),
      text: 'Explain cache warming.',
      messageId: 'u-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const asstTurn: RawMessageTurn = {
      role: 'assistant',
      element: document.createElement('div'),
      text: 'Cache warming pre-populates caches before peak load.',
      messageId: 'a-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const interactions = pairTurnsIntoInteractions([userTurn, asstTurn], {
      conversationId: 'conv-claude-123',
      title: 'Cache Warming',
      model: { provider: 'claude', name: 'Claude 3.5 Sonnet' },
      captureContext: 'on_generate',
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].platform).toBe('claude')
    expect(interactions[0].conversationId).toBe('conv-claude-123')
    expect(interactions[0].userMessageId).toBe('u-1')
    expect(interactions[0].messageId).toBe('a-1')
    expect(interactions[0].queryText).toBe('Explain cache warming.')
    expect(interactions[0].responseText).toBe(
      'Cache warming pre-populates caches before peak load.'
    )
    expect(interactions[0].model.provider).toBe('claude')
    expect(interactions[0].captureContext).toBe('on_generate')
  })

  it('does NOT pair turns when assistant is streaming', () => {
    const userTurn: RawMessageTurn = {
      role: 'user',
      element: document.createElement('div'),
      text: 'Explain cache invalidation.',
      messageId: 'u-1',
      sourceTimestamp: null,
      isStreaming: false,
    }

    const streamingAsstTurn: RawMessageTurn = {
      role: 'assistant',
      element: document.createElement('div'),
      text: 'Cache invalidation is hard...',
      messageId: 'a-1',
      sourceTimestamp: null,
      isStreaming: true,
    }

    const interactions = pairTurnsIntoInteractions([userTurn, streamingAsstTurn], {
      conversationId: 'conv-123',
      title: 'Invalidation',
      model: { provider: 'claude', name: null },
    })

    expect(interactions).toHaveLength(0)
  })
})
