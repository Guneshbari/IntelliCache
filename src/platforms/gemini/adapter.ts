/**
 * Gemini Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for Gemini (gemini.google.com) web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the service-worker messaging layer.
 *
 * Navigation detection uses a dedicated NavigationWatcher (popstate + polling) rather than
 * relying solely on the MutationObserver to detect URL changes. Gemini navigates via
 * history.pushState/replaceState which does NOT fire popstate and may not produce DOM
 * mutations large enough to trigger the observer before URL comparison is needed.
 *
 * Navigation state machine:
 *   new_chat_without_id  -> conversation_with_id  (/app -> /app/{id}, preserve on_generate)
 *   conversation_with_id -> conversation_with_id  (A -> B, reset to on_load, clear cache)
 *   conversation_with_id -> new_chat_without_id   (navigate to new chat, reset to on_load)
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
  isGeminiGuestSession,
  isPageGenerating,
  pairTurnsIntoInteractions,
} from './parser'
import { cleanQueryText } from '../shared/parser-utils'

export type GeminiAdapterOptions = BaseAdapterOptions

export class GeminiAdapter extends BaseAdapter {
  public readonly platform = 'gemini' as const
  protected readonly platformTag = 'GEMINI' as const

  private guestConversationId: string | null = null

  constructor(options?: GeminiAdapterOptions) {
    super(options)
  }

  canHandle(url: string): boolean {
    return detectPlatformFromUrl(url) === 'gemini'
  }

  start(): void {
    if (this.observing) return

    this.observing = true
    this.isInitialScan = true

    const initialUrl = window.location.href
    const initialConvId = extractConversationIdFromUrl(initialUrl)
    this.navState = initialConvId ? 'conversation_with_id' : 'new_chat_without_id'

    logger.info(
      'Adapter',
      'GEMINI',
      `Starting adapter lifecycle (initialUrl: ${redactUrlForLog(initialUrl)}, navState: ${this.navState}, conversationId: ${initialConvId ? 'present' : 'none'})`
    )

    this.startNavWatcher(initialUrl, (prev, next) => this.onNavigate(prev, next))
    this.scheduleProcessing(100)
    this.startMutationObserver(() => this.onDomMutation())

    logger.info('Adapter', 'GEMINI', 'Adapter started and observing conversation DOM mutations.')
  }

  stop(): void {
    if (!this.observing) return
    logger.info('Adapter', 'GEMINI', 'Stopping adapter and disconnecting observer.')
    this.guestConversationId = null
    this.stopShared()
  }

  /** Delegates to the shared handleNavigation from BaseAdapter. */
  handleNavigation(prevUrl: string, newUrl: string): void {
    super.handleNavigation(prevUrl, newUrl, extractConversationIdFromUrl, () =>
      extractConversationTitle(document)
    )
  }

  private onNavigate(prevUrl: string, newUrl: string): void {
    this.handleNavigation(prevUrl, newUrl)
  }

  protected handleDomMutation(): void {
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
      'GEMINI',
      `DOM scan started (URL: ${redactUrlForLog(currentUrl)}, navState: ${this.navState})`
    )
    logger.debug('Adapter', 'GEMINI', `Conversation ID: ${conversationId ? 'present' : 'null'}`)

    const root = document.body || document

    // Lightweight diagnostic counts queried only when debug logging is active to avoid double DOM walks
    if (logger.isDebugEnabled()) {
      const userQueryCount = root.querySelectorAll('user-query').length
      const modelResponseCount = root.querySelectorAll('model-response').length
      const docReadyState = typeof document !== 'undefined' ? document.readyState : 'unknown'

      logger.debug(
        'Adapter',
        'GEMINI',
        `Runtime DOM check | readyState=${docReadyState} | bodyExists=${!!document.body} | userQueryElements=${userQueryCount} | modelResponseElements=${modelResponseCount}`
      )

      if (userQueryCount === 0 && modelResponseCount === 0) {
        logger.debug(
          'Adapter',
          'GEMINI',
          'Gemini DOM contains 0 user-query and 0 model-response elements at scan time.'
        )
      }
    }

    const generating = isPageGenerating(root)
    logger.debug('Adapter', 'GEMINI', `Generation state: generating=${generating}`)

    if (generating) {
      this.handleStreamingDeferred()
      return
    }
    this.resetStreamingDeferrals()

    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId, title)
    }

    const captureContext = this.consumeCaptureContext()

    const standardContainersCount = root.querySelectorAll(
      'user-query, model-response, [data-message-author-role="user"], [data-message-author-role="assistant"]'
    ).length
    const turnContainers =
      standardContainersCount > 0
        ? standardContainersCount
        : root.querySelectorAll(
            '.user-query-container, .response-container, [data-query-id], [data-response-id]'
          ).length
    const turns = extractConversationTurns(root)
    if (this.checkAndDeferStaleDom(turns)) {
      return
    }
    const userTurns = turns.filter((t) => t.role === 'user').length
    const assistantTurns = turns.filter((t) => t.role === 'assistant').length

    diagnosticStats.increment('userTurnsFound', userTurns)
    diagnosticStats.increment('assistantTurnsFound', assistantTurns)

    if (userTurns === 0) {
      const rawUserQueries = root.querySelectorAll('user-query').length
      if (rawUserQueries > 0) {
        logger.warn(
          'Adapter',
          'GEMINI',
          `Parser extraction anomaly: ${rawUserQueries} user-query elements found in DOM, but 0 user turns extracted.`
        )
      }
    }

    if (userTurns > 0 && assistantTurns === 0) {
      logger.debug(
        'Adapter',
        'GEMINI',
        `Transient conversation state: ${userTurns} user turn(s) found, but 0 assistant turn(s) yet.`
      )
    }

    logger.debug(
      'Adapter',
      'GEMINI',
      `DOM scan completed: total=${turns.length}, userTurns=${userTurns}, assistantTurns=${assistantTurns}, captureContext=${captureContext}`
    )

    if (turns.length === 0) {
      if (this.guestConversationId) {
        this.guestConversationId = null
      }
      logger.debug('Adapter', 'GEMINI', 'No conversation turns discovered in DOM.')
      return
    }

    // Assign a deterministic session-scoped conversation ID for unauthenticated guest sessions
    const isGuest = isGeminiGuestSession(root)
    if (!conversationId && isGuest) {
      if (!this.guestConversationId) {
        this.guestConversationId = `guest_gemini_${Date.now()}_${Math.random().toString(36).slice(2, 8)}`
        logger.info(
          'Adapter',
          'GEMINI',
          `Unauthenticated / guest Gemini session detected. Assigned local session conversation ID: ${this.guestConversationId}`
        )
      }
    } else if (conversationId) {
      this.guestConversationId = null
    }

    const effectiveConvId = conversationId ?? this.guestConversationId
    const effectiveTitle =
      title ||
      (this.guestConversationId && turns.length > 0
        ? cleanQueryText(turns.find((t) => t.role === 'user')?.text || '').slice(0, 60) ||
          'Guest Chat'
        : null)

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId: effectiveConvId,
      title: effectiveTitle,
      model,
      captureContext,
    })

    if (userTurns > 0 && assistantTurns > 0 && interactions.length === 0) {
      logger.debug(
        'Adapter',
        'GEMINI',
        `Pairing problem: formed 0 complete pairs from ${userTurns} user turn(s) and ${assistantTurns} assistant turn(s).`
      )
    }

    diagnosticStats.increment('completePairs', interactions.length)
    diagnosticStats.increment('interactionsExtracted', interactions.length)
    logger.debug('Adapter', 'GEMINI', `Interactions paired: completePairs=${interactions.length}`)
    if (interactions.length > 0) {
      logger.info('Lifecycle', 'GEMINI', `pairing (formed complete pairs: ${interactions.length})`)
    }

    const { queuedCount, savedCount, duplicateCount, failureCount } =
      await this.processInteractions(interactions)

    if (effectiveConvId && this.unboundTurnRecords.size > 0) {
      this.flushRemainingUnboundTurnRecords(effectiveConvId, effectiveTitle)
    }

    logger.logScanSummary({
      platform: 'GEMINI',
      conversationId: effectiveConvId !== null,
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
