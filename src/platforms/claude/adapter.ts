/**
 * Claude Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for Claude (claude.ai) web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the existing Step 2 service-worker messaging layer.
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

import { diagnosticStats, logger } from '../../diagnostics'
import { NavigationWatcher } from '../../shared/navigation-watcher'
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
const NAV_POLL_INTERVAL_MS = 250

/**
 * Explicit navigation state for the current conversation context.
 * Replaces the implicit isInitialScan flag for navigation classification.
 */
type ConversationNavState = 'new_chat_without_id' | 'conversation_with_id' | 'unknown'

interface PendingUnboundInteraction {
  interaction: ExtractedInteraction
  key: string
  timer: ReturnType<typeof setTimeout>
}

export interface ClaudeAdapterOptions {
  mutationDebounceMs?: number
  newChatTimeoutMs?: number
  navPollIntervalMs?: number
}

export class ClaudeAdapter implements PlatformAdapter {
  public readonly platform = 'claude' as const

  private observing = false
  private observer: MutationObserver | null = null
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private isInitialScan = true
  private navState: ConversationNavState = 'unknown'
  private navWatcher: NavigationWatcher | null = null

  private mutationDebounceMs: number
  private newChatTimeoutMs: number
  private navPollIntervalMs: number

  /** Processed interaction keys for the current tab session to prevent duplicate work. */
  private processedKeys = new Set<string>()

  /** Pending interactions observed before Claude assigns a conversation ID in the URL. */
  private pendingUnboundInteractions = new Map<string, PendingUnboundInteraction>()

  constructor(options?: ClaudeAdapterOptions) {
    this.mutationDebounceMs = options?.mutationDebounceMs ?? MUTATION_DEBOUNCE_MS
    this.newChatTimeoutMs = options?.newChatTimeoutMs ?? NEW_CHAT_URL_TIMEOUT_MS
    this.navPollIntervalMs = options?.navPollIntervalMs ?? NAV_POLL_INTERVAL_MS
  }

  /**
   * Determines if this adapter is applicable for the current URL.
   */
  canHandle(url: string): boolean {
    return detectPlatformFromUrl(url) === 'claude'
  }

  /**
   * Starts DOM observation, navigation watching, and triggers initial extraction pass.
   */
  start(): void {
    if (this.observing) {
      return
    }

    this.observing = true
    this.isInitialScan = true

    const initialUrl = window.location.href
    const initialConvId = extractConversationIdFromUrl(initialUrl)
    this.navState = initialConvId ? 'conversation_with_id' : 'new_chat_without_id'

    logger.info(
      'Adapter',
      'CLAUDE',
      `Starting adapter lifecycle (initialUrl: ${initialUrl}, navState: ${this.navState}, conversationId: ${initialConvId ?? 'none'})`
    )

    // Set up the dedicated navigation watcher (popstate + polling).
    // This is independent of MutationObserver and reliably handles history.pushState/replaceState.
    this.navWatcher = new NavigationWatcher(
      (prevUrl, newUrl) => this.handleNavigation(prevUrl, newUrl),
      { pollIntervalMs: this.navPollIntervalMs }
    )
    this.navWatcher.start(initialUrl)
    logger.info(
      'Navigation',
      'CLAUDE',
      `Navigation listener installed (popstate event listener + polling every ${this.navPollIntervalMs}ms).`
    )

    // Schedule initial DOM scan for already-completed turns on page load
    this.scheduleProcessing(100)

    // Set up MutationObserver for conversation content changes (NOT URL detection)
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

    logger.info('Adapter', 'CLAUDE', 'Adapter started and observing conversation DOM mutations.')
  }

  /**
   * Stops DOM observation, navigation watching, and releases resources.
   */
  stop(): void {
    if (!this.observing) {
      return
    }

    logger.info('Adapter', 'CLAUDE', 'Stopping adapter and disconnecting observer.')

    if (this.navWatcher) {
      this.navWatcher.stop()
      this.navWatcher = null
      logger.info(
        'Navigation',
        'CLAUDE',
        'Navigation listener removed (popstate + polling stopped).'
      )
    }

    if (this.observer) {
      this.observer.disconnect()
      this.observer = null
    }

    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
      this.debounceTimer = null
    }

    const pendingCount = this.pendingUnboundInteractions.size
    for (const [, pending] of this.pendingUnboundInteractions) {
      clearTimeout(pending.timer)
    }
    this.pendingUnboundInteractions.clear()

