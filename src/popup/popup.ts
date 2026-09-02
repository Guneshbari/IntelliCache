/**
 * IntelliCache Collector - Modern Popup Dashboard Controller
 * Manages live IndexedDB stats, platform collection breakdown with provider logos,
 * recent interaction preview, interactive explorer, system health, and diagnostics & tools suite.
 */

import { logger } from '../diagnostics'
import {
  createDbGetIntegrityReportMessage,
  createDbGetStatsMessage,
  createGetStatusMessage,
  createPingMessage,
  sendExtensionMessage,
} from '../shared/messages'
import type {
  DbIntegrityReportData,
  DbStatsResponseData,
  Interaction,
  PingResponseData,
  StatusResponseData,
} from '../shared/types'

// Global in-memory UI state (reset atomically on each fresh query)
interface PopupState {
  theme: 'dark' | 'light'
  totalInteractions: number
  totalConversations: number
  chatgptCount: number
  claudeCount: number
  geminiCount: number
  recentInteractions: Interaction[]
  explorerFilter: 'all' | 'chatgpt' | 'claude' | 'gemini'
  explorerSearchQuery: string
  isExplorerExpanded: boolean
  isDiagnosticsExpanded: boolean
}

const state: PopupState = {
  theme: 'dark',
  totalInteractions: 0,
  totalConversations: 0,
  chatgptCount: 0,
  claudeCount: 0,
  geminiCount: 0,
  recentInteractions: [],
  explorerFilter: 'all',
  explorerSearchQuery: '',
  isExplorerExpanded: false,
  isDiagnosticsExpanded: true,
}

