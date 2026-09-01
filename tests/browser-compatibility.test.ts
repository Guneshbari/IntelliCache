// @vitest-environment happy-dom
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import {
  addRuntimeMessageListener,
  detectBrowserFamily,
  getBrowserRuntime,
  isChromium,
  isFirefox,
  onRuntimeInstalled,
  sendBrowserRuntimeMessage,
} from '../src/shared/browser'
import { createPingMessage, createSuccessResponse } from '../src/shared/messages'
import type { ExtensionResponse, PingResponseData } from '../src/shared/types'

describe('Cross-Browser Compatibility Layer (Chromium & Firefox)', () => {
  afterEach(() => {
    // Restore globalThis and navigator
    const g = globalThis as Record<string, unknown>
    delete g.browser
    delete g.chrome
    vi.restoreAllMocks()
  })

  // ─── RUNTIME & BROWSER DETECTION ─────────────────────────────────────────

  describe('Browser Family Detection', () => {
    it('detects Firefox when userAgent contains Firefox', () => {
      Object.defineProperty(globalThis, 'navigator', {
        value: {
          userAgent: 'Mozilla/5.0 (X11; Linux x86_64; rv:130.0) Gecko/20100101 Firefox/130.0',
        },
        configurable: true,
      })

      expect(detectBrowserFamily()).toBe('firefox')
      expect(isFirefox()).toBe(true)
      expect(isChromium()).toBe(false)
    })

    it('detects Firefox when browser.runtime.getBrowserInfo is present', () => {
      Object.defineProperty(globalThis, 'navigator', {
        value: { userAgent: 'Mozilla/5.0' },
        configurable: true,
      })
      const g = globalThis as Record<string, unknown>
      g.browser = {
        runtime: {
          getBrowserInfo: vi.fn().mockResolvedValue({ name: 'Firefox', version: '130.0' }),
        },
      }

      expect(detectBrowserFamily()).toBe('firefox')
      expect(isFirefox()).toBe(true)
      expect(isChromium()).toBe(false)
    })

    it('detects Chromium when userAgent contains Chrome / Chromium / Edg / Brave', () => {
      Object.defineProperty(globalThis, 'navigator', {
        value: {
          userAgent:
            'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36',
        },
        configurable: true,
      })

      expect(detectBrowserFamily()).toBe('chromium')
      expect(isChromium()).toBe(true)
      expect(isFirefox()).toBe(false)
    })

    it('resolves getBrowserRuntime from global browser object', () => {
      const mockRuntime = { sendMessage: vi.fn(), onMessage: { addListener: vi.fn() } }
      const g = globalThis as Record<string, unknown>
      g.browser = { runtime: mockRuntime }

      expect(getBrowserRuntime()).toBe(mockRuntime)
    })

    it('resolves getBrowserRuntime from global chrome object when browser is absent', () => {
      const mockRuntime = { sendMessage: vi.fn(), onMessage: { addListener: vi.fn() } }
      const g = globalThis as Record<string, unknown>
      delete g.browser
      g.chrome = { runtime: mockRuntime }

      expect(getBrowserRuntime()).toBe(mockRuntime)
    })
  })

  // ─── MESSAGE DISPATCHING (sendBrowserRuntimeMessage) ──────────────────────

  describe('Message Dispatching: Firefox Native Promise Runtime', () => {
    beforeEach(() => {
      const g = globalThis as Record<string, unknown>
      g.browser = {
        runtime: {
          sendMessage: vi.fn(),
        },
      }
    })

    it('successfully sends message and receives Promise response in Firefox', async () => {
      const pingData: PingResponseData = {
        reply: 'PONG',
        echoTimestamp: 123456789,
        receivedFrom: 'content-script',
      }
      const successRes = createSuccessResponse(pingData)

      const g = globalThis as Record<string, unknown>
      const browserApi = g.browser as { runtime: { sendMessage: ReturnType<typeof vi.fn> } }
      browserApi.runtime.sendMessage.mockResolvedValue(successRes)

      const pingMsg = createPingMessage('content-script')
      const response = await sendBrowserRuntimeMessage<typeof pingMsg, PingResponseData>(pingMsg)

      expect(response.success).toBe(true)
      expect(response.data).toEqual(pingData)
      expect(browserApi.runtime.sendMessage).toHaveBeenCalledWith(pingMsg)
    })

    it('handles Firefox Promise rejection (e.g. extension context invalidated)', async () => {
      const g = globalThis as Record<string, unknown>
      const browserApi = g.browser as { runtime: { sendMessage: ReturnType<typeof vi.fn> } }
      browserApi.runtime.sendMessage.mockRejectedValue(new Error('Extension context invalidated.'))

      const pingMsg = createPingMessage('popup')
      const response = await sendBrowserRuntimeMessage(pingMsg)

      expect(response.success).toBe(false)
      expect(response.error).toContain('Extension context invalidated.')
    })

    it('handles Firefox no receiving end error cleanly', async () => {
      const g = globalThis as Record<string, unknown>
      const browserApi = g.browser as { runtime: { sendMessage: ReturnType<typeof vi.fn> } }
      browserApi.runtime.sendMessage.mockRejectedValue(
        new Error('Could not establish connection. Receiving end does not exist.')
      )

      const pingMsg = createPingMessage('popup')
      const response = await sendBrowserRuntimeMessage(pingMsg)

      expect(response.success).toBe(false)
      expect(response.error).toContain('Receiving end does not exist')
    })
  })

  describe('Message Dispatching: Chromium Callback Runtime', () => {
    beforeEach(() => {
      const g = globalThis as Record<string, unknown>
      delete g.browser
      g.chrome = {
        runtime: {
          sendMessage: vi.fn(),
          lastError: null,
        },
      }
    })

    it('successfully sends message via chrome.runtime callback in Chromium', async () => {
      const pingData: PingResponseData = {
        reply: 'PONG',
        echoTimestamp: 987654321,
        receivedFrom: 'popup',
      }
      const successRes = createSuccessResponse(pingData)

      const g = globalThis as Record<string, unknown>
      const chromeApi = g.chrome as {
        runtime: { sendMessage: ReturnType<typeof vi.fn>; lastError: unknown }
      }
      chromeApi.runtime.sendMessage.mockImplementation(
        (_msg: unknown, cb: (res: ExtensionResponse) => void) => {
          cb(successRes)
        }
      )

      const pingMsg = createPingMessage('popup')
      const response = await sendBrowserRuntimeMessage<typeof pingMsg, PingResponseData>(pingMsg)

      expect(response.success).toBe(true)
      expect(response.data).toEqual(pingData)
    })

    it('handles chrome.runtime.lastError cleanly in Chromium', async () => {
      const g = globalThis as Record<string, unknown>
      const chromeApi = g.chrome as {
        runtime: { sendMessage: ReturnType<typeof vi.fn>; lastError: unknown }
      }
      chromeApi.runtime.sendMessage.mockImplementation(
        (_msg: unknown, cb: (res: unknown) => void) => {
          chromeApi.runtime.lastError = {
            message: 'Could not establish connection. Receiving end does not exist.',
          }
          cb(undefined)
        }
      )

      const pingMsg = createPingMessage('content-script')
      const response = await sendBrowserRuntimeMessage(pingMsg)

      expect(response.success).toBe(false)
      expect(response.error).toContain('Receiving end does not exist')
    })
  })

  // ─── MESSAGE LISTENER (addRuntimeMessageListener) ─────────────────────────

  describe('Message Listener Adapter: Dual Semantics', () => {
    it('supports Chromium boolean return / sendResponse callback model', () => {
      let registeredListener:
        ((raw: unknown, sender: unknown, sendRes: (r: unknown) => void) => boolean) | null = null
      const addListenerSpy = vi.fn().mockImplementation((fn) => {
        registeredListener = fn
      })

      const g = globalThis as Record<string, unknown>
      g.chrome = { runtime: { onMessage: { addListener: addListenerSpy } } }

      const mockSendResponse = vi.fn()
      addRuntimeMessageListener((_msg, _sender, sendResponse) => {
        sendResponse(createSuccessResponse({ test: true }))
        return true
      })

      expect(addListenerSpy).toHaveBeenCalled()
      expect(registeredListener).not.toBeNull()

      const keepChannelOpen = registeredListener!({}, {}, mockSendResponse)
      expect(keepChannelOpen).toBe(true)
      expect(mockSendResponse).toHaveBeenCalledWith(createSuccessResponse({ test: true }))
    })

    it('supports Firefox Promise return model seamlessly', async () => {
      let registeredListener:
        ((raw: unknown, sender: unknown, sendRes: (r: unknown) => void) => boolean) | null = null
      const addListenerSpy = vi.fn().mockImplementation((fn) => {
        registeredListener = fn
      })

      const g = globalThis as Record<string, unknown>
      g.browser = { runtime: { onMessage: { addListener: addListenerSpy } } }

      const mockSendResponse = vi.fn()
      addRuntimeMessageListener(async () => {
        return createSuccessResponse({ asyncReply: 'firefox-ok' })
      })

      expect(addListenerSpy).toHaveBeenCalled()
      const keepChannelOpen = registeredListener!({}, {}, mockSendResponse)
      expect(keepChannelOpen).toBe(true)

      // Wait for the async promise resolution
      await new Promise((r) => setTimeout(r, 10))
      expect(mockSendResponse).toHaveBeenCalledWith(
        expect.objectContaining({ success: true, data: { asyncReply: 'firefox-ok' } })
      )
    })
  })

  // ─── LIFECYCLE (onRuntimeInstalled) ──────────────────────────────────────

  describe('Lifecycle Listeners', () => {
    it('attaches onInstalled listener when runtime is present', () => {
      const addListenerSpy = vi.fn()
      const g = globalThis as Record<string, unknown>
      g.browser = { runtime: { onInstalled: { addListener: addListenerSpy } } }

      const callback = vi.fn()
      onRuntimeInstalled(callback)

      expect(addListenerSpy).toHaveBeenCalledWith(callback)
    })
  })
})
