/**
 * ChatGPT Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for ChatGPT web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the service-worker messaging layer.
 *
 * ChatGPT uses MutationObserver URL detection (not NavigationWatcher) because
 * ChatGPT's SPA navigation reliably produces DOM mutations large enough to
 * detect the URL change from within the mutation callback.
 */

import { diagnosticStats, logger, redactUrlForLog } from '../../diagnostics'
import {
  BaseAdapter,
  type BaseAdapterOptions,
  detectPlatformFromUrl,
} from '../../shared/base-adapter'
import {
  extractConversationIdFromUrl,
  extractConversationTitle,
  extractConversationTurns,
  extractModelInfo,
  isChatGPTGuestSession,
  isPageGenerating,
  pairTurnsIntoInteractions,
} from './parser'
import { CHATGPT_SELECTORS } from './selectors'
import type { ExtractedInteraction } from '../types'

export type ChatGPTAdapterOptions = BaseAdapterOptions

export class ChatGPTAdapter extends BaseAdapter {
  public readonly platform = 'chatgpt' as const
  protected readonly platformTag = 'CHATGPT' as const

  private lastObservedUrl = ''
  private isGuestSession = false

  constructor(options?: ChatGPTAdapterOptions) {
    super(options)
  }

  canHandle(url: string): boolean {
    return detectPlatformFromUrl(url) === 'chatgpt'
  }

  start(): void {
    if (this.observing) return

    this.observing = true
    this.isInitialScan = true
    this.lastObservedUrl = window.location.href

    const initialUrl = this.lastObservedUrl
    const initialConvId = extractConversationIdFromUrl(initialUrl)
    this.navState = initialConvId ? 'conversation_with_id' : 'new_chat_without_id'

    logger.info(
      'Adapter',
      'CHATGPT',
      `Starting adapter lifecycle (initialUrl: ${redactUrlForLog(initialUrl)}, navState: ${this.navState}, conversationId: ${initialConvId ? 'present' : 'none'})`
    )
    logger.debug('Adapter', 'CHATGPT', 'Scheduling initial DOM scan in 100ms...')

    this.startNavWatcher(initialUrl, (prev, next) => this.onNavigate(prev, next))
    this.scheduleProcessing(100)
    this.startMutationObserver(() => this.handleDomMutation())

    logger.info('Adapter', 'CHATGPT', 'Adapter started and observing conversation DOM mutations.')
  }

  stop(): void {
    if (!this.observing) return
    logger.info('Adapter', 'CHATGPT', 'Stopping adapter and disconnecting observer.')
    this.isGuestSession = false
    this.stopShared()
  }

  protected override shouldPersistUnboundImmediately(_interaction: ExtractedInteraction): boolean {
    if (this.isGuestSession) {
      return true
    }
    const root = typeof document !== 'undefined' ? document.body || document : null
    if (root && isChatGPTGuestSession(root)) {
      this.isGuestSession = true
      return true
    }
    return false
  }

  /** Delegates to the shared handleNavigation from BaseAdapter. */
  handleNavigation(prevUrl: string, newUrl: string): void {
    super.handleNavigation(prevUrl, newUrl, extractConversationIdFromUrl, () =>
      extractConversationTitle(document)
    )
  }

  private onNavigate(prevUrl: string, newUrl: string): void {
    this.lastObservedUrl = newUrl
    this.handleNavigation(prevUrl, newUrl)
  }

  /**
   * Handles DOM mutation events with debouncing and inline URL change detection.
   */
  private handleDomMutation(): void {
    if (!this.observing) return

    const currentUrl = window.location.href
    if (currentUrl !== this.lastObservedUrl) {
      const previousUrl = this.lastObservedUrl
      this.lastObservedUrl = currentUrl
      this.handleNavigation(previousUrl, currentUrl)
      return
    }

    this.onDomMutation()
  }

  /**
   * Scans the current conversation DOM, extracts complete pairs, and persists new interactions.
   */
  public async processConversation(): Promise<void> {
    if (!this.observing) return

    diagnosticStats.increment('domScans')
    const currentUrl = window.location.href
    const conversationId = extractConversationIdFromUrl(currentUrl)
    const hasConvId = conversationId !== null

    if (!hasConvId) {
      diagnosticStats.increment('missingConversationIds')
    }

    logger.debug(
      'Adapter',
      'CHATGPT',
      `Starting conversation DOM processing pass (URL: ${redactUrlForLog(currentUrl)})`
    )
    logger.debug('Adapter', 'CHATGPT', `Conversation ID: ${conversationId ? 'present' : 'null'}`)

    const root = document.body || document
    const generating = isPageGenerating(root)
    logger.debug('Adapter', 'CHATGPT', `Evaluating page generation state: generating=${generating}`)

    if (generating) {
      this.handleStreamingDeferred()
      return
    }
    this.resetStreamingDeferrals()

    this.isGuestSession = isChatGPTGuestSession(root)
    if (this.isGuestSession && !conversationId) {
      logger.info(
        'Adapter',
        'CHATGPT',
        'Unauthenticated / anonymous ChatGPT session detected without conversation ID. Using content-based fallback identity.'
      )
    }

    const extractedTitle = extractConversationTitle(document)
    const model = extractModelInfo(document)

    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId, extractedTitle)
    }

    const captureContext = this.consumeCaptureContext()

    const turnContainers = Array.from(root.querySelectorAll(CHATGPT_SELECTORS.TURN_ARTICLE)).length
    const turns = extractConversationTurns(root)
    if (this.checkAndDeferStaleDom(turns)) {
      return
    }
    const userTurns = turns.filter((t) => t.role === 'user').length
    const assistantTurns = turns.filter((t) => t.role === 'assistant').length

    diagnosticStats.increment('userTurnsFound', userTurns)
    diagnosticStats.increment('assistantTurnsFound', assistantTurns)

    logger.debug(
      'Adapter',
      'CHATGPT',
      `DOM turns discovered: total=${turns.length}, userTurns=${userTurns}, assistantTurns=${assistantTurns}`
    )

    if (turns.length === 0) {
      logger.debug('Adapter', 'CHATGPT', 'No conversation turns discovered in DOM.')
      return
    }

    const title = extractedTitle || null

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId,
      title,
      model,
      captureContext,
    })

    diagnosticStats.increment('completePairs', interactions.length)
    diagnosticStats.increment('interactionsExtracted', interactions.length)
    logger.debug('Adapter', 'CHATGPT', `Interactions paired: completePairs=${interactions.length}`)
    if (interactions.length > 0) {
      logger.info('Lifecycle', 'CHATGPT', `pairing (formed complete pairs: ${interactions.length})`)
    }

    const { queuedCount, savedCount, duplicateCount, failureCount } =
      await this.processInteractions(interactions)

    logger.logScanSummary({
      platform: 'CHATGPT',
      conversationId: conversationId !== null,
      turnContainers,
      userTurns,
      assistantTurns,
      completePairs: interactions.length,
      generating: false,
      extracted: interactions.length,
      queued: queuedCount,
      saved: savedCount,
      duplicates: duplicateCount,
      failures: failureCount,
    })
  }
}
