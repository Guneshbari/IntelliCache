/**
 * IntelliCache Collector - Content Script Entry Point
 *
 * NOTE: In Step 1, this script establishes the injection hook and communication channel
 * with the Service Worker. No platform scraping or DOM mutations are performed here yet.
 */

import { logger, toDiagnosticPlatform } from '../diagnostics'
import { getAdapterForUrl } from '../platforms/registry'
import type { PlatformAdapter } from '../platforms/types'
import { addRuntimeMessageListener, type WebExtensionSender } from '../shared/browser'
import {
  createContentScriptInitMessage,
  createErrorResponse,
  createSuccessResponse,
  detectPlatformFromUrl,
  isExtensionMessage,
  sendExtensionMessage,
} from '../shared/messages'
import type { ExtensionMessage, ExtensionResponse } from '../shared/types'

let activeAdapter: PlatformAdapter | null = null

function initializeContentScript() {
  const currentUrl = window.location.href
  const pageTitle = document.title || 'Untitled Page'
  const platform = detectPlatformFromUrl(currentUrl)
  const platformTag = toDiagnosticPlatform(platform)

  logger.info(
    'Content',
    platformTag,
    `Injected successfully into ${platform.toUpperCase()} page (${currentUrl})`
  )
  logger.info('Content', platformTag, `Platform detected: ${platform} (URL: ${currentUrl})`)

  // Send handshake message to Service Worker to verify content-to-background communication
  const initMessage = createContentScriptInitMessage(currentUrl, pageTitle)
  logger.debug(
    'Content',
    platformTag,
    'Sending initialization handshake message to service worker...'
  )

  sendExtensionMessage(initMessage)
    .then((response) => {
      if (response && response.success) {
        logger.info(
          'Content',
          platformTag,
          'Service worker acknowledged content script initialization.'
        )
      } else {
        logger.warn(
          'Content',
          platformTag,
          `Service worker returned error on init: ${response?.error ?? 'Unknown error'}`
        )
      }
    })
    .catch((err: unknown) => {
      logger.error(
        'Content',
        platformTag,
        `Failed to send initialization message to background: ${err instanceof Error ? err.message : String(err)}`
      )
    })

  // Discover and start platform adapter if available
  activeAdapter = getAdapterForUrl(currentUrl)
  if (activeAdapter) {
    logger.info(
      'Content',
      platformTag,
      `Selected adapter: ${activeAdapter.constructor.name} (platform: ${activeAdapter.platform})`
    )
    logger.info(
      'Content',
      platformTag,
      `Starting adapter for platform: ${activeAdapter.platform.toUpperCase()}`
    )
    try {
      activeAdapter.start()
      logger.info(
        'Content',
        platformTag,
        `Adapter started successfully for ${activeAdapter.platform.toUpperCase()}`
      )
    } catch (startErr) {
      logger.error(
        'Content',
        platformTag,
        `Adapter initialization/start failed: ${startErr instanceof Error ? startErr.message : String(startErr)}`
      )
    }
  } else {
    logger.info('Content', platformTag, 'No specialized collector adapter required for this page.')
  }

  // Cleanup on unload
  window.addEventListener('beforeunload', () => {
    if (activeAdapter) {
      logger.info(
        'Content',
        platformTag,
        `Page beforeunload triggered. Stopping adapter for ${activeAdapter.platform}...`
      )
      activeAdapter.stop()
      logger.info('Content', platformTag, 'Adapter stopped.')
      activeAdapter = null
    }
  })

  // Listen for any test messages sent directly to this tab from Popup or Service Worker
  addRuntimeMessageListener(
    (
      rawMessage: unknown,
      _sender: WebExtensionSender,
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
