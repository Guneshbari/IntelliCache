/**
 * IntelliCache Collector - Cross-Browser Compatibility Layer
 *
 * Provides a unified abstraction over WebExtension APIs across Chromium
 * (Chrome, Edge, Brave) and Mozilla Firefox.
 *
 * Architectural Principles:
 * 1. Prefers standard WebExtension APIs (`browser.*` / `chrome.*`).
 * 2. Surfaces all runtime errors through the structured logger.
 * 3. Never silently swallows communication errors.
 * 4. Transparently handles both Promise-based and callback-based messaging.
 */

import { logger } from '../diagnostics'
import { createErrorResponse } from './messages'
import type { ExtensionMessage, ExtensionResponse } from './types'

export type BrowserFamily = 'chromium' | 'firefox' | 'unknown'

/**
 * Universal interface for WebExtension runtime message sender.
 */
export interface WebExtensionSender {
  tab?: {
    id?: number
    url?: string
    title?: string
  }
  id?: string
  url?: string
  origin?: string
}

/**
 * Type-safe message listener signature supporting both sync (return boolean)
 * and async (return Promise) response semantics.
 */
export type WebExtensionMessageListener = (
  message: unknown,
  sender: WebExtensionSender,
  sendResponse: (response: ExtensionResponse) => void
) => boolean | Promise<ExtensionResponse | void> | void

/**
 * Resolves the underlying browser runtime object (`browser` or `chrome`).
 */
export function getBrowserRuntime(): typeof chrome.runtime | undefined {
  if (typeof globalThis !== 'undefined') {
    const g = globalThis as Record<string, unknown>
    if (
      g.browser &&
      typeof g.browser === 'object' &&
      (g.browser as { runtime?: typeof chrome.runtime }).runtime
    ) {
      return (g.browser as { runtime: typeof chrome.runtime }).runtime
    }
    if (
      g.chrome &&
      typeof g.chrome === 'object' &&
      (g.chrome as { runtime?: typeof chrome.runtime }).runtime
    ) {
      return (g.chrome as { runtime: typeof chrome.runtime }).runtime
    }
  }
  return undefined
}

/**
 * Detects the active browser family from runtime capabilities and userAgent.
 */
export function detectBrowserFamily(): BrowserFamily {
  if (typeof navigator !== 'undefined' && /firefox/i.test(navigator.userAgent)) {
    return 'firefox'
  }
  if (typeof globalThis !== 'undefined') {
    const g = globalThis as Record<string, unknown>
    if (
      g.browser &&
      typeof (g.browser as { runtime?: { getBrowserInfo?: unknown } }).runtime?.getBrowserInfo ===
        'function'
    ) {
      return 'firefox'
    }
  }
  if (typeof navigator !== 'undefined' && /chrome|chromium|edg|brave/i.test(navigator.userAgent)) {
    return 'chromium'
  }
  return 'unknown'
}

/**
 * Returns true if running inside Mozilla Firefox.
 */
export function isFirefox(): boolean {
  return detectBrowserFamily() === 'firefox'
}

/**
 * Returns true if running inside a Chromium-based browser (Chrome, Edge, Brave).
 */
export function isChromium(): boolean {
  return detectBrowserFamily() === 'chromium'
}

/**
 * Registers an extension lifecycle listener for installation and updates.
 */
export function onRuntimeInstalled(
  callback: (details: { reason: string; previousVersion?: string }) => void
): void {
  const runtime = getBrowserRuntime()
  if (runtime?.onInstalled) {
    runtime.onInstalled.addListener(callback)
  }
}

/**
 * Registers a message listener with dual support for Chromium callback/channel
 * keeping (returning true) and Firefox Promise-returning listeners.
 */
