/**
 * IntelliCache Collector - Modern Popup Dashboard Controller
 * Manages live IndexedDB stats, platform collection breakdown, recent interaction preview,
 * interactive interaction explorer, system health monitoring, and collapsed diagnostics.
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
  isDiagnosticsExpanded: false,
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

  const recentListEl = document.getElementById('recent-activity-list')
  const recentCountBadgeEl = document.getElementById('recent-count-badge')
  const toggleExplorerBtn = document.getElementById('toggle-explorer-btn')
  const toggleExplorerText = document.getElementById('toggle-explorer-text')

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
    timeSpan.textContent = new Date().toLocaleTimeString()

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
      card.className = 'recent-item'
      card.title = 'Click to open in Explorer'

      const platformClass = `pill-${item.platform}`
      const platformName =
        item.platform === 'chatgpt'
          ? 'ChatGPT'
          : item.platform === 'claude'
            ? 'Claude'
            : item.platform === 'gemini'
              ? 'Gemini'
              : 'AI'

      const title = item.conversation_title || 'Untitled Thread'
      const querySnippet = item.query?.text ? item.query.text.slice(0, 90) : '(Empty prompt)'
      const timeStr = formatRelativeTime(item.observed_at)

      card.innerHTML = `
        <div class="recent-item-header">
          <span class="platform-pill ${platformClass}">${platformName}</span>
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

      const platformClass = `pill-${item.platform}`
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

      card.innerHTML = `
        <div class="explorer-card-header">
          <div class="explorer-badges">
            <span class="platform-pill ${platformClass}">${platformName}</span>
            <span class="context-tag">${escapeHtml(context)}</span>
          </div>
          <span class="recent-time">${escapeHtml(timeStr)}</span>
        </div>

        <div class="recent-title">${escapeHtml(title)}</div>

        <div class="explorer-content-block">
          <span class="explorer-label">Prompt (${queryChars} chars)</span>
          <div class="explorer-text collapsed-text" data-type="prompt">${escapeHtml(promptText)}</div>
        </div>

        <div class="explorer-content-block">
          <span class="explorer-label">Response (${respChars} chars)</span>
          <div class="explorer-text collapsed-text" data-type="response">${escapeHtml(respText)}</div>
        </div>

        <div class="explorer-footer-row">
          <span class="fingerprint-tag">FP: ${escapeHtml(fpShort)}</span>
          <button class="btn-text btn-expand-card" type="button">Expand details</button>
        </div>
      `

      // Toggle expand on card text containers
      const expandBtn = card.querySelector('.btn-expand-card') as HTMLButtonElement | null
      const textBlocks = card.querySelectorAll('.explorer-text')

      expandBtn?.addEventListener('click', () => {
        const isCollapsed = textBlocks[0]?.classList.contains('collapsed-text')
        textBlocks.forEach((tb) => {
          if (isCollapsed) {
            tb.classList.remove('collapsed-text')
          } else {
            tb.classList.add('collapsed-text')
          }
        })
        if (expandBtn) {
          expandBtn.textContent = isCollapsed ? 'Collapse details' : 'Expand details'
        }
      })

      explorerItemsListEl.appendChild(card)
    })
  }

  function updateFilterChipUI(): void {
    if (!filterChipsEl) return
    const chips = filterChipsEl.querySelectorAll('.chip')
    chips.forEach((c) => {
      const filterVal = c.getAttribute('data-filter')
      if (filterVal === state.explorerFilter) {
        c.classList.add('active')
      } else {
        c.classList.remove('active')
      }
    })
  }

  function expandExplorer(): void {
    state.isExplorerExpanded = true
    explorerSectionEl?.classList.remove('collapsed')
    if (toggleExplorerText) toggleExplorerText.textContent = 'Collapse'
    renderExplorerItems()
  }

  function collapseExplorer(): void {
    state.isExplorerExpanded = false
    explorerSectionEl?.classList.add('collapsed')
    if (toggleExplorerText) toggleExplorerText.textContent = 'Explore All'
  }

  // ─── INITIALIZATION & DATA LOADING ────────────────────────────────────────

  async function checkInitialStatus(): Promise<void> {
    try {
      updateStatusPill('CONNECTING')

      // Query service worker runtime status
      const statusMessage = createGetStatusMessage('popup')
      const statusResponse = await sendExtensionMessage<typeof statusMessage, StatusResponseData>(
        statusMessage
      )

      if (statusResponse && statusResponse.success && statusResponse.data) {
        const statusData = statusResponse.data
        if (extVersionValEl) extVersionValEl.textContent = statusData.version
        if (swStatusValEl) swStatusValEl.textContent = 'Active (MV3)'
        updateStatusPill('ACTIVE')

        appendLog(
          `Service Worker connected (v${statusData.version}, manifest v${statusData.manifestVersion})`,
          'success'
        )

        // Query database statistics and recent interactions
        const statsMessage = createDbGetStatsMessage('popup')
        const statsResponse = await sendExtensionMessage<typeof statsMessage, DbStatsResponseData>(
          statsMessage
        )

        if (statsResponse && statsResponse.success && statsResponse.data) {
          const stats = statsResponse.data

          state.totalInteractions = stats.interactionCount
          state.totalConversations = stats.conversationCount
          state.chatgptCount = stats.platformCounts?.chatgpt ?? 0
          state.claudeCount = stats.platformCounts?.claude ?? 0
          state.geminiCount = stats.platformCounts?.gemini ?? 0
          state.recentInteractions = stats.recentInteractions || []

          if (dbConnectionValEl) {
            dbConnectionValEl.textContent = `Connected (v${stats.dbVersion})`
          }
          if (dbStorageValEl) {
            dbStorageValEl.textContent = `IndexedDB (${stats.interactionCount} items)`
          }
          if (healthSummaryBadgeEl) {
            healthSummaryBadgeEl.textContent = 'Healthy'
            healthSummaryBadgeEl.style.color = 'var(--success)'
          }

          renderMetricsAndBreakdown()
          renderRecentActivity()
          renderExplorerItems()

          appendLog(
            `Database '${stats.dbName}' connected: ${stats.interactionCount} interactions, ${stats.conversationCount} conversations`,
            'info'
          )

          logger.info(
            'UI',
            'CORE',
            `query=interactions raw=${stats.interactionCount} filtered=${stats.interactionCount} grouped=${stats.interactionCount} rendered=${stats.interactionCount}`
          )
        }
      } else {
        updateStatusPill('OFFLINE')
        if (swStatusValEl) swStatusValEl.textContent = 'Inactive'
        if (healthSummaryBadgeEl) {
          healthSummaryBadgeEl.textContent = 'Disconnected'
          healthSummaryBadgeEl.style.color = 'var(--warning)'
        }
        appendLog(`Service worker unreachable: ${statusResponse?.error ?? 'No response'}`, 'warn')
      }
    } catch (err) {
      updateStatusPill('ERROR')
      if (swStatusValEl) swStatusValEl.textContent = 'Error'
      if (healthSummaryBadgeEl) {
        healthSummaryBadgeEl.textContent = 'Error'
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

  // Clear Diagnostic Log
  clearLogBtn?.addEventListener('click', () => {
    if (logOutputEl) {
      logOutputEl.innerHTML = ''
      appendLog('Log cleared.', 'info')
    }
  })

  // Theme Toggle
  themeToggleBtn?.addEventListener('click', toggleTheme)

  // Initialize theme
  initTheme()

  // Run initial status check
  void checkInitialStatus()
})
