/**
 * ChatGPT Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for ChatGPT web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the existing Step 2 service-worker messaging layer.
 */

import { diagnosticStats, logger } from '../../diagnostics'
import {
  createDbSaveInteractionMessage,
  detectPlatformFromUrl,
  sendExtensionMessage,
} from '../../shared/messages'
import type { CaptureContext, CreateInteractionInput } from '../../shared/types'
import type { ExtractedInteraction, PlatformAdapter } from '../types'
import {
  extractConversationIdFromUrl,
  extractConversationTitle,
  extractConversationTurns,
  extractModelInfo,
  isPageGenerating,
  pairTurnsIntoInteractions,
} from './parser'

const MUTATION_DEBOUNCE_MS = 500
const NEW_CHAT_URL_TIMEOUT_MS = 4000

interface PendingUnboundInteraction {
  interaction: ExtractedInteraction
  key: string
  timer: ReturnType<typeof setTimeout>
}

export interface ChatGPTAdapterOptions {
  mutationDebounceMs?: number
  newChatTimeoutMs?: number
}

export class ChatGPTAdapter implements PlatformAdapter {
  public readonly platform = 'chatgpt' as const

  private observing = false
  private observer: MutationObserver | null = null
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private lastObservedUrl = ''
  private isInitialScan = true

  private mutationDebounceMs: number
  private newChatTimeoutMs: number

  /**
   * Set of processed interaction keys for the current tab session to prevent duplicate work.
   */
  private processedKeys = new Set<string>()

  /**
   * Pending interactions observed before ChatGPT assigns a conversation ID in the URL.
   */
  private pendingUnboundInteractions = new Map<string, PendingUnboundInteraction>()

  constructor(options?: ChatGPTAdapterOptions) {
    this.mutationDebounceMs = options?.mutationDebounceMs ?? MUTATION_DEBOUNCE_MS
    this.newChatTimeoutMs = options?.newChatTimeoutMs ?? NEW_CHAT_URL_TIMEOUT_MS
  }

  /**
   * Determines if this adapter is applicable for the current URL.
   */
  canHandle(url: string): boolean {
    return detectPlatformFromUrl(url) === 'chatgpt'
  }

  /**
   * Starts DOM observation and triggers initial extraction pass.
   */
  start(): void {
    if (this.observing) {
      return
    }

    this.observing = true
    this.isInitialScan = true
    this.lastObservedUrl = window.location.href

    logger.info(
      'Adapter',
      'CHATGPT',
      `Starting adapter lifecycle (initial URL: ${this.lastObservedUrl})`
    )
    logger.debug('Adapter', 'CHATGPT', 'Scheduling initial DOM scan in 100ms...')

    // Initial pass for already-completed turns on page load
    this.scheduleProcessing(100)

    // Observe DOM mutations
    this.observer = new MutationObserver(() => {
      this.handleDomMutation()
    })

    const targetNode = document.body || document.documentElement
    if (targetNode) {
      this.observer.observe(targetNode, {
        childList: true,
        subtree: true,
        characterData: true,
      })
    }

    logger.info('Adapter', 'CHATGPT', 'Adapter started and observing conversation DOM mutations.')
  }

  /**
   * Stops DOM observation and releases resources.
   */
  stop(): void {
    if (!this.observing) {
      return
    }

    logger.info('Adapter', 'CHATGPT', 'Stopping adapter and disconnecting observer.')

    if (this.observer) {
      this.observer.disconnect()
      this.observer = null
    }

    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
      this.debounceTimer = null
    }

    const pendingCount = this.pendingUnboundInteractions.size
    // Cancel all pending timers cleanly
    for (const [, pending] of this.pendingUnboundInteractions) {
      clearTimeout(pending.timer)
    }
    this.pendingUnboundInteractions.clear()

