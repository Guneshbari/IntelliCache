// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewayApiClient, GatewayApiError } from '../src/api-client'
import type {
  CacheStats,
  ChatMessage,
  ConversationMessageItem,
  NamespaceMetricsSnapshot,
  QueryResponse,
} from '../src/types'
import { DemoUIController } from '../src/ui-controller'
import { MessageRenderer } from '../src/ui/message-renderer'
import { MetricsController } from '../src/ui/metrics-controller'
import { SettingsController } from '../src/ui/settings-controller'

function setupTestDom(): void {
  document.body.innerHTML = `
    <header class="app-header">
      <div id="connection-badge" data-state="disconnected">
        <span id="connection-text">Disconnected</span>
      </div>
      <button id="btn-header-new-chat"></button>
      <button id="btn-header-settings"></button>
      <button id="btn-toggle-metrics"></button>
      <button id="btn-toggle-sidebar"></button>
      <button id="btn-close-sidebar"></button>
    </header>

    <div id="app-workspace">
      <div id="sidebar-backdrop"></div>
      <aside id="sidebar">
        <button id="btn-sidebar-new-chat"></button>
        <div id="conversation-list"></div>
      </aside>

      <section id="chat-area">
        <div id="messages-container"></div>
        <button id="btn-jump-latest" class="hidden"></button>
        <div id="composer-container">
          <div class="composer-options-row">
            <button id="mode-single" class="active"></button>
            <button id="mode-multi"></button>
            <input id="bypass-cache-checkbox" type="checkbox" />
            <input id="stream-response-checkbox" type="checkbox" />
          </div>
          <div id="turn-notice"></div>
          <textarea id="chat-textarea"></textarea>
          <button id="btn-send"></button>
          <button id="btn-stop" class="hidden"></button>
        </div>
      </section>

      <aside id="metrics-panel">
        <span id="metrics-namespace-badge">[—]</span>
        <span id="metrics-last-updated">Not refreshed</span>
        <button id="btn-refresh-metrics"></button>
        <div id="metrics-body"></div>
      </aside>
    </div>

    <div id="toast-container"></div>

    <div id="settings-backdrop"></div>
    <div id="settings-dialog">
      <button id="btn-close-settings"></button>
      <input id="settings-gateway-url" value="http://127.0.0.1:8000" />
      <input id="settings-client-token" type="password" />
      <button id="btn-settings-toggle-token"></button>
      <button id="btn-settings-connect"></button>
      <button id="btn-settings-disconnect"></button>
      <div id="settings-status-card"></div>
    </div>
  `
}

describe('MessageRenderer (UI Presentation & Diagnostics)', () => {
  it('renders empty scenario prompts when conversation history is empty', () => {
    const html = MessageRenderer.renderMessages([], false)
    expect(html).toContain('empty-state')
    expect(html).toContain('INTELLICACHE ARCHITECTURE DEMO')
    expect(html).toContain('Exact Cache Hit')
    expect(html).toContain('Semantic Cache Hit')
  })

  it('renders user message with safe escaping and author badge', () => {
    const userMsg: ChatMessage = {
      id: 'u1',
      role: 'user',
      content: '<script>alert(1)</script> User Question',
      timestamp: Date.now(),
    }

    const html = MessageRenderer.renderMessages([userMsg], false)
    expect(html).toContain('user-badge')
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt; User Question')
    expect(html).not.toContain('<script>')
  })

  it('renders assistant message with exact cache badge and diagnostics card', () => {
    const mockMeta: QueryResponse = {
      response: 'Deterministic cached response.',
      cached: true,
      cache_type: 'exact',
      model: 'llama-3.3-70b-versatile',
      latency_ms: 4.5,
      usage: { prompt_tokens: 10, completion_tokens: 20, total_tokens: 30 },
      cache_hit_count: 5,
      fingerprint: 'abcdef0123456789abcdef0123456789abcdef01',
      created_at: new Date().toISOString(),
    }

    const assistantMsg: ChatMessage = {
      id: 'a1',
      role: 'assistant',
      content: 'Deterministic cached response.',
      timestamp: Date.now(),
      responseMeta: mockMeta,
    }

    const html = MessageRenderer.renderMessages([assistantMsg], false)
    expect(html).toContain('⚡ Exact Cache Hit')
    expect(html).toContain('4.5 ms')
    expect(html).toContain('Hit Count:')
    expect(html).toContain('5')
    expect(html).toContain('SHA-256 Fingerprint:')
    expect(html).toContain('abcdef0123456789...')
    expect(html).toContain('btn-copy-response')
  })

  it('renders assistant message with semantic cache hit badge', () => {
    const mockMeta: QueryResponse = {
      response: 'Semantic match response.',
      cached: true,
      cache_type: 'semantic',
      model: 'llama-3.3-70b-versatile',
      latency_ms: 32.8,
      usage: { prompt_tokens: 12, completion_tokens: 25, total_tokens: 37 },
      cache_hit_count: 1,
      fingerprint: '1234567890abcdef1234567890abcdef12345678',
      created_at: new Date().toISOString(),
    }

    const assistantMsg: ChatMessage = {
      id: 'a2',
      role: 'assistant',
      content: 'Semantic match response.',
      timestamp: Date.now(),
      responseMeta: mockMeta,
    }

    const html = MessageRenderer.renderMessages([assistantMsg], false)
    expect(html).toContain('🧠 Semantic Cache Hit')
    expect(html).toContain('32.8 ms')
  })

  it('renders assistant message with idempotent replay badge', () => {
    const mockMeta: QueryResponse = {
      response: 'Deterministic replayed response.',
      cached: false,
      cache_type: 'none',
      idempotent_replay: true,
      model: 'llama-3.3-70b-versatile',
      latency_ms: 0.0,
      usage: { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 },
      cache_hit_count: 0,
      fingerprint: 'idempotent_replay',
      created_at: new Date().toISOString(),
    }

    const assistantMsg: ChatMessage = {
      id: 'a-replay',
      role: 'assistant',
      content: 'Deterministic replayed response.',
      timestamp: Date.now(),
      responseMeta: mockMeta,
    }

    const html = MessageRenderer.renderMessages([assistantMsg], false)
    expect(html).toContain('🔁 Idempotent Replay')
    expect(html).toContain('cache-badge replay')
  })

  it('renders unknown diagnostic badge and dashes for omitted historical diagnostics', () => {
    const historicalMeta: QueryResponse = {
      response: 'Historical restored response without diagnostic metadata.',
      cached: null,
      cache_type: 'none',
      model: '',
      latency_ms: null,
      usage: null,
      cache_hit_count: null,
      fingerprint: '',
      created_at: new Date().toISOString(),
    }

    const assistantMsg: ChatMessage = {
      id: 'a-hist-1',
      role: 'assistant',
      content: 'Historical restored response without diagnostic metadata.',
      timestamp: Date.now(),
      responseMeta: historicalMeta,
    }

    const html = MessageRenderer.renderMessages([assistantMsg], false)
    expect(html).toContain('cache-badge unknown')
    expect(html).toContain('ℹ️ Diagnostic History Unavailable')
    expect(html).toContain('<span class="cache-latency-pill latency-standard">—</span>')
    expect(html).toContain('<span class="diag-value highlight-accent">—</span>')
    expect(html).toContain('<span class="diag-value">—</span>')
    expect(html).toContain('<span class="diag-value">— / —</span>')
  })

  it('renders assistant message with upstream provider generation badge', () => {
    const mockMeta: QueryResponse = {
      response: 'Fresh upstream provider completion.',
      cached: false,
      cache_type: 'none',
      model: 'llama-3.3-70b-versatile',
      latency_ms: 1150.2,
      usage: { prompt_tokens: 25, completion_tokens: 120, total_tokens: 145 },
      cache_hit_count: 0,
      fingerprint: '9876543210fedcba9876543210fedcba98765432',
      created_at: new Date().toISOString(),
    }

    const assistantMsg: ChatMessage = {
      id: 'a3',
      role: 'assistant',
      content: 'Fresh upstream provider completion.',
      timestamp: Date.now(),
      responseMeta: mockMeta,
    }

    const html = MessageRenderer.renderMessages([assistantMsg], false)
    expect(html).toContain('🌐 Upstream Provider Generation')
    expect(html).toContain('1150.2 ms')
  })

  it('renders error row with retry button when message contains an error', () => {
    const errorMsg: ChatMessage = {
      id: 'e1',
      role: 'assistant',
      content: 'Error: Gateway returned HTTP status 503.',
      timestamp: Date.now(),
      error: 'Gateway returned HTTP status 503.',
    }

    const html = MessageRenderer.renderMessages([errorMsg], false)
    expect(html).toContain('⚠️ Gateway Error')
    expect(html).toContain('btn-retry')
    expect(html).toContain('data-retry-id="e1"')
  })

  it('renders loading state when isGenerating is true', () => {
    const html = MessageRenderer.renderMessages([], true)
    expect(html).toContain('loading-bubble')
    expect(html).toContain('Executing query on IntelliCache gateway...')
  })

  it('renders progressive streaming indicator with cursor and streaming pill when isStreaming is true', () => {
    const html = MessageRenderer.renderMessages([], true, true, 'Streaming in progress...')
    expect(html).toContain('streaming-row')
    expect(html).toContain('streaming-pill')
    expect(html).toContain('streaming')
    expect(html).toContain('streaming-cursor')
    expect(html).toContain('Streaming in progress...')
  })

  it('renders generation stopped state with stopped-badge and retry action when isCancelled is true', () => {
    const cancelledMsg: ChatMessage = {
      id: 'c1',
      role: 'assistant',
      content: 'Generation stopped by user.',
      timestamp: Date.now(),
      error: 'Generation stopped by user.',
      isCancelled: true,
    }

    const html = MessageRenderer.renderMessages([cancelledMsg], false)
    expect(html).toContain('cancelled-row')
    expect(html).toContain('stopped-badge')
    expect(html).toContain('⏹️ Generation Stopped')
    expect(html).toContain('cancelled-callout')
    expect(html).toContain('Generation stopped by user.')
    expect(html).toContain('btn-retry')
    expect(html).toContain('data-retry-id="c1"')
  })
})

