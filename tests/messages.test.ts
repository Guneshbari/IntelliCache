import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  createContentScriptInitMessage,
  createErrorResponse,
  createGetStatusMessage,
  createPingMessage,
  createSuccessResponse,
  detectPlatformFromUrl,
  isExtensionMessage,
  sendExtensionMessage,
} from '../src/shared/messages'
import type { ExtensionMessage } from '../src/shared/types'

describe('Message Factory Functions', () => {
  it('creates a valid PING message with timestamp and sender', () => {
    const before = Date.now()
    const msg = createPingMessage('popup', 'test ping')
    const after = Date.now()

    expect(msg.type).toBe('PING')
    expect(msg.sender).toBe('popup')
    expect(msg.payload?.text).toBe('test ping')
    expect(msg.timestamp).toBeGreaterThanOrEqual(before)
    expect(msg.timestamp).toBeLessThanOrEqual(after)
  })

  it('creates a valid GET_STATUS message', () => {
    const msg = createGetStatusMessage('service-worker')

    expect(msg.type).toBe('GET_STATUS')
    expect(msg.sender).toBe('service-worker')
    expect(typeof msg.timestamp).toBe('number')
  })

  it('creates a valid CONTENT_SCRIPT_INITIALIZED message', () => {
    const msg = createContentScriptInitMessage('https://chatgpt.com/c/test', 'ChatGPT Conversation')

    expect(msg.type).toBe('CONTENT_SCRIPT_INITIALIZED')
    expect(msg.sender).toBe('content-script')
    expect(msg.payload.url).toBe('https://chatgpt.com/c/test')
    expect(msg.payload.title).toBe('ChatGPT Conversation')
  })
})

describe('Response Envelope Helpers', () => {
  it('creates a standard success response envelope', () => {
    const payload = { version: '0.1.0', active: true }
    const response = createSuccessResponse(payload)

    expect(response.success).toBe(true)
    expect(response.data).toEqual(payload)
    expect(response.error).toBeUndefined()
    expect(typeof response.timestamp).toBe('number')
  })

  it('creates a standard error response envelope', () => {
    const errorMsg = 'Connection timed out'
    const response = createErrorResponse(errorMsg)

    expect(response.success).toBe(false)
    expect(response.error).toBe(errorMsg)
    expect(response.data).toBeUndefined()
    expect(typeof response.timestamp).toBe('number')
  })
})

describe('Type Guard: isExtensionMessage', () => {
  it('returns true for valid messages', () => {
    const ping: ExtensionMessage = {
      type: 'PING',
      sender: 'popup',
      timestamp: Date.now(),
    }
    const status: ExtensionMessage = {
      type: 'GET_STATUS',
      sender: 'content-script',
      timestamp: Date.now(),
    }
    const init: ExtensionMessage = {
      type: 'CONTENT_SCRIPT_INITIALIZED',
      sender: 'content-script',
      timestamp: Date.now(),
      payload: { url: 'https://claude.ai', title: 'Claude' },
    }

    expect(isExtensionMessage(ping)).toBe(true)
    expect(isExtensionMessage(status)).toBe(true)
    expect(isExtensionMessage(init)).toBe(true)
  })

  it('returns false for non-object or malformed inputs', () => {
    expect(isExtensionMessage(null)).toBe(false)
    expect(isExtensionMessage(undefined)).toBe(false)
    expect(isExtensionMessage('PING')).toBe(false)
    expect(isExtensionMessage(123)).toBe(false)
    expect(isExtensionMessage({})).toBe(false)
    expect(isExtensionMessage({ type: 'UNKNOWN', sender: 'popup', timestamp: 123 })).toBe(false)
    expect(isExtensionMessage({ type: 'PING', sender: 'invalid-sender', timestamp: 123 })).toBe(
      false
    )
    expect(
      isExtensionMessage({ type: 'PING', sender: 'popup', timestamp: 'invalid-timestamp' })
    ).toBe(false)
  })
})