    this.observing = false
    logger.info(
      'Adapter',
      'CLAUDE',
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
   * Handles URL transitions detected by NavigationWatcher.
   * Fires independently of DOM mutations — this is what catches history.pushState/replaceState
   * and popstate navigation that Claude uses for SPA routing.
   *
   * Navigation classification:
   *   new_chat_assignment: /new -> /chat/{id}  — preserve on_generate, flush pending
   *   existing_conversation_navigation: A -> B  — reset to on_load, clear session cache
   *   new_to_new: /new -> /new  — no action needed
   */
  handleNavigation(prevUrl: string, newUrl: string): void {
    if (!this.observing) {
      return
    }

    const prevConvId = extractConversationIdFromUrl(prevUrl)
    const newConvId = extractConversationIdFromUrl(newUrl)
    const prevPathname = (() => {
      try {
        return new URL(prevUrl).pathname
      } catch {
        return prevUrl
      }
    })()
    const newPathname = (() => {
      try {
        return new URL(newUrl).pathname
      } catch {
        return newUrl
      }
    })()

    const isNewChatAssignment = !prevConvId && !!newConvId
    const isConversationToConversation = !!prevConvId && !!newConvId && prevConvId !== newConvId
    const isNewToNew = !prevConvId && !newConvId

    const classification = isNewChatAssignment
      ? 'new_chat_assignment'
      : isConversationToConversation
        ? 'existing_conversation_navigation'
        : isNewToNew
          ? 'new_to_new'
          : 'unknown'

    logger.info(
      'Navigation',
      'CLAUDE',
      `URL transition | from=${prevPathname} | to=${newPathname} | classification=${classification} | conversationId=${newConvId ?? 'none'}`
    )

    if (isNewChatAssignment) {
      // /new -> /chat/{id}: new conversation ID was assigned.
      // DO NOT reset isInitialScan — this was a live generation, preserve on_generate context.
      this.navState = 'conversation_with_id'
      if (this.pendingUnboundInteractions.size > 0) {
        logger.info(
          'Navigation',
          'CLAUDE',
          `New-chat assignment: flushing ${this.pendingUnboundInteractions.size} pending interaction(s) with conversationId=${newConvId}`
        )
        this.flushPendingWithConversationId(newConvId!)
      } else {
        logger.debug(
          'Navigation',
          'CLAUDE',
          'New-chat assignment: no pending interactions to flush; scheduling DOM scan.'
        )
      }
    } else if (isConversationToConversation || isNewToNew) {
      // True SPA navigation — treat new content as historical (on_load).
      // Clear session key cache so new conversation's historical turns are not skipped.
      this.navState = newConvId ? 'conversation_with_id' : 'new_chat_without_id'
      this.isInitialScan = true
      this.processedKeys.clear()
      logger.info(
        'Navigation',
        'CLAUDE',
        `Existing-conversation navigation: resetting scan state to on_load, clearing session key cache (${this.processedKeys.size} keys cleared, prevConvId=${prevConvId ?? 'none'} -> newConvId=${newConvId ?? 'none'}).`
      )
    } else {
      this.navState = newConvId ? 'conversation_with_id' : 'new_chat_without_id'
      logger.debug(
        'Navigation',
        'CLAUDE',
        `URL transition classified as unknown; updating navState=${this.navState}.`
      )
    }

    logger.debug('Navigation', 'CLAUDE', 'DOM scan scheduled after navigation (delay: 250ms).')
    this.scheduleProcessing(250)
  }

  /**
   * Handles DOM mutation events.
   * URL detection is handled separately by NavigationWatcher.
   * This only schedules the debounced content processing pass.
   */
  private handleDomMutation(): void {
    if (!this.observing) {
      return
    }
    logger.debug('Adapter', 'CLAUDE', 'DOM mutation detected.')
    this.scheduleProcessing(this.mutationDebounceMs)
  }

  /**
   * Flushes any pending unbound interactions using the newly acquired conversation ID.
   */
  private flushPendingWithConversationId(conversationId: string): void {
    const title = extractConversationTitle(document)
    logger.info(
      'Adapter',
      'CLAUDE',
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
          'CLAUDE',
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
      'CLAUDE',
      `DOM scan started (URL: ${currentUrl}, navState: ${this.navState})`
    )
    logger.debug(
      'Adapter',
      'CLAUDE',
      `Conversation ID: ${conversationId ? `present (${conversationId})` : 'null'}`
    )

    const generating = isPageGenerating(document.body || document)
    logger.debug('Adapter', 'CLAUDE', `Generation state: generating=${generating}`)

    if (generating) {
      diagnosticStats.increment('streamingDeferrals')
      logger.debug(
        'Adapter',
        'CLAUDE',
        `Processing deferred: active generation detected. Rescheduling in ${this.mutationDebounceMs}ms.`
      )
      this.scheduleProcessing(this.mutationDebounceMs)
      return
    }

    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    // If a conversation ID is now present and we have pending unbound items, flush them
    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId)
    }

