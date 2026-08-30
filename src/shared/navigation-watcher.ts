/**
 * NavigationWatcher — Detects SPA URL changes independently from DOM mutation observation.
 *
 * Chrome MV3 content scripts run in an isolated world. While we share the `window` object
 * for events (so `popstate` works natively), `history.pushState`/`replaceState` do NOT fire
 * `popstate`. Patching history methods from a content-script isolated world is unreliable
 * because the page script may re-override the same methods after extension patching.
 *
 * Strategy:
 *   1. Listen to `popstate` for browser back/forward navigation (History API pops state).
 *   2. Poll `window.location.href` every 250ms for pushState/replaceState navigation.
 *      250ms polling is negligible CPU cost: a simple string comparison once per 250ms
 *      during a 1-2 day unattended collection run. This is the standard reliable approach
 *      used by production extensions that must track SPA navigation without requiring
 *      additional manifest permissions or page-world script injection.
 *
 * The callback is fired at most once per URL change even if both popstate and polling detect
 * the same transition (debounced and de-duplicated via lastKnownUrl update on first detection).
 */

export type UrlChangeCallback = (previousUrl: string, newUrl: string) => void

export interface NavigationWatcherOptions {
  /** How frequently to poll `location.href` for pushState/replaceState changes. Default 250ms. */
  pollIntervalMs?: number
  /** Debounce delay after a change fires before another change is reported. Default 100ms. */
  debounceMs?: number
}

export class NavigationWatcher {
  private isWatching = false
  private lastKnownUrl: string
  private onUrlChange: UrlChangeCallback
  private pollIntervalId: ReturnType<typeof setInterval> | null = null
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private popstateHandler: (() => void) | null = null
  private pollIntervalMs: number
  private debounceMs: number
  private pendingChange: { previousUrl: string; newUrl: string } | null = null

  constructor(onUrlChange: UrlChangeCallback, options?: NavigationWatcherOptions) {
    this.onUrlChange = onUrlChange
    this.lastKnownUrl = typeof window !== 'undefined' ? window.location.href : ''
    this.pollIntervalMs = options?.pollIntervalMs ?? 250
    this.debounceMs = options?.debounceMs ?? 100
  }

  /**
   * Starts the navigation watcher. No-op if already watching.
   * Records the current URL as the baseline for change detection.
   */
  start(initialUrl?: string): void {
    if (this.isWatching) {
      return
    }
    this.isWatching = true
    this.lastKnownUrl =
      initialUrl ?? (typeof window !== 'undefined' ? window.location.href : this.lastKnownUrl)

    // popstate: handles browser back/forward (browser fires popstate on history.back/forward).
    this.popstateHandler = () => this.checkForUrlChange()
    window.addEventListener('popstate', this.popstateHandler)

    // Polling: handles pushState/replaceState (no event fired natively in content-script isolated world).
    this.pollIntervalId = setInterval(() => {
      this.checkForUrlChange()
    }, this.pollIntervalMs)
  }

  /**
   * Stops the navigation watcher and removes all listeners/timers.
   */
  stop(): void {
    if (!this.isWatching) {
      return
    }
    this.isWatching = false

    if (this.popstateHandler) {
      window.removeEventListener('popstate', this.popstateHandler)
      this.popstateHandler = null
    }

    if (this.pollIntervalId !== null) {
      clearInterval(this.pollIntervalId)
      this.pollIntervalId = null
    }

    if (this.debounceTimer !== null) {
      clearTimeout(this.debounceTimer)
      this.debounceTimer = null
    }

    this.pendingChange = null
  }

  /**
   * Returns whether the watcher is currently active.
   */
  isActive(): boolean {
    return this.isWatching
  }

  /**
   * Returns the last known URL tracked by this watcher.
   */
  getCurrentUrl(): string {
    return this.lastKnownUrl
  }

  /**
   * Checks for a URL change and fires the debounced callback if one is detected.
   * Safe to call from multiple sources (popstate + interval) without duplication
   * because the lastKnownUrl is updated atomically on first detection.
   */
  checkForUrlChange(): void {
    if (!this.isWatching) {
      return
    }

    const currentUrl = typeof window !== 'undefined' ? window.location.href : this.lastKnownUrl
    if (currentUrl === this.lastKnownUrl) {
      return
    }

    // Capture the transition before updating lastKnownUrl to prevent duplicate fires
    const previousUrl = this.lastKnownUrl
    this.lastKnownUrl = currentUrl
    this.pendingChange = { previousUrl, newUrl: currentUrl }

    // Debounce: cancel any pending callback and reschedule
    if (this.debounceTimer !== null) {
      clearTimeout(this.debounceTimer)
    }
    this.debounceTimer = setTimeout(() => {
      const change = this.pendingChange
      this.pendingChange = null
      this.debounceTimer = null
      if (change) {
        this.onUrlChange(change.previousUrl, change.newUrl)
      }
    }, this.debounceMs)
  }
}
