/**
 * BaseAdapter — Abstract base class for platform-specific data collection adapters.
 *
 * Encapsulates the shared lifecycle, deduplication, pending-interaction queue,
 * scheduling, navigation classification, and persistence logic that was previously
 * duplicated verbatim across the ChatGPT, Claude, and Gemini adapters.
 *
 * Concrete subclasses only implement platform-specific DOM extraction.
 */

import { diagnosticStats, logger } from '../diagnostics'
import { NavigationWatcher } from './navigation-watcher'
import {
  createDbSaveInteractionMessage,
  detectPlatformFromUrl,
  sendExtensionMessage,
} from './messages'
import type { CaptureContext, CreateInteractionInput } from './types'
import type { DiagnosticPlatform } from '../diagnostics/types'
import type { ExtractedInteraction, PlatformAdapter } from '../platforms/types'

export const MUTATION_DEBOUNCE_MS = 500
export const NEW_CHAT_URL_TIMEOUT_MS = 4000
export const NAV_POLL_INTERVAL_MS = 250

export type ConversationNavState = 'new_chat_without_id' | 'conversation_with_id' | 'unknown'
export type PersistResult = 'saved' | 'duplicate' | 'failed'

interface PendingUnboundInteraction {
  interaction: ExtractedInteraction
  key: string
  timer: ReturnType<typeof setTimeout>
}

export interface BaseAdapterOptions {
  mutationDebounceMs?: number
  newChatTimeoutMs?: number
  navPollIntervalMs?: number
}

export abstract class BaseAdapter implements PlatformAdapter {
  abstract readonly platform: ExtractedInteraction['platform']

  /** Uppercase platform tag used for logger calls */
  protected abstract readonly platformTag: DiagnosticPlatform

  protected observing = false
  protected observer: MutationObserver | null = null
  protected debounceTimer: ReturnType<typeof setTimeout> | null = null
  protected isInitialScan = true
  protected navState: ConversationNavState = 'unknown'
  protected navWatcher: NavigationWatcher | null = null

  protected mutationDebounceMs: number
  protected newChatTimeoutMs: number
  protected navPollIntervalMs: number

  /** Processed interaction keys for the current tab session to prevent duplicate work. */
  protected processedKeys = new Set<string>()

  /** Pending interactions observed before a conversation ID appears in the URL. */
  protected pendingUnboundInteractions = new Map<string, PendingUnboundInteraction>()

  constructor(options?: BaseAdapterOptions) {
    this.mutationDebounceMs = options?.mutationDebounceMs ?? MUTATION_DEBOUNCE_MS
    this.newChatTimeoutMs = options?.newChatTimeoutMs ?? NEW_CHAT_URL_TIMEOUT_MS
    this.navPollIntervalMs = options?.navPollIntervalMs ?? NAV_POLL_INTERVAL_MS
  }

  abstract canHandle(url: string): boolean
  abstract start(): void
  abstract stop(): void

  isObserving(): boolean {
    return this.observing
  }

  // ─── Shared Utilities ────────────────────────────────────────────────────

  /**
   * Generates a stable in-memory deduplication key for an extracted interaction.
   * Key is independent of conversationId so that null→real-ID transition does not
   * create duplicate in-memory entries or duplicate database records.
   */
  protected generateInteractionKey(interaction: ExtractedInteraction): string {
    if (interaction.messageId) {
      return `msg:${interaction.messageId}`
    }
    const qSnippet = interaction.queryText.slice(0, 60)
    const rSnippet = interaction.responseText.slice(0, 60)
    return `pair:${qSnippet}|${rSnippet}`
  }

