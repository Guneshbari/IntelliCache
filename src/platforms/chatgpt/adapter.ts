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
import type { CreateInteractionInput } from '../../shared/types'
import type { ExtractedInteraction, PlatformAdapter } from '../types'
import {
  extractConversationIdFromUrl,
  extractConversationTitle,
  extractConversationTurns,
  extractModelInfo,
  isTurnStreaming,
  pairTurnsIntoInteractions,
} from './parser'

const MUTATION_DEBOUNCE_MS = 500

export class ChatGPTAdapter implements PlatformAdapter {
  public readonly platform = 'chatgpt' as const

  private observing = false
  private observer: MutationObserver | null = null
  private debounceTimer: ReturnType<typeof setTimeout> | null = null
  private lastObservedUrl = ''

  /**
   * Set of processed interaction keys for the current tab session to prevent duplicate work.
   */
  private processedKeys = new Set<string>()

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
   */
  private generateInteractionKey(interaction: ExtractedInteraction): string {
    if (interaction.messageId) {
      return `msg:${interaction.messageId}`
    }

    const conv = interaction.conversationId || 'unbound'
    const qSnippet = interaction.queryText.slice(0, 40)
    const rSnippet = interaction.responseText.slice(0, 40)
    return `pair:${conv}|${qSnippet}|${rSnippet}`
  }

  /**
   * Handles DOM mutation events with debouncing and URL change detection.
   */
  private handleDomMutation(): void {
    if (!this.observing) {
      return
    }

    // Check for SPA URL changes
    const currentUrl = window.location.href
    if (currentUrl !== this.lastObservedUrl) {
      this.lastObservedUrl = currentUrl
      // On conversation switch, trigger extraction for newly active conversation
      this.scheduleProcessing(200)
      return
    }

    // Debounce mutation handling
    this.scheduleProcessing(MUTATION_DEBOUNCE_MS)
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

    const currentUrl = window.location.href
    const conversationId = extractConversationIdFromUrl(currentUrl)
    const title = extractConversationTitle(document)
    const model = extractModelInfo(document)

    // Check if the page as a whole is actively generating / streaming
    if (isTurnStreaming(document.body, document)) {
      // Still generating, reschedule check
      this.scheduleProcessing(MUTATION_DEBOUNCE_MS)
      return
    }

    const turns = extractConversationTurns(document.body)
    if (turns.length === 0) {
      return
    }

    const interactions = pairTurnsIntoInteractions(turns, {
      conversationId,
      title,
      model,
    })

    for (const interaction of interactions) {
      const key = this.generateInteractionKey(interaction)
      if (this.processedKeys.has(key)) {
        continue
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
      observed_at: interaction.observedAt,
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
          `[IntelliCache ChatGPT] Successfully persisted interaction (Conversation: ${interaction.conversationId || 'unbound'})`
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
