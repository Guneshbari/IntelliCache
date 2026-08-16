// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import {
  extractAssistantResponseText,
  extractConversationTurns,
  extractUserQueryText,
  pairTurnsIntoInteractions,
} from '../src/platforms/chatgpt/parser'

describe('ChatGPT DOM Fixture Tests', () => {
  beforeEach(() => {
    document.body.innerHTML = ''
  })

  it('correctly parses a multi-turn conversation with 2 distinct query-response pairs', () => {
    document.body.innerHTML = `
      <main>
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user" data-message-id="user-msg-1">
            <div class="whitespace-pre-wrap">First question: What is caching?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-msg-1">
            <div class="markdown prose">
              <p>Caching is a high-speed data storage layer that stores a subset of data.</p>
              <button data-testid="copy-turn-action-button">Copy</button>
            </div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="user" data-message-id="user-msg-2">
            <div class="whitespace-pre-wrap">Second question: What is eviction?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-3">
          <div data-message-author-role="assistant" data-message-id="asst-msg-2">
            <div class="markdown prose">
              <p>Eviction removes items when cache capacity is reached (e.g. LRU or LFU).</p>
              <button data-testid="copy-turn-action-button">Copy</button>
            </div>
          </div>
        </article>
      </main>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(4)

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-multi-123',
      title: 'Caching Principles',
      model: { provider: 'openai', name: 'GPT-4o' },
    })

    expect(interactions).toHaveLength(2)
    expect(interactions[0].queryText).toBe('First question: What is caching?')
    expect(interactions[0].responseText).toBe(
      'Caching is a high-speed data storage layer that stores a subset of data.'
    )
    expect(interactions[0].messageId).toBe('asst-msg-1')

    expect(interactions[1].queryText).toBe('Second question: What is eviction?')
    expect(interactions[1].responseText).toBe(
      'Eviction removes items when cache capacity is reached (e.g. LRU or LFU).'
    )
    expect(interactions[1].messageId).toBe('asst-msg-2')
  })

  it('handles streaming in-progress states and rejects pairing until generation completes', () => {
    // Intermediate State 1: Stop button present & result-streaming class
    document.body.innerHTML = `
      <main>
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user" data-message-id="user-msg-stream">
            <div class="whitespace-pre-wrap">Explain quantum computing</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-msg-stream" class="result-streaming">
            <div class="markdown prose">
              <p>Quantum computing uses qubits</p>
              <span class="streaming-cursor"></span>
            </div>
          </div>
        </article>
        <button data-testid="stop-button">Stop generating</button>
      </main>
    `

    let turns = extractConversationTurns(document.body)
    expect(turns[1].isStreaming).toBe(true)

    let interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-qc',
      title: 'Quantum',
      model: { provider: 'openai', name: 'o1' },
    })
    // Incomplete stream must not be paired
    expect(interactions).toHaveLength(0)

    // State 2: Stream completes (stop button replaced by send button, result-streaming removed)
    document.body.innerHTML = `
      <main>
        <article data-testid="conversation-turn-0">
          <div data-message-author-role="user" data-message-id="user-msg-stream">
            <div class="whitespace-pre-wrap">Explain quantum computing</div>
          </div>
        </article>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="assistant" data-message-id="asst-msg-stream">
            <div class="markdown prose">
              <p>Quantum computing uses qubits to perform complex calculations via superposition and entanglement.</p>
              <button data-testid="copy-turn-action-button">Copy</button>
            </div>
          </div>
        </article>
        <button data-testid="send-button">Send</button>
      </main>
    `

    turns = extractConversationTurns(document.body)
    expect(turns[1].isStreaming).toBe(false)

    interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-qc',
      title: 'Quantum',
      model: { provider: 'openai', name: 'o1' },
    })
    expect(interactions).toHaveLength(1)
    expect(interactions[0].responseText).toBe(
      'Quantum computing uses qubits to perform complex calculations via superposition and entanglement.'
    )
  })

  it('correctly formats and preserves multiple code blocks with various languages', () => {
    document.body.innerHTML = `
      <article>
        <div data-message-author-role="assistant">
          <div class="markdown prose">
            <p>Here are implementations in Python and TypeScript:</p>
            <pre><code class="language-python">def hash_key(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()</code></pre>
            <p>And in TypeScript:</p>
            <pre><code class="language-typescript">export async function hashKey(text: string): Promise&lt;string&gt; {
  const buf = await crypto.subtle.digest('SHA-256', new TextEncoder().encode(text));
  return Array.from(new Uint8Array(buf)).map(b => b.toString(16).padStart(2, '0')).join('');
}</code></pre>
            <p>Both return 64-character hex strings.</p>
          </div>
        </div>
      </article>
    `

    const asstElement = document.querySelector('[data-message-author-role="assistant"]')!
    const text = extractAssistantResponseText(asstElement)

    expect(text).toContain(
      '```python\ndef hash_key(text: str) -> str:\n    return hashlib.sha256(text.encode()).hexdigest()\n```'
    )
    expect(text).toContain(
      '```typescript\nexport async function hashKey(text: string): Promise<string> {'
    )
    expect(text).toContain('Both return 64-character hex strings.')
  })

  it('strips all ancillary UI action bars, feedback icons, and citations', () => {
    document.body.innerHTML = `
      <article>
        <div data-message-author-role="assistant">
          <div class="markdown prose">
            <p>The capital of France is Paris.</p>
            <button data-testid="copy-turn-action-button" aria-label="Copy">Copy</button>
            <button data-testid="good-response-turn-action-button" aria-label="Good response">👍</button>
            <button data-testid="bad-response-turn-action-button" aria-label="Bad response">👎</button>
            <button data-testid="voice-play-turn-action-button" aria-label="Read aloud">🔊</button>
            <div data-testid="web-search-sources">Sources: wikipedia.org</div>
            <form><button>Submit Feedback</button></form>
          </div>
        </div>
      </article>
    `

    const asstElement = document.querySelector('[data-message-author-role="assistant"]')!
    const text = extractAssistantResponseText(asstElement)

    expect(text).toBe('The capital of France is Paris.')
    expect(text).not.toContain('Copy')
    expect(text).not.toContain('👍')
    expect(text).not.toContain('Read aloud')
    expect(text).not.toContain('Submit Feedback')
  })

  it('handles missing optional metadata gracefully without failing extraction', () => {
    document.body.innerHTML = `
      <article>
        <div data-message-author-role="user">
          <div>Simple question without IDs</div>
        </div>
      </article>
      <article>
        <div data-message-author-role="assistant">
          <div class="markdown">
            <p>Simple answer without IDs</p>
          </div>
        </div>
      </article>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns[0].messageId).toBeNull()
    expect(turns[1].messageId).toBeNull()

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: null,
      title: null,
      model: { provider: 'openai', name: null },
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].conversationId).toBeNull()
    expect(interactions[0].messageId).toBeNull()
    expect(interactions[0].conversationTitle).toBeNull()
    expect(interactions[0].model.name).toBeNull()
    expect(interactions[0].queryText).toBe('Simple question without IDs')
    expect(interactions[0].responseText).toBe('Simple answer without IDs')
  })

  it('preserves multiline formatting in user queries', () => {
    document.body.innerHTML = `
      <div data-message-author-role="user">
        <div class="whitespace-pre-wrap">Line 1
Line 2
  Indented Line 3

Line 4</div>
      </div>
    `

    const userElement = document.querySelector('[data-message-author-role="user"]')!
    const text = extractUserQueryText(userElement)
    expect(text).toBe('Line 1\nLine 2\n  Indented Line 3\n\nLine 4')
  })
})
