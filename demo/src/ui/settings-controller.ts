/**
 * Connection and Authentication Settings Controller for the IntelliCache Demonstration Chatbot.
 *
 * Manages the connection dialog, gateway endpoint URL configuration, and memory-only
 * client API token entry with real-time health and namespace diagnostic feedback.
 */

import { GatewayApiClient, type ConnectionTestResult } from '../api-client'
import type { ConnectionInfo, ConnectionState } from '../types'
import { MarkdownRenderer } from './markdown-renderer'

export interface SettingsCallbacks {
  onConnectionChange: (info: ConnectionInfo) => void
  onClearCredentials: () => void
}

export class SettingsController {
  private apiClient: GatewayApiClient
  private callbacks: SettingsCallbacks
  private isOpen: boolean = false

  // DOM Elements
  private dialogEl!: HTMLElement
  private backdropEl!: HTMLElement
  private closeBtn!: HTMLButtonElement
  private gatewayUrlInput!: HTMLInputElement
  private clientTokenInput!: HTMLInputElement
  private toggleTokenBtn!: HTMLButtonElement
  private connectBtn!: HTMLButtonElement
  private disconnectBtn!: HTMLButtonElement
  private statusCardEl!: HTMLElement

  constructor(apiClient: GatewayApiClient, callbacks: SettingsCallbacks) {
    this.apiClient = apiClient
    this.callbacks = callbacks
  }

  public init(): void {
    this.bindDomElements()
    this.attachEventListeners()
  }

  private bindDomElements(): void {
    this.dialogEl = document.getElementById('settings-dialog')!
    this.backdropEl = document.getElementById('settings-backdrop')!
    this.closeBtn = document.getElementById('btn-close-settings') as HTMLButtonElement
    this.gatewayUrlInput = document.getElementById('settings-gateway-url') as HTMLInputElement
    this.clientTokenInput = document.getElementById('settings-client-token') as HTMLInputElement
    this.toggleTokenBtn = document.getElementById('btn-settings-toggle-token') as HTMLButtonElement
    this.connectBtn = document.getElementById('btn-settings-connect') as HTMLButtonElement
    this.disconnectBtn = document.getElementById('btn-settings-disconnect') as HTMLButtonElement
    this.statusCardEl = document.getElementById('settings-status-card')!

    // Pre-populate with current client URL
    if (this.gatewayUrlInput) {
      this.gatewayUrlInput.value = this.apiClient.getBaseUrl()
    }
  }

