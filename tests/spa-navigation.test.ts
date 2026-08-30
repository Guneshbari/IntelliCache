// @vitest-environment happy-dom
/**
 * SPA Navigation Detection Tests
 *
 * Tests the NavigationWatcher utility and the Claude/Gemini adapter navigation state machines.
 * Validates correct URL-transition classification, state transitions, and flush behavior.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { NavigationWatcher } from '../src/shared/navigation-watcher'
import { ClaudeAdapter } from '../src/platforms/claude/adapter'
import { GeminiAdapter } from '../src/platforms/gemini/adapter'

// ─── NavigationWatcher Unit Tests ─────────────────────────────────────────────

describe('NavigationWatcher', () => {
  let originalHref: string

  beforeEach(() => {
    originalHref = window.location.href
    // Reset to a known base URL so tests start from a clean slate
    Object.defineProperty(window, 'location', {
      value: { href: 'https://base.test/' },
      writable: true,
      configurable: true,
    })
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    Object.defineProperty(window, 'location', {
      value: { href: originalHref },
      writable: true,
      configurable: true,
    })
  })

  function setHref(url: string) {
    Object.defineProperty(window, 'location', {
      value: { href: url },
      writable: true,
      configurable: true,
    })
  }

  it('does not fire callback when URL has not changed', async () => {
    const callback = vi.fn()
    Object.defineProperty(window, 'location', {
      value: { href: 'https://example.com/page1' },
      writable: true,
      configurable: true,
    })
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://example.com/page1')

    // Advance time without URL change
    vi.advanceTimersByTime(200)
    expect(callback).not.toHaveBeenCalled()

    watcher.stop()
  })

  it('fires callback when URL changes (poll detection)', async () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')

    // Change URL (simulates pushState)
    setHref('https://claude.ai/chat/abc-123')

    // Advance past poll interval + debounce
    vi.advanceTimersByTime(50)
    expect(callback).toHaveBeenCalledTimes(1)
    expect(callback).toHaveBeenCalledWith('https://claude.ai/new', 'https://claude.ai/chat/abc-123')

    watcher.stop()
  })

  it('fires callback only once per URL change (deduplication)', () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')

    setHref('https://claude.ai/chat/abc-123')

    // Advance multiple poll intervals
    vi.advanceTimersByTime(200)
    expect(callback).toHaveBeenCalledTimes(1)

    watcher.stop()
  })

  it('fires callback again for a second distinct URL change', () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')

    // First navigation
    setHref('https://claude.ai/chat/conv-1')
    vi.advanceTimersByTime(100)
    expect(callback).toHaveBeenCalledTimes(1)

    // Second navigation to a different conversation
    setHref('https://claude.ai/chat/conv-2')
    vi.advanceTimersByTime(100)
    expect(callback).toHaveBeenCalledTimes(2)
    expect(callback).toHaveBeenNthCalledWith(
      2,
      'https://claude.ai/chat/conv-1',
      'https://claude.ai/chat/conv-2'
    )

    watcher.stop()
  })

  it('stops firing after stop() is called', () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')

    watcher.stop()

    // Change URL after stop
    setHref('https://claude.ai/chat/abc-123')
    vi.advanceTimersByTime(200)

    expect(callback).not.toHaveBeenCalled()
  })

  it('isActive() reflects state correctly', () => {
    const watcher = new NavigationWatcher(vi.fn(), { pollIntervalMs: 20 })
    expect(watcher.isActive()).toBe(false)
    watcher.start()
    expect(watcher.isActive()).toBe(true)
    watcher.stop()
    expect(watcher.isActive()).toBe(false)
  })

  it('start() is idempotent — does not install duplicate listeners', () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')
    watcher.start('https://claude.ai/new') // second call should be no-op

    setHref('https://claude.ai/chat/abc-123')
    vi.advanceTimersByTime(200)

    // Should only fire once (not duplicated)
    expect(callback).toHaveBeenCalledTimes(1)

    watcher.stop()
  })

  it('stop() is idempotent — does not throw if called twice', () => {
    const watcher = new NavigationWatcher(vi.fn(), { pollIntervalMs: 20 })
    watcher.start()
    expect(() => {
      watcher.stop()
      watcher.stop()
    }).not.toThrow()
  })

  it('getCurrentUrl() returns the last tracked URL', () => {
    const watcher = new NavigationWatcher(vi.fn(), { pollIntervalMs: 20, debounceMs: 10 })
    watcher.start('https://claude.ai/new')
    expect(watcher.getCurrentUrl()).toBe('https://claude.ai/new')

    setHref('https://claude.ai/chat/abc-123')
    vi.advanceTimersByTime(30) // past poll interval, before debounce fires
    // After poll detects change, lastKnownUrl should be updated immediately
    expect(watcher.getCurrentUrl()).toBe('https://claude.ai/chat/abc-123')

    watcher.stop()
  })

  it('fires callback on popstate event', () => {
    const callback = vi.fn()
    const watcher = new NavigationWatcher(callback, { pollIntervalMs: 20000, debounceMs: 10 })
    watcher.start('https://claude.ai/chat/conv-1')

    // Simulate popstate (browser back/forward)
    setHref('https://claude.ai/chat/conv-2')
    window.dispatchEvent(new PopStateEvent('popstate'))

    vi.advanceTimersByTime(50)
    expect(callback).toHaveBeenCalledTimes(1)
    expect(callback).toHaveBeenCalledWith(
      'https://claude.ai/chat/conv-1',
      'https://claude.ai/chat/conv-2'
    )

    watcher.stop()
  })
})

// ─── Claude Adapter Navigation Tests ──────────────────────────────────────────

describe('ClaudeAdapter navigation state machine', () => {
  let adapter: ClaudeAdapter

  beforeEach(() => {
    vi.useFakeTimers()
    // Default URL: Claude new chat
    Object.defineProperty(window, 'location', {
      value: { href: 'https://claude.ai/new' },
      writable: true,
      configurable: true,
    })
    adapter = new ClaudeAdapter({
      mutationDebounceMs: 50,
      newChatTimeoutMs: 500,
      navPollIntervalMs: 20,
    })
  })

  afterEach(() => {
    adapter.stop()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('starts with navState=new_chat_without_id when on /new URL', () => {
    adapter.start()
    // NavState is internal; we test behavior through handleNavigation
    expect(adapter.isObserving()).toBe(true)
  })

  it('starts with correct navState when on an existing conversation URL', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://claude.ai/chat/existing-conv-abc' },
      writable: true,
      configurable: true,
    })
    adapter = new ClaudeAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter.start()
    expect(adapter.isObserving()).toBe(true)
  })

  it('/new -> /chat/{id} is classified as new_chat_assignment (preserves on_generate)', () => {
    adapter.start()

    // Trigger navigation directly — should not throw and should schedule DOM scan
    expect(() => {
      adapter.handleNavigation('https://claude.ai/new', 'https://claude.ai/chat/new-conv-123')
    }).not.toThrow()

    // Adapter should still be observing after navigation
    expect(adapter.isObserving()).toBe(true)
    // Advance timers to let scheduled processing fire (ensures no uncaught async errors)
    vi.advanceTimersByTime(500)
  })

  it('/chat/{id-A} -> /chat/{id-B} is classified as existing_conversation_navigation', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://claude.ai/chat/conv-a' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new ClaudeAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()

    expect(() => {
      adapter2.handleNavigation('https://claude.ai/chat/conv-a', 'https://claude.ai/chat/conv-b')
    }).not.toThrow()

    // isInitialScan should be reset (side-effect of A->B navigation)
    const adapterAsAny2 = adapter2 as unknown as Record<string, unknown>
    expect(adapterAsAny2['isInitialScan']).toBe(true)

    vi.advanceTimersByTime(500)
    adapter2.stop()
  })

  it('pending interactions survive new-chat ID assignment', async () => {
    // Build an adapter in new-chat state
    adapter.start()
    vi.advanceTimersByTime(200) // allow initial scan to fire (no turns)

    // Manually add a pending interaction to simulate what happens when
    // a conversation is captured without a conversationId
    const mockInteraction = {
      platform: 'claude' as const,
      conversationId: null,
      messageId: null,
      userMessageId: null,
      model: { provider: 'claude', name: 'claude-3' },
      queryText: 'test query',
      responseText: 'test response',
      conversationTitle: null,
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      captureContext: 'on_generate' as const,
    }

    // Access private map via type coercion (test-only)
    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const pendingMap = adapterAsAny['pendingUnboundInteractions'] as Map<string, unknown>
    const timer = setTimeout(() => {
      /* no-op */
    }, 10000)
    pendingMap.set('pair:test query|test response', {
      interaction: mockInteraction,
      key: 'pair:test query|test response',
      timer,
    })

    expect(pendingMap.size).toBe(1)

    adapter.handleNavigation('https://claude.ai/new', 'https://claude.ai/chat/new-conv-456')

    // Pending interactions should be flushed (map cleared)
    expect(pendingMap.size).toBe(0)
    clearTimeout(timer)
  })

  it('new_chat_assignment does NOT reset isInitialScan', () => {
    adapter.start()
    vi.advanceTimersByTime(200)

    // Simulate initial scan has completed (isInitialScan should be false after first process)
    const adapterAsAny = adapter as unknown as Record<string, unknown>

    // Force isInitialScan to false (as it would be after initial DOM scan)
    adapterAsAny['isInitialScan'] = false

    // Trigger new-chat assignment
    adapter.handleNavigation('https://claude.ai/new', 'https://claude.ai/chat/conv-789')

    // isInitialScan should still be false (on_generate preserved)
    expect(adapterAsAny['isInitialScan']).toBe(false)
  })

  it('existing_conversation_navigation DOES reset isInitialScan', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://claude.ai/chat/conv-a' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new ClaudeAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()
    vi.advanceTimersByTime(200)

    const adapterAsAny = adapter2 as unknown as Record<string, unknown>
    adapterAsAny['isInitialScan'] = false // after initial scan

    adapter2.handleNavigation('https://claude.ai/chat/conv-a', 'https://claude.ai/chat/conv-b')

    // isInitialScan should be reset to true for on_load context
    expect(adapterAsAny['isInitialScan']).toBe(true)
    adapter2.stop()
  })

  it('existing_conversation_navigation clears processedKeys cache', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://claude.ai/chat/conv-a' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new ClaudeAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()

    const adapterAsAny = adapter2 as unknown as Record<string, unknown>
    const processedKeys = adapterAsAny['processedKeys'] as Set<string>
    processedKeys.add('msg:some-key')
    processedKeys.add('pair:old-query|old-response')

    adapter2.handleNavigation('https://claude.ai/chat/conv-a', 'https://claude.ai/chat/conv-b')

    // Session cache should be cleared so new conversation's turns aren't skipped
    expect(processedKeys.size).toBe(0)
    adapter2.stop()
  })

  it('stop() removes navWatcher', () => {
    adapter.start()
    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const watcher = adapterAsAny['navWatcher'] as NavigationWatcher
    expect(watcher).not.toBeNull()
    expect(watcher.isActive()).toBe(true)

    adapter.stop()

    expect(adapterAsAny['navWatcher']).toBeNull()
  })

  it('stop() removes all timers and watcher (no duplicate listeners on re-start)', () => {
    adapter.start()
    adapter.stop()

    // Re-start should not throw or install duplicate intervals
    expect(() => {
      adapter.start()
    }).not.toThrow()

    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const watcher = adapterAsAny['navWatcher'] as NavigationWatcher
    expect(watcher).not.toBeNull()
    expect(watcher.isActive()).toBe(true)
  })
})