describe('SettingsController (Connection & Memory Token Management)', () => {
  let apiClient: GatewayApiClient
  let settingsCtrl: SettingsController

  beforeEach(() => {
    setupTestDom()
    apiClient = new GatewayApiClient('http://127.0.0.1:8000')
    settingsCtrl = new SettingsController(apiClient, {
      onConnectionChange: vi.fn(),
      onClearCredentials: vi.fn(),
    })
    settingsCtrl.init()
  })

  it('opens and closes dialog with aria attributes', () => {
    expect(settingsCtrl.isDialogOpen()).toBe(false)
    settingsCtrl.open()
    expect(settingsCtrl.isDialogOpen()).toBe(true)

    const dialog = document.getElementById('settings-dialog')!
    expect(dialog.classList.contains('open')).toBe(true)

    settingsCtrl.close()
    expect(settingsCtrl.isDialogOpen()).toBe(false)
    expect(dialog.classList.contains('open')).toBe(false)
  })

  it('handles connect and updates status card on successful authentication', async () => {
    const testSpy = vi.spyOn(apiClient, 'testConnection').mockResolvedValueOnce({
      online: true,
      authenticated: true,
      namespace: 'tenant-research',
      serverVersion: '0.1.0',
      configuredModel: 'llama-3.3-70b-versatile',
      cacheBackend: 'postgres',
      cacheEnabled: true,
    })

    const tokenInput = document.getElementById('settings-client-token') as HTMLInputElement
    tokenInput.value = 'valid-token-secret'

    const result = await settingsCtrl.handleConnect()

    expect(testSpy).toHaveBeenCalledTimes(1)
    expect(result.authenticated).toBe(true)
    expect(result.namespace).toBe('tenant-research')

    const statusCard = document.getElementById('settings-status-card')!
    expect(statusCard.innerHTML).toContain('Connected [tenant-research]')
    expect(statusCard.innerHTML).toContain('llama-3.3-70b-versatile')
    expect(statusCard.innerHTML).toContain('POSTGRES')
  })

  it('handles disconnect by clearing token from memory and resetting input', () => {
    apiClient.setClientToken('secret-test-token')
    expect(apiClient.hasToken()).toBe(true)

    const tokenInput = document.getElementById('settings-client-token') as HTMLInputElement
    tokenInput.value = 'secret-test-token'

    settingsCtrl.handleDisconnect()

    expect(apiClient.hasToken()).toBe(false)
    expect(tokenInput.value).toBe('')

    const statusCard = document.getElementById('settings-status-card')!
    expect(statusCard.innerHTML).toContain('Disconnected. In-memory client token cleared.')
  })

  it('traps Tab navigation within open settings dialog and restores focus on close', () => {
    const triggerBtn = document.getElementById('btn-header-settings')!
    triggerBtn.focus()
    expect(document.activeElement).toBe(triggerBtn)

    settingsCtrl.open()
    expect(settingsCtrl.isDialogOpen()).toBe(true)

    // Focus is moved into preferred focusable element inside dialog (the URL input)
    const gatewayInput = document.getElementById('settings-gateway-url')!
    expect(document.activeElement).toBe(gatewayInput)

    // Dispatch Shift+Tab when on first element -> wraps to last focusable element
    const closeBtn = document.getElementById('btn-close-settings')!
    closeBtn.focus()
    expect(document.activeElement).toBe(closeBtn)

    const shiftTabEvent = new KeyboardEvent('keydown', {
      key: 'Tab',
      shiftKey: true,
      bubbles: true,
      cancelable: true,
    })
    document.dispatchEvent(shiftTabEvent)
    expect(shiftTabEvent.defaultPrevented).toBe(true)

    // Close settings dialog -> focus is restored to trigger button
    settingsCtrl.close()
    expect(settingsCtrl.isDialogOpen()).toBe(false)
    expect(document.activeElement).toBe(triggerBtn)
  })
})

