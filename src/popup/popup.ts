/**
 * IntelliCache Collector - Modern Popup Dashboard Controller
 * Manages live IndexedDB stats, platform collection breakdown with provider logos,
 * recent interaction preview, interactive explorer, system health, and diagnostics & tools suite.
 */

import { logger } from '../diagnostics'
import {
  configureActionDisplayMode,
  type DisplayMode,
  getBrowserRuntime,
  isSidePanelSupported,
  openSidePanel,
  openStandaloneWindow,
} from '../shared/browser'
import {
  createDbGetIntegrityReportMessage,
  createDbGetStatsMessage,
  createDbGetStorageMetricsMessage,
  createGetStatusMessage,
  createPingMessage,
  sendExtensionMessage,
} from '../shared/messages'
import { formatBytes } from '../shared/storage-format'
import { cleanQueryText, cleanResponseText } from '../shared/text-cleaner'
import type {
  DbIntegrityReportData,
  DbStatsResponseData,
  Interaction,
  PingResponseData,
  StatusResponseData,
  StorageMetricsResponseData,
} from '../shared/types'

// Global in-memory UI state (reset atomically on each fresh query)
interface PopupState {
  theme: 'dark' | 'light'
  displayMode: DisplayMode
  totalInteractions: number
  totalConversations: number
  chatgptCount: number
  claudeCount: number
  geminiCount: number
  recentInteractions: Interaction[]
  storageMetrics: StorageMetricsResponseData | null
  explorerFilter: 'all' | 'chatgpt' | 'claude' | 'gemini'
  explorerSearchQuery: string
  isExplorerExpanded: boolean
  isDiagnosticsExpanded: boolean
}

const state: PopupState = {
  theme: 'light',
  displayMode: 'popup',
  totalInteractions: 0,
  totalConversations: 0,
  chatgptCount: 0,
  claudeCount: 0,
  geminiCount: 0,
  recentInteractions: [],
  storageMetrics: null,
  explorerFilter: 'all',
  explorerSearchQuery: '',
  isExplorerExpanded: false,
  isDiagnosticsExpanded: true,
}

const VALID_EXPLORER_FILTERS: ReadonlySet<string> = new Set(['all', 'chatgpt', 'claude', 'gemini'])
/** Max explorer cards rendered per pass (bounds DOM cost on large windows). */
const EXPLORER_RENDER_LIMIT = 100
/** Delay before revoking an export blob URL (slow disks/downloads safe margin). */
const EXPORT_BLOB_REVOKE_MS = 60_000

/**
 * Truncates text by Unicode code points (not UTF-16 units) so emoji and
 * surrogate pairs are never split mid-character.
 */
function safeSnippet(text: string, maxChars: number): string {
  if (!text) return ''
  const points = Array.from(text)
  return points.length > maxChars ? `${points.slice(0, maxChars).join('')}…` : text
}

/**
 * Coerces an unknown platform value into a valid explorer filter,
 * falling back to 'all' for unexpected platforms (e.g. future providers).
 */
function toExplorerFilter(value: unknown): PopupState['explorerFilter'] {
  return typeof value === 'string' && VALID_EXPLORER_FILTERS.has(value)
    ? (value as PopupState['explorerFilter'])
    : 'all'
}