document.addEventListener('DOMContentLoaded', () => {
  // DOM Elements
  const themeToggleBtn = document.getElementById('theme-toggle-btn') as HTMLButtonElement | null
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
    // Default to dark mode with pure black background
    applyTheme('dark')
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

  function escapeHtml(str: string): string {
    return str
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
            <svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor">
              <path d="M22.28 9.82a5.98 5.98 0 0 0-.52-4.91 6.05 6.05 0 0 0-6.51-2.9A6.06 6.06 0 0 0 4.98 4.18a5.98 5.98 0 0 0-4 2.9 6.05 6.05 0 0 0 .74 7.1 5.98 5.98 0 0 0 .51 4.91 6.05 6.05 0 0 0 6.51 2.9A5.98 5.98 0 0 0 13.26 24a6.06 6.06 0 0 0 5.77-4.2 5.99 5.99 0 0 0 4-2.9 6.06 6.06 0 0 0-.75-7.08zm-9.02 12.61a4.48 4.48 0 0 1-2.88-1.04l.14-.08 4.78-2.76a.8.8 0 0 0 .39-.68v-6.74l2.02 1.17a.07.07 0 0 1 .04.05v5.58a4.5 4.5 0 0 1-4.49 4.5zm-9.66-4.13a4.47 4.47 0 0 1-.53-3.01l.14.08 4.78 2.76a.77.77 0 0 0 .78 0l5.84-3.37v2.33a.08.08 0 0 1-.03.06L9.74 19.95a4.5 4.5 0 0 1-6.14-1.65zM2.34 7.9a4.49 4.49 0 0 1 2.37-1.98v5.68a.77.77 0 0 0 .38.68l5.82 3.35-2.02 1.17a.08.08 0 0 1-.07 0l-4.83-2.79A4.5 4.5 0 0 1 2.34 7.9zm16.1 3.86L12.6 8.38l2.02-1.16a.08.08 0 0 1 .07 0l4.83 2.79a4.5 4.5 0 0 1-.68 8.1v-5.68a.79.79 0 0 0-.4-.68zm2.01-3.03l-.14-.08-4.78-2.78a.78.78 0 0 0-.79 0L8.91 9.23V6.9a.07.07 0 0 1 .03-.06l4.83-2.79a4.5 4.5 0 0 1 6.68 4.68zm-12.64 4.14a.77.77 0 0 0-.39-.68L2.59 9.35a4.5 4.5 0 0 1 6.55-2.52v5.59a.79.79 0 0 0 .39.68l5.83 3.37-2.02 1.17a.08.08 0 0 1-.07 0l-4.83-2.79z" />
            </svg>
          </div>
        `
      case 'claude':
        return `
          <div class="recent-item-logo badge-claude" title="Claude">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor">
              <path d="M12 9.5a2.5 2.5 0 1 1 0 5 2.5 2.5 0 0 1 0-5z" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" />
              <rect x="11.2" y="14.5" width="1.6" height="8.5" rx="0.8" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(25.7 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(51.4 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(77.1 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(102.8 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(128.5 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(154.2 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(180 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(205.7 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(231.4 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(257.1 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(282.8 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(308.5 12 12)" />
              <rect x="11.2" y="1" width="1.6" height="8.5" rx="0.8" transform="rotate(334.2 12 12)" />
            </svg>
          </div>
        `
      case 'gemini':
        return `
          <div class="recent-item-logo badge-gemini" title="Gemini">
            <svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor">
              <path d="M12 1.5C12 7.3 7.3 12 1.5 12C7.3 12 12 16.7 12 22.5C12 16.7 16.7 12 22.5 12C16.7 12 12 7.3 12 1.5Z" />
            </svg>
          </div>
        `
      default:
        return `<div class="recent-item-logo badge-chatgpt"><span style="font-size: 8px;">AI</span></div>`
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

    const max = Math.max(state.totalInteractions, 1)
    const gptPercent = Math.round((state.chatgptCount / max) * 100)
    const claudePercent = Math.round((state.claudeCount / max) * 100)
    const geminiPercent = Math.round((state.geminiCount / max) * 100)

    if (barChatgptEl) barChatgptEl.style.width = `${state.totalInteractions > 0 ? gptPercent : 0}%`
    if (barClaudeEl) barClaudeEl.style.width = `${state.totalInteractions > 0 ? claudePercent : 0}%`
    if (barGeminiEl) barGeminiEl.style.width = `${state.totalInteractions > 0 ? geminiPercent : 0}%`

    if (percentChatgptEl) percentChatgptEl.textContent = `${gptPercent}% of interactions`
    if (percentClaudeEl) percentClaudeEl.textContent = `${claudePercent}% of interactions`
    if (percentGeminiEl) percentGeminiEl.textContent = `${geminiPercent}% of interactions`

    if (explorerBannerSubEl) {
      explorerBannerSubEl.textContent = `Browse, search and filter all ${state.totalInteractions} interactions`
    }
  }

  function renderRecentActivity(): void {
    if (!recentListEl) return
    const items = state.recentInteractions.slice(0, 4)

    if (recentCountBadgeEl) {
      recentCountBadgeEl.textContent = `${items.length}`
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
      const card = document.createElement('div')
      card.className = `recent-item item-${item.platform}`
      card.title = 'Click to open in Explorer'

      const platformName =
        item.platform === 'chatgpt'
          ? 'ChatGPT'
          : item.platform === 'claude'
            ? 'Claude'
            : item.platform === 'gemini'
              ? 'Gemini'
              : 'AI'

      const title = item.conversation_title || 'Untitled Thread'
      const querySnippet = item.query?.text ? item.query.text.slice(0, 85) : '(Empty prompt)'
      const timeStr = formatRelativeTime(item.observed_at)
      const logoHtml = getProviderLogoHtml(item.platform)

      card.innerHTML = `
        <div class="recent-item-header">
          <div class="recent-item-brand">
            ${logoHtml}
            <span class="recent-platform-label recent-platform-${item.platform}">${platformName}</span>
          </div>
          <span class="recent-time">${escapeHtml(timeStr)}</span>
        </div>
        <div class="recent-title">${escapeHtml(title)}</div>
        <div class="recent-query">${escapeHtml(querySnippet)}</div>
      `

      card.addEventListener('click', () => {
        expandExplorer()
        state.explorerFilter = item.platform as typeof state.explorerFilter
        updateFilterChipUI()
        renderExplorerItems()
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
        const queryText = i.query?.text?.toLowerCase() || ''
        const responseText = i.response?.text?.toLowerCase() || ''
        const titleText = i.conversation_title?.toLowerCase() || ''
        return queryText.includes(q) || responseText.includes(q) || titleText.includes(q)
      })
    }

    if (explorerMatchCountEl) {
      explorerMatchCountEl.textContent = `${filtered.length} item${filtered.length === 1 ? '' : 's'}`
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
    filtered.forEach((item) => {
      const card = document.createElement('div')
      card.className = 'explorer-card'

      const platformName =
        item.platform === 'chatgpt'
          ? 'ChatGPT'
          : item.platform === 'claude'
            ? 'Claude'
            : item.platform === 'gemini'
              ? 'Gemini'
              : 'AI'

      const title = item.conversation_title || 'Untitled Thread'
      const promptText = item.query?.text || ''
      const respText = item.response?.text || ''
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
            <span class="recent-platform-label recent-platform-${item.platform}">${platformName}</span>
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
    renderExplorerItems()
    explorerSectionEl?.scrollIntoView({ behavior: 'smooth' })
  }

  function collapseExplorer(): void {
    state.isExplorerExpanded = false
    if (explorerSectionEl) explorerSectionEl.classList.add('collapsed')
    if (toggleExplorerText) toggleExplorerText.textContent = 'View All'
  }

  // ─── STATUS & STATS LOADER ───────────────────────────────────────────────

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
          dbConnectionValEl.textContent = 'Connected (v1)'
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

        appendLog(
          `Database 'intelliCache' connected: ${state.totalInteractions} interactions, ${state.totalConversations} conversations`,
          'success'
        )
      } else {
        appendLog(`Failed to fetch database stats: ${statsRes?.error ?? 'Unknown error'}`, 'error')
      }
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

  // ─── EVENT LISTENERS ─────────────────────────────────────────────────────

  // Toggle Explorer button
  toggleExplorerBtn?.addEventListener('click', () => {
    if (state.isExplorerExpanded) {
      collapseExplorer()
    } else {
      expandExplorer()
    }
  })

  openExplorerBanner?.addEventListener('click', () => {
    expandExplorer()
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
    if (state.isExplorerExpanded) {
      collapseExplorer()
    } else {
      expandExplorer()
    }
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
    const filter = target.getAttribute('data-filter') as PopupState['explorerFilter']
    if (filter) {
      state.explorerFilter = filter
      updateFilterChipUI()
      renderExplorerItems()
    }
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
          total_interactions: state.totalInteractions,
          total_conversations: state.totalConversations,
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
      URL.revokeObjectURL(blobUrl)

      appendLog(
        `Exported ${state.recentInteractions.length} interaction records to JSON`,
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

  // Initialize theme
  initTheme()

  // Run initial status check
  void checkInitialStatus()
})
