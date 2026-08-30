/**
 * Diagnostic Logging & Instrumentation Types
 *
 * Defines components, log levels, platforms, counters, and metadata models
 * for observing the data collection lifecycle across all platforms.
 */

export type DiagnosticLogLevel = 'debug' | 'info' | 'warn' | 'error'

export type DiagnosticComponent =
  | 'Content'
  | 'Adapter'
  | 'Parser'
  | 'Extraction'
  | 'Messaging'
  | 'Background'
  | 'Database'
  | 'Navigation'
  | 'Lifecycle'
  | 'UI'

export type DiagnosticPlatform = 'CHATGPT' | 'CLAUDE' | 'GEMINI' | 'CORE'

/**
 * In-memory diagnostic counters for the active testing session.
 */
export interface DiagnosticCounters {
  domScans: number
  userTurnsFound: number
  assistantTurnsFound: number
  completePairs: number
  interactionsExtracted: number
  interactionsQueued: number
  interactionsSaved: number
  duplicates: number
  streamingDeferrals: number
  missingConversationIds: number
  extractionFailures: number
  persistenceFailures: number
}

/**
 * Data model for single-line scan summaries emitted after DOM processing passes.
 */
export interface ScanSummaryData {
  platform: DiagnosticPlatform
  conversationId: boolean | string
  turnContainers: number
  userTurns: number
  assistantTurns: number
  completePairs: number
  generating: boolean
  extracted: number
  queued: number
  saved: number
  duplicates: number
  failures: number
}

/**
 * Safe metadata model for logging extracted interactions without exposing query/response text.
 */
export interface ExtractedInteractionMetadata {
  platform: string
  conversationId: string | null
  userMessageId: string | null
  messageId: string | null
  queryCharCount: number
  queryByteCount?: number | null
  responseCharCount: number
  responseByteCount?: number | null
  modelProvider: string | null
  modelName: string | null
  captureContext: string
  sourceTimestamp: string | null
}