describe('MetricsController (Namespace Telemetry & Cache Capacity)', () => {
  let apiClient: GatewayApiClient
  let metricsCtrl: MetricsController

  beforeEach(() => {
    setupTestDom()
    apiClient = new GatewayApiClient('http://127.0.0.1:8000')
    metricsCtrl = new MetricsController(apiClient)
    metricsCtrl.init()
  })

  it('renders unauthenticated notice when client token is missing', () => {
    metricsCtrl.renderInitialState()
    const body = document.getElementById('metrics-body')!
    expect(body.innerHTML).toContain('Authentication Required')
  })

  it('renders comprehensive telemetry when authenticated', async () => {
    apiClient.setClientToken('test-token')

    const mockSnapshot: NamespaceMetricsSnapshot = {
      scope: 'namespace',
      namespace: 'production-tenant',
      collection_uptime_seconds: 3600,
      timestamp: new Date().toISOString(),
      request_and_cache: {
        total_authenticated_requests: 150,
        exact_cache_lookups: 150,
        exact_cache_hits: 60,
        exact_cache_misses: 90,
        cache_write_failures: 0,
        semantic_eligibility_checks: 90,
        semantic_eligibility_rejections: 20,
        semantic_lookups: 70,
        semantic_hits: 25,
        semantic_misses: 45,
        provider_generation_attempts: 45,
        provider_generation_successes: 45,
        provider_generation_failures: 0,
        cache_bypass_requests: 5,
        streaming_requests: 0,
        coalesced_joins: 12,
        coalesced_successes: 12,
        coalesced_failures: 0,
        request_cancellations: 0,
      },
      provider_and_savings: {
        provider_calls_attempted: 45,
        provider_calls_succeeded: 45,
        provider_calls_failed: 0,
        provider_prompt_tokens_used: 1200,
        provider_completion_tokens_used: 3500,
        provider_total_tokens_used: 4700,
        prompt_tokens_saved: 4500,
        completion_tokens_saved: 12000,
        total_tokens_saved: 16500,
        provider_calls_avoided: 85,
      },
      semantic: {
        semantic_eligibility_rejections: 20,
        semantic_lookup_attempts: 70,
        semantic_hits: 25,
        semantic_misses: 45,
        ambiguous_candidate_rejections: 2,
        incompatible_candidate_rejections: 1,
        embedding_failures: 0,
        vector_store_failures: 0,
        indexing_successes: 45,
        indexing_failures: 0,
      },
      derived_rates: {
        exact_hit_rate: 0.4,
        semantic_hit_rate: 0.357,
        overall_cache_hit_rate: 0.567,
        coalesced_reuse_rate: 0.267,
      },
      latencies: {
        total_request_latency_ms: {
          sample_count: 150,
          min: 2.1,
          mean: 85.4,
          p50: 12.0,
          p95: 1100.0,
          p99: 1400.0,
          percentile_type: 'nearest_rank',
          small_sample_warning: false,
        },
        exact_lookup_latency_ms: {
          sample_count: 150,
          min: 1.0,
          mean: 3.5,
          p50: 3.2,
          p95: 5.8,
          p99: 8.0,
          percentile_type: 'nearest_rank',
          small_sample_warning: false,
        },
        semantic_lookup_latency_ms: {
          sample_count: 70,
          min: 15.0,
          mean: 34.2,
          p50: 31.0,
          p95: 48.0,
          p99: 60.0,
          percentile_type: 'nearest_rank',
          small_sample_warning: false,
        },
        provider_generation_latency_ms: {
          sample_count: 45,
          min: 800.0,
          mean: 1120.0,
          p50: 1050.0,
          p95: 1450.0,
          p99: 1800.0,
          percentile_type: 'nearest_rank',
          small_sample_warning: false,
        },
      },
    }

    const mockCacheStats: CacheStats = {
      total_entries: 450,
      max_entries: 10000,
      hits: 85,
      misses: 45,
      coalesced_requests: 12,
      total_requests: 150,
      hit_rate: 0.567,
      total_tokens_saved: 16500,
      total_latency_saved_ms: 95400.0,
      ttl_seconds: 86400,
      enabled: true,
      backend: 'postgres',
    }

    vi.spyOn(apiClient, 'fetchNamespaceMetrics').mockResolvedValueOnce(mockSnapshot)
    vi.spyOn(apiClient, 'fetchCacheStats').mockResolvedValueOnce(mockCacheStats)

    await metricsCtrl.refresh()

    const body = document.getElementById('metrics-body')!
    expect(body.innerHTML).toContain('Cache Capacity &amp; Backend')
    expect(body.innerHTML).toContain('POSTGRES')
    expect(body.innerHTML).toContain('450 / 10000')
    expect(body.innerHTML).toContain('Exact Cache Hits:')
    expect(body.innerHTML).toContain('60')
    expect(body.innerHTML).toContain('56.7%') // Overall cache hit rate
    expect(body.innerHTML).toContain('Total Tokens Saved:')
    expect(body.innerHTML).toContain('16,500')
    expect(body.innerHTML).toContain('SingleFlight Coalesced:')
    expect(body.innerHTML).toContain('12')
  })

  it('toggles collapsed state on the metrics panel', () => {
    expect(metricsCtrl.isPanelCollapsed()).toBe(false)
    metricsCtrl.toggleCollapse()
    expect(metricsCtrl.isPanelCollapsed()).toBe(true)

    const workspace = document.getElementById('app-workspace')!
    expect(workspace.classList.contains('metrics-collapsed')).toBe(true)
  })
})

