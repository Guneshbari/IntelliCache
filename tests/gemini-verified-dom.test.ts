// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { diagnosticStats, logger, type DiagnosticLogLevel } from '../src/diagnostics'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import {
  extractAssistantResponseText,
  extractConversationIdFromUrl,
  extractConversationTurns,
  extractUserQueryText,
  pairTurnsIntoInteractions,
} from '../src/platforms/gemini/parser'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('Gemini Verified DOM Structure & Extraction Pipeline Tests', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let adapter: GeminiAdapter
  const originalChrome = globalThis.chrome
  let logSinkEntries: { level: DiagnosticLogLevel; message: string }[] = []

  const sampleVerifiedUserTurnHtml = `
    <user-query data-message-id="gem-u-101">
      <span class="user-query-container">
        <user-query-content>
          <div class="user-query-container">
            <div class="query-content">What is the difference between RAG and fine-tuning?</div>
          </div>
        </user-query-content>
      </span>
    </user-query>
  `

  const sampleVerifiedAssistantTurnHtml = `
    <model-response data-message-id="gem-a-101">
      <response-container>
        <div class="response-container-content">
          <div class="response-content">
            <structured-content-container>
              <div class="container">
                <message-content>
                  <div class="markdown markdown-main-panel md-content">
                    <p>RAG retrieves external knowledge dynamically while fine-tuning updates model weights.</p>
                    <pre><code class="language-python"># Example RAG pipeline
retrieved_docs = vector_db.search(query)
response = llm.generate(query, context=retrieved_docs)</code></pre>
                  </div>
                </message-content>
              </div>
            </structured-content-container>
          </div>
        </div>
      </response-container>
    </model-response>
  `

  beforeEach(() => {
    const testDbName = `test-gemini-verified-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)
    adapter = new GeminiAdapter({ newChatTimeoutMs: 100 })

    logSinkEntries = []
    logger.setSink((level, message) => {
      logSinkEntries.push({ level, message })
    })
    diagnosticStats.reset()

    // Mock chrome runtime to route DB_SAVE_INTERACTION to repository
    globalThis.chrome = {
      runtime: {
        lastError: undefined,
        sendMessage: vi.fn(
          (rawMessage: unknown, callback: (response: ExtensionResponse) => void) => {
            const msg = rawMessage as DbSaveInteractionMessage
            if (msg.type === 'DB_SAVE_INTERACTION') {
              void (async () => {
                try {
                  const created = await interactionRepo.create(msg.payload)
                  if (created.conversation_id) {
                    await conversationRepo.createOrUpdate({
                      id: created.conversation_id,
                      platform: created.platform,
                      title: created.conversation_title,
                      observed_at: created.observed_at,
                    })
                  }
                  callback(createSuccessResponse(created))
                } catch (err) {
                  callback(
                    createErrorResponse(
                      err instanceof Error ? err.message : 'Failed to save interaction'
                    )
                  )
                }
              })()
            } else {
              callback(createSuccessResponse({}))
            }
          }
        ),
      },
    } as unknown as typeof chrome
  })

  afterEach(async () => {
    adapter.stop()
    logger.setSink(null)
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
  })

  it('1 & 2. detects Gemini user-query and model-response elements in verified DOM', () => {
    document.body.innerHTML = `
      <main>
        ${sampleVerifiedUserTurnHtml}
        ${sampleVerifiedAssistantTurnHtml}
      </main>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(2)
    expect(turns[0].role).toBe('user')
    expect(turns[0].messageId).toBe('gem-u-101')
    expect(turns[1].role).toBe('assistant')
    expect(turns[1].messageId).toBe('gem-a-101')
  })

  it('3. extracts user text specifically from .query-content', () => {
    const container = document.createElement('div')
    container.innerHTML = sampleVerifiedUserTurnHtml
    const userQueryEl = container.querySelector('user-query')!

    const userText = extractUserQueryText(userQueryEl)
    expect(userText).toBe('What is the difference between RAG and fine-tuning?')
  })

  it('4. extracts assistant text specifically from message-content .markdown with code block formatting', () => {
    const container = document.createElement('div')
    container.innerHTML = sampleVerifiedAssistantTurnHtml
    const modelResponseEl = container.querySelector('model-response')!

    const asstText = extractAssistantResponseText(modelResponseEl)
    expect(asstText).toContain(
      'RAG retrieves external knowledge dynamically while fine-tuning updates model weights.'
    )
    expect(asstText).toContain('```python\n# Example RAG pipeline')
    expect(asstText).toContain('response = llm.generate(query, context=retrieved_docs)\n```')
  })

  it('5. produces a complete user/assistant pair from verified DOM structure', () => {
    document.body.innerHTML = `
      <main>
        ${sampleVerifiedUserTurnHtml}
        ${sampleVerifiedAssistantTurnHtml}
      </main>
    `

    const turns = extractConversationTurns(document.body)
    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'gemini-conv-abc',
      title: 'RAG vs Fine-tuning',
      model: { provider: 'google', name: 'Gemini 1.5 Pro' },
    })

    expect(interactions).toHaveLength(1)
    expect(interactions[0].platform).toBe('gemini')
    expect(interactions[0].conversationId).toBe('gemini-conv-abc')
    expect(interactions[0].userMessageId).toBe('gem-u-101')
    expect(interactions[0].messageId).toBe('gem-a-101')
    expect(interactions[0].queryText).toBe('What is the difference between RAG and fine-tuning?')
    expect(interactions[0].responseText).toContain('# Example RAG pipeline')
    expect(interactions[0].model.provider).toBe('google')
    expect(interactions[0].model.name).toBe('Gemini 1.5 Pro')
  })

  it('6. pairs multiple multi-turn exchanges correctly in document order', () => {
    document.body.innerHTML = `
      <main>
        <!-- Turn 1 -->
        <user-query data-message-id="u1">
          <div class="user-query-container"><div class="query-content">Prompt 1</div></div>
        </user-query>
        <model-response data-message-id="a1">
          <message-content><div class="markdown">Response 1</div></message-content>
        </model-response>

        <!-- Turn 2 -->
        <user-query data-message-id="u2">
          <div class="user-query-container"><div class="query-content">Prompt 2</div></div>
        </user-query>
        <model-response data-message-id="a2">
          <message-content><div class="markdown">Response 2</div></message-content>
        </model-response>
      </main>
    `

    const turns = extractConversationTurns(document.body)
    expect(turns).toHaveLength(4)

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: 'multi-turn-conv',
      title: 'Multi Turn',
      model: { provider: 'google', name: 'Gemini 1.5 Flash' },
    })

    expect(interactions).toHaveLength(2)
    expect(interactions[0].queryText).toBe('Prompt 1')
    expect(interactions[0].responseText).toBe('Response 1')
    expect(interactions[0].userMessageId).toBe('u1')
    expect(interactions[0].messageId).toBe('a1')

    expect(interactions[1].queryText).toBe('Prompt 2')
    expect(interactions[1].responseText).toBe('Response 2')
    expect(interactions[1].userMessageId).toBe('u2')
    expect(interactions[1].messageId).toBe('a2')
  })

  it('7. does NOT persist an incomplete or streaming response', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/streaming-conv-123'),
      writable: true,
    })

    document.body.innerHTML = `
      <main>
        <user-query>
          <div class="query-content">Generate a long essay</div>
        </user-query>
        <model-response class="streaming">
          <message-content><div class="markdown">Generating paragraph 1...</div></message-content>
        </model-response>
        <button aria-label="Stop response">Stop response</button>
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(0)
    expect(diagnosticStats.get('streamingDeferrals')).toBe(1)
  })

  it('8. extracts conversation ID from https://gemini.google.com/app/{id}', () => {
    expect(extractConversationIdFromUrl('https://gemini.google.com/app/6a8617f8ce4483ee')).toBe(
      '6a8617f8ce4483ee'
    )
    expect(extractConversationIdFromUrl('https://gemini.google.com/chat/gemini-789')).toBe(
      'gemini-789'
    )
  })

  it('9. treats /app without ID as unavailable and uses pending-queue behavior', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app'),
      writable: true,
    })

    document.body.innerHTML = `
      <main>
        ${sampleVerifiedUserTurnHtml}
        ${sampleVerifiedAssistantTurnHtml}
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    // Should be queued in pending buffer while conversationId is null
    expect(diagnosticStats.get('interactionsQueued')).toBe(1)

    // Wait for timeout to flush
    await new Promise((resolve) => setTimeout(resolve, 150))

    // After timeout, unbound interaction is saved with null conversationId
    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)
    expect(interactions[0].conversation_id).toBeNull()
    expect(interactions[0].platform).toBe('gemini')
  })

  it('10. persists valid Gemini interaction to IndexedDB end-to-end', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/conv-real-gemini-999'),
      writable: true,
    })
    document.title = 'RAG vs Fine-tuning - Google Gemini'

    document.body.innerHTML = `
      <main>
        ${sampleVerifiedUserTurnHtml}
        ${sampleVerifiedAssistantTurnHtml}
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)
    const saved = interactions[0]

    expect(saved.platform).toBe('gemini')
    expect(saved.conversation_id).toBe('gemini:conv-real-gemini-999')
    expect(saved.user_message_id).toBe('gem-u-101')
    expect(saved.message_id).toBe('gem-a-101')
    expect(saved.query.text).toBe('What is the difference between RAG and fine-tuning?')
    expect(saved.response.text).toContain('vector_db.search(query)')
    expect(saved.conversation_title).toBe('RAG vs Fine-tuning')

    // Verify conversation record
    const conv = await conversationRepo.getById('conv-real-gemini-999', 'gemini')
    expect(conv).not.toBeNull()
    expect(conv?.id).toBe('gemini:conv-real-gemini-999')
    expect(conv?.platform).toBe('gemini')
    expect(conv?.title).toBe('RAG vs Fine-tuning')

    // Verify diagnostic logs were emitted with exact specified format
    const domDiagLog = logSinkEntries.find((e) => e.message.includes('DOM diagnostics'))
    expect(domDiagLog).toBeDefined()
    expect(domDiagLog?.message).toContain(
      '[IntelliCache][Parser][GEMINI] DOM diagnostics | userQueries=1 | modelResponses=1 | userTexts=1 | assistantTexts=1 | completePairs=1'
    )

    const scanSummaryLog = logSinkEntries.find((e) => e.message.includes('SCAN SUMMARY'))
    expect(scanSummaryLog).toBeDefined()
    expect(scanSummaryLog?.message).toContain(
      '[IntelliCache][Adapter][GEMINI] SCAN SUMMARY | conversationId=yes | turnContainers=2 | userTurns=1 | assistantTurns=1 | completePairs=1 | generating=false | extracted=1 | queued=0 | saved=1 | duplicates=0 | failures=0'
    )
  })

  it('11. preserves existing deduplication behavior on repeated scans', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/conv-dedup-101'),
      writable: true,
    })

    document.body.innerHTML = `
      <main>
        ${sampleVerifiedUserTurnHtml}
        ${sampleVerifiedAssistantTurnHtml}
      </main>
    `

    adapter.start()
    await adapter.processConversation()

    let interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)

    // Run second processing pass on same DOM
    await adapter.processConversation()

    interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1) // Still exactly 1, deduplicated
  })
})
