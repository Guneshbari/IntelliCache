/**
 * UI Controller for the IntelliCache Demonstration Chatbot.
 * Manages DOM events, state, message rendering, cache diagnostics visualization,
 * and authenticated metrics panel updates.
 *
 * All state is kept strictly in-memory. No client tokens or conversation data
 * are written to localStorage, IndexedDB, sessionStorage, or cookies.
 */

import { GatewayApiClient, GatewayApiError } from './api-client'
import type {
  CacheStats,
  ChatMessage,
  ConnectionInfo,
  ConnectionState,
  NamespaceMetricsSnapshot,
  QueryRequestPayload,
  QueryResponse,
} from './types'

export class DemoUIController {
  private apiClient: GatewayApiClient
  private messages: ChatMessage[] = []
  private conversationId: string = crypto.randomUUID()
  private mode: 'single-turn' | 'multi-turn' = 'single-turn'
  private isGenerating: boolean = false
  private metricsCollapsed: boolean = false
  private connectionInfo: ConnectionInfo = {
    state: 'disconnected',
    gatewayUrl: 'http://127.0.0.1:8000',
  }

  // DOM Elements
  private statusBadgeEl!: HTMLElement
  private statusTextEl!: HTMLElement
  private gatewayUrlInput!: HTMLInputElement
  private clientTokenInput!: HTMLInputElement
  private toggleTokenBtn!: HTMLButtonElement
  private connectBtn!: HTMLButtonElement
  private toggleMetricsBtn!: HTMLButtonElement
  private clearChatBtn!: HTMLButtonElement
  private mainContainerEl!: HTMLElement
  private messagesContainerEl!: HTMLElement
  private chatTextarea!: HTMLTextAreaElement
  private sendBtn!: HTMLButtonElement
  private bypassCheckbox!: HTMLInputElement
  private modeSingleBtn!: HTMLButtonElement
  private modeMultiBtn!: HTMLButtonElement
  private turnNoticeEl!: HTMLElement
  private refreshMetricsBtn!: HTMLButtonElement
  private metricsBodyEl!: HTMLElement
  private metricsNamespaceEl!: HTMLElement

  constructor(apiClient: GatewayApiClient) {
    this.apiClient = apiClient
  }

  public init(): void {
    this.bindDomElements()
    this.attachEventListeners()
    this.updateConnectionBadge('disconnected', 'Disconnected')
    this.renderTurnNotice()
    this.renderMessages()
  }

  private bindDomElements(): void {
    this.statusBadgeEl = document.getElementById('connection-badge')!
    this.statusTextEl = document.getElementById('connection-text')!
    this.gatewayUrlInput = document.getElementById('gateway-url') as HTMLInputElement
    this.clientTokenInput = document.getElementById('client-token') as HTMLInputElement
    this.toggleTokenBtn = document.getElementById('btn-toggle-token') as HTMLButtonElement
    this.connectBtn = document.getElementById('btn-connect') as HTMLButtonElement
    this.toggleMetricsBtn = document.getElementById('btn-toggle-metrics') as HTMLButtonElement
    this.clearChatBtn = document.getElementById('btn-clear-chat') as HTMLButtonElement
    this.mainContainerEl = document.getElementById('main-container')!
    this.messagesContainerEl = document.getElementById('messages-container')!
    this.chatTextarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    this.sendBtn = document.getElementById('btn-send') as HTMLButtonElement
    this.bypassCheckbox = document.getElementById('bypass-cache-checkbox') as HTMLInputElement
    this.modeSingleBtn = document.getElementById('mode-single') as HTMLButtonElement
    this.modeMultiBtn = document.getElementById('mode-multi') as HTMLButtonElement
    this.turnNoticeEl = document.getElementById('turn-notice')!
    this.refreshMetricsBtn = document.getElementById('btn-refresh-metrics') as HTMLButtonElement
    this.metricsBodyEl = document.getElementById('metrics-body')!
    this.metricsNamespaceEl = document.getElementById('metrics-namespace')!
  }