describe('DemoUIController (End-to-End Coordination & Request Payloads)', () => {
  let apiClient: GatewayApiClient
  let uiController: DemoUIController

  beforeEach(() => {
    setupTestDom()
    apiClient = new GatewayApiClient('http://127.0.0.1:8000')
    uiController = new DemoUIController(apiClient)
    uiController.init()
  })

  it('constructs single-turn payload with prompt and omits conversation_id for semantic caching eligibility', async () => {
    const sendSpy = vi.spyOn(apiClient, 'sendQuery').mockResolvedValueOnce({
      response: 'Single turn answer',
      cached: false,
      model: 'test-model',
      latency_ms: 100,
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      cache_hit_count: 0,
      fingerprint: 'fp_123',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    uiController.setMode('single-turn')
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'What is the asyncio event loop?'

    await uiController.handleSend()

    expect(sendSpy).toHaveBeenCalledTimes(1)
    const payload = sendSpy.mock.calls[0][0]
    expect(payload.prompt).toBe('What is the asyncio event loop?')
    expect(payload.conversation_id).toBeUndefined()
    expect(payload.messages).toBeUndefined()
  })

  it('constructs multi-turn payload with prompt and active conversation_id without transmitting messages array', async () => {
    const sendSpy = vi.spyOn(apiClient, 'sendQuery').mockResolvedValueOnce({
      response: 'Multi turn answer',
      cached: false,
      model: 'test-model',
      latency_ms: 150,
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      cache_hit_count: 0,
      fingerprint: 'fp_456',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    uiController.setMode('multi-turn')
    const activeConvId = uiController.getConversationManager().getActiveConversationId()
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Explain state machines in TypeScript'

    await uiController.handleSend()

    expect(sendSpy).toHaveBeenCalledTimes(1)
    const payload = sendSpy.mock.calls[0][0]
    expect(payload.conversation_id).toBe(activeConvId)
    expect(payload.prompt).toBe('Explain state machines in TypeScript')
    expect(payload.messages).toBeUndefined()
    const idempotencyKey = sendSpy.mock.calls[0][1]
    expect(idempotencyKey).toBeDefined()
    expect(typeof idempotencyKey).toBe('string')
  })

  it('retries a failed query without duplicating the preceding user turn in conversation history', async () => {
    // 1. First send fails with an error
    vi.spyOn(apiClient, 'sendQuery').mockRejectedValueOnce(
      new Error('Gateway temporarily unavailable (503)')
    )

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'First attempt that fails'
    await uiController.handleSend()

    const manager = uiController.getConversationManager()
    const activeConv = manager.getActiveConversation()
    expect(activeConv.messages.length).toBe(2) // User turn + Assistant Error turn
    expect(activeConv.messages[0].role).toBe('user')
    expect(activeConv.messages[1].error).toBe('Gateway temporarily unavailable (503)')

    const failedMsgId = activeConv.messages[1].id

    // 2. Retry succeeds
    const sendSpy = vi.spyOn(apiClient, 'sendQuery')
    sendSpy.mockClear()
    sendSpy.mockResolvedValueOnce({
      response: 'Retried answer success',
      cached: false,
      model: 'test-model',
      latency_ms: 80,
      usage: { prompt_tokens: 5, completion_tokens: 10, total_tokens: 15 },
      cache_hit_count: 0,
      fingerprint: 'fp_retry',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await uiController.handleRetry(failedMsgId)

    expect(sendSpy).toHaveBeenCalledTimes(1)
    expect(activeConv.messages.length).toBe(2) // Still exactly 2: 1 user turn + 1 assistant success turn
    expect(activeConv.messages[0].role).toBe('user')
    expect(activeConv.messages[0].content).toBe('First attempt that fails')
    expect(activeConv.messages[1].role).toBe('assistant')
    expect(activeConv.messages[1].content).toBe('Retried answer success')
    expect(activeConv.messages[1].error).toBeUndefined()
  })

  it('creates a new server conversation on handleNewChat when connected', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const createSpy = vi.spyOn(apiClient, 'createConversation').mockResolvedValueOnce({
      id: 'server-conv-123',
      title: 'New Conversation',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })

    await uiController.handleNewChat()

    expect(createSpy).toHaveBeenCalledWith('New Conversation')
    const activeId = uiController.getConversationManager().getActiveConversationId()
    expect(activeId).toBe('server-conv-123')
  })

  it('initializes a server conversation on syncConversationsFromServer when server returns empty list', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    vi.spyOn(apiClient, 'listConversations').mockResolvedValueOnce({
      conversations: [],
      total: 0,
    })

    const createSpy = vi.spyOn(apiClient, 'createConversation').mockResolvedValueOnce({
      id: 'server-initial-conv-456',
      title: 'New Conversation',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })

    await uiController.syncConversationsFromServer()

    expect(createSpy).toHaveBeenCalledWith('New Conversation')
    expect(uiController.getConversationManager().getActiveConversationId()).toBe(
      'server-initial-conv-456'
    )
  })

  it('reuses the preceding turn idempotency key on handleRetry', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const promptText = 'Prompt with fixed idempotency key'
    const sendSpy = vi.spyOn(apiClient, 'sendQuery')

    // 1. Initial send fails
    sendSpy.mockRejectedValueOnce(new Error('Gateway timeout'))
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = promptText
    await uiController.handleSend()

    expect(sendSpy).toHaveBeenCalledTimes(1)
    const initialKey = sendSpy.mock.calls[0][1]
    expect(initialKey).toBeDefined()
    expect(typeof initialKey).toBe('string')

    // Check message store
    const activeConv = uiController.getConversationManager().getActiveConversation()
    expect(activeConv.messages[0].idempotencyKey).toBe(initialKey)
    const failedMsgId = activeConv.messages[1].id

    // 2. Retry succeeds and reuses the exact same idempotencyKey
    sendSpy.mockClear()
    sendSpy.mockResolvedValueOnce({
      response: 'Retried successfully',
      cached: false,
      model: 'test-model',
      latency_ms: 100,
      usage: { prompt_tokens: 5, completion_tokens: 10, total_tokens: 15 },
      cache_hit_count: 0,
      fingerprint: 'fp_retry_key',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await uiController.handleRetry(failedMsgId)

    expect(sendSpy).toHaveBeenCalledTimes(1)
    const retryKey = sendSpy.mock.calls[0][1]
    expect(retryKey).toBe(initialKey)
  })

  it('awaits server deletion and prevents local deletion if server deletion fails', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const deleteSpy = vi
      .spyOn(apiClient, 'deleteConversation')
      .mockRejectedValueOnce(new Error('Network error'))

    await uiController.handleDeleteConversation(activeConv.id)

    expect(deleteSpy).toHaveBeenCalledWith(activeConv.id)
    // Conversation should still exist locally because server deletion failed
    expect(uiController.getConversationManager().hasConversation(activeConv.id)).toBe(true)
  })

  it('creates replacement on server when the last conversation is deleted', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const deleteSpy = vi.spyOn(apiClient, 'deleteConversation').mockResolvedValueOnce(true)
    const createSpy = vi.spyOn(apiClient, 'createConversation').mockResolvedValueOnce({
      id: 'server-replacement-conv',
      title: 'New Conversation',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })

    await uiController.handleDeleteConversation(activeConv.id)

    expect(deleteSpy).toHaveBeenCalledWith(activeConv.id)
    expect(createSpy).toHaveBeenCalledWith('New Conversation')
    expect(uiController.getConversationManager().getActiveConversationId()).toBe(
      'server-replacement-conv'
    )
  })

  it('restores idempotencyKey and originalOptions on loadMessagesForActiveConversation after refresh', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: activeConv.id,
      messages: [
        {
          id: 'msg-restored-1',
          conversation_id: activeConv.id,
          turn_id: 'turn-restored-1',
          sequence_number: 1,
          role: 'user',
          content: 'Restored prompt after browser refresh',
          created_at: new Date().toISOString(),
          metadata: {
            idempotency_key: 'idemp-restored-key-999',
            options: {
              bypass_cache: true,
            },
          },
        },
      ],
      total: 1,
    })

    await uiController.loadMessagesForActiveConversation()

    const restoredMsg = uiController.getConversationManager().getActiveConversation().messages[0]
    expect(restoredMsg).toBeDefined()
    expect(restoredMsg.idempotencyKey).toBe('idemp-restored-key-999')
    expect(restoredMsg.originalOptions?.bypass_cache).toBe(true)
  })

  it('handleRetry preserves original bypass_cache option even if checkbox was changed', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const promptText = 'Prompt with original bypass false'
    const sendSpy = vi.spyOn(apiClient, 'sendQuery')

    // 1. Initial send with bypass = false fails
    const bypassCheckbox = document.getElementById('bypass-cache-checkbox') as HTMLInputElement
    bypassCheckbox.checked = false

    sendSpy.mockRejectedValueOnce(new Error('Provider timeout'))
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = promptText
    await uiController.handleSend()

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const failedMsgId = activeConv.messages[1].id

    // User subsequently toggles checkbox to true
    bypassCheckbox.checked = true

    // 2. Retry succeeds and must preserve bypass_cache = false from originalOptions
    sendSpy.mockClear()
    sendSpy.mockResolvedValueOnce({
      response: 'Retried successfully',
      cached: false,
      model: 'test-model',
      latency_ms: 50,
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      cache_hit_count: 0,
      fingerprint: 'fp_retry_preserved',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await uiController.handleRetry(failedMsgId)

    expect(sendSpy).toHaveBeenCalledTimes(1)
    const payload = sendSpy.mock.calls[0][0]
    expect(payload.bypass_cache).toBe(false)
  })

  it('restores failed turn as a retryable error message with failed attempt badge after refresh', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: activeConv.id,
      messages: [
        {
          id: 'msg-failed-user-1',
          conversation_id: activeConv.id,
          turn_id: 'turn-failed-1',
          sequence_number: 1,
          role: 'user',
          content: 'Failed prompt before refresh',
          created_at: new Date().toISOString(),
          turn_status: 'failed',
          turn_error_code: 'PROVIDER_TIMEOUT',
          metadata: {
            idempotency_key: 'idemp-failed-key-123',
            options: { bypass_cache: false },
          },
        },
      ],
      total: 1,
    })

    await uiController.loadMessagesForActiveConversation()

    const messages = uiController.getConversationManager().getActiveConversation().messages
    expect(messages.length).toBe(2)

    // User message has isFailedAttempt = true
    const userMsg = messages[0]
    expect(userMsg.role).toBe('user')
    expect(userMsg.isFailedAttempt).toBe(true)
    expect(userMsg.turnStatus).toBe('failed')
    expect(userMsg.idempotencyKey).toBe('idemp-failed-key-123')

    // Assistant error message was synthesized with retry button
    const errorMsg = messages[1]
    expect(errorMsg.role).toBe('assistant')
    expect(errorMsg.error).toContain('PROVIDER_TIMEOUT')

    uiController['renderMessages']()
    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('Failed Attempt')
    expect(container.innerHTML).toContain('Retry Query')
  })

  it('ensures visible conversation immediately after successful retry matches conversation after refresh', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const convId = activeConv.id

    // Initial state: A failed turn in the conversation
    const initialServerMessages: ConversationMessageItem[] = [
      {
        id: 'msg-user-attempt-1',
        conversation_id: convId,
        turn_id: 'turn-1',
        sequence_number: 1,
        role: 'user',
        content: 'Tell me about quantum computing',
        created_at: new Date(Date.now() - 60000).toISOString(),
        turn_status: 'failed',
        turn_error_code: 'PROVIDER_TIMEOUT',
        metadata: {
          idempotency_key: 'idemp-attempt-1',
          options: { bypass_cache: false },
        },
      },
    ]

    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: convId,
      messages: initialServerMessages,
      total: 1,
    })

    // Hydrate initially
    await uiController.loadMessagesForActiveConversation()
    const initialMessages = activeConv.messages
    expect(initialMessages.length).toBe(2) // 1 failed user message + 1 synthetic error card
    expect(initialMessages[0].isFailedAttempt).toBe(true)
    expect(initialMessages[1].error).toBeDefined()

    const errorCardId = initialMessages[1].id

    // Setup server messages after retry:
    // Original failed user turn (status: failed)
    // Retried user turn (status: completed)
    // Completed assistant turn (status: completed)
    const afterRetryServerMessages: ConversationMessageItem[] = [
      initialServerMessages[0],
      {
        id: 'msg-user-attempt-2',
        conversation_id: convId,
        turn_id: 'turn-2',
        sequence_number: 2,
        role: 'user',
        content: 'Tell me about quantum computing',
        created_at: new Date(Date.now() - 30000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
        metadata: {
          idempotency_key: 'idemp-attempt-1',
          options: { bypass_cache: false },
        },
      },
      {
        id: 'msg-assistant-turn-2',
        conversation_id: convId,
        turn_id: 'turn-2',
        sequence_number: 3,
        role: 'assistant',
        content: 'Quantum computing uses qubits.',
        created_at: new Date(Date.now() - 25000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
        metadata: {
          cached: false,
          cache_type: 'none',
          model: 'test-model',
          fingerprint: 'fp_retry_quantum',
        },
      },
    ]

    vi.spyOn(apiClient, 'sendQuery').mockResolvedValueOnce({
      response: 'Quantum computing uses qubits.',
      cached: false,
      model: 'test-model',
      latency_ms: 120,
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      cache_hit_count: 0,
      fingerprint: 'fp_retry_quantum',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    // When handleRetry calls loadMessagesForActiveConversation, it fetches afterRetryServerMessages
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: convId,
      messages: afterRetryServerMessages,
      total: 3,
    })

    // Execute retry
    await uiController.handleRetry(errorCardId)

    // Render immediately after retry
    uiController['renderMessages']()
    const container = document.getElementById('messages-container')!
    const liveHtmlAfterRetry = container.innerHTML
    const liveMessagesAfterRetry = [...activeConv.messages]

    // Verify immediate state:
    // 3 messages: failed attempt user, retried user, assistant response
    expect(liveMessagesAfterRetry.length).toBe(3)
    expect(liveMessagesAfterRetry[0].isFailedAttempt).toBe(true)
    expect(liveMessagesAfterRetry[0].id).toBe('msg-user-attempt-1')
    expect(liveMessagesAfterRetry[1].role).toBe('user')
    expect(liveMessagesAfterRetry[1].id).toBe('msg-user-attempt-2')
    expect(liveMessagesAfterRetry[2].role).toBe('assistant')
    expect(liveMessagesAfterRetry[2].content).toBe('Quantum computing uses qubits.')
    expect(liveHtmlAfterRetry).toContain('Failed Attempt')

    // Now simulate refresh / reload:
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: convId,
      messages: afterRetryServerMessages,
      total: 3,
    })
    await uiController.loadMessagesForActiveConversation()
    uiController['renderMessages']()
    const reloadedHtml = container.innerHTML
    const reloadedMessages = [...activeConv.messages]

    // Verify that live state after retry matches reloaded state after refresh
    expect(reloadedMessages.length).toBe(liveMessagesAfterRetry.length)
    expect(
      reloadedMessages.map((m) => ({
        role: m.role,
        content: m.content,
        isFailed: m.isFailedAttempt,
      }))
    ).toEqual(
      liveMessagesAfterRetry.map((m) => ({
        role: m.role,
        content: m.content,
        isFailed: m.isFailedAttempt,
      }))
    )
    expect(reloadedHtml).toBe(liveHtmlAfterRetry)
  })

  it('prevents race condition when switching conversation during retry', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const manager = uiController.getConversationManager()
    const convA = manager.getActiveConversation()
    const convAId = convA.id

    // Create Conversation B
    const convB = manager.createConversation('Conversation B')
    const convBId = convB.id

    // Switch back to Conversation A
    manager.setActiveConversation(convAId)

    // Setup failed turn in Conversation A
    const failedMessagesA: ConversationMessageItem[] = [
      {
        id: 'msg-user-a',
        conversation_id: convAId,
        turn_id: 'turn-a-1',
        sequence_number: 1,
        role: 'user',
        content: 'Question for Conversation A',
        created_at: new Date(Date.now() - 60000).toISOString(),
        turn_status: 'failed',
        turn_error_code: 'PROVIDER_ERROR',
        metadata: { idempotency_key: 'idemp-a' },
      },
    ]

    vi.spyOn(apiClient, 'getConversationMessages').mockImplementation(async (id: string) => {
      if (id === convAId) {
        return {
          conversation_id: convAId,
          messages: failedMessagesA,
          total: 1,
        }
      }
      return { conversation_id: id, messages: [], total: 0 }
    })

    await uiController.loadMessagesForActiveConversation()
    const initialMessagesA = convA.messages
    expect(initialMessagesA.length).toBe(2) // failed user + assistant error card
    const errorCardId = initialMessagesA[1].id

    // Setup sendQuery mock that switches to Conversation B while in-flight!
    vi.spyOn(apiClient, 'sendQuery').mockImplementation(async () => {
      // Simulate user switching to Conversation B while query is in flight
      manager.setActiveConversation(convBId)
      return {
        response: 'Answer for Conversation A',
        cached: false,
        model: 'test-model',
        latency_ms: 100,
        usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
        cache_hit_count: 0,
        fingerprint: 'fp_a',
        created_at: new Date().toISOString(),
        cache_type: 'none',
      }
    })

    // After retry completes, getConversationMessages for convA returns the completed turn
    vi.spyOn(apiClient, 'getConversationMessages').mockImplementation(async (id: string) => {
      if (id === convAId) {
        return {
          conversation_id: convAId,
          messages: [
            failedMessagesA[0],
            {
              id: 'msg-user-a-retry',
              conversation_id: convAId,
              turn_id: 'turn-a-2',
              sequence_number: 2,
              role: 'user',
              content: 'Question for Conversation A',
              created_at: new Date().toISOString(),
              turn_status: 'completed',
              metadata: { idempotency_key: 'idemp-a' },
            },
            {
              id: 'msg-assistant-a-2',
              conversation_id: convAId,
              turn_id: 'turn-a-2',
              sequence_number: 3,
              role: 'assistant',
              content: 'Answer for Conversation A',
              created_at: new Date().toISOString(),
              turn_status: 'completed',
              metadata: { model: 'test-model', cached: false },
            },
          ],
          total: 3,
        }
      }
      return { conversation_id: id, messages: [], total: 0 }
    })

    // Execute retry from Conversation A (which switches active conversation to convB mid-flight)
    await uiController.handleRetry(errorCardId)

    // Verify Conversation B was NOT polluted
    expect(manager.getActiveConversationId()).toBe(convBId)
    const messagesB = manager.getConversation(convBId)!.messages
    expect(messagesB.length).toBe(0) // Conversation B remains clean

    // Verify Conversation A received the reconciled messages
    const messagesA = manager.getConversation(convAId)!.messages
    expect(messagesA.length).toBe(3)
    expect(messagesA[0].isFailedAttempt).toBe(true)
    expect(messagesA[1].content).toBe('Question for Conversation A')
    expect(messagesA[2].content).toBe('Answer for Conversation A')
  })

  it('preserves full cache diagnostics (cached, cache_type, latency, usage) after reloading conversation', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const convId = activeConv.id

    const serverMessages: ConversationMessageItem[] = [
      {
        id: 'msg-user-sem-1',
        conversation_id: convId,
        turn_id: 'turn-sem-1',
        sequence_number: 1,
        role: 'user',
        content: 'Tell me about distributed caching',
        created_at: new Date(Date.now() - 10000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
      },
      {
        id: 'msg-assistant-sem-1',
        conversation_id: convId,
        turn_id: 'turn-sem-1',
        sequence_number: 2,
        role: 'assistant',
        content: 'Distributed caching partitions cached values across cluster nodes.',
        created_at: new Date(Date.now() - 9000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
        metadata: {
          cached: true,
          cache_type: 'semantic',
          latency_ms: 18.5,
          cache_hit_count: 2,
          model: 'llama-3.3-70b-versatile',
          fingerprint: 'fp_semantic_dist_cache',
          context_status: 'complete',
          usage: { prompt_tokens: 25, completion_tokens: 35, total_tokens: 60 },
        },
      },
    ]

    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: convId,
      messages: serverMessages,
      total: 2,
    })

    await uiController.loadMessagesForActiveConversation()

    const messages = activeConv.messages
    expect(messages.length).toBe(2)

    const assistantMsg = messages[1]
    expect(assistantMsg.role).toBe('assistant')
    expect(assistantMsg.responseMeta).toBeDefined()

    const meta = assistantMsg.responseMeta!
    // Crucial: Must be preserved as an actual semantic cache hit, not defaulted to false / 0!
    expect(meta.cached).toBe(true)
    expect(meta.cache_type).toBe('semantic')
    expect(meta.latency_ms).toBe(18.5)
    expect(meta.cache_hit_count).toBe(2)
    expect(meta.model).toBe('llama-3.3-70b-versatile')
    expect(meta.fingerprint).toBe('fp_semantic_dist_cache')
    expect(meta.context_status).toBe('complete')
    expect(meta.usage).toEqual({
      prompt_tokens: 25,
      completion_tokens: 35,
      total_tokens: 60,
    })
  })

  it('reconstructs historical messages with omitted metadata as null diagnostics instead of fabricated zeros', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const activeConv = uiController.getConversationManager().getActiveConversation()
    const convId = activeConv.id

    const serverMessages: ConversationMessageItem[] = [
      {
        id: 'msg-user-hist-1',
        conversation_id: convId,
        turn_id: 'turn-hist-1',
        sequence_number: 1,
        role: 'user',
        content: 'Historical prompt without metadata',
        created_at: new Date(Date.now() - 50000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
      },
      {
        id: 'msg-assistant-hist-1',
        conversation_id: convId,
        turn_id: 'turn-hist-1',
        sequence_number: 2,
        role: 'assistant',
        content: 'Historical response with omitted diagnostics',
        created_at: new Date(Date.now() - 45000).toISOString(),
        turn_status: 'completed',
        turn_error_code: null,
      },
    ]

    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValueOnce({
      conversation_id: convId,
      messages: serverMessages,
      total: 2,
    })

    await uiController.loadMessagesForActiveConversation()

    const messages = activeConv.messages
    expect(messages.length).toBe(2)

    const assistantMsg = messages[1]
    expect(assistantMsg.role).toBe('assistant')
    expect(assistantMsg.responseMeta).toBeDefined()

    const meta = assistantMsg.responseMeta!
    // Diagnostics must be null, not fabricated as false or 0
    expect(meta.cached).toBeNull()
    expect(meta.latency_ms).toBeNull()
    expect(meta.cache_hit_count).toBeNull()
    expect(meta.usage).toBeNull()

    uiController['renderMessages']()
    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('ℹ️ Diagnostic History Unavailable')
    expect(container.innerHTML).toContain('cache-badge unknown')
    expect(container.innerHTML).not.toContain('0 hits')
    expect(container.innerHTML).not.toContain('0.0 ms')
  })

  it('clears generating indicator from switched conversation when in-flight retry completes', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const cm = uiController.getConversationManager()
    const convA = cm.getActiveConversation()
    const convB = cm.createConversation('Conversation B')

    // Switch back to Conv A
    cm.setActiveConversation(convA.id)

    // Add user message and failed assistant message to Conv A
    const userMsg: ChatMessage = {
      id: 'u-conv-a',
      role: 'user',
      content: 'Question in Conv A',
      timestamp: Date.now(),
      idempotencyKey: 'idemp-a',
      originalOptions: { bypass_cache: false },
    }
    const failedMsg: ChatMessage = {
      id: 'f-conv-a',
      role: 'assistant',
      content: 'Error: timeout',
      timestamp: Date.now(),
      error: 'timeout',
    }
    cm.addMessage(convA.id, userMsg)
    cm.addMessage(convA.id, failedMsg)

    let resolveRetry!: (val: QueryResponse) => void
    const retryPromise = new Promise<QueryResponse>((res) => {
      resolveRetry = res
    })
    vi.spyOn(apiClient, 'sendQuery').mockReturnValueOnce(retryPromise)
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValue({
      conversation_id: convA.id,
      messages: [],
      total: 0,
    })

    // Start retry in Conv A (unawaited)
    const retryExecution = uiController.handleRetry(failedMsg.id)

    // While retry is generating in Conv A, user switches to Conv B
    await uiController.handleSwitchConversation(convB.id)

    // Verify Conv B is active and DOES NOT show Conv A's generating spinner
    expect(cm.getActiveConversationId()).toBe(convB.id)
    const container = document.getElementById('messages-container')!
    expect(container.querySelector('#msg-loading')).toBeNull()

    // When switching back to Conv A while still in flight, Conv A shows spinner
    await uiController.handleSwitchConversation(convA.id)
    expect(container.querySelector('#msg-loading')).not.toBeNull()

    // Switch back to Conv B before resolving
    await uiController.handleSwitchConversation(convB.id)
    expect(container.querySelector('#msg-loading')).toBeNull()

    // Now resolve the retry in Conv A
    resolveRetry({
      response: 'Resolved response for A',
      cached: false,
      model: 'test-model',
      latency_ms: 100,
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      cache_hit_count: 0,
      fingerprint: 'fp-a',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await retryExecution

    // Verify Conv B view still has NO stuck generating spinner
    expect(container.querySelector('#msg-loading')).toBeNull()
  })

  it('clears generating indicator from switched conversation when in-flight send completes', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const cm = uiController.getConversationManager()
    const convA = cm.getActiveConversation()
    const convB = cm.createConversation('Conversation B')

    cm.setActiveConversation(convA.id)

    let resolveSend!: (val: QueryResponse) => void
    const sendPromise = new Promise<QueryResponse>((res) => {
      resolveSend = res
    })
    vi.spyOn(apiClient, 'sendQuery').mockReturnValueOnce(sendPromise)
    vi.spyOn(apiClient, 'getConversation').mockResolvedValueOnce({
      id: convA.id,
      title: 'Conversation A',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Send in Conv A'

    // Start send in Conv A (unawaited)
    const sendExecution = uiController.handleSend()

    // While generation is in flight, switch to Conv B
    await uiController.handleSwitchConversation(convB.id)
    expect(cm.getActiveConversationId()).toBe(convB.id)
    const container = document.getElementById('messages-container')!
    expect(container.querySelector('#msg-loading')).toBeNull()

    // Switch back to Conv A while still generating
    await uiController.handleSwitchConversation(convA.id)
    expect(container.querySelector('#msg-loading')).not.toBeNull()

    // Switch back to Conv B
    await uiController.handleSwitchConversation(convB.id)
    expect(container.querySelector('#msg-loading')).toBeNull()

    // Resolve send in Conv A
    resolveSend({
      response: 'Resolved send for A',
      cached: false,
      model: 'test-model',
      latency_ms: 80,
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      cache_hit_count: 0,
      fingerprint: 'fp-send-a',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await sendExecution

    // Conv B view must have cleared the generating indicator
    expect(container.querySelector('#msg-loading')).toBeNull()
  })

  it('stream checkbox defaults to unchecked and updates notice when toggled', () => {
    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    expect(streamCheckbox).not.toBeNull()
    expect(streamCheckbox.checked).toBe(false)

    const turnNotice = document.getElementById('turn-notice')!
    expect(turnNotice.textContent).toContain('Single-Turn Query Mode')

    // Toggle stream mode ON
    streamCheckbox.checked = true
    streamCheckbox.dispatchEvent(new Event('change'))

    expect(turnNotice.textContent).toContain('Single-Turn Query Mode (Streaming)')
    expect(turnNotice.textContent).toContain('exact cache only')

    // Toggle stream mode OFF
    streamCheckbox.checked = false
    streamCheckbox.dispatchEvent(new Event('change'))
    expect(turnNotice.textContent).not.toContain('(Streaming)')
  })

  it('non-streaming query uses sendQuery and keeps stop button hidden', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const sendQuerySpy = vi.spyOn(apiClient, 'sendQuery').mockResolvedValueOnce({
      response: 'Standard response',
      cached: false,
      model: 'test-model',
      latency_ms: 120,
      usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
      cache_hit_count: 0,
      fingerprint: 'fp-std',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    const sendStreamSpy = vi.spyOn(apiClient, 'sendStreamQuery')

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement

    textarea.value = 'Hello standard query'
    await uiController.handleSend()

    expect(sendQuerySpy).toHaveBeenCalledTimes(1)
    expect(sendStreamSpy).not.toHaveBeenCalled()
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
  })

  it('streaming query uses sendStreamQuery, toggles stop button, renders progressive text, and shows final message', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    const mockStreamResponse: QueryResponse = {
      response: 'Streamed hello world response',
      cached: false,
      model: 'llama-3.3-70b-versatile',
      latency_ms: 250,
      usage: { prompt_tokens: 12, completion_tokens: 15, total_tokens: 27 },
      cache_hit_count: 0,
      fingerprint: 'fp-streamed',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    }

    const sendStreamSpy = vi
      .spyOn(apiClient, 'sendStreamQuery')
      .mockImplementation(async (_query, _idempotencyKey, callbacks) => {
        callbacks?.onDelta?.('Streamed hello world response')
        return mockStreamResponse
      })

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Stream query'

    await uiController.handleSend()

    expect(sendStreamSpy).toHaveBeenCalledTimes(1)
    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('Streamed hello world response')
    expect(container.innerHTML).toContain('Upstream Provider Generation')
  })

  it('stop generation aborts streaming request, renders stopped badge, and leaves retry button without cache diagnostics', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    // Simulate an aborted stream when signal is aborted
    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation(
      (_query, _idempotencyKey, _callbacks, signal) =>
        new Promise<QueryResponse>((_resolve, reject) => {
          if (signal) {
            signal.addEventListener('abort', () => {
              reject(new GatewayApiError(0, 'Generation stopped by user.', 'ABORTED'))
            })
          }
        })
    )

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Stream to be stopped'

    const sendExecution = uiController.handleSend()

    // Stop generation while in flight
    uiController.handleStopGeneration()

    await sendExecution

    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('Generation stopped by user.')
    expect(container.innerHTML).toContain('⏹️ Generation Stopped')
    expect(container.innerHTML).toContain('🔄 Retry Query')
    // Must NOT fabricate cache hit or diagnostics card for cancelled turn
    expect(container.innerHTML).not.toContain('Exact Cache Hit')
    expect(container.innerHTML).not.toContain('Semantic Cache Hit')
    expect(container.innerHTML).not.toContain('Technical Cache Diagnostics')
  })

  it('switching conversations during streaming preserves active stream for initiating conversation only', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    const cm = uiController.getConversationManager()
    const convA = cm.getActiveConversation()
    const convB = cm.createConversation('Conversation B')
    cm.setActiveConversation(convA.id)

    let deltaCb!: (delta: string) => void
    let resolveStream!: (val: QueryResponse) => void
    const streamPromise = new Promise<QueryResponse>((res) => {
      resolveStream = res
    })

    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation(async (_query, _key, callbacks) => {
      deltaCb = (delta: string) => callbacks?.onDelta?.(delta)
      return streamPromise
    })
    vi.spyOn(apiClient, 'getConversation').mockResolvedValue({
      id: convA.id,
      title: 'Conversation A',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValue({
      conversation_id: convB.id,
      messages: [],
      total: 0,
    })

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Stream in Conv A'

    const sendExecution = uiController.handleSend()
    await Promise.resolve()
    await Promise.resolve()

    // Deliver first chunk
    deltaCb('Alpha ')

    // User switches to Conv B
    await uiController.handleSwitchConversation(convB.id)
    const container = document.getElementById('messages-container')!
    // Conv B must NOT show Conv A's streaming bubble or spinner
    expect(container.querySelector('#msg-streaming')).toBeNull()
    expect(container.querySelector('#msg-loading')).toBeNull()

    // Switch back to Conv A while still generating
    await uiController.handleSwitchConversation(convA.id)
    expect(container.querySelector('#msg-streaming')).not.toBeNull()

    // Resolve stream
    resolveStream({
      response: 'Alpha Beta Completed',
      cached: false,
      model: 'test-model',
      latency_ms: 300,
      usage: { prompt_tokens: 5, completion_tokens: 5, total_tokens: 10 },
      cache_hit_count: 0,
      fingerprint: 'fp-ab',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })

    await sendExecution
    expect(container.querySelector('#msg-streaming')).toBeNull()
  })

  it('auto-scroll manages Jump to latest button and scrolls to bottom on click', () => {
    const messagesContainer = document.getElementById('messages-container')!
    const btnJump = document.getElementById('btn-jump-latest') as HTMLButtonElement
    expect(btnJump).not.toBeNull()
    expect(btnJump.classList.contains('hidden')).toBe(true)

    // Simulate scrolled up state
    Object.defineProperty(messagesContainer, 'scrollHeight', { value: 1200, configurable: true })
    Object.defineProperty(messagesContainer, 'clientHeight', { value: 400, configurable: true })
    Object.defineProperty(messagesContainer, 'scrollTop', {
      value: 200,
      writable: true,
      configurable: true,
    })

    messagesContainer.dispatchEvent(new Event('scroll'))
    expect(btnJump.classList.contains('hidden')).toBe(false)

    // Click Jump to latest
    btnJump.click()
    expect(btnJump.classList.contains('hidden')).toBe(true)
  })

  it('copy response button copies text and displays toast notification', async () => {
    const writeTextSpy = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(navigator, 'clipboard', {
      value: {
        writeText: writeTextSpy,
      },
      writable: true,
      configurable: true,
    })

    const cm = uiController.getConversationManager()
    const activeConv = cm.getActiveConversation()
    cm.addMessage(activeConv.id, {
      id: 'a-copy',
      role: 'assistant',
      content: 'Copy this assistant response text.',
      timestamp: Date.now(),
    })
    ;(uiController as unknown as { renderMessages: () => void }).renderMessages()

    const container = document.getElementById('messages-container')!
    const copyBtn = container.querySelector('.btn-copy-response') as HTMLButtonElement
    expect(copyBtn).not.toBeNull()

    copyBtn.click()
    await Promise.resolve()
    expect(writeTextSpy).toHaveBeenCalledWith('Copy this assistant response text.')

    const toastContainer = document.getElementById('toast-container')!
    expect(toastContainer.innerHTML).toContain('Response markdown copied to clipboard')
  })

  it('keyboard navigation on conversation items supports Enter to switch and Delete to remove', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const cm = uiController.getConversationManager()
    const conv1 = cm.getActiveConversation()
    const conv2 = cm.createConversation('Second Conversation')
    cm.setActiveConversation(conv1.id)
    expect(cm.getActiveConversationId()).toBe(conv1.id)

    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValue({
      conversation_id: conv2.id,
      messages: [],
      total: 0,
    })
    vi.spyOn(apiClient, 'getConversation').mockResolvedValue({
      id: conv2.id,
      title: 'Second Conversation',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })
    vi.spyOn(apiClient, 'deleteConversation').mockResolvedValue(true)

    ;(uiController as unknown as { renderConversationList: () => void }).renderConversationList()
    const convList = document.getElementById('conversation-list')!
    const itemForConv2 = convList.querySelector<HTMLElement>(`[data-conv-id="${conv2.id}"]`)!
    expect(itemForConv2).not.toBeNull()

    // Press Enter to switch to conv2
    itemForConv2.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }))
    await new Promise((r) => setTimeout(r, 20))
    expect(cm.getActiveConversationId()).toBe(conv2.id)

    // Re-query item from DOM since renderConversationList() refreshed the list
    const currentItemForConv2 = convList.querySelector<HTMLElement>(`[data-conv-id="${conv2.id}"]`)!
    expect(currentItemForConv2).not.toBeNull()

    // Press Delete on conv2 item to remove it
    currentItemForConv2.dispatchEvent(
      new KeyboardEvent('keydown', { key: 'Delete', bubbles: true })
    )
    await new Promise((r) => setTimeout(r, 20))
    expect(cm.getConversations().find((c) => c.id === conv2.id)).toBeUndefined()
  })

  it('maintains Stop hidden and Send disabled during in-flight non-streaming generation', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement

    let resolveQuery!: (res: QueryResponse) => void
    const queryPromise = new Promise<QueryResponse>((resolve) => {
      resolveQuery = resolve
    })
    vi.spyOn(apiClient, 'sendQuery').mockImplementation(() => queryPromise)

    textarea.value = 'Non-streaming in-flight test'
    const sendPromise = uiController.handleSend()

    // During in-flight non-streaming request:
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(true)

    // Complete generation
    resolveQuery({
      response: 'Non-streaming completed',
      cached: false,
      model: 'test-model',
      latency_ms: 100,
      fingerprint: 'fp-ns',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    await sendPromise

    // After completion:
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(false)
  })

  it('shows Stop button and hides Send button during in-flight streaming generation, resetting after completion', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement

    let resolveStream!: (res: QueryResponse) => void
    const streamPromise = new Promise<QueryResponse>((resolve) => {
      resolveStream = resolve
    })
    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation(() => streamPromise)

    textarea.value = 'Streaming in-flight test'
    const sendPromise = uiController.handleSend()

    // During in-flight streaming request:
    expect(btnStop.classList.contains('hidden')).toBe(false)
    expect(btnStop.disabled).toBe(false)
    expect(btnSend.classList.contains('hidden')).toBe(true)
    expect(btnSend.disabled).toBe(true)

    // Complete generation
    resolveStream({
      response: 'Streaming completed',
      cached: false,
      model: 'test-model',
      latency_ms: 200,
      fingerprint: 'fp-str',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    await sendPromise

    // After completion:
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(false)
  })

  it('ignores handleStopGeneration during non-streaming requests without aborting or altering controls', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement

    let resolveQuery!: (res: QueryResponse) => void
    const queryPromise = new Promise<QueryResponse>((resolve) => {
      resolveQuery = resolve
    })
    vi.spyOn(apiClient, 'sendQuery').mockImplementation(() => queryPromise)

    textarea.value = 'Non-streaming stop attempt'
    const sendPromise = uiController.handleSend()

    // Try calling handleStopGeneration while non-streaming is in flight
    uiController.handleStopGeneration()

    // Controls must remain non-streaming mode: Stop hidden, Send disabled
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.disabled).toBe(true)

    // Complete the query normally
    resolveQuery({
      response: 'Non-streaming completed normally despite stop click',
      cached: false,
      model: 'test-model',
      latency_ms: 100,
      fingerprint: 'fp-ns-norm',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    await sendPromise

    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('Non-streaming completed normally despite stop click')
    expect(container.innerHTML).not.toContain('Generation stopped by user.')
  })

  it('retrying a cancelled streaming request preserves prompt, idempotency key, and shows Stop button during retry', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    let capturedIdempotencyKey: string | undefined
    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation(
      (_query, idempKey, _callbacks, signal) =>
        new Promise<QueryResponse>((_resolve, reject) => {
          capturedIdempotencyKey = idempKey
          signal?.addEventListener('abort', () => {
            reject(new GatewayApiError(0, 'Generation stopped by user.', 'ABORTED'))
          })
        })
    )

    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    textarea.value = 'Prompt to cancel and retry'

    const sendPromise = uiController.handleSend()
    uiController.handleStopGeneration()
    await sendPromise

    const container = document.getElementById('messages-container')!
    expect(container.innerHTML).toContain('Generation stopped by user.')
    expect(container.innerHTML).toContain('🔄 Retry Query')

    const initialIdempKey = capturedIdempotencyKey
    expect(initialIdempKey).toBeDefined()

    // Now set up mock for the retry attempt
    let retryResolve!: (res: QueryResponse) => void
    let retryCapturedKey: string | undefined
    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation((_query, idempKey) => {
      retryCapturedKey = idempKey
      return new Promise<QueryResponse>((resolve) => {
        retryResolve = resolve
      })
    })

    const retryBtn = container.querySelector('.btn-retry') as HTMLButtonElement
    expect(retryBtn).not.toBeNull()
    const retryId = retryBtn.getAttribute('data-retry-id')!
    expect(retryId).toBeDefined()

    // Trigger retry
    const retryExecution = uiController.handleRetry(retryId)

    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement

    // During retry: Stop must be visible, Send hidden
    expect(btnStop.classList.contains('hidden')).toBe(false)
    expect(btnSend.classList.contains('hidden')).toBe(true)

    // Verify original idempotency key was reused
    expect(retryCapturedKey).toBe(initialIdempKey)

    // Complete the retry
    retryResolve({
      response: 'Recovered after retry',
      cached: false,
      model: 'test-model',
      latency_ms: 150,
      fingerprint: 'fp-retry-ok',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    await retryExecution

    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(false)
    expect(container.innerHTML).toContain('Recovered after retry')
  })

  it('conversation switching during generation properly adjusts Send/Stop controls and does not leak loading indicator', async () => {
    uiController.updateConnectionBadge('connected', 'Connected: default')
    apiClient.setClientToken('test_token')
    uiController.setMode('multi-turn')

    const cm = uiController.getConversationManager()
    const convA = cm.getActiveConversation()
    const convB = cm.createConversation('Conversation B')
    cm.setActiveConversation(convA.id)

    const streamCheckbox = document.getElementById('stream-response-checkbox') as HTMLInputElement
    streamCheckbox.checked = true

    let resolveStream!: (res: QueryResponse) => void
    const streamPromise = new Promise<QueryResponse>((resolve) => {
      resolveStream = resolve
    })
    vi.spyOn(apiClient, 'sendStreamQuery').mockImplementation(() => streamPromise)
    vi.spyOn(apiClient, 'getConversation').mockResolvedValue({
      id: convA.id,
      title: 'Conversation A',
      cache_namespace: 'default',
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      last_message_at: new Date().toISOString(),
    })
    vi.spyOn(apiClient, 'getConversationMessages').mockResolvedValue({
      conversation_id: convB.id,
      messages: [],
      total: 0,
    })

    const btnSend = document.getElementById('btn-send') as HTMLButtonElement
    const btnStop = document.getElementById('btn-stop') as HTMLButtonElement
    const textarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    const container = document.getElementById('messages-container')!

    textarea.value = 'Stream in Conv A'
    const sendPromise = uiController.handleSend()

    // In Conv A: Stop visible, Send hidden
    expect(btnStop.classList.contains('hidden')).toBe(false)
    expect(btnSend.classList.contains('hidden')).toBe(true)

    // Switch to Conv B while Conv A is generating
    await uiController.handleSwitchConversation(convB.id)

    // In Conv B: Stop must be hidden, Send must be disabled, no loading/streaming bubbles
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(true)
    expect(container.querySelector('#msg-streaming')).toBeNull()
    expect(container.querySelector('#msg-loading')).toBeNull()

    // Switch back to Conv A: Stop becomes visible again, Send hidden
    await uiController.handleSwitchConversation(convA.id)
    expect(btnStop.classList.contains('hidden')).toBe(false)
    expect(btnSend.classList.contains('hidden')).toBe(true)
    expect(container.querySelector('#msg-streaming')).not.toBeNull()

    // Resolve stream
    resolveStream({
      response: 'Conv A finished stream',
      cached: false,
      model: 'test-model',
      latency_ms: 220,
      fingerprint: 'fp-conv-a',
      created_at: new Date().toISOString(),
      cache_type: 'none',
    })
    await sendPromise

    // After finish: Stop hidden, Send enabled
    expect(btnStop.classList.contains('hidden')).toBe(true)
    expect(btnSend.classList.contains('hidden')).toBe(false)
    expect(btnSend.disabled).toBe(false)
    expect(container.querySelector('#msg-streaming')).toBeNull()
  })

  it('toast notification receives visible class, matches CSS selector, supports multiple concurrent toasts, and cleans up after duration', async () => {
    vi.useFakeTimers()
    const toastContainer = document.getElementById('toast-container')!
    toastContainer.innerHTML = ''

    uiController.showToast('First toast notification', 1000)

    expect(toastContainer.children.length).toBe(1)
    const firstToast = toastContainer.children[0] as HTMLElement
    expect(firstToast.textContent).toBe('First toast notification')
    expect(firstToast.className).toBe('toast-notification')

    // Advance frame for requestAnimationFrame
    vi.advanceTimersByTime(20)
    expect(firstToast.classList.contains('visible')).toBe(true)
    expect(document.querySelector('.toast-notification.visible')).toBe(firstToast)

    // Add a second concurrent toast with longer duration
    uiController.showToast('Second toast notification', 3000)
    expect(toastContainer.children.length).toBe(2)
    const secondToast = toastContainer.children[1] as HTMLElement
    expect(secondToast.textContent).toBe('Second toast notification')

    vi.advanceTimersByTime(20)
    expect(secondToast.classList.contains('visible')).toBe(true)

    // Advance past first toast duration (1000ms from start)
    vi.advanceTimersByTime(1000)
    expect(firstToast.classList.contains('visible')).toBe(false)

    // Advance past transition removal (200ms)
    vi.advanceTimersByTime(250)
    expect(toastContainer.contains(firstToast)).toBe(false)
    expect(toastContainer.contains(secondToast)).toBe(true)

    // Advance past second toast removal
    vi.advanceTimersByTime(2500)
    expect(toastContainer.contains(secondToast)).toBe(false)

    vi.useRealTimers()
  })
})