    this.observing = false
    logger.info(
      'Adapter',
      'CHATGPT',
      `Adapter stopped. Pending buffer cleared: ${pendingCount} items.`
    )
  }

  /**
   * Returns whether the adapter is actively observing.
   */
  isObserving(): boolean {
    return this.observing
  }

  /**
   * Generates a stable in-memory deduplication key for an extracted interaction.
   * NOTE: Key is independent of conversationId so that transition from null ID to real ID
   * does not create duplicate in-memory entries or duplicate database records.
   */
  private generateInteractionKey(interaction: ExtractedInteraction): string {
    if (interaction.messageId) {
      return `msg:${interaction.messageId}`
    }

    const qSnippet = interaction.queryText.slice(0, 60)
    const rSnippet = interaction.responseText.slice(0, 60)
    return `pair:${qSnippet}|${rSnippet}`
  }

  /**
   * Handles DOM mutation events with debouncing and URL change detection.
   */
  private handleDomMutation(): void {
    if (!this.observing) {
      return
    }

    logger.debug('Adapter', 'CHATGPT', 'DOM mutation detected.')

    // Check for SPA URL changes
    const currentUrl = window.location.href
    if (currentUrl !== this.lastObservedUrl) {
      const previousUrl = this.lastObservedUrl
      this.lastObservedUrl = currentUrl

      const previousConvId = extractConversationIdFromUrl(previousUrl)
      const newConvId = extractConversationIdFromUrl(currentUrl)

      logger.info(
        'Adapter',
        'CHATGPT',
        `Navigation detected: '${previousUrl}' -> '${currentUrl}' (previousConvId: ${previousConvId ?? 'none'}, newConvId: ${newConvId ?? 'none'})`
      )

      // If new conversation ID appeared, flush pending unbound interactions with it
      if (newConvId && this.pendingUnboundInteractions.size > 0) {
        logger.info(
          'Adapter',
          'CHATGPT',
          `Releasing ${this.pendingUnboundInteractions.size} pending unbound interaction(s) with new conversation ID: ${newConvId}`
        )
        this.flushPendingWithConversationId(newConvId)
      }

      const isNewChatAssignment = !previousConvId && !!newConvId
      if (!isNewChatAssignment) {
        // True SPA navigation (A->B, A->/, etc.) — treat next scan as historical content.
        logger.debug(
          'Adapter',
          'CHATGPT',
          'URL change classified as true SPA navigation; resetting scan state to on_load.'
        )
        this.isInitialScan = true
      } else {
        logger.debug(
          'Adapter',
          'CHATGPT',
          'URL change classified as new-chat ID assignment; preserving on_generate capture context.'
        )
      }

      this.scheduleProcessing(200)
      return
    }

    // Debounce mutation handling
    this.scheduleProcessing(this.mutationDebounceMs)
  }

  /**
   * Flushes any pending unbound interactions using the newly acquired conversation ID.
   */
  private flushPendingWithConversationId(conversationId: string): void {
    const title = extractConversationTitle(document)
    logger.info(
      'Adapter',
      'CHATGPT',
      `Flushing ${this.pendingUnboundInteractions.size} pending unbound interaction(s) with conversation ID: ${conversationId}`
    )

    for (const [key, pending] of this.pendingUnboundInteractions) {
      clearTimeout(pending.timer)
      pending.interaction.conversationId = conversationId
      if (title) {
        pending.interaction.conversationTitle = title
      }
      this.pendingUnboundInteractions.delete(key)
      void this.persistInteraction(pending.interaction, key)
    }
  }

  /**
   * Schedules a conversation processing pass with debouncing.
   */
  private scheduleProcessing(delayMs: number): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
    }

    this.debounceTimer = setTimeout(() => {
      this.processConversation().catch((err) => {
        diagnosticStats.increment('extractionFailures')
        logger.error(
          'Adapter',
          'CHATGPT',
          `Unexpected error processing conversation: ${err instanceof Error ? err.message : String(err)}`
        )
      })
    }, delayMs)
  }

  /**
   * Scans the current conversation DOM, extracts complete pairs, and persists new interactions.
   */
  public async processConversation(): Promise<void> {
    if (!this.observing) {
      return
    }

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
      `Starting conversation DOM processing pass (URL: ${currentUrl})`
    )
    logger.debug(
      'Adapter',
      'CHATGPT',
      `Conversation ID availability: ${conversationId ? `present (${conversationId})` : 'null'}`
    )

    // Generation completion guard: If any stop button or active streaming class is present, reschedule
    const generating = isPageGenerating(document.body || document)
    logger.debug('Adapter', 'CHATGPT', `Evaluating page generation state: generating=${generating}`)

    if (generating) {
      diagnosticStats.increment('streamingDeferrals')
      logger.info(
        'Adapter',
        'CHATGPT',
        `Processing deferred: Active generation/streaming detected (stop button or streaming cursor present). Rescheduling in ${this.mutationDebounceMs}ms.`
      )
      this.scheduleProcessing(this.mutationDebounceMs)
      return
    }

    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    // If conversation ID is now present and we have pending unbound items, flush them
    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId)
    }

    const currentCaptureContext: CaptureContext = this.isInitialScan ? 'on_load' : 'on_generate'
    this.isInitialScan = false

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
      captureContext: currentCaptureContext,
    })

    diagnosticStats.increment('completePairs', interactions.length)
    diagnosticStats.increment('interactionsExtracted', interactions.length)

    logger.debug('Adapter', 'CHATGPT', `Interactions paired: completePairs=${interactions.length}`)

    let queuedCount = 0
    let savedCount = 0
    let duplicateCount = 0
    let failureCount = 0

    for (const interaction of interactions) {
      const key = this.generateInteractionKey(interaction)

      logger.logExtraction('CHATGPT', {
        platform: 'chatgpt',
        conversationId: interaction.conversationId,
        userMessageId: interaction.userMessageId,
        messageId: interaction.messageId,
        queryCharCount: interaction.queryText.length,
        responseCharCount: interaction.responseText.length,
        modelProvider: interaction.model.provider,
        modelName: interaction.model.name,
        captureContext: interaction.captureContext,
        sourceTimestamp: interaction.sourceTimestamp,
      })

      if (this.processedKeys.has(key)) {
        logger.debug(
          'Adapter',
          'CHATGPT',
          `Skipping interaction (${key}): already processed in this session.`
        )
        duplicateCount++
        continue
      }

      // If conversation ID is currently null (new chat at /), hold in bounded pending queue
      if (interaction.conversationId === null) {
        if (!this.pendingUnboundInteractions.has(key)) {
          queuedCount++
          diagnosticStats.increment('interactionsQueued')
          logger.info(
            'Adapter',
            'CHATGPT',
            `Conversation ID is null; queuing interaction in pending unbound buffer (key: ${key}, timeout: ${this.newChatTimeoutMs}ms)`
          )

          const timer = setTimeout(() => {
            const pending = this.pendingUnboundInteractions.get(key)
            if (pending) {
              this.pendingUnboundInteractions.delete(key)
              void this.persistInteraction(pending.interaction, key)
            }
          }, this.newChatTimeoutMs)

          this.pendingUnboundInteractions.set(key, {
            interaction,
            key,
            timer,
          })
        }
        continue
      }

      // If already held in pending, clear its pending timer and remove from map
      if (this.pendingUnboundInteractions.has(key)) {
        const pending = this.pendingUnboundInteractions.get(key)
        if (pending) {
          clearTimeout(pending.timer)
          this.pendingUnboundInteractions.delete(key)
        }
      }

      const result = await this.persistInteraction(interaction, key)
      if (result === 'saved') {
        savedCount++
      } else if (result === 'duplicate') {
        duplicateCount++
      } else {
        failureCount++
      }
    }

    // Emit single-line scan summary
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

  /**
   * Dispatches an interaction to the service worker for persistence.
   */
  private async persistInteraction(
    interaction: ExtractedInteraction,
    key: string
  ): Promise<'saved' | 'duplicate' | 'failed'> {
    const input: CreateInteractionInput = {
      platform: 'chatgpt',
      conversation_id: interaction.conversationId,
      message_id: interaction.messageId,
      user_message_id: interaction.userMessageId,
      observed_at: interaction.observedAt,
      source_timestamp: interaction.sourceTimestamp,
      capture_context: interaction.captureContext,
      model: interaction.model,
      query: {
        text: interaction.queryText,
      },
      response: {
        text: interaction.responseText,
      },
      conversation_title: interaction.conversationTitle,
    }

    logger.debug(
      'Messaging',
      'CHATGPT',
      `Dispatching DB_SAVE_INTERACTION to service worker (conversationId: ${interaction.conversationId ?? 'null'}, captureContext: ${interaction.captureContext}, queryChars: ${interaction.queryText.length}, responseChars: ${interaction.responseText.length})`
    )

    try {
      const msg = createDbSaveInteractionMessage('content-script', input)
      const response = await sendExtensionMessage(msg)

      if (response.success) {
        this.processedKeys.add(key)
        diagnosticStats.increment('interactionsSaved')
        logger.info(
          'Messaging',
          'CHATGPT',
          `Service worker acknowledged DB_SAVE_INTERACTION successfully (conversationId: ${interaction.conversationId || 'unbound'}, context: ${interaction.captureContext})`
        )
        return 'saved'
      } else {
        // If duplicate in DB, also mark as processed so we don't keep attempting
        if (response.error && response.error.includes('already exists')) {
          this.processedKeys.add(key)
          diagnosticStats.increment('duplicates')
          logger.info('Database', 'CHATGPT', `Duplicate interaction detected: ${response.error}`)
          return 'duplicate'
        } else {
          diagnosticStats.increment('persistenceFailures')
          logger.error(
            'Messaging',
            'CHATGPT',
            `Service worker returned error saving interaction: ${response.error ?? 'Unknown error'}`
          )
          return 'failed'
        }
      }
    } catch (err) {
      diagnosticStats.increment('persistenceFailures')
      const errMsg = err instanceof Error ? err.message : String(err)
      if (/extension context invalidated/i.test(errMsg)) {
        logger.error(
          'Messaging',
          'CHATGPT',
          'Extension context invalidated! The extension runtime was reloaded or updated while the page remained open.'
        )
      } else {
        logger.error(
          'Messaging',
          'CHATGPT',
          `Failed to dispatch DB_SAVE_INTERACTION to service worker: ${errMsg}`
        )
      }
      return 'failed'
    }
  }
}