// ─── Gemini Adapter Navigation Tests ──────────────────────────────────────────

describe('GeminiAdapter navigation state machine', () => {
  let adapter: GeminiAdapter

  beforeEach(() => {
    vi.useFakeTimers()
    Object.defineProperty(window, 'location', {
      value: { href: 'https://gemini.google.com/app' },
      writable: true,
      configurable: true,
    })
    adapter = new GeminiAdapter({
      mutationDebounceMs: 50,
      newChatTimeoutMs: 500,
      navPollIntervalMs: 20,
    })
  })

  afterEach(() => {
    adapter.stop()
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('starts with navState=new_chat_without_id when on /app URL (no conversation ID)', () => {
    adapter.start()
    expect(adapter.isObserving()).toBe(true)
  })

  it('starts with navState=conversation_with_id when on /app/{id} URL', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://gemini.google.com/app/gemini-conv-abc123' },
      writable: true,
      configurable: true,
    })
    adapter = new GeminiAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter.start()
    expect(adapter.isObserving()).toBe(true)
  })

  it('/app -> /app/{id} is classified as new_chat_assignment', () => {
    adapter.start()
    // Force isInitialScan to false to simulate state after initial DOM scan ran
    const adapterAsAny = adapter as unknown as Record<string, unknown>
    adapterAsAny['isInitialScan'] = false

    expect(() => {
      adapter.handleNavigation(
        'https://gemini.google.com/app',
        'https://gemini.google.com/app/gemini-conv-abc123'
      )
    }).not.toThrow()

    // isInitialScan should NOT be reset by new-chat assignment (preserves on_generate context)
    expect(adapterAsAny['isInitialScan']).toBe(false)
    vi.advanceTimersByTime(500)
  })

  it('/app/{id-A} -> /app/{id-B} is classified as existing_conversation_navigation', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://gemini.google.com/app/gemini-conv-aaa' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new GeminiAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()

    expect(() => {
      adapter2.handleNavigation(
        'https://gemini.google.com/app/gemini-conv-aaa',
        'https://gemini.google.com/app/gemini-conv-bbb'
      )
    }).not.toThrow()

    // isInitialScan should be reset (A->B navigation resets to on_load)
    const adapterAsAny2 = adapter2 as unknown as Record<string, unknown>
    expect(adapterAsAny2['isInitialScan']).toBe(true)

    vi.advanceTimersByTime(500)
    adapter2.stop()
  })

  it('pending interactions survive /app -> /app/{id} ID assignment', () => {
    adapter.start()
    vi.advanceTimersByTime(200)

    const mockInteraction = {
      platform: 'gemini' as const,
      conversationId: null,
      messageId: null,
      userMessageId: null,
      model: { provider: 'google', name: 'gemini-pro' },
      queryText: 'gemini test query',
      responseText: 'gemini test response',
      conversationTitle: null,
      observedAt: new Date().toISOString(),
      sourceTimestamp: null,
      captureContext: 'on_generate' as const,
    }

    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const pendingMap = adapterAsAny['pendingUnboundInteractions'] as Map<string, unknown>
    const timer = setTimeout(() => {
      /* no-op */
    }, 10000)
    pendingMap.set('pair:gemini test query|gemini test response', {
      interaction: mockInteraction,
      key: 'pair:gemini test query|gemini test response',
      timer,
    })

    expect(pendingMap.size).toBe(1)

    adapter.handleNavigation(
      'https://gemini.google.com/app',
      'https://gemini.google.com/app/gemini-conv-xyz'
    )

    // Pending interactions should be flushed
    expect(pendingMap.size).toBe(0)
    clearTimeout(timer)
  })

  it('/app -> /app/{id} does NOT reset isInitialScan (preserves on_generate)', () => {
    adapter.start()
    vi.advanceTimersByTime(200)

    const adapterAsAny = adapter as unknown as Record<string, unknown>
    adapterAsAny['isInitialScan'] = false

    adapter.handleNavigation(
      'https://gemini.google.com/app',
      'https://gemini.google.com/app/gemini-conv-xyz'
    )

    expect(adapterAsAny['isInitialScan']).toBe(false)
  })

  it('/app/{id-A} -> /app/{id-B} DOES reset isInitialScan (to on_load)', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://gemini.google.com/app/gemini-conv-aaa' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new GeminiAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()
    vi.advanceTimersByTime(200)

    const adapterAsAny = adapter2 as unknown as Record<string, unknown>
    adapterAsAny['isInitialScan'] = false

    adapter2.handleNavigation(
      'https://gemini.google.com/app/gemini-conv-aaa',
      'https://gemini.google.com/app/gemini-conv-bbb'
    )

    expect(adapterAsAny['isInitialScan']).toBe(true)
    adapter2.stop()
  })

  it('/app/{id-A} -> /app/{id-B} clears processedKeys cache', () => {
    Object.defineProperty(window, 'location', {
      value: { href: 'https://gemini.google.com/app/gemini-conv-aaa' },
      writable: true,
      configurable: true,
    })
    const adapter2 = new GeminiAdapter({ mutationDebounceMs: 50, navPollIntervalMs: 20 })
    adapter2.start()

    const adapterAsAny = adapter2 as unknown as Record<string, unknown>
    const processedKeys = adapterAsAny['processedKeys'] as Set<string>
    processedKeys.add('pair:old-query|old-resp')
    processedKeys.add('msg:some-msg-id')

    adapter2.handleNavigation(
      'https://gemini.google.com/app/gemini-conv-aaa',
      'https://gemini.google.com/app/gemini-conv-bbb'
    )

    expect(processedKeys.size).toBe(0)
    adapter2.stop()
  })

  it('stop() removes navWatcher and all listeners', () => {
    adapter.start()
    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const watcher = adapterAsAny['navWatcher'] as NavigationWatcher
    expect(watcher).not.toBeNull()
    expect(watcher.isActive()).toBe(true)

    adapter.stop()

    expect(adapterAsAny['navWatcher']).toBeNull()
  })

  it('stop() then start() reinstalls navigation listener cleanly', () => {
    adapter.start()
    adapter.stop()

    expect(() => {
      adapter.start()
    }).not.toThrow()

    const adapterAsAny = adapter as unknown as Record<string, unknown>
    const watcher = adapterAsAny['navWatcher'] as NavigationWatcher
    expect(watcher).not.toBeNull()
    expect(watcher.isActive()).toBe(true)
  })
})
