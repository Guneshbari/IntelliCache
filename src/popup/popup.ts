/**
 * IntelliCache Collector - Popup Controller
 * Manages minimal diagnostic UI and communications testing with the Service Worker & Data Layer.
 */

import { logger } from '../diagnostics'
import {
  createDbGetStatsMessage,
  createGetStatusMessage,
  createPingMessage,
  sendExtensionMessage,
} from '../shared/messages'
import type { DbStatsResponseData, PingResponseData, StatusResponseData } from '../shared/types'

document.addEventListener('DOMContentLoaded', () => {
  const statusBadge = document.getElementById('status-badge')
  const statusText = document.getElementById('status-text')
  const swStatus = document.getElementById('sw-status')
  const dbStatus = document.getElementById('db-status')
  const extVersion = document.getElementById('ext-version')
  const pingBtn = document.getElementById('ping-btn') as HTMLButtonElement | null
  const clearLogBtn = document.getElementById('clear-log-btn') as HTMLButtonElement | null
  const logOutput = document.getElementById('log-output')

  function appendLog(message: string, type: 'info' | 'success' | 'error' = 'info') {
    if (!logOutput) return
    const entry = document.createElement('div')
    entry.className = `log-entry log-entry-${type}`

    const timeSpan = document.createElement('span')
    timeSpan.className = 'log-time'
    timeSpan.textContent = new Date().toLocaleTimeString()

    const msgSpan = document.createElement('span')
    msgSpan.className = 'log-msg'
    msgSpan.textContent = message

    entry.appendChild(timeSpan)
    entry.appendChild(msgSpan)
    logOutput.appendChild(entry)
    logOutput.scrollTop = logOutput.scrollHeight
  }

  function updateStatus(active: boolean, text: string, swState: string) {
    if (statusBadge) {
      if (active) {
        statusBadge.classList.add('active')
      } else {
        statusBadge.classList.remove('active')
      }
    }
    if (statusText) statusText.textContent = text
    if (swStatus) swStatus.textContent = swState
  }

  // Check Service Worker and Database status on popup launch
  async function checkInitialStatus() {
    try {
      const statusMessage = createGetStatusMessage('popup')
      const statusResponse = await sendExtensionMessage<typeof statusMessage, StatusResponseData>(
        statusMessage
      )

      if (statusResponse && statusResponse.success && statusResponse.data) {
        const data = statusResponse.data
        if (extVersion) extVersion.textContent = data.version
        updateStatus(true, 'Connected', 'Active (MV3)')
        appendLog(
          `Service Worker online (version ${data.version}, manifest v${data.manifestVersion})`,
          'success'
        )

        // Query database statistics
        const statsMessage = createDbGetStatsMessage('popup')
        const statsResponse = await sendExtensionMessage<typeof statsMessage, DbStatsResponseData>(
          statsMessage
        )

        if (statsResponse && statsResponse.success && statsResponse.data) {
          const stats = statsResponse.data
          if (dbStatus) {
            dbStatus.textContent = `IndexedDB (${stats.interactionCount} interactions)`
          }
          appendLog(
            `Database '${stats.dbName}' connected (v${stats.dbVersion}): ${stats.interactionCount} interactions, ${stats.conversationCount} conversations`,
            'info'
          )
          logger.info(
            'UI',
            'CORE',
            `Query result | platform=all | conversations=${stats.conversationCount} | interactions=${stats.interactionCount} | rendered=${stats.interactionCount}`
          )
        }
      } else {
        updateStatus(false, 'Disconnected', 'Unavailable')
        appendLog(
          `Failed to query service worker: ${statusResponse?.error ?? 'No response'}`,
          'error'
        )
      }
    } catch (err) {
      updateStatus(false, 'Error', 'Error')
      appendLog(`Status check error: ${err instanceof Error ? err.message : String(err)}`, 'error')
    }
  }

  // Ping Service Worker on button click
  async function handlePing() {
    if (!pingBtn) return

    pingBtn.disabled = true
    const startTime = performance.now()
    appendLog('Sending PING to Service Worker...', 'info')

    try {
      const pingMessage = createPingMessage('popup', 'Diagnostic ping from popup UI')
      const response = await sendExtensionMessage<typeof pingMessage, PingResponseData>(pingMessage)
      const latency = Math.round(performance.now() - startTime)

      if (response && response.success && response.data) {
        appendLog(
          `Received: ${response.data.reply} (RTT: ${latency}ms, Echo: ${new Date(response.data.echoTimestamp).toLocaleTimeString()})`,
          'success'
        )
        updateStatus(true, 'Connected', 'Active (MV3)')
      } else {
        appendLog(`Ping failed: ${response?.error ?? 'No response'}`, 'error')
        updateStatus(false, 'Error', 'Unresponsive')
      }
    } catch (err) {
      appendLog(`Ping exception: ${err instanceof Error ? err.message : String(err)}`, 'error')
      updateStatus(false, 'Error', 'Error')
    } finally {
      pingBtn.disabled = false
    }
  }

  // Clear log
  if (clearLogBtn && logOutput) {
    clearLogBtn.addEventListener('click', () => {
      logOutput.innerHTML = ''
      appendLog('Log cleared.', 'info')
    })
  }

  if (pingBtn) {
    pingBtn.addEventListener('click', handlePing)
  }

  // Run initial status check
  void checkInitialStatus()
})
