/**
 * Centralized Diagnostic Logger
 *
 * Provides structured, privacy-preserving logging for the IntelliCache
 * data collection pipeline across all components and platforms.
 *
 * Format: [IntelliCache][<component>][<platform>] <message>
 *
 * Privacy Guarantees:
 * - Never outputs raw prompt or response text.
 * - Outputs text lengths, IDs, counts, booleans, and timing metadata only.
 * - Never logs credentials, session headers, or authentication tokens.
 */

import type {
  DiagnosticComponent,
  DiagnosticLogLevel,
  DiagnosticPlatform,
  ExtractedInteractionMetadata,
  ScanSummaryData,
} from './types'

const LOG_LEVEL_SEVERITY: Record<DiagnosticLogLevel, number> = {
  debug: 0,
  info: 1,
  warn: 2,
  error: 3,
}

/**
 * Normalizes any platform string into a standardized DiagnosticPlatform tag.
 */
export function toDiagnosticPlatform(platform?: string | null): DiagnosticPlatform {
  if (!platform) {
    return 'CORE'
  }
  const norm = platform.trim().toLowerCase()
  if (norm.includes('chatgpt') || norm.includes('openai')) {
    return 'CHATGPT'
  }
  if (norm.includes('claude')) {
    return 'CLAUDE'
  }
  if (norm.includes('gemini')) {
    return 'GEMINI'
  }
  return 'CORE'
}

export type DiagnosticLogSink = (
  level: DiagnosticLogLevel,
  formattedMessage: string,
  extra?: unknown
) => void

export class DiagnosticLogger {
  private level: DiagnosticLogLevel = 'debug'
  private customSink: DiagnosticLogSink | null = null

  /**
   * Sets minimum logging level ('debug' | 'info' | 'warn' | 'error').
   */
  setLevel(level: DiagnosticLogLevel): void {
    this.level = level
  }

  /**
   * Retrieves the current logging level.
   */
  getLevel(): DiagnosticLogLevel {
    return this.level
  }

  /**
   * Overrides console output with a custom sink (e.g. for unit testing).
   */
  setSink(sink: DiagnosticLogSink | null): void {
    this.customSink = sink
  }

  /**
   * Formats the standardized log prefix: `[IntelliCache][<Component>][<Platform>]`.
   */
  formatPrefix(component: DiagnosticComponent, platform: DiagnosticPlatform): string {
    return `[IntelliCache][${component}][${platform}]`
  }

  /**
   * Formats the complete message string.
   */
  formatMessage(
    component: DiagnosticComponent,
    platform: DiagnosticPlatform,
    message: string
  ): string {
    return `${this.formatPrefix(component, platform)} ${message}`
  }

  /**
   * Internal dispatcher for logging with severity filtering and formatting.
   */
  private emit(
    level: DiagnosticLogLevel,
    component: DiagnosticComponent,
    rawPlatform: DiagnosticPlatform | string,
    message: string,
    extra?: unknown
  ): void {
    if (LOG_LEVEL_SEVERITY[level] < LOG_LEVEL_SEVERITY[this.level]) {
      return
    }

    const platform =
      typeof rawPlatform === 'string' &&
      (rawPlatform === 'CHATGPT' ||
        rawPlatform === 'CLAUDE' ||
        rawPlatform === 'GEMINI' ||
        rawPlatform === 'CORE')
        ? (rawPlatform as DiagnosticPlatform)
        : toDiagnosticPlatform(rawPlatform)

    const formatted = this.formatMessage(component, platform, message)

    if (this.customSink) {
      this.customSink(level, formatted, extra)
      return
    }

    switch (level) {
      case 'debug':
        if (extra !== undefined) {
          console.debug(formatted, extra)
        } else {
          console.debug(formatted)
        }
        break
      case 'info':
        if (extra !== undefined) {
          console.info(formatted, extra)
        } else {
          console.info(formatted)
        }
        break
      case 'warn':
        if (extra !== undefined) {
          console.warn(formatted, extra)
        } else {
          console.warn(formatted)
        }
        break
      case 'error':
        if (extra !== undefined) {
          console.error(formatted, extra)
        } else {
          console.error(formatted)
        }
        break
    }
  }

