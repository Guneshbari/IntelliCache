// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import {
  extractConversationTurns,
  extractModelInfo,
  isPageGenerating,
  pairTurnsIntoInteractions,
} from '../src/platforms/claude/parser'

describe('Claude DOM Fixtures Integration Tests', () => {
  it('correctly parses a multi-turn conversation with 2 distinct query-response pairs', () => {
    document.body.innerHTML = `
      <div class="conversation-container">
        <!-- Turn 1 User -->
        <div data-testid="user-message" data-message-id="u-turn-1">
          <div>What is cache stampede?</div>
        </div>
        <!-- Turn 1 Assistant -->
        <div data-testid="assistant-message" data-message-id="a-turn-1">
          <div class="font-claude-message">
            <p>Cache stampede happens when multiple requests miss the cache simultaneously.</p>
          </div>
        </div>
        <!-- Turn 2 User -->
        <div data-testid="user-message" data-message-id="u-turn-2">
          <div>How can we prevent it?</div>
        </div>
        <!-- Turn 2 Assistant -->
        <div data-testid="assistant-message" data-message-id="a-turn-2">
          <div class="font-claude-message">
            <p>You can prevent it using mutex locks or probabilistic early recomputation.</p>
          </div>
        </div>
      </div>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(4)
    expect(turns[0].role).toBe('user')
    expect(turns[1].role).toBe('assistant')
    expect(turns[2].role).toBe('user')
    expect(turns[3].role).toBe('assistant')

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: '6a8617f8-ce44-83ee-b5b6-72eb43d13516',
      title: 'Cache Stampede Mitigation',
      model: { provider: 'claude', name: 'Claude 3.5 Sonnet' },
    })

    expect(interactions).toHaveLength(2)
    expect(interactions[0].queryText).toBe('What is cache stampede?')
    expect(interactions[0].responseText).toBe(
      'Cache stampede happens when multiple requests miss the cache simultaneously.'
    )
    expect(interactions[0].userMessageId).toBe('u-turn-1')
    expect(interactions[0].messageId).toBe('a-turn-1')

    expect(interactions[1].queryText).toBe('How can we prevent it?')
    expect(interactions[1].responseText).toBe(
      'You can prevent it using mutex locks or probabilistic early recomputation.'
    )
    expect(interactions[1].userMessageId).toBe('u-turn-2')
    expect(interactions[1].messageId).toBe('a-turn-2')
  })

  it('correctly handles streaming in-progress states and rejects pairing until generation completes', () => {
    document.body.innerHTML = `
      <div>
        <div data-testid="user-message">
          <div>Write a quicksort implementation</div>
        </div>
        <div data-testid="assistant-message" class="streaming">
          <div class="font-claude-message">
            <p>def quicksort(arr):</p>
          </div>
        </div>
        <button aria-label="Stop Response">Stop</button>
      </div>
    `

    expect(isPageGenerating(document)).toBe(true)

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[1].isStreaming).toBe(true)

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-stream-test',
      title: 'Quicksort',
      model: { provider: 'claude', name: null },
    })

    expect(interactions).toHaveLength(0)
  })

  it('formats code blocks with language annotations and strips UI buttons', () => {
    document.body.innerHTML = `
      <div data-testid="user-message">
        <div>Show me a TypeScript interface for a cache entry</div>
      </div>
      <div data-testid="assistant-message">
        <div class="font-claude-message">
          <p>Here is the interface:</p>
          <pre><code class="language-typescript">interface CacheEntry&lt;T&gt; {
  key: string;
  value: T;
  ttl: number;
}</code></pre>
          <button aria-label="Copy">Copy</button>
          <button aria-label="Retry">Retry</button>
        </div>
      </div>
    `

    const turns = extractConversationTurns(document.body)
    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-code-test',
      title: 'TS Cache Interface',
      model: { provider: 'claude', name: 'Claude 3.5 Sonnet' },
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].responseText).toContain('Here is the interface:')
    expect(interactions[0].responseText).toContain(
      '```typescript\ninterface CacheEntry<T> {\n  key: string;\n  value: T;\n  ttl: number;\n}\n```'
    )
    expect(interactions[0].responseText).not.toContain('Copy')
    expect(interactions[0].responseText).not.toContain('Retry')
  })

  it('gracefully handles missing model selector and returns provider: claude, name: null', () => {
    document.body.innerHTML = `
      <div data-testid="user-message">
        <div>Simple question</div>
      </div>
      <div data-testid="assistant-message">
        <div class="font-claude-message">Simple answer</div>
      </div>
    `

    const model = extractModelInfo(document)
    expect(model.provider).toBe('claude')
    expect(model.name).toBeNull()
  })

  it('handles malformed or empty DOM without throwing', () => {
    document.body.innerHTML = `<div><span>Random noise</span></div>`
    const turns = extractConversationTurns(document.body)
    expect(turns).toEqual([])

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: null,
      title: null,
      model: { provider: 'claude', name: null },
    })
    expect(interactions).toEqual([])
  })
})
