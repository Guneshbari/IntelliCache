// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import { diagnosticStats, logger, type DiagnosticLogLevel } from '../src/diagnostics'
import { ChatGPTAdapter } from '../src/platforms/chatgpt/adapter'
import { ClaudeAdapter } from '../src/platforms/claude/adapter'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'
import { createErrorResponse, createSuccessResponse } from '../src/shared/messages'
import type { DbSaveInteractionMessage, ExtensionResponse } from '../src/shared/types'

describe('Diagnostic Instrumentation & Extraction Regression Tests', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let chatgptAdapter: ChatGPTAdapter
  let claudeAdapter: ClaudeAdapter
  let geminiAdapter: GeminiAdapter
  const originalChrome = globalThis.chrome
  let logSinkEntries: { level: DiagnosticLogLevel; message: string }[] = []

  beforeEach(() => {
    const testDbName = `test-diag-regression-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(testDbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

    chatgptAdapter = new ChatGPTAdapter()
    claudeAdapter = new ClaudeAdapter()
    geminiAdapter = new GeminiAdapter()

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
    chatgptAdapter.stop()
    claudeAdapter.stop()
    geminiAdapter.stop()
    logger.setSink(null)
    globalThis.chrome = originalChrome
    await db.delete()
    await closeDatabase()
  })

  it('preserves exact extracted interaction data integrity for ChatGPT under instrumentation', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://chatgpt.com/c/6789abcd-1111-2222-3333-444455556666'),
      writable: true,
    })
    document.title = 'TypeScript Generics - ChatGPT'

    document.body.innerHTML = `
      <main>
        <article data-testid="conversation-turn-1">
          <div data-message-author-role="user" data-message-id="chatgpt-u1">
            <div>How do I write a generic function in TypeScript?</div>
          </div>
        </article>
        <article data-testid="conversation-turn-2">
          <div data-message-author-role="assistant" data-message-id="chatgpt-a1">
            <div class="markdown">
              <p>Here is an example:</p>
              <pre><code class="language-typescript">function identity&lt;T&gt;(arg: T): T { return arg; }</code></pre>
            </div>
          </div>
        </article>
      </main>
    `

    chatgptAdapter.start()
    await chatgptAdapter.processConversation()

    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)
    const record = interactions[0]

    // Verify data contract integrity
    expect(record.platform).toBe('chatgpt')
    expect(record.conversation_id).toBe('chatgpt:6789abcd-1111-2222-3333-444455556666')
    expect(record.message_id).toBe('chatgpt-a1')
    expect(record.user_message_id).toBe('chatgpt-u1')
    expect(record.conversation_title).toBe('TypeScript Generics')
    expect(record.query.text).toBe('How do I write a generic function in TypeScript?')
    expect(record.response.text).toContain('function identity<T>(arg: T): T { return arg; }')
    expect(record.response.text).toContain('```typescript')
    expect(record.query.characters).toBe(record.query.text.length)
    expect(record.response.characters).toBe(record.response.text.length)
    expect(record.fingerprint_strategy).toBe('level_1')

    // Verify diagnostic counters
    expect(diagnosticStats.get('domScans')).toBeGreaterThanOrEqual(1)
    expect(diagnosticStats.get('userTurnsFound')).toBe(1)
    expect(diagnosticStats.get('assistantTurnsFound')).toBe(1)
    expect(diagnosticStats.get('completePairs')).toBe(1)
    expect(diagnosticStats.get('interactionsExtracted')).toBe(1)
    expect(diagnosticStats.get('interactionsSaved')).toBe(1)

    // Verify scan summary was logged
    const scanSummaryLog = logSinkEntries.find((entry) => entry.message.includes('SCAN SUMMARY'))
    expect(scanSummaryLog).toBeDefined()
    expect(scanSummaryLog?.message).toContain('[IntelliCache][Adapter][CHATGPT] SCAN SUMMARY')
    expect(scanSummaryLog?.message).toContain('conversationId=yes')
    expect(scanSummaryLog?.message).toContain('extracted=1')
    expect(scanSummaryLog?.message).toContain('saved=1')
  })

  it('produces full observable diagnostic trace for Claude', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://claude.ai/chat/claude-uuid-1234'),
      writable: true,
    })
    document.title = 'React Hooks Deep Dive - Claude'

    document.body.innerHTML = `
      <main>
        <div data-testid="user-message" data-message-id="claude-u1">
          <div>Explain useEffect dependencies in React</div>
        </div>
        <div data-testid="assistant-message" data-message-id="claude-a1">
          <div class="font-claude-message">
            <p>useEffect runs after render based on its dependency array:</p>
            <pre><code class="language-tsx">useEffect(() => { doWork(); }, [dep]);</code></pre>
          </div>
        </div>
      </main>
    `

    claudeAdapter.start()
    await claudeAdapter.processConversation()

    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)
    const record = interactions[0]

    expect(record.platform).toBe('claude')
    expect(record.conversation_id).toBe('claude:claude-uuid-1234')
    expect(record.message_id).toBe('claude-a1')
    expect(record.user_message_id).toBe('claude-u1')
    expect(record.conversation_title).toBe('React Hooks Deep Dive')
    expect(record.query.text).toBe('Explain useEffect dependencies in React')
    expect(record.response.text).toContain('```tsx')

    // Verify observable diagnostic trace presence
    const adapterStartLog = logSinkEntries.find((e) =>
      e.message.includes('Starting adapter lifecycle')
    )
    expect(adapterStartLog).toBeDefined()
    expect(adapterStartLog?.message).toContain('[IntelliCache][Adapter][CLAUDE]')

    const domScanLog = logSinkEntries.find(
      (e) => e.message.includes('DOM scan completed') || e.message.includes('DOM turns discovered')
    )
    expect(domScanLog).toBeDefined()
    expect(domScanLog?.message).toContain('[IntelliCache][Adapter][CLAUDE]')

    const extractionLog = logSinkEntries.find((e) =>
      e.message.includes('Extracted interaction turn')
    )
    expect(extractionLog).toBeDefined()
    expect(extractionLog?.message).toContain('[IntelliCache][Extraction][CLAUDE]')

    const dispatchLog = logSinkEntries.find((e) =>
      e.message.includes('Dispatching DB_SAVE_INTERACTION')
    )
    expect(dispatchLog).toBeDefined()
    expect(dispatchLog?.message).toContain('[IntelliCache][Messaging][CLAUDE]')

    const ackLog = logSinkEntries.find(
      (e) =>
        e.message.includes('Service worker acknowledged DB_SAVE_INTERACTION') ||
        e.message.includes('DB_SAVE_INTERACTION acknowledged successfully')
    )
    expect(ackLog).toBeDefined()
    expect(ackLog?.message).toContain('[IntelliCache][Messaging][CLAUDE]')

    const scanSummaryLog = logSinkEntries.find((e) => e.message.includes('SCAN SUMMARY'))
    expect(scanSummaryLog).toBeDefined()
    expect(scanSummaryLog?.message).toContain('[IntelliCache][Adapter][CLAUDE] SCAN SUMMARY')
  })

  it('produces full observable diagnostic trace for Gemini', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/gemini-conv-5678'),
      writable: true,
    })
    document.title = 'Python AsyncIO - Google Gemini'

    document.body.innerHTML = `
      <main>
        <div class="user-query-container" data-message-author-role="user" data-message-id="gemini-u1">
          <div>What is asyncio.gather in Python?</div>
        </div>
        <div class="model-response-container" data-message-author-role="assistant" data-message-id="gemini-a1">
          <div>
            <p>asyncio.gather runs multiple awaitables concurrently:</p>
            <pre><code class="language-python">results = await asyncio.gather(task1(), task2())</code></pre>
          </div>
        </div>
      </main>
    `

    geminiAdapter.start()
    await geminiAdapter.processConversation()

    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(1)
    const record = interactions[0]

    expect(record.platform).toBe('gemini')
    expect(record.conversation_id).toBe('gemini:gemini-conv-5678')
    expect(record.message_id).toBe('gemini-a1')
    expect(record.user_message_id).toBe('gemini-u1')
    expect(record.conversation_title).toBe('Python AsyncIO')
    expect(record.query.text).toBe('What is asyncio.gather in Python?')
    expect(record.response.text).toContain('```python')

    // Verify diagnostic logs
    const scanSummaryLog = logSinkEntries.find((e) => e.message.includes('SCAN SUMMARY'))
    expect(scanSummaryLog).toBeDefined()
    expect(scanSummaryLog?.message).toContain('[IntelliCache][Adapter][GEMINI] SCAN SUMMARY')
    expect(scanSummaryLog?.message).toContain('conversationId=yes')
    expect(scanSummaryLog?.message).toContain('extracted=1')
    expect(scanSummaryLog?.message).toContain('saved=1')
  })

  it('diagnoses streaming deferrals and updates streamingDeferrals counter', async () => {
    Object.defineProperty(window, 'location', {
      value: new URL('https://gemini.google.com/app/test-streaming'),
      writable: true,
    })

    document.body.innerHTML = `
      <main>
        <button aria-label="Stop response">Stop</button>
        <user-query>Generating turn prompt</user-query>
        <model-response class="loading">Incomplete answer...</model-response>
      </main>
    `

    geminiAdapter.start()
    await geminiAdapter.processConversation()

    // No interactions should be saved while streaming is active
    const interactions = await interactionRepo.getAll()
    expect(interactions).toHaveLength(0)

    // Streaming deferrals counter must be incremented
    expect(diagnosticStats.get('streamingDeferrals')).toBe(1)

    const deferralLog = logSinkEntries.find((e) => e.message.includes('Processing deferred'))
    expect(deferralLog).toBeDefined()
    expect(deferralLog?.message).toContain('[IntelliCache][Adapter][GEMINI]')
    expect(deferralLog?.level).toBe('debug')

    // Verify no warnings or errors were emitted for normal streaming deferral
    const warnOrErrorLogs = logSinkEntries.filter((e) => e.level === 'warn' || e.level === 'error')
    expect(warnOrErrorLogs).toHaveLength(0)
  })

  it('classifies transient parser states (0 turns, 0 user/asst turns, incomplete pairs) as debug and not warn/error', async () => {
    // 1. Test ChatGPT transient states
    logSinkEntries = []
    document.body.innerHTML = '<div></div>'
    chatgptAdapter.start()
    await chatgptAdapter.processConversation()

    let warns = logSinkEntries.filter((e) => e.level === 'warn' || e.level === 'error')
    expect(warns).toHaveLength(0)
    let debugScan = logSinkEntries.find((e) => e.message.includes('0 conversation turns found'))
    expect(debugScan?.level).toBe('debug')

    // 2. Test Claude transient states
    logSinkEntries = []
    document.body.innerHTML = '<div></div>'
    claudeAdapter.start()
    await claudeAdapter.processConversation()

    warns = logSinkEntries.filter((e) => e.level === 'warn' || e.level === 'error')
    expect(warns).toHaveLength(0)
    debugScan = logSinkEntries.find((e) => e.message.includes('0 conversation turns found'))
    expect(debugScan?.level).toBe('debug')

    // 3. Test Gemini transient states
    logSinkEntries = []
    document.body.innerHTML = '<div></div>'
    geminiAdapter.start()
    await geminiAdapter.processConversation()

    warns = logSinkEntries.filter((e) => e.level === 'warn' || e.level === 'error')
    expect(warns).toHaveLength(0)
    debugScan = logSinkEntries.find((e) => e.message.includes('0 conversation turns found'))
    expect(debugScan?.level).toBe('debug')
  })
})
