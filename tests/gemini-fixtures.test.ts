// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import {
  extractConversationTurns,
  extractModelInfo,
  isPageGenerating,
  pairTurnsIntoInteractions,
} from '../src/platforms/gemini/parser'

describe('Gemini DOM Fixtures Integration Tests', () => {
  it('correctly parses a multi-turn conversation with custom elements', () => {
    document.body.innerHTML = `
      <div class="chat-history">
        <!-- Turn 1 -->
        <user-query data-message-id="u-gem-1">
          <div class="query-text">What is inverted index?</div>
        </user-query>
        <model-response data-message-id="a-gem-1">
          <div class="response-container">
            <p>An inverted index maps words to their location in documents.</p>
          </div>
        </model-response>

        <!-- Turn 2 -->
        <user-query data-message-id="u-gem-2">
          <div class="query-text">How does BM25 use it?</div>
        </user-query>
        <model-response data-message-id="a-gem-2">
          <div class="response-container">
            <p>BM25 calculates TF-IDF based term weights across inverted postings lists.</p>
          </div>
        </model-response>
      </div>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(4)
    expect(turns[0].role).toBe('user')
    expect(turns[1].role).toBe('assistant')
    expect(turns[2].role).toBe('user')
    expect(turns[3].role).toBe('assistant')

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'gemini-app-uuid-555',
      title: 'Information Retrieval with BM25',
      model: { provider: 'google', name: 'Gemini 1.5 Pro' },
    })

    expect(interactions).toHaveLength(2)
    expect(interactions[0].platform).toBe('gemini')
    expect(interactions[0].queryText).toBe('What is inverted index?')
    expect(interactions[0].responseText).toBe(
      'An inverted index maps words to their location in documents.'
    )
    expect(interactions[0].userMessageId).toBe('u-gem-1')
    expect(interactions[0].messageId).toBe('a-gem-1')

    expect(interactions[1].platform).toBe('gemini')
    expect(interactions[1].queryText).toBe('How does BM25 use it?')
    expect(interactions[1].responseText).toBe(
      'BM25 calculates TF-IDF based term weights across inverted postings lists.'
    )
    expect(interactions[1].userMessageId).toBe('u-gem-2')
    expect(interactions[1].messageId).toBe('a-gem-2')
  })

  it('correctly handles streaming in-progress states with stop button or spinner', () => {
    document.body.innerHTML = `
      <div>
        <user-query>
          <div>Calculate fibonacci series</div>
        </user-query>
        <model-response class="loading">
          <mat-spinner></mat-spinner>
          <div>Computing...</div>
        </model-response>
        <button aria-label="Stop response">Stop</button>
      </div>
    `

    expect(isPageGenerating(document)).toBe(true)

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[1].isStreaming).toBe(true)

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'conv-stream-gemini',
      title: 'Fibonacci',
      model: { provider: 'google', name: null },
    })

    expect(interactions).toHaveLength(0)
  })

  it('formats code blocks and strips Gemini citation badges and feedback controls', () => {
    document.body.innerHTML = `
      <user-query>
        <div>Show binary search in Go</div>
      </user-query>
      <model-response>
        <div class="response-container">
          <p>Here is binary search in Go:</p>
          <pre><code class="language-go">func binarySearch(arr []int, target int) int {
    low, high := 0, len(arr)-1
    for low <= high {
        mid := (low + high) / 2
        if arr[mid] == target {
            return mid
        } else if arr[mid] < target {
            low = mid + 1
        } else {
            high = mid - 1
        }
    }
    return -1
}</code></pre>
          <div class="citation">Source: golang.org</div>
          <button aria-label="Copy code">Copy</button>
          <button aria-label="Good response">Thumbs up</button>
        </div>
      </model-response>
    `

    const turns = extractConversationTurns(document.body)
    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'gemini-go-binary-search',
      title: 'Binary Search in Go',
      model: { provider: 'google', name: 'Gemini 1.5 Flash' },
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].responseText).toContain('Here is binary search in Go:')
    expect(interactions[0].responseText).toContain('```go\nfunc binarySearch(arr []int')
    expect(interactions[0].responseText).not.toContain('Copy code')
    expect(interactions[0].responseText).not.toContain('Thumbs up')
  })

  it('handles missing model selector gracefully (provider = google, name = null)', () => {
    document.body.innerHTML = `
      <user-query><div>Hi</div></user-query>
      <model-response><div>Hello</div></model-response>
    `

    const model = extractModelInfo(document)
    expect(model.provider).toBe('google')
    expect(model.name).toBeNull()
  })

  it('handles malformed DOM without throwing', () => {
    document.body.innerHTML = `<div><p>Empty page</p></div>`
    const turns = extractConversationTurns(document.body)
    expect(turns).toEqual([])
  })
})
