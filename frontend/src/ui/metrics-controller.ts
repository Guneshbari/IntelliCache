/**
 * Namespace Metrics and Cache Telemetry Controller for the IntelliCache Demonstration Chatbot.
 *
 * Fetches and presents namespace-isolated runtime performance metrics from GET /metrics/me
 * and cache capacity statistics from GET /cache/stats.
 */

import { GatewayApiClient } from '../api-client'
import type { CacheStats, NamespaceMetricsSnapshot } from '../types'
import { ICON_CHART, ICON_LOCK } from './icons'
import { MarkdownRenderer } from './markdown-renderer'

export class MetricsController {
  private apiClient: GatewayApiClient
  private isCollapsed: boolean = false
  private isRefreshing: boolean = false
  private lastRefreshedAt: number | null = null

  // DOM Elements
  private metricsPanelEl!: HTMLElement
  private metricsBodyEl!: HTMLElement
  private namespaceBadgeEl!: HTMLElement
  private lastUpdatedEl!: HTMLElement
  private refreshBtn!: HTMLButtonElement
  private togglePanelBtn!: HTMLButtonElement

  constructor(apiClient: GatewayApiClient) {
    this.apiClient = apiClient
  }

  public init(): void {
    this.bindDomElements()
    this.attachEventListeners()
    this.renderInitialState()
  }

  private bindDomElements(): void {
    this.metricsPanelEl = document.getElementById('metrics-panel')!
    this.metricsBodyEl = document.getElementById('metrics-body')!
    this.namespaceBadgeEl = document.getElementById('metrics-namespace-badge')!
    this.lastUpdatedEl = document.getElementById('metrics-last-updated')!
    this.refreshBtn = document.getElementById('btn-refresh-metrics') as HTMLButtonElement
    this.togglePanelBtn = document.getElementById('btn-toggle-metrics') as HTMLButtonElement
  }

  private attachEventListeners(): void {
    this.refreshBtn?.addEventListener('click', () => this.refresh())

    this.togglePanelBtn?.addEventListener('click', () => {
      this.toggleCollapse()
    })
  }

  public toggleCollapse(forceState?: boolean): void {
    this.isCollapsed = forceState !== undefined ? forceState : !this.isCollapsed
    const appWorkspace = document.getElementById('app-workspace')
    appWorkspace?.classList.toggle('metrics-collapsed', this.isCollapsed)
    this.metricsPanelEl?.classList.toggle('collapsed', this.isCollapsed)

    if (this.togglePanelBtn) {
      this.togglePanelBtn.innerHTML = this.isCollapsed
        ? `${ICON_CHART}<span class="btn-text">Telemetry</span>`
        : `${ICON_CHART}<span class="btn-text">Close Telemetry</span>`
    }
  }

  public isPanelCollapsed(): boolean {
    return this.isCollapsed
  }

  public setNamespace(namespace?: string): void {
    if (this.namespaceBadgeEl) {
      this.namespaceBadgeEl.textContent = namespace ? `[${namespace}]` : '[—]'
    }
  }

  public renderInitialState(): void {
    if (!this.apiClient.hasToken()) {
      this.metricsBodyEl.innerHTML = `
        <div class="metrics-notice">
          <div class="notice-icon">${ICON_LOCK}</div>
          <div class="notice-title">Authentication Required</div>
          <p class="notice-desc">
            Authenticate with a client API token in Connection Settings to inspect real-time namespace cache statistics, hit rates, token savings, and latency distributions.
          </p>
        </div>
      `
      return
    }
    this.refresh()
  }