  private attachEventListeners(): void {
    // Show/hide token visibility
    this.toggleTokenBtn?.addEventListener('click', () => {
      const isPassword = this.clientTokenInput.type === 'password'
      this.clientTokenInput.type = isPassword ? 'text' : 'password'
      this.toggleTokenBtn.textContent = isPassword ? '🙈' : '👁️'
      this.toggleTokenBtn.setAttribute(
        'aria-label',
        isPassword ? 'Hide client API token' : 'Show client API token'
      )
    })

    // Connect & Test button
    this.connectBtn?.addEventListener('click', () => this.handleConnect())

    // Disconnect button
    this.disconnectBtn?.addEventListener('click', () => this.handleDisconnect())

    // Close button & backdrop
    this.closeBtn?.addEventListener('click', () => this.close())
    this.backdropEl?.addEventListener('click', () => this.close())

    // Keyboard support: Escape to close
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape' && this.isOpen) {
        this.close()
      }
    })
  }

  public open(): void {
    this.isOpen = true
    this.dialogEl.classList.add('open')
    this.backdropEl.classList.add('open')
    this.dialogEl.setAttribute('aria-hidden', 'false')
    this.gatewayUrlInput?.focus()
  }

  public close(): void {
    this.isOpen = false
    this.dialogEl.classList.remove('open')
    this.backdropEl.classList.remove('open')
    this.dialogEl.setAttribute('aria-hidden', 'true')
  }

  public isDialogOpen(): boolean {
    return this.isOpen
  }

  public async handleConnect(): Promise<ConnectionTestResult> {
    const url = this.gatewayUrlInput.value.trim()
    const token = this.clientTokenInput.value.trim()

    this.apiClient.setBaseUrl(url)
    this.apiClient.setClientToken(token)

    this.connectBtn.disabled = true
    this.renderStatusFeedback('connecting', 'Testing gateway connectivity...')

    try {
      const result = await this.apiClient.testConnection()
      let state: ConnectionState = 'disconnected'
      let detailMsg = ''

      if (!result.online) {
        state = 'error'
        detailMsg = result.error || 'Gateway offline or unreachable. Check CORS configuration.'
        this.renderStatusFeedback('error', detailMsg, result)
      } else if (!result.authenticated) {
        state = 'unauthorized'
        detailMsg =
          result.error || 'Gateway is online, but client token was rejected or not provided.'
        this.renderStatusFeedback('unauthorized', detailMsg, result)
      } else {
        state = 'connected'
        detailMsg = `Successfully connected to namespace: ${result.namespace}`
        this.renderStatusFeedback('connected', detailMsg, result)
      }

      this.callbacks.onConnectionChange({
        state,
        gatewayUrl: url,
        namespace: result.namespace,
        serverVersion: result.serverVersion,
        errorDetail: detailMsg,
      })

      return result
    } catch (err) {
      const errMsg = err instanceof Error ? err.message : String(err)
      this.renderStatusFeedback('error', `Connection error: ${errMsg}`)
      this.callbacks.onConnectionChange({
        state: 'error',
        gatewayUrl: url,
        errorDetail: errMsg,
      })
      return {
        online: false,
        authenticated: false,
        error: errMsg,
      }
    } finally {
      this.connectBtn.disabled = false
    }
  }

  public handleDisconnect(): void {
    this.apiClient.clearToken()
    this.clientTokenInput.value = ''
    this.renderStatusFeedback('disconnected', 'Disconnected. In-memory client token cleared.')

    this.callbacks.onClearCredentials()
    this.callbacks.onConnectionChange({
      state: 'disconnected',
      gatewayUrl: this.apiClient.getBaseUrl(),
    })
  }

  private renderStatusFeedback(
    state: ConnectionState,
    message: string,
    result?: ConnectionTestResult
  ): void {
    let badgeClass = 'badge-disconnected'
    let stateLabel = 'Disconnected'

    switch (state) {
      case 'connecting':
        badgeClass = 'badge-connecting'
        stateLabel = 'Connecting...'
        break
      case 'connected':
        badgeClass = 'badge-connected'
        stateLabel = `Connected [${result?.namespace || 'authorized'}]`
        break
      case 'unauthorized':
        badgeClass = 'badge-unauthorized'
        stateLabel = 'Unauthorized (401)'
        break
      case 'error':
        badgeClass = 'badge-error'
        stateLabel = 'Connection Failed'
        break
    }

    let metaHtml = ''
    if (result && result.online) {
      metaHtml = `
        <div class="status-meta-grid">
          <div class="status-meta-item">
            <span class="meta-label">Server Version:</span>
            <span class="meta-value">${MarkdownRenderer.escapeHtml(result.serverVersion || '—')}</span>
          </div>
          <div class="status-meta-item">
            <span class="meta-label">Configured Model:</span>
            <span class="meta-value">${MarkdownRenderer.escapeHtml(result.configuredModel || '—')}</span>
          </div>
          <div class="status-meta-item">
            <span class="meta-label">Cache Storage:</span>
            <span class="meta-value">${MarkdownRenderer.escapeHtml(result.cacheBackend ? result.cacheBackend.toUpperCase() : '—')}</span>
          </div>
          <div class="status-meta-item">
            <span class="meta-label">Cache Active:</span>
            <span class="meta-value ${result.cacheEnabled ? 'green-text' : 'amber-text'}">
              ${result.cacheEnabled ? 'Enabled' : 'Disabled'}
            </span>
          </div>
        </div>
      `
    }

    this.statusCardEl.innerHTML = `
      <div class="status-card-header">
        <span class="status-badge-pill ${badgeClass}">${stateLabel}</span>
      </div>
      <div class="status-message">${MarkdownRenderer.escapeHtml(message)}</div>
      ${metaHtml}
    `
  }
}