describe('Platform URL Detection', () => {
  it('identifies ChatGPT domains correctly', () => {
    expect(detectPlatformFromUrl('https://chatgpt.com/')).toBe('chatgpt')
    expect(detectPlatformFromUrl('https://chatgpt.com/c/1234-5678')).toBe('chatgpt')
    expect(detectPlatformFromUrl('https://chat.openai.com/')).toBe('chatgpt')
    expect(detectPlatformFromUrl('https://chat.openai.com/g/g-abc123-custom-gpt')).toBe('chatgpt')
  })

  it('identifies Claude domain correctly', () => {
    expect(detectPlatformFromUrl('https://claude.ai/')).toBe('claude')
    expect(
      detectPlatformFromUrl('https://claude.ai/chat/550e8400-e29b-41d4-a716-446655440000')
    ).toBe('claude')
  })

  it('identifies Gemini domain correctly', () => {
    expect(detectPlatformFromUrl('https://gemini.google.com/')).toBe('gemini')
    expect(detectPlatformFromUrl('https://gemini.google.com/app/12345')).toBe('gemini')
  })

  it('returns "unknown" for non-supported domains or malformed URLs', () => {
    expect(detectPlatformFromUrl('https://example.com/')).toBe('unknown')
    expect(detectPlatformFromUrl('https://google.com/search?q=ai')).toBe('unknown')
    expect(detectPlatformFromUrl('invalid-url-string')).toBe('unknown')
    expect(detectPlatformFromUrl('')).toBe('unknown')
  })
})

describe('sendExtensionMessage Runtime Dispatcher', () => {
  const originalChrome = globalThis.chrome

  interface MockRuntime {
    sendMessage: (message: unknown, callback: (response: unknown) => void) => void
    lastError?: { message?: string }
  }

  let mockRuntime: MockRuntime

  beforeEach(() => {
    mockRuntime = {
      sendMessage: vi.fn(),
      lastError: undefined,
    }
    globalThis.chrome = {
      runtime: mockRuntime,
    } as unknown as typeof chrome
  })

  afterEach(() => {
    globalThis.chrome = originalChrome
  })

  it('returns error response when chrome.runtime.lastError is present', async () => {
    mockRuntime.sendMessage = vi.fn((_msg, callback) => {
      mockRuntime.lastError = {
        message: 'Could not establish connection. Receiving end does not exist.',
      }
      callback(undefined)
    })

    const ping = createPingMessage('popup')
    const res = await sendExtensionMessage(ping)

    expect(res.success).toBe(false)
    expect(res.error).toBe('Could not establish connection. Receiving end does not exist.')
    expect(typeof res.timestamp).toBe('number')
  })

  it('returns error response when response is undefined without lastError', async () => {
    mockRuntime.sendMessage = vi.fn((_msg, callback) => {
      mockRuntime.lastError = undefined
      callback(undefined)
    })

    const ping = createPingMessage('popup')
    const res = await sendExtensionMessage(ping)

    expect(res.success).toBe(false)
    expect(res.error).toBe('No response received from extension component')
    expect(typeof res.timestamp).toBe('number')
  })

  it('returns error response when response is explicitly null without lastError', async () => {
    mockRuntime.sendMessage = vi.fn((_msg, callback) => {
      mockRuntime.lastError = undefined
      callback(null)
    })

    const ping = createPingMessage('popup')
    const res = await sendExtensionMessage(ping)

    expect(res.success).toBe(false)
    expect(res.error).toBe('No response received from extension component')
    expect(typeof res.timestamp).toBe('number')
  })

  it('returns valid response unchanged when communication succeeds', async () => {
    const expectedResponse = createSuccessResponse({ reply: 'PONG' })
    mockRuntime.sendMessage = vi.fn((_msg, callback) => {
      mockRuntime.lastError = undefined
      callback(expectedResponse)
    })

    const ping = createPingMessage('popup')
    const res = await sendExtensionMessage(ping)

    expect(res.success).toBe(true)
    expect(res).toEqual(expectedResponse)
  })

  it('throws an error if chrome runtime is not available in environment', async () => {
    // @ts-expect-error - intentionally testing environment without chrome
    delete globalThis.chrome

    const ping = createPingMessage('popup')
    await expect(sendExtensionMessage(ping)).rejects.toThrow(
      'Chrome extension runtime API is not available in the current environment.'
    )
  })
})
