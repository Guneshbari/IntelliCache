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

import { diagnosticStats, logger } from '../../diagnostics'
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
  isPageGenerating,
  pairTurnsIntoInteractions,
} from './parser'

export type GeminiAdapterOptions = BaseAdapterOptions

export class GeminiAdapter extends BaseAdapter {
  public readonly platform = 'gemini' as const
  protected readonly platformTag = 'GEMINI' as const

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
      `Starting adapter lifecycle (initialUrl: ${initialUrl}, navState: ${this.navState}, conversationId: ${initialConvId ?? 'none'})`
    )

    this.startNavWatcher(initialUrl, (prev, next) => this.onNavigate(prev, next))
    this.scheduleProcessing(100)
    this.startMutationObserver(() => this.handleDomMutation())

    logger.info('Adapter', 'GEMINI', 'Adapter started and observing conversation DOM mutations.')
  }

  stop(): void {
    if (!this.observing) return
    logger.info('Adapter', 'GEMINI', 'Stopping adapter and disconnecting observer.')
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

  private handleDomMutation(): void {
    if (!this.observing) return
    logger.debug('Adapter', 'GEMINI', 'DOM mutation detected.')
    this.scheduleProcessing(this.mutationDebounceMs)
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
      `DOM scan started (URL: ${currentUrl}, navState: ${this.navState})`
    )
    logger.debug(
      'Adapter',
      'GEMINI',
      `Conversation ID: ${conversationId ? `present (${conversationId})` : 'null'}`
    )

    const root = document.body || document

    // Diagnostic element counts to aid debugging Gemini DOM changes
    const userQueryCount = root.querySelectorAll('user-query').length
    const modelResponseCount = root.querySelectorAll('model-response').length
    const userRoleCount = root.querySelectorAll('[data-message-author-role="user"]').length
    const asstRoleCount = root.querySelectorAll('[data-message-author-role="assistant"]').length
    const userTextCount = root.querySelectorAll(
      'user-query .query-content, [id^="user-query-content"]'
    ).length
    const asstTextCount = root.querySelectorAll(
      'model-response .markdown, model-response message-content'
    ).length
    const docReadyState = typeof document !== 'undefined' ? document.readyState : 'unknown'

    logger.debug(
      'Adapter',
      'GEMINI',
      `Runtime DOM check | readyState=${docReadyState} | bodyExists=${!!document.body} | userQueryElements=${userQueryCount} | modelResponseElements=${modelResponseCount} | userRoleElements=${userRoleCount} | asstRoleElements=${asstRoleCount} | userTextElements=${userTextCount} | asstTextElements=${asstTextCount}`
    )

    if (userQueryCount === 0 && modelResponseCount === 0) {
      logger.debug(
        'Adapter',
        'GEMINI',
        'Gemini DOM contains 0 user-query and 0 model-response elements at scan time.'
      )
    }

    const generating = isPageGenerating(root)
    logger.debug('Adapter', 'GEMINI', `Generation state: generating=${generating}`)

    if (generating) {
      diagnosticStats.increment('streamingDeferrals')
      logger.debug(
        'Adapter',
        'GEMINI',
        `Processing deferred: active generation detected. Rescheduling in ${this.mutationDebounceMs}ms.`
      )
      this.scheduleProcessing(this.mutationDebounceMs)
      return
    }

    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId, title)
    }

    const captureContext = this.consumeCaptureContext()

    const turnContainers = Array.from(
      root.querySelectorAll(
        'user-query, model-response, [data-message-author-role="user"], [data-message-author-role="assistant"]'
      )
    ).length
    const turns = extractConversationTurns(root)
    const userTurns = turns.filter((t) => t.role === 'user').length
    const assistantTurns = turns.filter((t) => t.role === 'assistant').length

    diagnosticStats.increment('userTurnsFound', userTurns)
    diagnosticStats.increment('assistantTurnsFound', assistantTurns)

    if (userQueryCount > 0 && userTurns === 0) {
      logger.warn(
        'Adapter',
        'GEMINI',
        `Parser extraction anomaly: ${userQueryCount} user-query elements found in DOM, but 0 user turns extracted.`
      )
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
      logger.debug('Adapter', 'GEMINI', 'No conversation turns discovered in DOM.')
      return
    }

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId,
      title,
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

    const { queuedCount, savedCount, duplicateCount, failureCount } =
      await this.processInteractions(interactions)

    logger.logScanSummary({
      platform: 'GEMINI',
      conversationId: hasConvId,
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
