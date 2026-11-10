// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewayApiClient } from '../demo/src/api-client'
import type {
  CacheStats,
  ChatMessage,
  ConversationMessageItem,
  NamespaceMetricsSnapshot,
  QueryResponse,
} from '../demo/src/types'
import { DemoUIController } from '../demo/src/ui-controller'
import { MessageRenderer } from '../demo/src/ui/message-renderer'
import { MetricsController } from '../demo/src/ui/metrics-controller'
import { SettingsController } from '../demo/src/ui/settings-controller'

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
        <div id="composer-container">
          <div class="composer-options-row">
            <button id="mode-single" class="active"></button>
            <button id="mode-multi"></button>
            <input id="bypass-cache-checkbox" type="checkbox" />
          </div>
          <div id="turn-notice"></div>
          <textarea id="chat-textarea"></textarea>
          <button id="btn-send"></button>
        </div>
      </section>

      <aside id="metrics-panel">
        <span id="metrics-namespace-badge">[—]</span>
        <span id="metrics-last-updated">Not refreshed</span>
        <button id="btn-refresh-metrics"></button>
        <div id="metrics-body"></div>
      </aside>
    </div>

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

    // Verify Conv B is the active conversation
    expect(cm.getActiveConversationId()).toBe(convB.id)
    const container = document.getElementById('messages-container')!
    expect(container.querySelector('#msg-loading')).not.toBeNull()

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

    // Verify Conv B view has NO stuck generating spinner
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
    expect(container.querySelector('#msg-loading')).not.toBeNull()

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
})