  private attachEventListeners(): void {
    // Show/hide token visibility
    this.toggleTokenBtn.addEventListener('click', () => {
      const isPassword = this.clientTokenInput.type === 'password'
      this.clientTokenInput.type = isPassword ? 'text' : 'password'
      this.toggleTokenBtn.textContent = isPassword ? '🙈' : '👁️'
    })

    // Connect button
    this.connectBtn.addEventListener('click', () => this.handleConnect())

    // Toggle Metrics Panel
    this.toggleMetricsBtn.addEventListener('click', () => {
      this.metricsCollapsed = !this.metricsCollapsed
      this.mainContainerEl.classList.toggle('metrics-collapsed', this.metricsCollapsed)
      this.toggleMetricsBtn.textContent = this.metricsCollapsed
        ? '📊 Show Metrics'
        : '📊 Hide Metrics'
    })

    // Clear Chat
    this.clearChatBtn.addEventListener('click', () => this.handleClearChat())

    // Mode Selector
    this.modeSingleBtn.addEventListener('click', () => this.setMode('single-turn'))
    this.modeMultiBtn.addEventListener('click', () => this.setMode('multi-turn'))

    // Send on click and Ctrl+Enter / Enter
    this.sendBtn.addEventListener('click', () => this.handleSend())
    this.chatTextarea.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        this.handleSend()
      }
    })

    // Refresh metrics
    this.refreshMetricsBtn.addEventListener('click', () => this.handleRefreshMetrics())

    // Delegate clicks for sample prompt chips
    this.messagesContainerEl.addEventListener('click', (e) => {
      const target = (e.target as HTMLElement).closest('.prompt-chip')
      if (target && target.textContent) {
        this.chatTextarea.value = target.textContent.trim()
        this.chatTextarea.focus()
      }
    })
  }

  private updateConnectionBadge(state: ConnectionState, text: string): void {
    this.connectionInfo.state = state
    this.statusBadgeEl.setAttribute('data-state', state)
    this.statusTextEl.textContent = text
  }

  public async handleConnect(): Promise<void> {
    const url = this.gatewayUrlInput.value.trim()
    const token = this.clientTokenInput.value.trim()

    this.apiClient.setBaseUrl(url)
    this.apiClient.setClientToken(token)

    this.updateConnectionBadge('connecting', 'Connecting...')
    this.connectBtn.disabled = true

    try {
      const testResult = await this.apiClient.testConnection()
      if (!testResult.online) {
        this.updateConnectionBadge('error', 'Gateway Offline')
        alert(
          `Cannot connect to IntelliCache Gateway at ${url}.\n${testResult.error || 'Connection refused or CORS blocked.'}`
        )
      } else if (!testResult.authenticated) {
        this.updateConnectionBadge('unauthorized', 'Unauthorized (401)')
        alert(
          testResult.error ||
            'Client token was rejected or not provided. Provide a valid token mapped in CLIENT_API_TOKENS.'
        )
      } else {
        const ns = testResult.namespace || 'default'
        this.connectionInfo.namespace = ns
        this.updateConnectionBadge('connected', `Connected: ${ns}`)
        this.metricsNamespaceEl.textContent = `[${ns}]`
        await this.handleRefreshMetrics()
      }
    } catch (err) {
      this.updateConnectionBadge('error', 'Connection Error')
      const msg = err instanceof Error ? err.message : String(err)
      alert(`Connection failed: ${msg}`)
    } finally {
      this.connectBtn.disabled = false
    }
  }

  private setMode(mode: 'single-turn' | 'multi-turn'): void {
    this.mode = mode
    this.modeSingleBtn.classList.toggle('active', mode === 'single-turn')
    this.modeMultiBtn.classList.toggle('active', mode === 'multi-turn')
    this.renderTurnNotice()
  }

  private renderTurnNotice(): void {
    if (this.mode === 'single-turn') {
      this.turnNoticeEl.textContent =
        '⚡ Single-turn query mode: Eligible for exact matching and semantic response caching (technical queries).'
    } else {
      this.turnNoticeEl.textContent =
        'ℹ️ Multi-turn conversation mode: History sent via messages payload. Excluded from semantic caching by gateway policy to prevent context ambiguity.'
    }
  }

  private handleClearChat(): void {
    this.messages = []
    this.conversationId = crypto.randomUUID()
    this.renderMessages()
  }

  private async handleSend(): Promise<void> {
    const text = this.chatTextarea.value.trim()
    if (!text || this.isGenerating) return

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      timestamp: Date.now(),
    }
    this.messages.push(userMessage)
    this.chatTextarea.value = ''
    this.isGenerating = true
    this.sendBtn.disabled = true
    this.renderMessages()

    const bypass = this.bypassCheckbox.checked
    const payload: QueryRequestPayload = {
      bypass_cache: bypass,
    }

    if (this.mode === 'single-turn') {
      // In single-turn mode, send prompt and omit conversation_id for semantic cache eligibility
      payload.prompt = text
    } else {
      // In multi-turn mode, send messages array and conversation_id
      payload.conversation_id = this.conversationId
      payload.messages = this.messages
        .filter((m) => !m.error)
        .map((m) => ({ role: m.role, content: m.content }))
    }

    try {
      const response = await this.apiClient.sendQuery(payload)
      const assistantMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: response.response,
        timestamp: Date.now(),
        responseMeta: response,
      }
      this.messages.push(assistantMessage)
      // Refresh metrics silently on successful queries
      this.handleRefreshMetrics().catch(() => {})
    } catch (err) {
      let errMsg = 'Failed to execute query.'
      if (err instanceof GatewayApiError) {
        errMsg = err.message
      } else if (err instanceof Error) {
        errMsg = err.message
      }
      const assistantError: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: `Error: ${errMsg}`,
        timestamp: Date.now(),
        error: errMsg,
      }
      this.messages.push(assistantError)
    } finally {
      this.isGenerating = false
      this.sendBtn.disabled = false
      this.renderMessages()
    }
  }

  public async handleRefreshMetrics(): Promise<void> {
    if (!this.apiClient.hasToken()) {
      this.metricsBodyEl.innerHTML = `
        <div class="metrics-notice">
          Authenticate with a client API token to view namespace runtime metrics.
        </div>
      `
      return
    }

    this.refreshMetricsBtn.disabled = true
    try {
      const [snapshot, cacheStats] = await Promise.all([
        this.apiClient.fetchNamespaceMetrics(),
        this.apiClient.fetchCacheStats().catch(() => null),
      ])
      this.renderMetrics(snapshot, cacheStats)
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      this.metricsBodyEl.innerHTML = `
        <div class="error-callout" style="font-size: 0.78rem;">
          Failed to fetch metrics: ${this.escapeHtml(errMsg)}
        </div>
      `
    } finally {
      this.refreshMetricsBtn.disabled = false
    }
  }

  private renderMetrics(m: NamespaceMetricsSnapshot, cacheStats: CacheStats | null): void {
    const rc = m.request_and_cache
    const ps = m.provider_and_savings
    const sm = m.semantic
    const dr = m.derived_rates
    const lat = m.latencies

    const formatRate = (rate: number): string => `${(rate * 100).toFixed(1)}%`
    const formatMs = (val: number | null | undefined): string =>
      val !== null && val !== undefined ? `${val.toFixed(1)} ms` : '—'

    this.metricsBodyEl.innerHTML = `
      ${
        cacheStats
          ? `
      <div class="metric-panel">
        <div class="panel-title">Cache Capacity & Backend</div>
        <div class="metric-row">
          <span class="metric-row-label">Storage Backend:</span>
          <span class="metric-row-value highlight">${this.escapeHtml(cacheStats.backend.toUpperCase())}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Entries (Active / Max):</span>
          <span class="metric-row-value">${cacheStats.total_entries} / ${cacheStats.max_entries}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">TTL Duration:</span>
          <span class="metric-row-value">${cacheStats.ttl_seconds}s</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Cache Operational:</span>
          <span class="metric-row-value ${cacheStats.enabled ? 'green' : 'amber'}">${cacheStats.enabled ? 'Enabled' : 'Disabled'}</span>
        </div>
      </div>
      `
          : ''
      }
      <div class="metric-panel">
        <div class="panel-title">Cache Outcomes</div>
        <div class="metric-row">
          <span class="metric-row-label">Exact Cache Hits:</span>
          <span class="metric-row-value green">${rc.exact_cache_hits}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Exact Hit Rate:</span>
          <span class="metric-row-value green">${formatRate(dr.exact_hit_rate)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Semantic Cache Hits:</span>
          <span class="metric-row-value blue">${rc.semantic_hits}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Semantic Hit Rate:</span>
          <span class="metric-row-value blue">${formatRate(dr.semantic_hit_rate)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Overall Cache Hit Rate:</span>
          <span class="metric-row-value highlight">${formatRate(dr.overall_cache_hit_rate)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Exact Cache Misses:</span>
          <span class="metric-row-value">${rc.exact_cache_misses}</span>
        </div>
      </div>

      <div class="metric-panel">
        <div class="panel-title">Provider & Concurrency</div>
        <div class="metric-row">
          <span class="metric-row-label">Provider Calls (OK / Total):</span>
          <span class="metric-row-value amber">${ps.provider_calls_succeeded} / ${ps.provider_calls_attempted}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">SingleFlight Coalesced:</span>
          <span class="metric-row-value blue">${rc.coalesced_joins}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Cache Bypass Requests:</span>
          <span class="metric-row-value">${rc.cache_bypass_requests}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Total Requests:</span>
          <span class="metric-row-value">${rc.total_authenticated_requests}</span>
        </div>
      </div>

      <div class="metric-panel">
        <div class="panel-title">Token Savings</div>
        <div class="metric-row">
          <span class="metric-row-label">Total Tokens Saved:</span>
          <span class="metric-row-value green">${ps.total_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Prompt Tokens Saved:</span>
          <span class="metric-row-value">${ps.prompt_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Completion Tokens Saved:</span>
          <span class="metric-row-value">${ps.completion_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Provider Tokens Used:</span>
          <span class="metric-row-value amber">${ps.provider_total_tokens_used.toLocaleString()}</span>
        </div>
      </div>

      <div class="metric-panel">
        <div class="panel-title">Latency Diagnostics</div>
        <div class="metric-row">
          <span class="metric-row-label">Total Latency (Mean):</span>
          <span class="metric-row-value">${formatMs(lat.total_request_latency_ms?.mean)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Exact Lookup (Mean):</span>
          <span class="metric-row-value green">${formatMs(lat.exact_lookup_latency_ms?.mean)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Semantic Lookup (Mean):</span>
          <span class="metric-row-value blue">${formatMs(lat.semantic_lookup_latency_ms?.mean)}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Provider Gen (Mean):</span>
          <span class="metric-row-value amber">${formatMs(lat.provider_generation_latency_ms?.mean)}</span>
        </div>
      </div>

      <div class="metric-panel">
        <div class="panel-title">Semantic Diagnostics</div>
        <div class="metric-row">
          <span class="metric-row-label">Eligibility Rejections:</span>
          <span class="metric-row-value">${sm.semantic_eligibility_rejections}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Ambiguous Rejections:</span>
          <span class="metric-row-value">${sm.ambiguous_candidate_rejections}</span>
        </div>
        <div class="metric-row">
          <span class="metric-row-label">Indexing Successes:</span>
          <span class="metric-row-value green">${sm.indexing_successes}</span>
        </div>
      </div>

      <div class="metrics-notice">
        Displaying namespace-isolated telemetry from <code>GET /metrics/me</code> for <strong>${this.escapeHtml(m.namespace)}</strong>. System administrator telemetry (<code>/admin/metrics</code>) is strictly restricted to administrator tokens.
      </div>
    `
  }

  private renderMessages(): void {
    if (this.messages.length === 0) {
      this.messagesContainerEl.innerHTML = `
        <div class="empty-state">
          <h2 class="empty-title">IntelliCache Demonstration Gateway</h2>
          <p class="empty-desc">
            Interact with the FastAPI gateway to observe real-time Exact Cache Hits, Semantic Similarity Reuse, and Upstream Provider Generations.
          </p>

          <div class="sample-prompts-section">
            <div class="sample-prompts-label">Quick Demonstration Prompts</div>
            <div class="sample-group">
              <div class="sample-group-title">1. Exact Cache Hit Test (submit query, then submit identical query)</div>
              <div class="prompt-chips">
                <button type="button" class="prompt-chip">Explain Python asyncio event loop and tasks</button>
                <button type="button" class="prompt-chip">What is the difference between let, const, and var in JavaScript?</button>
              </div>
            </div>

            <div class="sample-group">
              <div class="sample-group-title">2. Semantic Cache Hit Test (requires SEMANTIC_CACHE_ENABLED=true)</div>
              <div class="prompt-chips">
                <button type="button" class="prompt-chip">Describe how tasks and the event loop work in Python asyncio</button>
                <button type="button" class="prompt-chip">How do let, const, and var differ in JavaScript?</button>
                <button type="button" class="prompt-chip">How does PostgreSQL pgvector perform vector similarity searches?</button>
              </div>
            </div>
          </div>
        </div>
      `
      return
    }

    let html = ''
    for (const msg of this.messages) {
      const isUser = msg.role === 'user'
      const timeStr = new Date(msg.timestamp).toLocaleTimeString([], {
        hour: '2-digit',
        minute: '2-digit',
        second: '2-digit',
      })

      if (isUser) {
        html += `
          <div class="message-row user">
            <div class="message-header">
              <span>You</span>
              <span>${timeStr}</span>
            </div>
            <div class="message-bubble">${this.escapeHtml(msg.content)}</div>
          </div>
        `
      } else if (msg.error) {
        html += `
          <div class="message-row assistant">
            <div class="message-header">
              <span>Assistant</span>
              <span>${timeStr}</span>
            </div>
            <div class="error-callout">${this.escapeHtml(msg.content)}</div>
          </div>
        `
      } else {
        const meta = msg.responseMeta
        html += `
          <div class="message-row assistant">
            <div class="message-header">
              <span>IntelliCache Assistant (${this.escapeHtml(meta?.model || 'gateway')})</span>
              <span>${timeStr}</span>
            </div>
            <div class="message-bubble">${this.escapeHtml(msg.content)}</div>
            ${meta ? this.renderCacheCard(meta) : ''}
          </div>
        `
      }
    }

    if (this.isGenerating) {
      html += `
        <div class="message-row assistant">
          <div class="message-header">
            <span>IntelliCache Assistant</span>
            <span>processing...</span>
          </div>
          <div class="message-bubble" style="display: flex; align-items: center; gap: 0.5rem; color: var(--text-muted);">
            <div class="spinner"></div> Executing query on gateway...
          </div>
        </div>
      `
    }

    this.messagesContainerEl.innerHTML = html
    this.messagesContainerEl.scrollTop = this.messagesContainerEl.scrollHeight
  }

  private renderCacheCard(meta: QueryResponse): string {
    let badgeClass = 'provider'
    let badgeText = '🌐 Upstream Provider Generation'

    if (meta.cached && meta.cache_type === 'exact') {
      badgeClass = 'exact'
      badgeText = '⚡ Exact Cache Hit'
    } else if (meta.cached && meta.cache_type === 'semantic') {
      badgeClass = 'semantic'
      badgeText = '🧠 Semantic Cache Hit'
    }

    const latencyClass = meta.cached ? 'fast' : 'standard'
    const shortFp = meta.fingerprint ? `${meta.fingerprint.slice(0, 16)}...` : 'n/a'
    const totalTokens = meta.usage?.total_tokens ?? 0
    const promptTokens = meta.usage?.prompt_tokens ?? 0
    const compTokens = meta.usage?.completion_tokens ?? 0

    return `
      <div class="cache-card">
        <div class="cache-card-header">
          <span class="cache-badge ${badgeClass}">${badgeText}</span>
          <span class="cache-latency ${latencyClass}">${meta.latency_ms.toFixed(1)} ms</span>
        </div>
        <div class="cache-stats-grid">
          <div class="stat-item">
            <span class="stat-label">Model</span>
            <span class="stat-value" title="${this.escapeHtml(meta.model)}">${this.escapeHtml(meta.model)}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Hit Count</span>
            <span class="stat-value highlight">${meta.cache_hit_count}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Total Tokens</span>
            <span class="stat-value">${totalTokens}</span>
          </div>
          <div class="stat-item">
            <span class="stat-label">Prompt / Comp</span>
            <span class="stat-value">${promptTokens} / ${compTokens}</span>
          </div>
          <div class="stat-item" style="grid-column: 1 / -1;">
            <span class="stat-label">Fingerprint (SHA-256)</span>
            <span class="fingerprint-text" title="${this.escapeHtml(meta.fingerprint)}">${this.escapeHtml(shortFp)}</span>
          </div>
        </div>
      </div>
    `
  }

  private escapeHtml(str: string): string {
    return (str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
  }
}
