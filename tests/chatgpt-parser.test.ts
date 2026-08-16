// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import {
  extractAssistantResponseText,
  extractConversationIdFromUrl,
  extractConversationTitle,
  extractConversationTurns,
  extractMessageId,
  extractModelInfo,
  extractUserQueryText,
  isTurnStreaming,
  pairTurnsIntoInteractions,
} from '../src/platforms/chatgpt/parser'
import type { RawMessageTurn } from '../src/platforms/types'

describe('ChatGPT URL & Conversation ID Parser', () => {
  it('extracts UUID conversation ID from standard ChatGPT URLs', () => {
    const url = 'https://chatgpt.com/c/6789abcd-1234-5678-90ab-cdef12345678'
    expect(extractConversationIdFromUrl(url)).toBe('6789abcd-1234-5678-90ab-cdef12345678')
  })

  it('extracts conversation ID from legacy chat.openai.com URLs', () => {
    const url = 'https://chat.openai.com/c/conv-legacy-uuid-999'
    expect(extractConversationIdFromUrl(url)).toBe('conv-legacy-uuid-999')
  })

  it('extracts conversation ID from custom GPT URLs', () => {
    const url = 'https://chatgpt.com/g/g-abc123-research-assistant/c/custom-gpt-conv-1'
    expect(extractConversationIdFromUrl(url)).toBe('custom-gpt-conv-1')
  })

  it('returns null for root or non-conversation URLs', () => {
    expect(extractConversationIdFromUrl('https://chatgpt.com/')).toBeNull()
    expect(
      extractConversationIdFromUrl('https://chatgpt.com/g/g-abc123-research-assistant')
    ).toBeNull()
    expect(extractConversationIdFromUrl('invalid-url')).toBeNull()
  })
})

describe('ChatGPT Conversation Title & Model Extraction', () => {
  it('extracts clean conversation title from page title', () => {
    document.title = 'Vector Caching Deep Dive - ChatGPT'
    expect(extractConversationTitle(document)).toBe('Vector Caching Deep Dive')
  })

  it('returns null for default generic page titles', () => {
    document.title = 'ChatGPT'
    expect(extractConversationTitle(document)).toBeNull()

    document.title = 'New chat - ChatGPT'
    expect(extractConversationTitle(document)).toBeNull()
  })

  it('extracts model info when switcher button is present', () => {
    document.body.innerHTML = `
      <div>
        <button data-testid="model-switcher-dropdown-button">ChatGPT 4o</button>
      </div>
    `
    const model = extractModelInfo(document)
    expect(model.provider).toBe('openai')
    expect(model.name).toBe('ChatGPT 4o')
  })

  it('returns null model name when switcher is absent', () => {
    document.body.innerHTML = `<div><main></main></div>`
    const model = extractModelInfo(document)
    expect(model.provider).toBe('openai')
    expect(model.name).toBeNull()
  })
})

describe('ChatGPT Message ID & Turn Text Extraction', () => {
  it('extracts message ID from data-message-id attribute', () => {
    const div = document.createElement('div')
    div.setAttribute('data-message-id', 'msg-uuid-1234')
    expect(extractMessageId(div)).toBe('msg-uuid-1234')
  })

  it('extracts user query text and strips button controls', () => {
    const userTurn = document.createElement('div')
    userTurn.setAttribute('data-message-author-role', 'user')
    userTurn.innerHTML = `
      <div class="whitespace-pre-wrap">
        What is the difference between semantic cache and exact key cache?
        <button aria-label="Edit message">Edit</button>
      </div>
    `
    const text = extractUserQueryText(userTurn)
    expect(text).toBe('What is the difference between semantic cache and exact key cache?')
  })

  it('extracts assistant response and preserves code blocks with language headers', () => {
    const assistantTurn = document.createElement('div')
    assistantTurn.setAttribute('data-message-author-role', 'assistant')
    assistantTurn.innerHTML = `
      <div class="markdown prose">
        <p>Here is an example in Python:</p>
        <pre><code class="language-python">def get_cache(key):
    return store.get(key)</code></pre>
        <p>And that completes the implementation.</p>
        <button data-testid="copy-turn-action-button">Copy</button>
        <button data-testid="good-response-turn-action-button">Good</button>
      </div>
    `
    const text = extractAssistantResponseText(assistantTurn)

    expect(text).toContain('Here is an example in Python:')
    expect(text).toContain('```python')
    expect(text).toContain('def get_cache(key):')
    expect(text).toContain('return store.get(key)')
    expect(text).toContain('```')
    expect(text).toContain('And that completes the implementation.')
    expect(text).not.toContain('Copy')
    expect(text).not.toContain('Good')
  })
})

describe('ChatGPT Streaming Detection & Turn Pairing', () => {
  it('detects streaming turn when result-streaming or stop button is present', () => {
    const turn = document.createElement('div')
    turn.className = 'result-streaming'
    expect(isTurnStreaming(turn)).toBe(true)

    document.body.innerHTML = `<button data-testid="stop-button">Stop generating</button>`
    const nonStreamingTurn = document.createElement('div')
    expect(isTurnStreaming(nonStreamingTurn, document.body)).toBe(true)
  })

  it('detects completed turn when stop button is gone', () => {
    document.body.innerHTML = `<button data-testid="send-button">Send</button>`
    const completedTurn = document.createElement('div')
    completedTurn.className = 'markdown prose'
    expect(isTurnStreaming(completedTurn, document.body)).toBe(false)
  })

  it('extracts conversation turns and pairs complete interactions correctly', () => {
    document.body.innerHTML = `
      <article data-testid="conversation-turn-0">
        <div data-message-author-role="user" data-message-id="msg-u1">
          <div class="whitespace-pre-wrap">Hello AI</div>
        </div>
      </article>
      <article data-testid="conversation-turn-1">
        <div data-message-author-role="assistant" data-message-id="msg-a1">
          <div class="markdown prose">
            <p>Hello! How can I assist you today?</p>
          </div>
        </div>
      </article>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[0].role).toBe('user')
    expect(turns[0].text).toBe('Hello AI')
    expect(turns[1].role).toBe('assistant')
    expect(turns[1].text).toContain('Hello! How can I assist you today?')

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-123',
      title: 'Greetings Chat',
      model: { provider: 'openai', name: 'GPT-4o' },
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].platform).toBe('chatgpt')
    expect(interactions[0].conversationId).toBe('conv-123')
    expect(interactions[0].messageId).toBe('msg-a1')
    expect(interactions[0].queryText).toBe('Hello AI')
    expect(interactions[0].responseText).toContain('Hello! How can I assist you today?')
    expect(interactions[0].model.name).toBe('GPT-4o')
  })

  it('does NOT pair assistant turn if it is currently streaming', () => {
    const turns: RawMessageTurn[] = [
      {
        role: 'user',
        element: document.createElement('div'),
        text: 'Tell me a story',
        messageId: 'u1',
        isStreaming: false,
      },
      {
        role: 'assistant',
        element: document.createElement('div'),
        text: 'Once upon a time...',
        messageId: 'a1',
        isStreaming: true, // Currently streaming!
      },
    ]

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-stream',
      title: 'Story',
      model: { provider: 'openai', name: null },
    })

    expect(interactions).toHaveLength(0) // Must NOT pair while streaming
  })
})
