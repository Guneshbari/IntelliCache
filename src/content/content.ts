/**
 * IntelliCache Collector - Content Script Entry Point
 *
 * NOTE: In Step 1, this script establishes the injection hook and communication channel
 * with the Service Worker. No platform scraping or DOM mutations are performed here yet.
 */

import {
  createContentScriptInitMessage,
  createErrorResponse,
  createSuccessResponse,
  detectPlatformFromUrl,
  isExtensionMessage,
  sendExtensionMessage,
} from '../shared/messages'
import type { ExtensionMessage, ExtensionResponse } from '../shared/types'

function initializeContentScript() {
  const currentUrl = window.location.href
  const pageTitle = document.title || 'Untitled Page'
  const platform = detectPlatformFromUrl(currentUrl)

  console.log(
    `[IntelliCache Content] Injected successfully into ${platform.toUpperCase()} page (${currentUrl})`
  )

  // Send handshake message to Service Worker to verify content-to-background communication
  const initMessage = createContentScriptInitMessage(currentUrl, pageTitle)

  sendExtensionMessage(initMessage)
    .then((response) => {
      if (response && response.success) {
        console.log('[IntelliCache Content] Service worker acknowledged initialization:', response)
      } else {
        console.warn(
          '[IntelliCache Content] Service worker returned error on init:',
          response?.error
        )
      }
    })
    .catch((err: unknown) => {
      console.error(
        '[IntelliCache Content] Failed to send initialization message to background:',
        err
      )
    })

  // Listen for any test messages sent directly to this tab from Popup or Service Worker
  chrome.runtime.onMessage.addListener(
    (
      rawMessage: unknown,
      _sender: chrome.runtime.MessageSender,
      sendResponse: (response: ExtensionResponse) => void
    ): boolean => {
      if (!isExtensionMessage(rawMessage)) {
        return false
      }

      const message = rawMessage as ExtensionMessage

      if (message.type === 'PING') {
        sendResponse(
          createSuccessResponse({
            reply: 'PONG_FROM_CONTENT_SCRIPT',
            url: window.location.href,
            platform,
            echoTimestamp: message.timestamp,
          })
        )
      } else {
        sendResponse(createErrorResponse('Unhandled content script message type'))
      }

      return false
    }
  )
}

// Execute when DOM is ready or idle
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', initializeContentScript)
} else {
  initializeContentScript()
}
