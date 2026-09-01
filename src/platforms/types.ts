/**
 * Platform Adapter Type Definitions
 *
 * Establishes a lightweight, platform-agnostic abstraction for observing and
 * extracting AI query/response interactions from target web pages.
 */

import type { CaptureContext, SupportedPlatform } from '../shared/types'

/**
 * Raw conversation turn extracted from the page DOM.
 */
export interface RawMessageTurn {
  role: 'user' | 'assistant'
  element: Element
  text: string
  messageId: string | null
  sourceTimestamp: string | null
  isStreaming: boolean
}

/**
 * Normalized interaction extracted from a platform's DOM before persistence.
 */
export interface ExtractedInteraction {
  platform: SupportedPlatform
  conversationId: string | null
  messageId: string | null // Assistant message ID
  userMessageId: string | null // User message ID
  model: {
    provider: string | null
    name: string | null
  }
  queryText: string
  responseText: string
  conversationTitle: string | null
  observedAt: string // ISO-8601 string: timestamp when interaction was observed by extension
  sourceTimestamp: string | null // Original timestamp if exposed by platform DOM, else null
  captureContext: CaptureContext // 'on_load' | 'on_generate'
  traceId?: string // Temporary diagnostic lifecycle trace ID (not persisted to DB)
}

/**
 * Interface that all platform-specific collectors must implement.
 */
export interface PlatformAdapter {
  /**
   * Platform identifier.
   */
  readonly platform: SupportedPlatform

  /**
   * Determines if this adapter can handle the given URL.
   */
  canHandle(url: string): boolean

  /**
   * Starts DOM observation and data collection.
   */
  start(): void

  /**
   * Stops DOM observation and cleans up all listeners and observers.
   */
  stop(): void

  /**
   * Returns true if the adapter is currently active and observing the DOM.
   */
  isObserving(): boolean
}
