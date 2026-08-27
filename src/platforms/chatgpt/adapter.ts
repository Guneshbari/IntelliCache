/**
 * ChatGPT Data Collection Adapter
 *
 * Implements event-driven DOM observation and extraction for ChatGPT web sessions.
 * Manages streaming response stability, deduplication, and persistence through
 * the existing Step 2 service-worker messaging layer.
 */

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

    console.log('[IntelliCache ChatGPT] Adapter started and observing conversation DOM.')
  }

  /**
   * Stops DOM observation and releases resources.
   */
  stop(): void {
    if (!this.observing) {
      return
    }

    if (this.observer) {
      this.observer.disconnect()
      this.observer = null
    }

    if (this.debounceTimer) {
      clearTimeout(this.debounceTimer)
      this.debounceTimer = null
    }

    // Cancel all pending timers cleanly
    for (const [, pending] of this.pendingUnboundInteractions) {
      clearTimeout(pending.timer)
    }
    this.pendingUnboundInteractions.clear()

    this.observing = false
    console.log('[IntelliCache ChatGPT] Adapter stopped.')
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
   *
   * capture_context logic:
   *
   * isInitialScan = true  →  next processConversation() assigns "on_load"
   * isInitialScan = false →  next processConversation() assigns "on_generate"
   *
   * URL transition rules:
   * - /        → /c/{id}  : New chat assignment. The user just generated this interaction.
   *                         Do NOT reset isInitialScan — it must remain false so the
   *                         interaction is classified as "on_generate".
   * - /c/A     → /c/B     : True SPA navigation to a different conversation.
   *                         Reset isInitialScan = true → "on_load".
   * - /c/A     → /        : User navigated back to new-chat page.
   *                         Reset isInitialScan = true (nothing meaningful to extract yet).
   * - any      → same     : Normal mutations, no URL change, no reset.
   */
  private handleDomMutation(): void {
    if (!this.observing) {
      return
    }

    // Check for SPA URL changes
    const currentUrl = window.location.href
    if (currentUrl !== this.lastObservedUrl) {
      const previousUrl = this.lastObservedUrl
      this.lastObservedUrl = currentUrl

      const previousConvId = extractConversationIdFromUrl(previousUrl)
      const newConvId = extractConversationIdFromUrl(currentUrl)

      // If new conversation ID appeared, flush pending unbound interactions with it
      if (newConvId && this.pendingUnboundInteractions.size > 0) {
        this.flushPendingWithConversationId(newConvId)
      }

      // Determine whether this URL change represents true SPA navigation between
      // conversations (which should produce on_load for discovered turns) or a
      // new-chat URL assignment (/ → /c/{id}) that must remain on_generate.
      //
      // A new-chat URL assignment is when the previous URL had NO conversation ID
      // and the new URL HAS one. The user just created this conversation; any turns
      // found in the DOM belong to the active generation session, not historical content.
      const isNewChatAssignment = !previousConvId && !!newConvId
      if (!isNewChatAssignment) {
        // True SPA navigation (A→B, A→/, etc.) — treat next scan as historical content.
        this.isInitialScan = true
      }
      // If isNewChatAssignment: preserve the current isInitialScan value (false after
      // the initial 100ms pass) so interactions are classified as on_generate.

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
        console.error('[IntelliCache ChatGPT] Error processing conversation:', err)
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

    // Generation completion guard: If any stop button or active streaming class is present, reschedule
    if (isPageGenerating(document.body || document)) {
      this.scheduleProcessing(this.mutationDebounceMs)
      return
    }

    const currentUrl = window.location.href
    const conversationId = extractConversationIdFromUrl(currentUrl)
    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    // If conversation ID is now present and we have pending unbound items, flush them
    if (conversationId && this.pendingUnboundInteractions.size > 0) {
      this.flushPendingWithConversationId(conversationId)
    }

    const currentCaptureContext: CaptureContext = this.isInitialScan ? 'on_load' : 'on_generate'
    this.isInitialScan = false

    const turns = extractConversationTurns(document.body || document)
    if (turns.length === 0) {
      return
    }

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId,
      title,
      model,
      captureContext: currentCaptureContext,
    })

    for (const interaction of interactions) {
      const key = this.generateInteractionKey(interaction)

      if (this.processedKeys.has(key)) {
        continue
      }

      // If conversation ID is currently null (new chat at /), hold in bounded pending queue
      if (interaction.conversationId === null) {
        if (!this.pendingUnboundInteractions.has(key)) {
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

      await this.persistInteraction(interaction, key)
    }
  }

  /**
   * Dispatches an interaction to the service worker for persistence.
   */
  private async persistInteraction(interaction: ExtractedInteraction, key: string): Promise<void> {
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

    try {
      const msg = createDbSaveInteractionMessage('content-script', input)
      const response = await sendExtensionMessage(msg)

      if (response.success) {
        this.processedKeys.add(key)
        console.log(
          `[IntelliCache ChatGPT] Successfully persisted interaction (Conversation: ${interaction.conversationId || 'unbound'}, Context: ${interaction.captureContext})`
        )
      } else {
        // If duplicate in DB, also mark as processed so we don't keep attempting
        if (response.error && response.error.includes('already exists')) {
          this.processedKeys.add(key)
        } else {
          console.warn(
            '[IntelliCache ChatGPT] Service worker error saving interaction:',
            response.error
          )
        }
      }
    } catch (err) {
      console.error(
        '[IntelliCache ChatGPT] Failed to dispatch save message to service worker:',
        err
      )
    }
  }
}