  /**
   * Schedules a conversation processing pass with debouncing.
   */
  protected scheduleProcessing(delayMs: number): void {
    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
    }
    this.debounceTimer = setTimeout(() => {
      this.processConversation().catch((err) => {
        diagnosticStats.increment('extractionFailures')
        logger.error(
          'Adapter',
          this.platformTag,
          `Unexpected error processing conversation: ${err instanceof Error ? err.message : String(err)}`
        )
      })
    }, delayMs)
  }

  /**
   * Flushes any pending unbound interactions using the newly acquired conversation ID.
   * Subclasses call this once a real conversation ID appears in the URL.
   */
  protected flushPendingWithConversationId(conversationId: string, title: string | null): void {
    logger.info(
      'Adapter',
      this.platformTag,
      `Flushing ${this.pendingUnboundInteractions.size} pending unbound interaction(s) with conversation ID: ${conversationId}`
    )

    for (const [key, pending] of this.pendingUnboundInteractions) {
      clearTimeout(pending.timer)
      pending.interaction.conversationId = conversationId
      if (title) {
        pending.interaction.conversationTitle = title
      }
      if (pending.interaction.traceId) {
        logger.info(
          'Lifecycle',
          this.platformTag,
          `conversation-bound trace=${pending.interaction.traceId} (convId=${conversationId})`
        )
      }
      this.pendingUnboundInteractions.delete(key)
      void this.persistInteraction(pending.interaction, key)
    }
  }

  /**
   * Handles URL transitions for SPA navigation.
   * Shared by Claude and Gemini adapters. ChatGPT uses its own inline handler.
   *
   * Navigation classification:
   *   new_chat_assignment: /new -> /chat/{id}  — preserve on_generate, flush pending
   *   existing_conversation_navigation: A -> B — reset to on_load, clear session cache
   *   new_to_new: /new -> /new               — no action needed
   */
  handleNavigation(
    prevUrl: string,
    newUrl: string,
    extractConversationId: (url: string) => string | null,
    extractTitle: () => string | null
  ): void {
    if (!this.observing) return

    const prevConvId = extractConversationId(prevUrl)
    const newConvId = extractConversationId(newUrl)
    const prevPathname = safePathname(prevUrl)
    const newPathname = safePathname(newUrl)

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
      this.platformTag,
      `URL transition | from=${prevPathname} | to=${newPathname} | classification=${classification} | conversationId=${newConvId ?? 'none'}`
    )

    if (isNewChatAssignment) {
      this.navState = 'conversation_with_id'
      if (this.pendingUnboundInteractions.size > 0) {
        logger.info(
          'Navigation',
          this.platformTag,
          `New-chat assignment: flushing ${this.pendingUnboundInteractions.size} pending interaction(s) with conversationId=${newConvId}`
        )
        this.flushPendingWithConversationId(newConvId!, extractTitle())
      } else {
        logger.debug(
          'Navigation',
          this.platformTag,
          'New-chat assignment: no pending interactions to flush; scheduling DOM scan.'
        )
      }
    } else if (isConversationToConversation || isNewToNew) {
      this.navState = newConvId ? 'conversation_with_id' : 'new_chat_without_id'
      this.isInitialScan = true
      this.processedKeys.clear()
      logger.info(
        'Navigation',
        this.platformTag,
        `Existing-conversation navigation: resetting scan state to on_load, clearing session key cache (prevConvId=${prevConvId ?? 'none'} -> newConvId=${newConvId ?? 'none'}).`
      )
    } else {
      this.navState = newConvId ? 'conversation_with_id' : 'new_chat_without_id'
      logger.debug(
        'Navigation',
        this.platformTag,
        `URL transition classified as unknown; updating navState=${this.navState}.`
      )
    }

    logger.debug(
      'Navigation',
      this.platformTag,
      'DOM scan scheduled after navigation (delay: 250ms).'
    )
    this.scheduleProcessing(250)
  }

  /**
   * Processes a list of extracted interactions: deduplicates in-memory,
   * queues unbound interactions, and persists new ones.
   * Returns counters for the scan summary log.
   */
  protected async processInteractions(interactions: ExtractedInteraction[]): Promise<{
    queuedCount: number
    savedCount: number
    duplicateCount: number
    failureCount: number
  }> {
    let queuedCount = 0
    let savedCount = 0
    let duplicateCount = 0
    let failureCount = 0

    for (const interaction of interactions) {
      const traceId =
        interaction.traceId || `trace_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
      interaction.traceId = traceId

      logger.info(
        'Lifecycle',
        this.platformTag,
        `candidate-detected trace=${traceId} (convId=${interaction.conversationId ?? 'null'}, queryChars=${interaction.queryText.length}, responseChars=${interaction.responseText.length})`
      )
      logger.info(
        'Lifecycle',
        this.platformTag,
        `extracted trace=${traceId} (convId=${interaction.conversationId ?? 'null'}, userMsgId=${interaction.userMessageId ?? 'null'}, asstMsgId=${interaction.messageId ?? 'null'})`
      )

      logger.logExtraction(this.platformTag, {
        platform: this.platform,
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

      const key = this.generateInteractionKey(interaction)

      if (this.processedKeys.has(key)) {
        logger.debug(
          'Adapter',
          this.platformTag,
          `Skipping interaction (${key}): already processed in this session.`
        )
        duplicateCount++
        continue
      }

      // Queue unbound interactions until a conversation ID is available
      if (interaction.conversationId === null) {
        if (!this.pendingUnboundInteractions.has(key)) {
          queuedCount++
          diagnosticStats.increment('interactionsQueued')
          logger.info(
            'Adapter',
            this.platformTag,
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

      // Promote from pending if already held there
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

    return { queuedCount, savedCount, duplicateCount, failureCount }
  }

  /**
   * Dispatches an interaction to the service worker for persistence.
   */
  protected async persistInteraction(
    interaction: ExtractedInteraction,
    key: string
  ): Promise<PersistResult> {
    const traceId =
      interaction.traceId || `trace_${Date.now()}_${Math.random().toString(36).slice(2, 7)}`
    interaction.traceId = traceId

    const input: CreateInteractionInput = {
      platform: this.platform,
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
      trace_id: traceId,
    }

    logger.info(
      'Lifecycle',
      this.platformTag,
      `persistence-request trace=${traceId} (conversationId=${interaction.conversationId ?? 'null'}, context=${interaction.captureContext})`
    )
    logger.debug(
      'Messaging',
      this.platformTag,
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
          this.platformTag,
          `DB_SAVE_INTERACTION acknowledged successfully (conversationId: ${interaction.conversationId || 'unbound'}, context: ${interaction.captureContext})`
        )
        return 'saved'
      }

      if (response.error?.includes('already exists')) {
        this.processedKeys.add(key)
        diagnosticStats.increment('duplicates')
        logger.info(
          'Database',
          this.platformTag,
          `Duplicate interaction detected: ${response.error}`
        )
        return 'duplicate'
      }

      diagnosticStats.increment('persistenceFailures')
      logger.error(
        'Messaging',
        this.platformTag,
        `Service worker returned error saving interaction: ${response.error ?? 'Unknown error'}`
      )
      return 'failed'
    } catch (err) {
      diagnosticStats.increment('persistenceFailures')
      const errMsg = err instanceof Error ? err.message : String(err)
      if (/extension context invalidated/i.test(errMsg)) {
        logger.error(
          'Messaging',
          this.platformTag,
          'Extension context invalidated! The extension runtime was reloaded or updated while the page remained open.'
        )
      } else {
        logger.error(
          'Messaging',
          this.platformTag,
          `Failed to dispatch DB_SAVE_INTERACTION to service worker: ${errMsg}`
        )
      }
      return 'failed'
    }
  }

  /** Subclasses implement the actual DOM processing pass. */
  abstract processConversation(): Promise<void>

  /** Shared stop helper: tears down observer, navWatcher, timers, and pending queue. */
  protected stopShared(): void {
    if (this.navWatcher) {
      this.navWatcher.stop()
      this.navWatcher = null
      logger.info(
        'Navigation',
        this.platformTag,
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
      this.platformTag,
      `Adapter stopped. Pending buffer cleared: ${pendingCount} items.`
    )
  }

  /** Shared start helper: installs MutationObserver targeting document.body. */
  protected startMutationObserver(onMutation: () => void): void {
    this.observer = new MutationObserver(onMutation)
    const targetNode = document.body || document.documentElement
    if (targetNode) {
      this.observer.observe(targetNode, {
        childList: true,
        subtree: true,
        characterData: true,
      })
    }
  }

  /** Shared start helper: installs NavigationWatcher (popstate + polling). */
  protected startNavWatcher(
    initialUrl: string,
    onUrlChange: (prev: string, next: string) => void
  ): void {
    this.navWatcher = new NavigationWatcher(onUrlChange, { pollIntervalMs: this.navPollIntervalMs })
    this.navWatcher.start(initialUrl)
    logger.info(
      'Navigation',
      this.platformTag,
      `Navigation listener installed (popstate event listener + polling every ${this.navPollIntervalMs}ms).`
    )
  }

  /** Returns the current capture context and advances the isInitialScan flag. */
  protected consumeCaptureContext(): CaptureContext {
    const ctx: CaptureContext = this.isInitialScan ? 'on_load' : 'on_generate'
    this.isInitialScan = false
    return ctx
  }
}

function safePathname(url: string): string {
  try {
    return new URL(url).pathname
  } catch {
    return url
  }
}

// Re-export so adapters can use detectPlatformFromUrl without a separate import
export { detectPlatformFromUrl }