  public async refresh(): Promise<void> {
    if (!this.apiClient.hasToken()) {
      this.renderInitialState()
      return
    }

    if (this.isRefreshing) return
    this.isRefreshing = true

    if (this.refreshBtn) {
      this.refreshBtn.disabled = true
      this.refreshBtn.classList.add('spinning')
    }

    try {
      const [snapshot, cacheStats] = await Promise.all([
        this.apiClient.fetchNamespaceMetrics(),
        this.apiClient.fetchCacheStats().catch(() => null),
      ])

      this.lastRefreshedAt = Date.now()
      this.setNamespace(snapshot.namespace)
      this.renderMetrics(snapshot, cacheStats)

      if (this.lastUpdatedEl) {
        this.lastUpdatedEl.textContent = `Updated ${new Date(this.lastRefreshedAt).toLocaleTimeString()}`
      }
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      this.metricsBodyEl.innerHTML = `
        <div class="error-callout" style="font-size: 0.8rem; margin: 1rem 0;">
          <strong>Failed to fetch telemetry:</strong>
          <p style="margin-top: 0.35rem;">${MarkdownRenderer.escapeHtml(errMsg)}</p>
        </div>
      `
    } finally {
      this.isRefreshing = false
      if (this.refreshBtn) {
        this.refreshBtn.disabled = false
        this.refreshBtn.classList.remove('spinning')
      }
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
        <div class="telemetry-card">
          <div class="telemetry-card-title">Cache Capacity & Backend</div>
          <div class="telemetry-row">
            <span class="telemetry-label">Storage Backend:</span>
            <span class="telemetry-val highlight-accent">${MarkdownRenderer.escapeHtml(cacheStats.backend.toUpperCase())}</span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">Active / Max Entries:</span>
            <span class="telemetry-val">${cacheStats.total_entries} / ${cacheStats.max_entries}</span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">TTL Duration:</span>
            <span class="telemetry-val">${cacheStats.ttl_seconds}s</span>
          </div>
          <div class="telemetry-row">
            <span class="telemetry-label">Operational State:</span>
            <span class="telemetry-val ${cacheStats.enabled ? 'green-text' : 'amber-text'}">
              ${cacheStats.enabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
        </div>
      `
          : ''
      }

      <div class="telemetry-card">
        <div class="telemetry-card-title">Cache Outcomes</div>
        <div class="telemetry-row">
          <span class="telemetry-label">Exact Cache Hits:</span>
          <span class="telemetry-val green-text font-bold">${rc.exact_cache_hits}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Exact Hit Rate:</span>
          <span class="telemetry-val green-text">${formatRate(dr.exact_hit_rate)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Semantic Cache Hits:</span>
          <span class="telemetry-val blue-text font-bold">${rc.semantic_hits}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Semantic Hit Rate:</span>
          <span class="telemetry-val blue-text">${formatRate(dr.semantic_hit_rate)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Overall Cache Hit Rate:</span>
          <span class="telemetry-val highlight-accent font-bold">${formatRate(dr.overall_cache_hit_rate)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Exact Cache Misses:</span>
          <span class="telemetry-val">${rc.exact_cache_misses}</span>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-card-title">Provider & Concurrency</div>
        <div class="telemetry-row">
          <span class="telemetry-label">Provider Calls (OK / Total):</span>
          <span class="telemetry-val amber-text">${ps.provider_calls_succeeded} / ${ps.provider_calls_attempted}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">SingleFlight Coalesced:</span>
          <span class="telemetry-val blue-text font-bold">${rc.coalesced_joins}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Cache Bypass Requests:</span>
          <span class="telemetry-val purple-text">${rc.cache_bypass_requests}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Total Requests:</span>
          <span class="telemetry-val font-bold">${rc.total_authenticated_requests}</span>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-card-title">Token Savings</div>
        <div class="telemetry-row">
          <span class="telemetry-label">Total Tokens Saved:</span>
          <span class="telemetry-val green-text font-bold">${ps.total_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Prompt Tokens Saved:</span>
          <span class="telemetry-val">${ps.prompt_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Completion Tokens Saved:</span>
          <span class="telemetry-val">${ps.completion_tokens_saved.toLocaleString()}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Provider Tokens Used:</span>
          <span class="telemetry-val amber-text">${ps.provider_total_tokens_used.toLocaleString()}</span>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-card-title">Latency Diagnostics</div>
        <div class="telemetry-row">
          <span class="telemetry-label">Total Latency (Mean):</span>
          <span class="telemetry-val">${formatMs(lat.total_request_latency_ms?.mean)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Exact Lookup (Mean):</span>
          <span class="telemetry-val green-text">${formatMs(lat.exact_lookup_latency_ms?.mean)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Semantic Lookup (Mean):</span>
          <span class="telemetry-val blue-text">${formatMs(lat.semantic_lookup_latency_ms?.mean)}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Provider Gen (Mean):</span>
          <span class="telemetry-val amber-text">${formatMs(lat.provider_generation_latency_ms?.mean)}</span>
        </div>
      </div>

      <div class="telemetry-card">
        <div class="telemetry-card-title">Semantic Diagnostics</div>
        <div class="telemetry-row">
          <span class="telemetry-label">Eligibility Rejections:</span>
          <span class="telemetry-val">${sm.semantic_eligibility_rejections}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Ambiguous Rejections:</span>
          <span class="telemetry-val">${sm.ambiguous_candidate_rejections}</span>
        </div>
        <div class="telemetry-row">
          <span class="telemetry-label">Indexing Successes:</span>
          <span class="telemetry-val green-text">${sm.indexing_successes}</span>
        </div>
      </div>

      <div class="metrics-notice" style="margin-top: 1rem;">
        Displaying process-local namespace telemetry from <code>GET /metrics/me</code> for <strong>${MarkdownRenderer.escapeHtml(m.namespace)}</strong>. Refreshed on demand.
      </div>
    `
  }
}