export function addRuntimeMessageListener(listener: WebExtensionMessageListener): void {
  const runtime = getBrowserRuntime()
  if (!runtime?.onMessage) {
    logger.warn(
      'Messaging',
      'CORE',
      'Cannot attach runtime message listener: runtime.onMessage unavailable.'
    )
    return
  }

  runtime.onMessage.addListener(
    (
      rawMessage: unknown,
      sender: chrome.runtime.MessageSender,
      sendResponse: (response: ExtensionResponse) => void
    ): boolean => {
      const result = listener(rawMessage, sender as WebExtensionSender, sendResponse)

      // If the listener returned a Promise (standard in Firefox browser.runtime.onMessage),
      // forward the resolved response to sendResponse
      if (result instanceof Promise) {
        result
          .then((res) => {
            if (res) {
              sendResponse(res)
            }
          })
          .catch((err) => {
            const errorMsg = err instanceof Error ? err.message : String(err)
            logger.error('Messaging', 'CORE', `Async message handler rejected: ${errorMsg}`)
            sendResponse(createErrorResponse(errorMsg))
          })
        return true
      }

      // If boolean returned, return it directly (true keeps channel open for async sendResponse)
      return result === true
    }
  )
}

/**
 * Cross-browser message dispatcher.
 * Supports standard Firefox `browser.runtime.sendMessage` (Promise) and Chromium
 * `chrome.runtime.sendMessage` (callback/Promise) with uniform error classification.
 */
export async function sendBrowserRuntimeMessage<M extends ExtensionMessage, R = unknown>(
  message: M
): Promise<ExtensionResponse<R>> {
  const runtime = getBrowserRuntime()
  if (!runtime || !runtime.sendMessage) {
    const errorMsg = 'Extension runtime API is not available in the current environment.'
    logger.error('Messaging', 'CORE', errorMsg)
    throw new Error(errorMsg)
  }

  // Check if native Promise-returning browser.runtime.sendMessage is present (Firefox standard)
  const g = typeof globalThis !== 'undefined' ? (globalThis as Record<string, unknown>) : {}
  const browserApi = g.browser as
    { runtime?: { sendMessage?: (msg: unknown) => Promise<ExtensionResponse<R>> } } | undefined

  if (browserApi?.runtime?.sendMessage) {
    try {
      const response = await browserApi.runtime.sendMessage(message)
      if (!response) {
        logger.warn(
          'Messaging',
          'CORE',
          `No response received from extension runtime for '${message.type}'`
        )
        return createErrorResponse('No response received from extension component')
      }
      return response
    } catch (err) {
      const rawErrorMsg = err instanceof Error ? err.message : String(err)
      if (/extension context invalidated/i.test(rawErrorMsg)) {
        logger.error(
          'Messaging',
          'CORE',
          'Extension context invalidated! The extension was reloaded or updated; please refresh the active page.'
        )
      } else if (/receiving end does not exist|could not establish connection/i.test(rawErrorMsg)) {
        logger.warn(
          'Messaging',
          'CORE',
          `Background recipient not ready for '${message.type}': ${rawErrorMsg}`
        )
      } else {
        logger.warn(
          'Messaging',
          'CORE',
          `Runtime message error on '${message.type}': ${rawErrorMsg}`
        )
      }
      return createErrorResponse(rawErrorMsg)
    }
  }

  // Fallback to chrome.runtime.sendMessage with callback/lastError
  return new Promise<ExtensionResponse<R>>((resolve) => {
    try {
      runtime.sendMessage(message, (response: ExtensionResponse<R> | null | undefined) => {
        const lastError = runtime.lastError
        if (lastError) {
          const errorMsg = lastError.message ?? 'Unknown runtime error'
          if (/extension context invalidated/i.test(errorMsg)) {
            logger.error(
              'Messaging',
              'CORE',
              'Extension context invalidated! The extension was reloaded or updated; please refresh the active page.'
            )
          } else if (
            /receiving end does not exist|could not establish connection/i.test(errorMsg)
          ) {
            logger.warn(
              'Messaging',
              'CORE',
              `Background recipient not ready for '${message.type}': ${errorMsg}`
            )
          } else {
            logger.warn(
              'Messaging',
              'CORE',
              `Runtime message error on '${message.type}': ${errorMsg}`
            )
          }
          resolve(createErrorResponse(errorMsg))
        } else if (!response) {
          logger.warn(
            'Messaging',
            'CORE',
            `No response received from extension runtime for '${message.type}'`
          )
          resolve(createErrorResponse('No response received from extension component'))
        } else {
          resolve(response)
        }
      })
    } catch (sendEx) {
      const exMsg = sendEx instanceof Error ? sendEx.message : String(sendEx)
      logger.error('Messaging', 'CORE', `Exception invoking runtime.sendMessage: ${exMsg}`)
      resolve(createErrorResponse(exMsg))
    }
  })
}