  debug(
    component: DiagnosticComponent,
    platform: DiagnosticPlatform | string,
    message: string,
    extra?: unknown
  ): void {
    this.emit('debug', component, platform, message, extra)
  }

  info(
    component: DiagnosticComponent,
    platform: DiagnosticPlatform | string,
    message: string,
    extra?: unknown
  ): void {
    this.emit('info', component, platform, message, extra)
  }

  warn(
    component: DiagnosticComponent,
    platform: DiagnosticPlatform | string,
    message: string,
    extra?: unknown
  ): void {
    this.emit('warn', component, platform, message, extra)
  }

  error(
    component: DiagnosticComponent,
    platform: DiagnosticPlatform | string,
    message: string,
    error?: unknown
  ): void {
    this.emit('error', component, platform, message, error)
  }

  /**
   * Logs safe metadata of an extracted interaction turn pair without exposing query/response text.
   */
  logExtraction(
    platform: DiagnosticPlatform | string,
    metadata: ExtractedInteractionMetadata
  ): void {
    const p = toDiagnosticPlatform(platform)
    const convId = metadata.conversationId ? metadata.conversationId : 'null'
    const userMsgId = metadata.userMessageId ? metadata.userMessageId : 'null'
    const asstMsgId = metadata.messageId ? metadata.messageId : 'null'
    const model = `${metadata.modelProvider ?? 'null'}:${metadata.modelName ?? 'null'}`
    const sourceTs = metadata.sourceTimestamp ?? 'null'

    const queryMetrics =
      metadata.queryByteCount !== undefined && metadata.queryByteCount !== null
        ? `queryChars=${metadata.queryCharCount} (bytes=${metadata.queryByteCount})`
        : `queryChars=${metadata.queryCharCount}`

    const responseMetrics =
      metadata.responseByteCount !== undefined && metadata.responseByteCount !== null
        ? `responseChars=${metadata.responseCharCount} (bytes=${metadata.responseByteCount})`
        : `responseChars=${metadata.responseCharCount}`

    const msg =
      `Extracted interaction turn | conversationId=${convId} | userMsgId=${userMsgId} | asstMsgId=${asstMsgId} ` +
      `| ${queryMetrics} | ${responseMetrics} | model=${model} | captureContext=${metadata.captureContext} | sourceTimestamp=${sourceTs}`

    this.emit('info', 'Extraction', p, msg)
  }

  /**
   * Emits a single-line standardized scan summary at the end of a DOM processing pass.
   *
   * Example:
   * [IntelliCache][Adapter][GEMINI] SCAN SUMMARY | conversationId=yes | turnContainers=4 | userTurns=2 | assistantTurns=2 | completePairs=2 | generating=false | extracted=2 | queued=0 | saved=2 | duplicates=0 | failures=0
   */
  logScanSummary(data: ScanSummaryData): void {
    const p = toDiagnosticPlatform(data.platform)
    let convIdStr = 'no'
    if (typeof data.conversationId === 'boolean') {
      convIdStr = data.conversationId ? 'yes' : 'no'
    } else if (typeof data.conversationId === 'string') {
      const lower = data.conversationId.trim().toLowerCase()
      convIdStr =
        lower === 'yes' || (lower !== 'no' && lower !== 'null' && lower !== '') ? 'yes' : 'no'
    }

    const msg =
      `SCAN SUMMARY | conversationId=${convIdStr} | turnContainers=${data.turnContainers} ` +
      `| userTurns=${data.userTurns} | assistantTurns=${data.assistantTurns} | completePairs=${data.completePairs} ` +
      `| generating=${data.generating} | extracted=${data.extracted} | queued=${data.queued} | saved=${data.saved} ` +
      `| duplicates=${data.duplicates} | failures=${data.failures}`

    this.emit('info', 'Adapter', p, msg)
  }
}

export const logger = new DiagnosticLogger()