document.addEventListener('DOMContentLoaded', () => {
  // DOM Elements
  const themeToggleBtn = document.getElementById('theme-toggle-btn') as HTMLButtonElement | null
  const sidepanelBtn = document.getElementById('sidepanel-btn') as HTMLButtonElement | null
  const popoutBtn = document.getElementById('popout-btn') as HTMLButtonElement | null
  const modeSelector = document.getElementById('display-mode-selector')
  const modeChips = document.querySelectorAll<HTMLButtonElement>('.mode-chip')

  // Detect display mode from URL or viewport dimensions
  function updateViewportMode(): void {
    const urlParams = new URLSearchParams(window.location.search)
    const isWindowMode = urlParams.get('mode') === 'window'
    const isSidepanelMode =
      urlParams.get('mode') === 'sidepanel' ||
      window.location.pathname.includes('sidepanel') ||
      document.documentElement.classList.contains('dock-view')

    const isExpandedViewport = window.innerHeight > 580 || window.innerWidth > 410

    if (isWindowMode) {
      document.body.classList.add('mode-standalone')
      document.body.classList.remove('mode-popup')
    } else if (isSidepanelMode || isExpandedViewport) {
      document.body.classList.add('mode-docked', 'mode-sidepanel', 'mode-expanded')
      document.body.classList.remove('mode-popup')
    } else {
      document.body.classList.add('mode-popup')
      document.body.classList.remove('mode-docked', 'mode-sidepanel', 'mode-expanded')
    }
  }

  updateViewportMode()
  window.addEventListener('resize', updateViewportMode)

  function isStandaloneOrSidepanel(): boolean {
    return (
      document.body.classList.contains('mode-standalone') ||
      document.body.classList.contains('mode-sidepanel') ||
      document.body.classList.contains('mode-docked') ||
      document.body.classList.contains('mode-expanded') ||
      window.innerHeight > 580 ||
      window.innerWidth > 410
    )
  }

  if (!isSidePanelSupported() && sidepanelBtn) {
    sidepanelBtn.title = 'Dock into Side Panel (Not supported in this browser)'
  }

  const statusBadge = document.getElementById('status-badge')
  const statusText = document.getElementById('status-text')
  const totalInteractionsEl = document.getElementById('total-interactions-count')
  const totalConversationsEl = document.getElementById('total-conversations-count')

  const countChatgptEl = document.getElementById('count-chatgpt')
  const countClaudeEl = document.getElementById('count-claude')
  const countGeminiEl = document.getElementById('count-gemini')

  const barChatgptEl = document.getElementById('bar-chatgpt')
  const barClaudeEl = document.getElementById('bar-claude')
  const barGeminiEl = document.getElementById('bar-gemini')

  const percentChatgptEl = document.getElementById('percent-chatgpt')
  const percentClaudeEl = document.getElementById('percent-claude')
  const percentGeminiEl = document.getElementById('percent-gemini')

  const recentListEl = document.getElementById('recent-activity-list')
  const recentCountBadgeEl = document.getElementById('recent-count-badge')
  const toggleExplorerBtn = document.getElementById('toggle-explorer-btn')
  const toggleExplorerText = document.getElementById('toggle-explorer-text')
  const openExplorerBanner = document.getElementById('open-explorer-banner')
  const explorerBannerTitleEl = document.getElementById('explorer-banner-title')
  const explorerBannerSubEl = document.getElementById('explorer-banner-sub')

  const explorerSectionEl = document.getElementById('explorer-section')
  const explorerHeaderToggleEl = document.getElementById('explorer-header-toggle')
  const explorerCloseBtn = document.getElementById('explorer-close-btn')
  const explorerSearchInput = document.getElementById('explorer-search') as HTMLInputElement | null
  const explorerSearchClearBtn = document.getElementById('explorer-search-clear')
  const filterChipsEl = document.getElementById('filter-chips')
  const explorerItemsListEl = document.getElementById('explorer-items-list')
  const explorerMatchCountEl = document.getElementById('explorer-match-count')

  const swStatusValEl = document.getElementById('sw-status-val')
  const dbStorageValEl = document.getElementById('db-storage-val')
  const dbConnectionValEl = document.getElementById('db-connection-val')
  const extVersionValEl = document.getElementById('ext-version-val')
  const healthSummaryBadgeEl = document.getElementById('health-summary-badge')

  const storageDatasetValEl = document.getElementById('storage-dataset-val')
  const storageUsageValEl = document.getElementById('storage-usage-val')
  const storageQuotaValEl = document.getElementById('storage-quota-val')
  const storageInteractionsValEl = document.getElementById('storage-interactions-val')
  const storageConversationsValEl = document.getElementById('storage-conversations-val')
  const storageAvgValEl = document.getElementById('storage-avg-val')
  const storageQueryValEl = document.getElementById('storage-query-val')
  const storageResponseValEl = document.getElementById('storage-response-val')
  const storageMetadataValEl = document.getElementById('storage-metadata-val')
  const storageRefreshBtn = document.getElementById(
    'storage-refresh-btn'
  ) as HTMLButtonElement | null

  const diagnosticsToggleEl = document.getElementById('diagnostics-toggle')
  const diagnosticsContentEl = document.getElementById('diagnostics-content')
  const pingBtn = document.getElementById('ping-btn') as HTMLButtonElement | null
  const integrityBtn = document.getElementById('integrity-btn') as HTMLButtonElement | null
  const exportBtn = document.getElementById('export-btn') as HTMLButtonElement | null
  const clearLogBtn = document.getElementById('clear-log-btn') as HTMLButtonElement | null
  const logOutputEl = document.getElementById('log-output')

  // ─── THEME MANAGEMENT ───────────────────────────────────────────────────

  function applyTheme(theme: 'dark' | 'light'): void {
    state.theme = theme
    document.documentElement.setAttribute('data-theme', theme)
    if (themeToggleBtn) {
      themeToggleBtn.setAttribute(
        'aria-label',
        theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
      )
      themeToggleBtn.title = theme === 'dark' ? 'Switch to light mode' : 'Switch to dark mode'
    }
    try {
      localStorage.setItem('intellicache_theme', theme)
    } catch {
      // ignore storage errors
    }
  }

  function initTheme(): void {
    try {
      const savedTheme = localStorage.getItem('intellicache_theme')
      if (savedTheme === 'light' || savedTheme === 'dark') {
        applyTheme(savedTheme)
        return
      }
    } catch {
      // ignore
    }
    // Default to light mode (signature warm off-white Neo-Brutalist look)
    applyTheme('light')
  }

  function toggleTheme(): void {
    const nextTheme: 'dark' | 'light' = state.theme === 'dark' ? 'light' : 'dark'
    applyTheme(nextTheme)
    appendLog(`Theme switched to ${nextTheme.toUpperCase()} mode`, 'info')
  }

  // ─── UTILITY & LOGGING ───────────────────────────────────────────────────

  function appendLog(message: string, type: 'info' | 'success' | 'warn' | 'error' = 'info'): void {
    if (!logOutputEl) return
    const entry = document.createElement('div')
    entry.className = `log-entry log-entry-${type}`

    const timeSpan = document.createElement('span')
    timeSpan.className = 'log-time'
    timeSpan.textContent = `[${new Date().toLocaleTimeString()}]`

    const msgSpan = document.createElement('span')
    msgSpan.className = 'log-msg'
    msgSpan.textContent = message

    entry.appendChild(timeSpan)
    entry.appendChild(msgSpan)
    logOutputEl.appendChild(entry)
    logOutputEl.scrollTop = logOutputEl.scrollHeight
  }

  function updateStatusPill(status: 'ACTIVE' | 'CONNECTING' | 'OFFLINE' | 'ERROR'): void {
    if (!statusBadge || !statusText) return
    statusBadge.className = 'status-pill'

    switch (status) {
      case 'ACTIVE':
        statusBadge.classList.add('active')
        statusText.textContent = 'ACTIVE'
        break
      case 'CONNECTING':
        statusText.textContent = 'CONNECTING'
        break
      case 'OFFLINE':
        statusText.textContent = 'OFFLINE'
        break
      case 'ERROR':
        statusBadge.classList.add('error')
        statusText.textContent = 'ERROR'
        break
    }
  }

  function formatRelativeTime(isoString: string): string {
    try {
      const timestamp = new Date(isoString).getTime()
      if (isNaN(timestamp)) return isoString
      const diffMs = Date.now() - timestamp
      const diffSec = Math.floor(diffMs / 1000)
      if (diffSec < 45) return 'just now'
      const diffMin = Math.floor(diffSec / 60)
      if (diffMin < 60) return `${diffMin}m ago`
      const diffHr = Math.floor(diffMin / 60)
      if (diffHr < 24) return `${diffHr}h ago`
      const diffDays = Math.floor(diffHr / 24)
      return `${diffDays}d ago`
    } catch {
      return isoString
    }
  }

  function escapeHtml(str: unknown): string {
    if (str === null || str === undefined) return ''
    return String(str)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;')
  }

  function getProviderLogoHtml(platform: string): string {
    switch (platform) {
      case 'chatgpt':
        return `
          <div class="recent-item-logo badge-chatgpt" title="ChatGPT">
            <svg class="oai-blossom" viewBox="172 172 372 372" width="13" height="13" fill="currentColor">
              <path fill-rule="evenodd" clip-rule="evenodd" d="M508.749 317.399C516.777 287.314 508.991 253.884 485.389 230.282C461.788 206.681 428.36 198.895 398.273 206.923C376.231 184.928 343.39 174.956 311.148 183.596C278.906 192.234 255.45 217.292 247.36 247.361C217.291 255.451 192.233 278.91 183.595 311.149C174.957 343.391 184.927 376.232 206.924 398.274C198.896 428.359 206.683 461.789 230.284 485.391C253.885 508.992 287.313 516.779 317.401 508.75C339.442 530.745 372.286 540.717 404.525 532.079C436.767 523.441 460.223 498.384 468.313 468.315C498.383 460.224 523.44 436.766 532.078 404.526C540.716 372.285 530.747 339.443 508.749 317.402V317.399ZM470.899 244.776C486.892 260.77 493.488 282.601 490.687 303.412L415.577 260.046C412.411 258.218 408.509 258.218 405.345 260.046L317.401 310.82V277.526C317.401 275.191 318.652 273.005 320.676 271.837L387.644 233.174C414.178 218.353 448.346 222.223 470.901 244.776H470.899ZM357.837 311.144L398.275 334.491V381.185L357.837 404.532L317.398 381.185V334.491L357.837 311.144ZM264.776 269.693C265.207 239.305 285.644 211.649 316.453 203.393C338.3 197.54 360.505 202.744 377.127 215.573L302.014 258.937C298.848 260.764 296.898 264.144 296.898 267.798V369.346L268.065 352.699C266.043 351.531 264.776 349.353 264.776 347.017V269.691V269.693ZM203.391 316.454C209.244 294.608 224.854 277.978 244.276 269.999V356.73C244.276 360.384 246.226 363.763 249.392 365.591L337.337 416.365L308.503 433.013C306.481 434.181 303.961 434.188 301.939 433.02L234.971 394.357C208.868 378.789 195.138 347.261 203.391 316.454ZM244.775 470.9C228.781 454.906 222.186 433.075 224.986 412.264L300.096 455.63C303.263 457.457 307.164 457.457 310.328 455.63L398.273 404.856V438.149C398.273 440.485 397.022 442.671 394.997 443.839L328.029 482.502C301.495 497.322 267.327 493.452 244.772 470.9H244.775ZM450.897 445.982C450.466 476.371 430.029 504.027 399.22 512.283C377.373 518.136 355.168 512.932 338.547 500.102L413.659 456.738C416.826 454.911 418.775 451.532 418.775 447.877V346.329L447.609 362.977C449.631 364.145 450.897 366.323 450.897 368.659V445.985V445.982ZM512.282 399.221C506.429 421.068 490.819 437.697 471.397 445.676V358.946C471.397 355.292 469.448 351.912 466.281 350.085L378.336 299.311L407.17 282.663C409.192 281.495 411.712 281.487 413.734 282.655L480.702 321.318C506.805 336.887 520.536 368.415 512.282 399.221Z" fill="currentColor"/>
            </svg>
          </div>
        `
      case 'claude':
        return `
          <div class="recent-item-logo badge-claude" title="Claude">
            <svg viewBox="0 0 16 16" width="11" height="11" fill="currentColor">
              <path fill-rule="evenodd" d="M9.218 2h2.402L16 12.987h-2.402zM4.379 2h2.512l4.38 10.987H8.82l-.895-2.308h-4.58l-.896 2.307H0L4.38 2.001zm2.755 6.64L5.635 4.777 4.137 8.64z"/>
            </svg>
          </div>
        `
      case 'gemini':
        return `
          <div class="recent-item-logo badge-gemini" title="Gemini">
            <svg viewBox="0 0 24 24" width="11" height="11">
              <defs>
                <linearGradient id="gemini-grad" x1="0%" y1="0%" x2="100%" y2="100%">
                  <stop offset="0%" stop-color="#1BA1E3" />
                  <stop offset="50%" stop-color="#5460E6" />
                  <stop offset="100%" stop-color="#9162C0" />
                </linearGradient>
              </defs>
              <path d="M12 0C12 6.627 6.627 12 0 12c6.627 0 12 5.627 12 12 0-6.373 5.627-12 12-12-6.373 0-12-5.373-12-12Z" fill="url(#gemini-grad)" />
            </svg>
          </div>
        `
      default:
        return `<div class="recent-item-logo badge-chatgpt"><span style="font-size: 10px; font-weight: 700;">AI</span></div>`
    }
  }

  // ─── RENDERERS ───────────────────────────────────────────────────────────

  function renderMetricsAndBreakdown(): void {
    if (totalInteractionsEl) {
      totalInteractionsEl.textContent = state.totalInteractions.toLocaleString()
    }
    if (totalConversationsEl) {
      totalConversationsEl.textContent = state.totalConversations.toLocaleString()
    }

    if (countChatgptEl) countChatgptEl.textContent = state.chatgptCount.toLocaleString()
    if (countClaudeEl) countClaudeEl.textContent = state.claudeCount.toLocaleString()
    if (countGeminiEl) countGeminiEl.textContent = state.geminiCount.toLocaleString()

    const denominator = Math.max(state.totalInteractions, 1)
    const active = state.totalInteractions > 0
    const gptPercent = Math.round((state.chatgptCount / denominator) * 100)
    const claudePercent = Math.round((state.claudeCount / denominator) * 100)
    const geminiPercent = Math.round((state.geminiCount / denominator) * 100)

    if (barChatgptEl) barChatgptEl.style.width = `${active ? gptPercent : 0}%`
    if (barClaudeEl) barClaudeEl.style.width = `${active ? claudePercent : 0}%`
    if (barGeminiEl) barGeminiEl.style.width = `${active ? geminiPercent : 0}%`

    if (percentChatgptEl) percentChatgptEl.textContent = `${gptPercent}% of interactions`
    if (percentClaudeEl) percentClaudeEl.textContent = `${claudePercent}% of interactions`
    if (percentGeminiEl) percentGeminiEl.textContent = `${geminiPercent}% of interactions`

    if (explorerBannerTitleEl) {
      explorerBannerTitleEl.textContent = state.isExplorerExpanded
        ? 'CLOSE INTERACTION EXPLORER'
        : 'OPEN INTERACTION EXPLORER'
    }

    if (explorerBannerSubEl) {
      if (state.isExplorerExpanded) {
        explorerBannerSubEl.textContent = 'Click to collapse the interaction explorer'
      } else {
        explorerBannerSubEl.textContent = `Browse, search and filter all ${state.totalInteractions} interactions`
      }
    }
  }

  function renderStorageMetrics(): void {
    const m = state.storageMetrics
    if (storageDatasetValEl) {
      storageDatasetValEl.textContent = m ? formatBytes(m.logicalDatasetBytes) : '—'
    }
    if (storageUsageValEl) {
      storageUsageValEl.textContent =
        m && m.browserUsageBytes !== null ? formatBytes(m.browserUsageBytes) : 'Unavailable'
    }
    if (storageQuotaValEl) {
      storageQuotaValEl.textContent =
        m && m.browserQuotaBytes !== null ? formatBytes(m.browserQuotaBytes) : 'Unavailable'
    }
    if (storageInteractionsValEl) {
      storageInteractionsValEl.textContent = m ? m.interactionCount.toLocaleString() : '—'
    }
    if (storageConversationsValEl) {
      storageConversationsValEl.textContent = m ? m.conversationCount.toLocaleString() : '—'
    }
    if (storageAvgValEl) {
      storageAvgValEl.textContent = m ? formatBytes(m.averageInteractionBytes) : '—'
    }
    if (storageQueryValEl) {
      storageQueryValEl.textContent = m ? formatBytes(m.queryBytes) : '—'
    }
    if (storageResponseValEl) {
      storageResponseValEl.textContent = m ? formatBytes(m.responseBytes) : '—'
    }
    if (storageMetadataValEl) {
      storageMetadataValEl.textContent = m ? formatBytes(m.metadataBytes) : '—'
    }
  }

  function renderRecentActivity(): void {
    if (!recentListEl) return
    // Render up to 3 preview cards so the recent activity section fits cleanly
    // without clipping a card in half at the bottom fold above the footer.
    const items = state.recentInteractions.slice(0, 3)

    if (recentCountBadgeEl) {
      // Badge shows the total stored interaction count, not the number of
      // preview cards rendered (which is capped at 3).
      recentCountBadgeEl.textContent = state.totalInteractions.toLocaleString()
    }

    if (items.length === 0) {
      recentListEl.innerHTML = `
        <div class="empty-state" id="empty-recent-state">
          <div class="empty-icon">
            <svg viewBox="0 0 24 24" width="22" height="22" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">
              <circle cx="12" cy="12" r="10"></circle>
              <line x1="12" y1="8" x2="12" y2="12"></line>
              <line x1="12" y1="16" x2="12.01" y2="16"></line>
            </svg>
          </div>
          <h3 class="empty-title">No interactions collected yet</h3>
          <p class="empty-desc">Start a conversation on ChatGPT, Claude, or Gemini and IntelliCache will capture it locally.</p>
        </div>`
      return
    }

    recentListEl.innerHTML = ''
    items.forEach((item) => {
      const safePlatform = escapeHtml(item.platform.replace(/[^a-zA-Z0-9_-]/g, ''))
      const card = document.createElement('div')
      card.className = `recent-item item-${safePlatform}`
      card.title = 'Click or press Enter to open in Explorer'
      card.setAttribute('role', 'button')
      card.setAttribute('tabindex', '0')

      const platformName =
        item.platform === 'chatgpt'
          ? 'ChatGPT'
          : item.platform === 'claude'
            ? 'Claude'
            : item.platform === 'gemini'
              ? 'Gemini'
              : 'AI'

      const title = item.conversation_title || 'Untitled Thread'
      const rawPrompt = item.query?.text ? cleanQueryText(item.query.text) : ''
      const querySnippet = rawPrompt ? safeSnippet(rawPrompt, 85) : '(Empty prompt)'
      const timeStr = formatRelativeTime(item.observed_at)
      const logoHtml = getProviderLogoHtml(item.platform)

      card.innerHTML = `
        <div class="recent-item-header">
          <div class="recent-item-brand">
            ${logoHtml}
            <span class="recent-platform-label recent-platform-${safePlatform}">${platformName}</span>
          </div>
          <span class="recent-time">${escapeHtml(timeStr)}</span>
        </div>
        <div class="recent-title">${escapeHtml(title)}</div>
        <div class="recent-query">${escapeHtml(querySnippet)}</div>
      `

      const openItemInExplorer = () => {
        expandExplorer()
        state.explorerFilter = toExplorerFilter(item.platform)
        updateFilterChipUI()
        renderExplorerItems()
      }

      card.addEventListener('click', openItemInExplorer)
      card.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          openItemInExplorer()
        }
      })

      recentListEl.appendChild(card)
    })
  }

  function renderExplorerItems(): void {
    if (!explorerItemsListEl) return

    let filtered = state.recentInteractions

    // Platform filter
    if (state.explorerFilter !== 'all') {
      filtered = filtered.filter((i) => i.platform === state.explorerFilter)
    }

    // Text search query
    const q = state.explorerSearchQuery.trim().toLowerCase()
    if (q) {
      filtered = filtered.filter((i) => {
        const queryText = cleanQueryText(i.query?.text || '').toLowerCase()
        const responseText = cleanResponseText(i.response?.text || '').toLowerCase()
        const titleText = i.conversation_title?.toLowerCase() || ''
        return queryText.includes(q) || responseText.includes(q) || titleText.includes(q)
      })
    }

    if (explorerMatchCountEl) {
      explorerMatchCountEl.textContent =
        filtered.length > EXPLORER_RENDER_LIMIT
          ? `showing ${EXPLORER_RENDER_LIMIT} of ${filtered.length} items`
          : `${filtered.length} item${filtered.length === 1 ? '' : 's'}`
    }

    if (filtered.length === 0) {
      explorerItemsListEl.innerHTML = `
        <div class="empty-state">
          <h3 class="empty-title">No matching interactions</h3>
          <p class="empty-desc">Try adjusting your search query or platform filter.</p>
        </div>`
      return
    }

    explorerItemsListEl.innerHTML = ''
    filtered.slice(0, EXPLORER_RENDER_LIMIT).forEach((item) => {
      const safePlatform = escapeHtml(item.platform.replace(/[^a-zA-Z0-9_-]/g, ''))
      const card = document.createElement('div')
      card.className = `explorer-card explorer-card-${safePlatform}`

      const platformName =
        item.platform === 'chatgpt'
          ? 'ChatGPT'
          : item.platform === 'claude'
            ? 'Claude'
            : item.platform === 'gemini'
              ? 'Gemini'
              : 'AI'

      const title = item.conversation_title || 'Untitled Thread'
      const promptText = cleanQueryText(item.query?.text || '')
      const respText = cleanResponseText(item.response?.text || '')
      const timeStr = formatRelativeTime(item.observed_at)
      const context = item.capture_context || 'on_load'
      const fpShort = item.fingerprint ? `${item.fingerprint.slice(0, 12)}...` : 'n/a'
      const queryChars = item.query?.characters ?? promptText.length
      const respChars = item.response?.characters ?? respText.length
      const logoHtml = getProviderLogoHtml(item.platform)

      card.innerHTML = `
        <div class="explorer-card-header">
          <div class="explorer-badges">
            ${logoHtml}
            <span class="recent-platform-label recent-platform-${safePlatform}">${platformName}</span>
            <span class="context-tag">${escapeHtml(context)}</span>
          </div>
          <span class="recent-time">${escapeHtml(timeStr)}</span>
        </div>

        <div class="recent-title">${escapeHtml(title)}</div>

        <div class="explorer-content-block">
          <div class="explorer-label">User Query (${queryChars} chars)</div>
          <div class="explorer-text">${escapeHtml(promptText)}</div>
        </div>

        <div class="explorer-content-block">
          <div class="explorer-label">Assistant Response (${respChars} chars)</div>
          <div class="explorer-text">${escapeHtml(respText)}</div>
        </div>

        <div class="explorer-footer-row">
          <span class="fingerprint-tag" title="SHA-256: ${escapeHtml(item.fingerprint || '')}">fp: ${escapeHtml(fpShort)}</span>
          <span>turn: ${item.message_id ? escapeHtml(item.message_id.slice(0, 8)) : 'turn-0'}</span>
        </div>
      `

      explorerItemsListEl.appendChild(card)
    })
  }

  function updateFilterChipUI(): void {
    if (!filterChipsEl) return
    const chips = filterChipsEl.querySelectorAll<HTMLButtonElement>('.chip')
    chips.forEach((chip) => {
      const f = chip.getAttribute('data-filter')
      if (f === state.explorerFilter) {
        chip.classList.add('active')
      } else {
        chip.classList.remove('active')
      }
    })
  }

  function expandExplorer(): void {
    state.isExplorerExpanded = true
    if (explorerSectionEl) explorerSectionEl.classList.remove('collapsed')
    if (toggleExplorerText) toggleExplorerText.textContent = 'Collapse'
    if (openExplorerBanner) {
      openExplorerBanner.classList.add('expanded')
      openExplorerBanner.setAttribute('aria-expanded', 'true')
      openExplorerBanner.setAttribute('aria-label', 'Close interaction explorer')
    }
    if (explorerBannerTitleEl) {
      explorerBannerTitleEl.textContent = 'CLOSE INTERACTION EXPLORER'
    }
    if (explorerBannerSubEl) {
      explorerBannerSubEl.textContent = 'Click to collapse the interaction explorer'
    }
    renderExplorerItems()
    explorerSectionEl?.scrollIntoView({ behavior: 'smooth' })
  }

  function collapseExplorer(): void {
    state.isExplorerExpanded = false
    if (explorerSectionEl) explorerSectionEl.classList.add('collapsed')
    if (toggleExplorerText) toggleExplorerText.textContent = 'View All'
    if (openExplorerBanner) {
      openExplorerBanner.classList.remove('expanded')
      openExplorerBanner.setAttribute('aria-expanded', 'false')
      openExplorerBanner.setAttribute('aria-label', 'Open interaction explorer')
    }
    if (explorerBannerTitleEl) {
      explorerBannerTitleEl.textContent = 'OPEN INTERACTION EXPLORER'
    }
    if (explorerBannerSubEl) {
      explorerBannerSubEl.textContent = `Browse, search and filter all ${state.totalInteractions} interactions`
    }
  }

  // ─── STATUS & STATS LOADER ───────────────────────────────────────────────

  async function refreshStats(silent = false): Promise<void> {
    try {
      const statsMsg = createDbGetStatsMessage('popup')
      const statsRes = await sendExtensionMessage<typeof statsMsg, DbStatsResponseData>(statsMsg)

      if (statsRes && statsRes.success && statsRes.data) {
        const d = statsRes.data
        state.totalInteractions = d.interactionCount
        state.totalConversations = d.conversationCount
        state.chatgptCount = d.platformCounts?.chatgpt ?? 0
        state.claudeCount = d.platformCounts?.claude ?? 0
        state.geminiCount = d.platformCounts?.gemini ?? 0
        state.recentInteractions = d.recentInteractions ?? []

        if (dbStorageValEl) {
          dbStorageValEl.textContent = `IndexedDB (${state.totalInteractions} items)`
        }
        if (dbConnectionValEl) {
          dbConnectionValEl.textContent = `Connected (v${d.dbVersion})`
        }
        if (healthSummaryBadgeEl) {
          healthSummaryBadgeEl.innerHTML = `
            <svg viewBox="0 0 24 24" width="11" height="11" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round">
              <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"></path>
            </svg>
            <span>Healthy</span>`
        }

        renderMetricsAndBreakdown()
        renderRecentActivity()
        renderExplorerItems()

        if (!silent) {
          appendLog(
            `Database 'intelliCache' connected: ${state.totalInteractions} interactions, ${state.totalConversations} conversations`,
            'success'
          )
        }
      } else if (!silent) {
        appendLog(`Failed to fetch database stats: ${statsRes?.error ?? 'Unknown error'}`, 'error')
      }
    } catch (err) {
      if (!silent) {
        appendLog(
          `Failed to fetch database stats: ${err instanceof Error ? err.message : String(err)}`,
          'error'
        )
      }
    }
  }

  async function checkInitialStatus(): Promise<void> {
    try {
      updateStatusPill('CONNECTING')
      appendLog('Connecting to IntelliCache background service worker...', 'info')

      const statusMsg = createGetStatusMessage('popup')
      const statusRes = await sendExtensionMessage<typeof statusMsg, StatusResponseData>(statusMsg)

      if (statusRes && statusRes.success && statusRes.data) {
        updateStatusPill('ACTIVE')
        if (swStatusValEl) swStatusValEl.textContent = 'Active (MV3)'
        if (extVersionValEl) extVersionValEl.textContent = statusRes.data.version
        appendLog(`Service Worker connected (v${statusRes.data.version}, manifest v3)`, 'success')
      } else {
        updateStatusPill('OFFLINE')
        if (swStatusValEl) swStatusValEl.textContent = 'Offline'
        appendLog(
          'Service worker not responding to GET_STATUS. Checking database directly...',
          'warn'
        )
      }

      // Fetch live database metrics
      await refreshStats(false)

      // Storage footprint measured once on dashboard open (explicit refresh
      // available via the Storage card button).
      void loadStorageMetrics('open')
    } catch (err) {
      logger.error('UI', 'CORE', 'Error during initial popup status check', { error: err })
      updateStatusPill('ERROR')
      if (swStatusValEl) swStatusValEl.textContent = 'Error'
      if (healthSummaryBadgeEl) {
        healthSummaryBadgeEl.innerHTML = `<span>Error</span>`
        healthSummaryBadgeEl.style.color = 'var(--error)'
      }
      appendLog(`Status check failed: ${err instanceof Error ? err.message : String(err)}`, 'error')
    }
  }

  /**
   * Loads storage metrics from the background service worker and renders the
   * Storage card. Runs on dashboard open and on explicit refresh only — never
   * on DOM mutations or inside scraping hot paths.
   */
  async function loadStorageMetrics(reason: 'open' | 'refresh'): Promise<void> {
    if (storageRefreshBtn) storageRefreshBtn.disabled = true
    try {
      const msg = createDbGetStorageMetricsMessage('popup')
      const res = await sendExtensionMessage<typeof msg, StorageMetricsResponseData>(msg)
      if (res && res.success && res.data) {
        state.storageMetrics = res.data
        renderStorageMetrics()
        if (reason === 'refresh') {
          appendLog(
            `Storage metrics refreshed: ~${formatBytes(res.data.logicalDatasetBytes)} dataset across ${res.data.interactionCount} interactions`,
            'success'
          )
        }
      } else {
        state.storageMetrics = null
        renderStorageMetrics()
        appendLog(`Storage metrics unavailable: ${res?.error ?? 'Unknown error'}`, 'warn')
      }
    } catch (err) {
      state.storageMetrics = null
      renderStorageMetrics()
      appendLog(
        `Storage metrics error: ${err instanceof Error ? err.message : String(err)}`,
        'warn'
      )
    } finally {
      if (storageRefreshBtn) storageRefreshBtn.disabled = false
    }
  }

  // ─── EVENT LISTENERS ─────────────────────────────────────────────────────

  // Refresh Storage Metrics (explicit refresh + dashboard open; never on DOM mutations)
  storageRefreshBtn?.addEventListener('click', () => {
    void loadStorageMetrics('refresh')
  })

  // Toggle Explorer button
  toggleExplorerBtn?.addEventListener('click', () => {
    if (state.isExplorerExpanded) {
      collapseExplorer()
    } else {
      expandExplorer()
    }
  })

  openExplorerBanner?.addEventListener('click', () => {
    if (state.isExplorerExpanded) {
      collapseExplorer()
    } else {
      expandExplorer()
    }
  })

  openExplorerBanner?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault()
      if (state.isExplorerExpanded) {
        collapseExplorer()
      } else {
        expandExplorer()
      }
    }
  })

  // Header click / close icon in explorer
  explorerHeaderToggleEl?.addEventListener('click', () => {
    if (state.isExplorerExpanded) {
      collapseExplorer()
    } else {
      expandExplorer()
    }
  })

  explorerCloseBtn?.addEventListener('click', (e) => {
    e.stopPropagation()
    collapseExplorer()
  })

  // Search input debounced
  let searchDebounceTimeout: number | undefined
  explorerSearchInput?.addEventListener('input', () => {
    window.clearTimeout(searchDebounceTimeout)
    searchDebounceTimeout = window.setTimeout(() => {
      state.explorerSearchQuery = explorerSearchInput.value
      if (explorerSearchClearBtn) {
        explorerSearchClearBtn.hidden = state.explorerSearchQuery.length === 0
      }
      renderExplorerItems()
    }, 150)
  })

  explorerSearchClearBtn?.addEventListener('click', () => {
    if (explorerSearchInput) explorerSearchInput.value = ''
    state.explorerSearchQuery = ''
    if (explorerSearchClearBtn) explorerSearchClearBtn.hidden = true
    renderExplorerItems()
  })

  // Platform Filter Chips
  filterChipsEl?.addEventListener('click', (e) => {
    const target = (e.target as HTMLElement).closest('.chip')
    if (!target) return
    const filter = toExplorerFilter(target.getAttribute('data-filter'))
    state.explorerFilter = filter
    updateFilterChipUI()
    renderExplorerItems()
  })

  // Diagnostics Accordion Toggle
  diagnosticsToggleEl?.addEventListener('click', () => {
    state.isDiagnosticsExpanded = !state.isDiagnosticsExpanded
    diagnosticsToggleEl.setAttribute('aria-expanded', String(state.isDiagnosticsExpanded))
    if (diagnosticsContentEl) {
      diagnosticsContentEl.hidden = !state.isDiagnosticsExpanded
    }
  })

  // Ping Service Worker
  pingBtn?.addEventListener('click', async () => {
    if (!pingBtn) return
    pingBtn.disabled = true
    const startTime = performance.now()
    appendLog('Sending PING to Service Worker...', 'info')

    try {
      const pingMessage = createPingMessage('popup', 'Diagnostic ping from popup UI')
      const response = await sendExtensionMessage<typeof pingMessage, PingResponseData>(pingMessage)
      const latency = Math.round(performance.now() - startTime)

      if (response && response.success && response.data) {
        appendLog(
          `Received PONG (RTT: ${latency}ms, Echo: ${new Date(response.data.echoTimestamp).toLocaleTimeString()})`,
          'success'
        )
        updateStatusPill('ACTIVE')
      } else {
        appendLog(`Ping failed: ${response?.error ?? 'No response'}`, 'error')
        updateStatusPill('ERROR')
      }
    } catch (err) {
      appendLog(`Ping error: ${err instanceof Error ? err.message : String(err)}`, 'error')
      updateStatusPill('ERROR')
    } finally {
      pingBtn.disabled = false
    }
  })

  // Run Integrity Audit
  integrityBtn?.addEventListener('click', async () => {
    if (!integrityBtn) return
    integrityBtn.disabled = true
    appendLog('Starting database integrity check...', 'info')

    try {
      const msg = createDbGetIntegrityReportMessage('popup')
      const response = await sendExtensionMessage<typeof msg, DbIntegrityReportData>(msg)

      if (response && response.success && response.data) {
        const d = response.data
        appendLog(
          `Integrity OK: ${d.conversations.total} conversations (${d.conversations.duplicates} dupes), ${d.interactions.total} interactions (${d.interactions.duplicateFingerprints} dupes)`,
          'success'
        )
      } else {
        appendLog(`Integrity report failed: ${response?.error ?? 'Error'}`, 'error')
      }
    } catch (err) {
      appendLog(`Integrity error: ${err instanceof Error ? err.message : String(err)}`, 'error')
    } finally {
      integrityBtn.disabled = false
    }
  })

  // Export JSON Dataset
  exportBtn?.addEventListener('click', () => {
    if (!exportBtn) return
    exportBtn.disabled = true
    appendLog('Preparing dataset export...', 'info')

    try {
      const exportPayload = {
        meta: {
          exported_at: new Date().toISOString(),
          collector: 'IntelliCache',
          version: '0.1.0',
          scope: 'recent-window',
          scope_note: 'Popup exports the loaded recent-interactions window, not the full database.',
          total_interactions: state.totalInteractions,
          total_conversations: state.totalConversations,
          exported_records: state.recentInteractions.length,
          breakdown: {
            chatgpt: state.chatgptCount,
            claude: state.claudeCount,
            gemini: state.geminiCount,
          },
        },
        interactions: state.recentInteractions,
      }

      const jsonStr = JSON.stringify(exportPayload, null, 2)
      const blob = new Blob([jsonStr], { type: 'application/json' })
      const blobUrl = URL.createObjectURL(blob)

      const downloadAnchor = document.createElement('a')
      downloadAnchor.href = blobUrl
      downloadAnchor.download = `intellicache-export-${new Date().toISOString().slice(0, 10)}.json`
      document.body.appendChild(downloadAnchor)
      downloadAnchor.click()
      document.body.removeChild(downloadAnchor)
      setTimeout(() => {
        URL.revokeObjectURL(blobUrl)
      }, EXPORT_BLOB_REVOKE_MS)

      appendLog(
        `Exported ${state.recentInteractions.length} recent interaction records to JSON (of ${state.totalInteractions} total)`,
        'success'
      )
    } catch (err) {
      appendLog(`Export error: ${err instanceof Error ? err.message : String(err)}`, 'error')
    } finally {
      exportBtn.disabled = false
    }
  })

  // Clear Diagnostic Log
  clearLogBtn?.addEventListener('click', () => {
    if (logOutputEl) {
      logOutputEl.innerHTML = ''
      appendLog('Diagnostic log cleared.', 'info')
    }
  })

  // Theme Toggle
  themeToggleBtn?.addEventListener('click', toggleTheme)

  // Standalone Window & Side Panel Controls
  sidepanelBtn?.addEventListener('click', async () => {
    appendLog('Opening persistent Side Panel...', 'info')
    const success = await openSidePanel()
    if (success) {
      if (!isStandaloneOrSidepanel()) {
        window.close()
      }
    } else {
      appendLog(
        'Side Panel not supported by browser. Opening persistent floating window...',
        'warn'
      )
      await openStandaloneWindow()
      if (!isStandaloneOrSidepanel()) {
        window.close()
      }
    }
  })

  popoutBtn?.addEventListener('click', async () => {
    appendLog('Opening persistent floating window...', 'info')
    const success = await openStandaloneWindow()
    if (success && !isStandaloneOrSidepanel()) {
      window.close()
    }
  })

  // Display Mode / Toolbar Click Selector
  function updateDisplayModeChips(activeMode: DisplayMode): void {
    modeChips.forEach((chip) => {
      const mode = chip.getAttribute('data-mode')
      const isActive = mode === activeMode
      chip.classList.toggle('active', isActive)
      chip.setAttribute('aria-checked', String(isActive))
    })
  }

  // Read saved display mode preference
  try {
    const savedDisplayMode = localStorage.getItem('intellicache_display_mode') as DisplayMode | null
    if (
      savedDisplayMode === 'popup' ||
      savedDisplayMode === 'sidepanel' ||
      savedDisplayMode === 'window'
    ) {
      state.displayMode = savedDisplayMode
    }
  } catch {
    // localStorage may fail in private mode
  }
  updateDisplayModeChips(state.displayMode)

  modeSelector?.addEventListener('click', async (e) => {
    const target = (e.target as HTMLElement).closest<HTMLButtonElement>('.mode-chip')
    if (!target) return
    const selectedMode = target.getAttribute('data-mode') as DisplayMode | null
    if (!selectedMode || selectedMode === state.displayMode) return

    state.displayMode = selectedMode
    try {
      localStorage.setItem('intellicache_display_mode', selectedMode)
    } catch {
      // ignore
    }
    updateDisplayModeChips(selectedMode)

    appendLog(`Configuring toolbar click behavior to '${selectedMode}'...`, 'info')
    const ok = await configureActionDisplayMode(selectedMode)
    if (ok) {
      appendLog(`Toolbar click behavior updated: ${selectedMode}`, 'success')
    } else {
      appendLog('Could not update toolbar click behavior: API unavailable', 'warn')
    }
  })

  // Live messaging listener for instant dashboard sync while persistent panel is open
  let liveUpdateTimeout: number | undefined
  const runtime = getBrowserRuntime()
  if (runtime?.onMessage) {
    try {
      runtime.onMessage.addListener(
        (msg: unknown, _sender: unknown, sendResponse?: (res: unknown) => void) => {
          if (
            typeof msg === 'object' &&
            msg !== null &&
            (msg as { type?: string }).type === 'INTERACTION_SAVED'
          ) {
            sendResponse?.({ acknowledged: true })
            // Debounce: coalesce rapid bursts (e.g. on_load + on_generate for the same turn)
            // into a single refreshStats call. Storage metrics (full table scan) are NOT
            // triggered here — they are expensive and only needed on explicit refresh.
            window.clearTimeout(liveUpdateTimeout)
            liveUpdateTimeout = window.setTimeout(() => {
              void refreshStats(true)
            }, 300)
          }
        }
      )
    } catch {
      // runtime.onMessage unavailable in non-extension environment
    }
  }

  // Refresh stats when the window gains focus (user switching back to sidepanel/popout)
  window.addEventListener('focus', () => {
    void refreshStats(true)
  })

  // Initialize theme
  initTheme()

  // Run initial status check
  void checkInitialStatus()
})
