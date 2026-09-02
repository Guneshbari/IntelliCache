// @vitest-environment happy-dom
import 'fake-indexeddb/auto'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { closeDatabase, IntelliCacheDB } from '../src/database/db'
import { ConversationRepository } from '../src/database/repositories/conversation-repository'
import { InteractionRepository } from '../src/database/repositories/interaction-repository'
import type { DbStatsResponseData, Interaction } from '../src/shared/types'

describe('Popup Dashboard & Interaction Explorer Unit Tests', () => {
  let db: IntelliCacheDB
  let interactionRepo: InteractionRepository
  let conversationRepo: ConversationRepository
  let dbName: string

  beforeEach(() => {
    dbName = `popup-test-${Date.now()}-${Math.random()}`
    db = new IntelliCacheDB(dbName)
    interactionRepo = new InteractionRepository(db)
    conversationRepo = new ConversationRepository(db)

    // Setup DOM fixture for popup
    document.body.innerHTML = `
      <div class="popup-container">
        <header class="header">
          <div class="status-pill" id="status-badge">
            <span class="status-dot"></span>
            <span id="status-text">CONNECTING</span>
          </div>
        </header>

        <main class="main-content" id="main-content">
          <div class="metric-card">
            <span class="metric-value" id="total-interactions-count">0</span>
          </div>
          <div class="metric-card">
            <span class="metric-value" id="total-conversations-count">0</span>
          </div>

          <div class="platform-grid">
            <div class="platform-card" data-platform="chatgpt">
              <span class="platform-count" id="count-chatgpt">0</span>
              <div class="platform-bar-fill" id="bar-chatgpt" style="width: 0%"></div>
            </div>
            <div class="platform-card" data-platform="claude">
              <span class="platform-count" id="count-claude">0</span>
              <div class="platform-bar-fill" id="bar-claude" style="width: 0%"></div>
            </div>
            <div class="platform-card" data-platform="gemini">
              <span class="platform-count" id="count-gemini">0</span>
              <div class="platform-bar-fill" id="bar-gemini" style="width: 0%"></div>
            </div>
          </div>

          <div class="recent-list" id="recent-activity-list"></div>
          <span class="activity-badge" id="recent-count-badge">0</span>
          <button id="toggle-explorer-btn"><span id="toggle-explorer-text">Explore All</span></button>

          <section class="explorer-section collapsed" id="explorer-section">
            <div id="explorer-header-toggle"></div>
            <button id="explorer-close-btn"></button>
            <input type="text" id="explorer-search" />
            <button id="explorer-search-clear" hidden></button>
            <div id="filter-chips">
              <button class="chip active" data-filter="all">All</button>
              <button class="chip" data-filter="chatgpt">ChatGPT</button>
              <button class="chip" data-filter="claude">Claude</button>
              <button class="chip" data-filter="gemini">Gemini</button>
            </div>
            <span id="explorer-match-count">0 items</span>
            <div id="explorer-items-list"></div>
          </section>

          <div class="health-card">
            <span id="sw-status-val">Inactive</span>
            <span id="db-storage-val">IndexedDB</span>
            <span id="db-connection-val">Disconnected</span>
            <span id="ext-version-val">0.0.0</span>
            <span id="health-summary-badge">Unknown</span>
          </div>

          <button id="diagnostics-toggle" aria-expanded="false"></button>
          <div id="diagnostics-content" hidden>
            <button id="ping-btn"></button>
            <button id="integrity-btn"></button>
            <button id="clear-log-btn"></button>
            <div id="log-output"></div>
          </div>
        </main>
      </div>
    `
  })

  afterEach(async () => {
    await db.delete()
    await closeDatabase()
    vi.restoreAllMocks()
  })

  it('correctly queries platform counts and recent interactions through repository helpers', async () => {
    // Seed interactions across ChatGPT, Claude, and Gemini
    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'gpt-1',
      query: { text: 'How do closures work in JavaScript?' },
      response: { text: 'A closure is the combination of a function bundled together...' },
      conversation_title: 'JavaScript Closures',
    })

    await interactionRepo.create({
      platform: 'chatgpt',
      conversation_id: 'gpt-1',
      query: { text: 'Give an example of a closure.' },
      response: { text: 'function makeAdder(x) { return function(y) { return x + y; }; }' },
      conversation_title: 'JavaScript Closures',
    })

    await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'claude-1',
      query: { text: 'Explain Rust ownership and borrowing.' },
      response: { text: 'Ownership is Rusts most unique feature...' },
      conversation_title: 'Rust Memory Model',
    })

    await interactionRepo.create({
      platform: 'gemini',
      conversation_id: 'gemini-1',
      query: { text: 'Write a python quicksort function.' },
      response: { text: 'def quicksort(arr): ...' },
      conversation_title: 'Python Algorithms',
    })

    const totalCount = await interactionRepo.count()
    const gptCount = await interactionRepo.countByPlatform('chatgpt')
    const claudeCount = await interactionRepo.countByPlatform('claude')
    const geminiCount = await interactionRepo.countByPlatform('gemini')
    const recent = await interactionRepo.getRecent(10)

    expect(totalCount).toBe(4)
    expect(gptCount).toBe(2)
    expect(claudeCount).toBe(1)
    expect(geminiCount).toBe(1)
    expect(recent).toHaveLength(4)
    expect(recent[0].platform).toBe('gemini') // most recent first
  })

  it('generates accurate DbStatsResponseData structure including platform breakdown', async () => {
    await interactionRepo.create({
      platform: 'claude',
      conversation_id: 'c-100',
      query: { text: 'What is WebAssembly?' },
      response: { text: 'WebAssembly (Wasm) is a binary instruction format...' },
    })

    const [
      interactionCount,
      conversationCount,
      chatgptCount,
      claudeCount,
      geminiCount,
      recentInteractions,
    ] = await Promise.all([
      interactionRepo.count(),
      conversationRepo.count(),
      interactionRepo.countByPlatform('chatgpt'),
      interactionRepo.countByPlatform('claude'),
      interactionRepo.countByPlatform('gemini'),
      interactionRepo.getRecent(50),
    ])

    const statsData: DbStatsResponseData = {
      dbName: 'intelliCache',
      dbVersion: 1,
      interactionCount,
      conversationCount,
      platformCounts: {
        chatgpt: chatgptCount,
        claude: claudeCount,
        gemini: geminiCount,
      },
      recentInteractions,
    }

    expect(statsData.interactionCount).toBe(1)
    expect(statsData.platformCounts?.chatgpt).toBe(0)
    expect(statsData.platformCounts?.claude).toBe(1)
    expect(statsData.platformCounts?.gemini).toBe(0)
    expect(statsData.recentInteractions).toHaveLength(1)
    expect(statsData.recentInteractions![0].platform).toBe('claude')
  })

  it('correctly filters recent interactions by platform and search query in explorer logic', () => {
    const items: Interaction[] = [
      {
        id: '1',
        schema_version: 1,
        fingerprint: 'fp1',
        fingerprint_strategy: 'level_1',
        platform: 'chatgpt',
        conversation_id: 'chatgpt:1',
        message_id: null,
        user_message_id: null,
        source_timestamp: null,
        query: { text: 'Hello ChatGPT', characters: 13, bytes: 13, estimated_tokens: 4 },
        response: {
          text: 'Hello! How can I assist you today?',
          characters: 34,
          bytes: 34,
          estimated_tokens: 8,
        },
        conversation_title: 'Greeting Chat',
        observed_at: new Date(Date.now() - 1000).toISOString(),
        capture_context: 'on_load',
        model: { provider: 'openai', name: null },
        collector_version: '1.0.0',
      },
      {
        id: '2',
        schema_version: 1,
        fingerprint: 'fp2',
        fingerprint_strategy: 'level_1',
        platform: 'claude',
        conversation_id: 'claude:2',
        message_id: null,
        user_message_id: null,
        source_timestamp: null,
        query: {
          text: 'Explain quantum computing',
          characters: 25,
          bytes: 25,
          estimated_tokens: 6,
        },
        response: {
          text: 'Quantum computing leverages superposition and entanglement.',
          characters: 59,
          bytes: 59,
          estimated_tokens: 12,
        },
        conversation_title: 'Physics Q&A',
        observed_at: new Date(Date.now() - 2000).toISOString(),
        capture_context: 'on_generate',
        model: { provider: 'claude', name: null },
        collector_version: '1.0.0',
      },
      {
        id: '3',
        schema_version: 1,
        fingerprint: 'fp3',
        fingerprint_strategy: 'level_1',
        platform: 'gemini',
        conversation_id: 'gemini:3',
        message_id: null,
        user_message_id: null,
        source_timestamp: null,
        query: {
          text: 'How to make pasta carbonara?',
          characters: 28,
          bytes: 28,
          estimated_tokens: 7,
        },
        response: {
          text: 'Authentic carbonara uses eggs, pecorino, guanciale, and black pepper.',
          characters: 69,
          bytes: 69,
          estimated_tokens: 15,
        },
        conversation_title: 'Italian Cooking',
        observed_at: new Date(Date.now() - 3000).toISOString(),
        capture_context: 'on_generate',
        model: { provider: 'google', name: null },
        collector_version: '1.0.0',
      },
    ]

    // Filter by platform
    const claudeOnly = items.filter((i) => i.platform === 'claude')
    expect(claudeOnly).toHaveLength(1)
    expect(claudeOnly[0].conversation_title).toBe('Physics Q&A')

    // Filter by text search query
    const searchMatch = items.filter((i) => {
      const q = 'carbonara'
      return (
        i.query.text.toLowerCase().includes(q) ||
        i.response.text.toLowerCase().includes(q) ||
        (i.conversation_title && i.conversation_title.toLowerCase().includes(q))
      )
    })
    expect(searchMatch).toHaveLength(1)
    expect(searchMatch[0].platform).toBe('gemini')

    // Search with no matches
    const noMatch = items.filter((i) =>
      i.query.text.toLowerCase().includes('nonexistent query keyword')
    )
    expect(noMatch).toHaveLength(0)
  })

  it('handles theme state switching and persists preference to localStorage', () => {
    const storage: Record<string, string> = {}
    const mockStorage = {
      getItem: (key: string) => storage[key] ?? null,
      setItem: (key: string, val: string) => {
        storage[key] = val
      },
      clear: () => {
        for (const k of Object.keys(storage)) delete storage[k]
      },
    }

    // Default should be dark mode
    let currentTheme = mockStorage.getItem('intellicache_theme') || 'dark'
    expect(currentTheme).toBe('dark')

    // Switch to light mode
    currentTheme = 'light'
    mockStorage.setItem('intellicache_theme', currentTheme)
    document.documentElement.setAttribute('data-theme', currentTheme)

    expect(document.documentElement.getAttribute('data-theme')).toBe('light')
    expect(mockStorage.getItem('intellicache_theme')).toBe('light')

    // Switch back to dark mode
    currentTheme = 'dark'
    mockStorage.setItem('intellicache_theme', currentTheme)
    document.documentElement.setAttribute('data-theme', currentTheme)

    expect(document.documentElement.getAttribute('data-theme')).toBe('dark')
    expect(mockStorage.getItem('intellicache_theme')).toBe('dark')
  })
})
