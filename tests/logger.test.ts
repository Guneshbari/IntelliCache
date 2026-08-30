import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  DiagnosticLogger,
  toDiagnosticPlatform,
  type DiagnosticComponent,
  type DiagnosticLogLevel,
  type DiagnosticPlatform,
  type ExtractedInteractionMetadata,
  type ScanSummaryData,
} from '../src/diagnostics'

describe('Diagnostic Logger Unit & Privacy Tests', () => {
  let logger: DiagnosticLogger
  let capturedLogs: { level: DiagnosticLogLevel; message: string; extra?: unknown }[]

  beforeEach(() => {
    logger = new DiagnosticLogger()
    capturedLogs = []
    logger.setSink((level, message, extra) => {
      capturedLogs.push({ level, message, extra })
    })
  })

  afterEach(() => {
    logger.setSink(null)
  })

  it('normalizes platform names correctly to uppercase tags', () => {
    expect(toDiagnosticPlatform('chatgpt')).toBe('CHATGPT')
    expect(toDiagnosticPlatform('ChatGPT.com')).toBe('CHATGPT')
    expect(toDiagnosticPlatform('openai')).toBe('CHATGPT')
    expect(toDiagnosticPlatform('claude')).toBe('CLAUDE')
    expect(toDiagnosticPlatform('claude.ai')).toBe('CLAUDE')
    expect(toDiagnosticPlatform('gemini')).toBe('GEMINI')
    expect(toDiagnosticPlatform('gemini.google.com')).toBe('GEMINI')
    expect(toDiagnosticPlatform('unknown')).toBe('CORE')
    expect(toDiagnosticPlatform('')).toBe('CORE')
    expect(toDiagnosticPlatform(null)).toBe('CORE')
    expect(toDiagnosticPlatform(undefined)).toBe('CORE')
  })

  it('formats standardized log prefix [IntelliCache][<Component>][<Platform>]', () => {
    const prefix = logger.formatPrefix('Adapter', 'CHATGPT')
    expect(prefix).toBe('[IntelliCache][Adapter][CHATGPT]')

    const fullMessage = logger.formatMessage('Parser', 'CLAUDE', 'Turn extraction started')
    expect(fullMessage).toBe('[IntelliCache][Parser][CLAUDE] Turn extraction started')
  })

  it('logs across all supported components and platforms', () => {
    const components: DiagnosticComponent[] = [
      'Content',
      'Adapter',
      'Parser',
      'Extraction',
      'Messaging',
      'Background',
      'Database',
    ]
    const platforms: DiagnosticPlatform[] = ['CHATGPT', 'CLAUDE', 'GEMINI', 'CORE']

    for (const comp of components) {
      for (const plat of platforms) {
        logger.info(comp, plat, `Test message for ${comp} on ${plat}`)
      }
    }

    expect(capturedLogs.length).toBe(components.length * platforms.length)
    expect(capturedLogs[0].message).toBe(
      '[IntelliCache][Content][CHATGPT] Test message for Content on CHATGPT'
    )
  })

  it('respects log level severity filtering', () => {
    logger.setLevel('warn')

    logger.debug('Adapter', 'GEMINI', 'Debug message')
    logger.info('Adapter', 'GEMINI', 'Info message')
    logger.warn('Adapter', 'GEMINI', 'Warn message')
    logger.error('Adapter', 'GEMINI', 'Error message')

    expect(capturedLogs.length).toBe(2)
    expect(capturedLogs[0].level).toBe('warn')
    expect(capturedLogs[0].message).toBe('[IntelliCache][Adapter][GEMINI] Warn message')
    expect(capturedLogs[1].level).toBe('error')
    expect(capturedLogs[1].message).toBe('[IntelliCache][Adapter][GEMINI] Error message')

    logger.setLevel('error')
    logger.warn('Adapter', 'GEMINI', 'Another warn')
    logger.error('Adapter', 'GEMINI', 'Fatal error')

    expect(capturedLogs.length).toBe(3)
    expect(capturedLogs[2].level).toBe('error')
    expect(capturedLogs[2].message).toBe('[IntelliCache][Adapter][GEMINI] Fatal error')
  })

  it('emits scan summary in exact specified format', () => {
    const summaryData: ScanSummaryData = {
      platform: 'GEMINI',
      conversationId: true,
      turnContainers: 4,
      userTurns: 2,
      assistantTurns: 2,
      completePairs: 2,
      generating: false,
      extracted: 2,
      queued: 0,
      saved: 2,
      duplicates: 0,
      failures: 0,
    }

    logger.logScanSummary(summaryData)

    expect(capturedLogs.length).toBe(1)
    expect(capturedLogs[0].level).toBe('info')
    expect(capturedLogs[0].message).toBe(
      '[IntelliCache][Adapter][GEMINI] SCAN SUMMARY | conversationId=yes | turnContainers=4 | userTurns=2 | assistantTurns=2 | completePairs=2 | generating=false | extracted=2 | queued=0 | saved=2 | duplicates=0 | failures=0'
    )
  })

  it('correctly formats conversationId=no when conversationId is false or null in scan summary', () => {
    const summaryData: ScanSummaryData = {
      platform: 'CLAUDE',
      conversationId: false,
      turnContainers: 2,
      userTurns: 1,
      assistantTurns: 1,
      completePairs: 1,
      generating: false,
      extracted: 1,
      queued: 1,
      saved: 0,
      duplicates: 0,
      failures: 0,
    }

    logger.logScanSummary(summaryData)

    expect(capturedLogs[0].message).toContain('conversationId=no')
    expect(capturedLogs[0].message).toContain('queued=1')
  })

  describe('Privacy & Sensitive Content Protection', () => {
    it('never logs actual user query text or assistant response text during extraction logging', () => {
      const sensitivePrompt = 'My secret password is P@ssw0rd123! and my SSN is 000-11-2222'
      const sensitiveResponse =
        'Here is the confidential internal server token: eyJhbGciOiJIUzI1Ni...'

      const meta: ExtractedInteractionMetadata = {
        platform: 'chatgpt',
        conversationId: 'c-12345',
        userMessageId: 'user-turn-0',
        messageId: 'asst-turn-1',
        queryCharCount: sensitivePrompt.length,
        queryByteCount: new TextEncoder().encode(sensitivePrompt).length,
        responseCharCount: sensitiveResponse.length,
        responseByteCount: new TextEncoder().encode(sensitiveResponse).length,
        modelProvider: 'openai',
        modelName: 'gpt-4o',
        captureContext: 'on_generate',
        sourceTimestamp: '2026-08-30T00:00:00.000Z',
      }

      logger.logExtraction('CHATGPT', meta)

      expect(capturedLogs.length).toBe(1)
      const logEntry = capturedLogs[0].message

      // Must NOT contain any part of the sensitive text
      expect(logEntry).not.toContain(sensitivePrompt)
      expect(logEntry).not.toContain('P@ssw0rd123!')
      expect(logEntry).not.toContain('000-11-2222')
      expect(logEntry).not.toContain(sensitiveResponse)
      expect(logEntry).not.toContain('eyJhbGciOiJIUzI1Ni')

      // MUST contain safe metadata
      expect(logEntry).toContain(`queryChars=${sensitivePrompt.length}`)
      expect(logEntry).toContain(`responseChars=${sensitiveResponse.length}`)
      expect(logEntry).toContain('conversationId=c-12345')
      expect(logEntry).toContain('model=openai:gpt-4o')
      expect(logEntry).toContain('captureContext=on_generate')
    })

    it('falls back to console methods when no custom sink is provided', () => {
      const consoleDebugSpy = vi.spyOn(console, 'debug').mockImplementation(() => {})
      const consoleInfoSpy = vi.spyOn(console, 'info').mockImplementation(() => {})
      const consoleWarnSpy = vi.spyOn(console, 'warn').mockImplementation(() => {})
      const consoleErrorSpy = vi.spyOn(console, 'error').mockImplementation(() => {})

      const standaloneLogger = new DiagnosticLogger()
      standaloneLogger.debug('Content', 'CORE', 'Debug test')
      standaloneLogger.info('Content', 'CORE', 'Info test')
      standaloneLogger.warn('Content', 'CORE', 'Warn test')
      standaloneLogger.error('Content', 'CORE', 'Error test')

      expect(consoleDebugSpy).toHaveBeenCalledWith('[IntelliCache][Content][CORE] Debug test')
      expect(consoleInfoSpy).toHaveBeenCalledWith('[IntelliCache][Content][CORE] Info test')
      expect(consoleWarnSpy).toHaveBeenCalledWith('[IntelliCache][Content][CORE] Warn test')
      expect(consoleErrorSpy).toHaveBeenCalledWith('[IntelliCache][Content][CORE] Error test')

      const sampleErr = new Error('Sample failure')
      standaloneLogger.error('Database', 'CORE', 'Failed db op', sampleErr)
      expect(consoleErrorSpy).toHaveBeenCalledWith(
        '[IntelliCache][Database][CORE] Failed db op',
        sampleErr
      )

      consoleDebugSpy.mockRestore()
      consoleInfoSpy.mockRestore()
      consoleWarnSpy.mockRestore()
      consoleErrorSpy.mockRestore()
    })
  })
})
