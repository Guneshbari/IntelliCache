// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewayApiClient } from '../demo/src/api-client'
import type {
  CacheStats,
  ChatMessage,
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
})