    const currentCaptureContext: CaptureContext = this.isInitialScan ? 'on_load' : 'on_generate'
    this.isInitialScan = false

    const turnContainers = Array.from(
      document.querySelectorAll(
        '[data-testid="transcript-list"] [data-testid="transcript-row"], [data-testid="user-message"], [data-testid="assistant-message"]'
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
      `DOM scan completed: total=${turns.length}, userTurns=${userTurns}, assistantTurns=${assistantTurns}, captureContext=${currentCaptureContext}`
    )

    if (turns.length === 0) {
      logger.debug('Adapter', 'CLAUDE', 'No conversation turns discovered in DOM.')
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

    logger.debug('Adapter', 'CLAUDE', `Interactions paired: completePairs=${interactions.length}`)

    let queuedCount = 0
    let savedCount = 0
    let duplicateCount = 0
    let failureCount = 0

    for (const interaction of interactions) {
      const key = this.generateInteractionKey(interaction)

      logger.logExtraction('CLAUDE', {
        platform: 'claude',
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
          'CLAUDE',
          `Skipping interaction (${key}): already processed in this session.`
        )
        duplicateCount++
        continue
      }

      if (interaction.conversationId === null) {
        if (!this.pendingUnboundInteractions.has(key)) {
          queuedCount++
          diagnosticStats.increment('interactionsQueued')
          logger.info(
            'Adapter',
            'CLAUDE',
            `Conversation ID is null; queuing interaction in pending buffer (key: ${key}, timeout: ${this.newChatTimeoutMs}ms)`
          )
          const timer = setTimeout(() => {
            const pending = this.pendingUnboundInteractions.get(key)
            if (pending) {
              this.pendingUnboundInteractions.delete(key)
              void this.persistInteraction(pending.interaction, key)
            }
          }, this.newChatTimeoutMs)
          this.pendingUnboundInteractions.set(key, { interaction, key, timer })
        }
        continue
      }

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

  /**
   * Dispatches an interaction to the service worker for persistence.
   */
  private async persistInteraction(
    interaction: ExtractedInteraction,
    key: string
  ): Promise<'saved' | 'duplicate' | 'failed'> {
    const input: CreateInteractionInput = {
      platform: 'claude',
      conversation_id: interaction.conversationId,
      message_id: interaction.messageId,
      user_message_id: interaction.userMessageId,
      observed_at: interaction.observedAt,
      source_timestamp: interaction.sourceTimestamp,
      capture_context: interaction.captureContext,
      model: interaction.model,
      query: { text: interaction.queryText },
      response: { text: interaction.responseText },
      conversation_title: interaction.conversationTitle,
    }

    logger.debug(
      'Messaging',
      'CLAUDE',
      `Dispatching DB_SAVE_INTERACTION (conversationId: ${interaction.conversationId ?? 'null'}, captureContext: ${interaction.captureContext}, queryChars: ${interaction.queryText.length}, responseChars: ${interaction.responseText.length})`
    )

    try {
      const msg = createDbSaveInteractionMessage('content-script', input)
      const response = await sendExtensionMessage(msg)

      if (response.success) {
        this.processedKeys.add(key)
        diagnosticStats.increment('interactionsSaved')
        logger.info(
          'Messaging',
          'CLAUDE',
          `DB_SAVE_INTERACTION acknowledged successfully (conversationId: ${interaction.conversationId || 'unbound'}, context: ${interaction.captureContext})`
        )
        return 'saved'
      } else {
        if (response.error && response.error.includes('already exists')) {
          this.processedKeys.add(key)
          diagnosticStats.increment('duplicates')
          logger.info('Database', 'CLAUDE', `Duplicate interaction detected: ${response.error}`)
          return 'duplicate'
        } else {
          diagnosticStats.increment('persistenceFailures')
          logger.error(
            'Messaging',
            'CLAUDE',
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
          'CLAUDE',
          'Extension context invalidated! The extension runtime was reloaded or updated while the page remained open.'
        )
      } else {
        logger.error(
          'Messaging',
          'CLAUDE',
          `Failed to dispatch DB_SAVE_INTERACTION to service worker: ${errMsg}`
        )
      }
      return 'failed'
    }
  }
}
