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

export type ChatGPTAdapterOptions = BaseAdapterOptions

export class ChatGPTAdapter extends BaseAdapter {
  public readonly platform = 'chatgpt' as const
  protected readonly platformTag = 'CHATGPT' as const

  private lastObservedUrl = ''

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

    logger.info('Adapter', 'CHATGPT', `Starting adapter lifecycle (initial URL: ${this.lastObservedUrl})`)
    logger.debug('Adapter', 'CHATGPT', 'Scheduling initial DOM scan in 100ms...')

    this.scheduleProcessing(100)
    this.startMutationObserver(() => this.handleDomMutation())

    logger.info('Adapter', 'CHATGPT', 'Adapter started and observing conversation DOM mutations.')
  }

  stop(): void {
    if (!this.observing) return
    logger.info('Adapter', 'CHATGPT', 'Stopping adapter and disconnecting observer.')
    this.stopShared()
  }

  /**
   * Handles DOM mutation events with debouncing and inline URL change detection.
   * ChatGPT's SPA navigation reliably triggers DOM mutations, so we detect URL changes here
   * rather than using a separate NavigationWatcher.
   */
  private handleDomMutation(): void {
    if (!this.observing) return

    logger.debug('Adapter', 'CHATGPT', 'DOM mutation detected.')

    const currentUrl = window.location.href
    if (currentUrl !== this.lastObservedUrl) {
      const previousUrl = this.lastObservedUrl
      this.lastObservedUrl = currentUrl

      const previousConvId = extractConversationIdFromUrl(previousUrl)
      const newConvId = extractConversationIdFromUrl(currentUrl)

      logger.info(
        'Navigation',
        'CHATGPT',
        `Navigation detected: '${previousUrl}' -> '${currentUrl}' (previousConvId: ${previousConvId ?? 'none'}, newConvId: ${newConvId ?? 'none'})`
      )

      if (newConvId && this.pendingUnboundInteractions.size > 0) {
        logger.info(
          'Navigation',
          'CHATGPT',
          `Releasing ${this.pendingUnboundInteractions.size} pending unbound interaction(s) with new conversation ID: ${newConvId}`
        )
        this.flushPendingWithConversationId(newConvId, extractConversationTitle(document))
      }

      const isNewChatAssignment = !previousConvId && !!newConvId
      if (!isNewChatAssignment) {
        logger.debug('Navigation', 'CHATGPT', 'URL change classified as true SPA navigation; resetting scan state to on_load.')
        this.isInitialScan = true
      } else {
        logger.debug('Navigation', 'CHATGPT', 'URL change classified as new-chat ID assignment; preserving on_generate capture context.')
      }

      this.scheduleProcessing(200)
      return
    }

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

    logger.debug('Adapter', 'CHATGPT', `Starting conversation DOM processing pass (URL: ${currentUrl})`)
    logger.debug('Adapter', 'CHATGPT', `Conversation ID: ${conversationId ? `present (${conversationId})` : 'null'}`)

    const generating = isPageGenerating(document.body || document)
    logger.debug('Adapter', 'CHATGPT', `Evaluating page generation state: generating=${generating}`)

    if (generating) {
      diagnosticStats.increment('streamingDeferrals')
      logger.debug(
        'Adapter',
        'CHATGPT',
        `Processing deferred: Active generation/streaming detected. Rescheduling in ${this.mutationDebounceMs}ms.`
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
      document.querySelectorAll('article[data-testid^="conversation-turn-"]')
    ).length
    const turns = extractConversationTurns(document.body || document)
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

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId,
      title,
      model,
      captureContext,
    })

    diagnosticStats.increment('completePairs', interactions.length)
    diagnosticStats.increment('interactionsExtracted', interactions.length)
    logger.debug('Adapter', 'CHATGPT', `Interactions paired: completePairs=${interactions.length}`)

    const { queuedCount, savedCount, duplicateCount, failureCount } =
      await this.processInteractions(interactions)

    logger.logScanSummary({
      platform: 'CHATGPT',
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
