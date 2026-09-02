/**
 * Shared type definitions for the IntelliCache Collector extension.
 * Defines message protocols, extension lifecycle states, and platform identifiers.
 */

import type { CreateInteractionInput, Interaction } from '../database/types'

export * from '../database/types'
export * from '../fingerprint/fingerprint'
export * from '../fingerprint/normalize'

/**
 * Known AI platforms targeted for collection adapters.
 */
export type SupportedPlatform = 'chatgpt' | 'claude' | 'gemini' | 'unknown'

/**
 * Origin components within the browser extension architecture.
 */
export type MessageSenderType = 'popup' | 'content-script' | 'service-worker'

/**
 * Base structure for all internal extension messages.
 */
export interface BaseMessage {
  type: string
  sender: MessageSenderType
  timestamp: number
}

/**
 * Message sent to ping a component and verify communication channels.
 */
export interface PingMessage extends BaseMessage {
  type: 'PING'
  payload?: {
    text?: string
  }
}

/**
 * Message sent to query the service worker for extension runtime status.
 */
export interface GetStatusMessage extends BaseMessage {
  type: 'GET_STATUS'
}

/**
 * Message sent by a content script upon injection into a target page.
 */
export interface ContentScriptInitMessage extends BaseMessage {
  type: 'CONTENT_SCRIPT_INITIALIZED'
  payload: {
    url: string
    title: string
  }
}

/**
 * Message sent to retrieve local database statistics (record counts, version).
 */
export interface DbGetStatsMessage extends BaseMessage {
  type: 'DB_GET_STATS'
}

/**
 * Message sent to persist an interaction through the background service worker.
 */
export interface DbSaveInteractionMessage extends BaseMessage {
  type: 'DB_SAVE_INTERACTION'
  payload: CreateInteractionInput
}

/**
 * Message sent to fetch an interaction by ID through the background service worker.
 */
export interface DbGetInteractionMessage extends BaseMessage {
  type: 'DB_GET_INTERACTION'
  payload: {
    id: string
  }
}

/**
 * Development-only message sent to request a full database integrity report.
 * Returns duplicate counts for both conversations and interactions.
 */
export interface DbGetIntegrityReportMessage extends BaseMessage {
  type: 'DB_GET_INTEGRITY_REPORT'
}

/**
 * Discriminated union of all messages supported across the extension architecture.
 */
export type ExtensionMessage =
  | PingMessage
  | GetStatusMessage
  | ContentScriptInitMessage
  | DbGetStatsMessage
  | DbSaveInteractionMessage
  | DbGetInteractionMessage
  | DbGetIntegrityReportMessage

/**
 * Standardized response envelope returned by message handlers.
 */
export interface ExtensionResponse<T = unknown> {
  success: boolean
  data?: T
  error?: string
  timestamp: number
}

/**
 * Payload data returned for status requests.
 */
export interface StatusResponseData {
  extensionName: string
  version: string
  serviceWorkerStatus: 'active'
  manifestVersion: number
  uptimeMs: number
}

/**
 * Payload data returned for ping requests.
 */
export interface PingResponseData {
  reply: string
  echoTimestamp: number
  receivedFrom: MessageSenderType
}

/**
 * Payload data returned for database statistics requests.
 */
export interface DbStatsResponseData {
  dbName: string
  dbVersion: number
  interactionCount: number
  conversationCount: number
  platformCounts?: {
    chatgpt: number
    claude: number
    gemini: number
  }
  recentInteractions?: Interaction[]
}

/**
 * Payload data returned for database integrity report requests.
 */
export interface DbIntegrityReportData {
  conversations: {
    total: number
    unique: number
    duplicates: number
    byPlatform: Record<string, { total: number; unique: number; duplicates: number }>
  }
  interactions: {
    total: number
    uniqueFingerprints: number
    duplicateFingerprints: number
    uniqueIds: number
    duplicateIds: number
    byPlatform: Record<
      string,
      { total: number; uniqueFingerprints: number; duplicateFingerprints: number }
    >
  }
}
