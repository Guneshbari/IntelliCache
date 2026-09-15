/**
 * Claude Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for Claude (claude.ai) web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the service-worker messaging layer.
 *
 * Navigation detection uses a dedicated NavigationWatcher (popstate + polling) rather than
 * relying solely on the MutationObserver to detect URL changes. Claude navigates via
 * history.pushState/replaceState which does NOT fire popstate and may not produce DOM
 * mutations large enough to trigger the observer before URL comparison is needed.
 *
 * Navigation state machine:
 *   new_chat_without_id  -> conversation_with_id  (/new -> /chat/{id}, preserve on_generate)
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
  isPageGenerating,
  pairTurnsIntoInteractions,
} from './parser'
import { CLAUDE_SELECTORS } from './selectors'

export type ClaudeAdapterOptions = BaseAdapterOptions

export class ClaudeAdapter extends BaseAdapter {
  public readonly platform = 'claude' as const
  protected readonly platformTag = 'CLAUDE' as const

  constructor(options?: ClaudeAdapterOptions) {
    super(options)
  }

  canHandle(url: string): boolean {
    return detectPlatformFromUrl(url) === 'claude'
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
      'CLAUDE',
      `Starting adapter lifecycle (initialUrl: ${redactUrlForLog(initialUrl)}, navState: ${this.navState}, conversationId: ${initialConvId ? 'present' : 'none'})`
    )

    this.startNavWatcher(initialUrl, (prev, next) => this.onNavigate(prev, next))
    this.scheduleProcessing(100)
    this.startMutationObserver(() => this.handleDomMutation())

    logger.info('Adapter', 'CLAUDE', 'Adapter started and observing conversation DOM mutations.')
  }

  stop(): void {
    if (!this.observing) return
    logger.info('Adapter', 'CLAUDE', 'Stopping adapter and disconnecting observer.')
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
    logger.debug('Adapter', 'CLAUDE', 'DOM mutation detected.')
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
      'CLAUDE',
      `DOM scan started (URL: ${redactUrlForLog(currentUrl)}, navState: ${this.navState})`
    )
    logger.debug(
      'Adapter',
      'CLAUDE',
      `Conversation ID: ${conversationId ? 'present' : 'null'}`
    )

    const generating = isPageGenerating(document.body || document)
    logger.debug('Adapter', 'CLAUDE', `Generation state: generating=${generating}`)

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

    const turnContainers = Array.from(
      document.querySelectorAll(
        `${CLAUDE_SELECTORS.TRANSCRIPT_LIST} [data-testid="transcript-row"], ${CLAUDE_SELECTORS.USER_MESSAGE}, ${CLAUDE_SELECTORS.ASSISTANT_MESSAGE}`
      )
    ).length

    const turns = extractConversationTurns(document.body || document)
    const userTurns = turns.filter((t) => t.role === 'user').length
    const assistantTurns = turns.filter((t) => t.role === 'assistant').length

    diagnosticStats.increment('userTurnsFound', userTurns)
    diagnosticStats.increment('assistantTurnsFound', assistantTurns)

    logger.debug(
      'Adapter',
      'CLAUDE',
      `DOM scan completed: total=${turns.length}, userTurns=${userTurns}, assistantTurns=${assistantTurns}, captureContext=${captureContext}`
    )

    if (turns.length === 0) {
      logger.debug('Adapter', 'CLAUDE', 'No conversation turns discovered in DOM.')
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
    logger.debug('Adapter', 'CLAUDE', `Interactions paired: completePairs=${interactions.length}`)

    const { queuedCount, savedCount, duplicateCount, failureCount } =
      await this.processInteractions(interactions)

    logger.logScanSummary({
      platform: 'CLAUDE',
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
