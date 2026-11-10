/**
 * UI Controller for the IntelliCache Demonstration Chatbot.
 *
 * Coordinates:
 * - Temporary in-memory conversation sessions (ConversationManager).
 * - Safe Markdown parsing and code block highlighting (MarkdownRenderer).
 * - Chat message and cache diagnostics presentation (MessageRenderer).
 * - Connection and API credential management (SettingsController).
 * - Namespace metrics and cache capacity telemetry (MetricsController).
 *
 * All state is kept strictly in-memory. No client tokens or conversation data
 * are written to localStorage, IndexedDB, sessionStorage, or cookies.
 */

import { GatewayApiClient, GatewayApiError } from './api-client'
import type {
  ChatMessage,
  ConnectionInfo,
  ConnectionState,
  QueryRequestPayload,
  QueryResponse,
} from './types'
import { ConversationManager } from './ui/conversation-manager'
import { MarkdownRenderer } from './ui/markdown-renderer'
import { MessageRenderer } from './ui/message-renderer'
import { MetricsController } from './ui/metrics-controller'
import { SettingsController } from './ui/settings-controller'

export class DemoUIController {
  private apiClient: GatewayApiClient
  private conversationManager: ConversationManager
  private settingsController: SettingsController
  private metricsController: MetricsController

  private mode: 'single-turn' | 'multi-turn' = 'single-turn'
  private isGenerating: boolean = false
  private connectionInfo: ConnectionInfo = {
    state: 'disconnected',
    gatewayUrl: 'http://127.0.0.1:8000',
  }

  // DOM Elements
  private connectionBadgeEl!: HTMLElement
  private connectionTextEl!: HTMLElement
  private headerNewChatBtn!: HTMLButtonElement
  private headerSettingsBtn!: HTMLButtonElement
  private toggleSidebarBtn!: HTMLButtonElement
  private closeSidebarBtn!: HTMLButtonElement
  private sidebarEl!: HTMLElement
  private sidebarBackdropEl!: HTMLElement
  private sidebarNewChatBtn!: HTMLButtonElement
  private conversationListEl!: HTMLElement
  private messagesContainerEl!: HTMLElement
  private chatTextarea!: HTMLTextAreaElement
  private sendBtn!: HTMLButtonElement
  private bypassCheckbox!: HTMLInputElement
  private modeSingleBtn!: HTMLButtonElement
  private modeMultiBtn!: HTMLButtonElement
  private turnNoticeEl!: HTMLElement

  constructor(apiClient: GatewayApiClient) {
    this.apiClient = apiClient
    this.conversationManager = new ConversationManager()

    this.settingsController = new SettingsController(this.apiClient, {
      onConnectionChange: (info) => this.handleConnectionStateChange(info),
      onClearCredentials: () => {
        this.metricsController.renderInitialState()
      },
    })

    this.metricsController = new MetricsController(this.apiClient)
  }

  public init(): void {
    MarkdownRenderer.init()
    this.bindDomElements()
    this.settingsController.init()
    this.metricsController.init()
    this.attachEventListeners()
    this.updateConnectionBadge('disconnected', 'Disconnected')
    this.renderTurnNotice()
    this.renderConversationList()
    this.renderMessages()
  }

  public getConversationManager(): ConversationManager {
    return this.conversationManager
  }

  public getSettingsController(): SettingsController {
    return this.settingsController
  }

  public getMetricsController(): MetricsController {
    return this.metricsController
  }

  private bindDomElements(): void {
    this.connectionBadgeEl = document.getElementById('connection-badge')!
    this.connectionTextEl = document.getElementById('connection-text')!
    this.headerNewChatBtn = document.getElementById('btn-header-new-chat') as HTMLButtonElement
    this.headerSettingsBtn = document.getElementById('btn-header-settings') as HTMLButtonElement
    this.toggleSidebarBtn = document.getElementById('btn-toggle-sidebar') as HTMLButtonElement
    this.closeSidebarBtn = document.getElementById('btn-close-sidebar') as HTMLButtonElement
    this.sidebarEl = document.getElementById('sidebar')!
    this.sidebarBackdropEl = document.getElementById('sidebar-backdrop')!
    this.sidebarNewChatBtn = document.getElementById('btn-sidebar-new-chat') as HTMLButtonElement
    this.conversationListEl = document.getElementById('conversation-list')!
    this.messagesContainerEl = document.getElementById('messages-container')!
    this.chatTextarea = document.getElementById('chat-textarea') as HTMLTextAreaElement
    this.sendBtn = document.getElementById('btn-send') as HTMLButtonElement
    this.bypassCheckbox = document.getElementById('bypass-cache-checkbox') as HTMLInputElement
    this.modeSingleBtn = document.getElementById('mode-single') as HTMLButtonElement
    this.modeMultiBtn = document.getElementById('mode-multi') as HTMLButtonElement
    this.turnNoticeEl = document.getElementById('turn-notice')!
  }

  private attachEventListeners(): void {
    // Header actions
    this.connectionBadgeEl?.addEventListener('click', () => {
      this.settingsController.open()
    })

    this.headerSettingsBtn?.addEventListener('click', () => {
      this.settingsController.open()
    })

    this.headerNewChatBtn?.addEventListener('click', () => {
      this.handleNewChat()
    })

    // Sidebar actions
    this.sidebarNewChatBtn?.addEventListener('click', () => {
      this.handleNewChat()
    })

    this.toggleSidebarBtn?.addEventListener('click', () => {
      this.toggleMobileSidebar()
    })

    this.closeSidebarBtn?.addEventListener('click', () => {
      this.closeMobileSidebar()
    })

    this.sidebarBackdropEl?.addEventListener('click', () => {
      this.closeMobileSidebar()
    })

    // Mode Selector
    this.modeSingleBtn?.addEventListener('click', () => this.setMode('single-turn'))
    this.modeMultiBtn?.addEventListener('click', () => this.setMode('multi-turn'))

    // Send on click and Enter (without Shift)
    this.sendBtn?.addEventListener('click', () => this.handleSend())

    this.chatTextarea?.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey) {
        e.preventDefault()
        this.handleSend()
      }
    })

    // Auto-resize textarea
    this.chatTextarea?.addEventListener('input', () => {
      this.autoResizeTextarea()
    })

    // Delegated clicks inside messages container
    this.messagesContainerEl?.addEventListener('click', (e) => {
      const target = e.target as HTMLElement

      // Sample prompt chips
      const promptChip = target.closest('.prompt-chip') as HTMLElement
      if (promptChip) {
        const promptText =
          promptChip.getAttribute('data-prompt') || promptChip.textContent?.trim() || ''
        this.chatTextarea.value = promptText
        this.autoResizeTextarea()
        this.chatTextarea.focus()
        return
      }

      // Retry button
      const retryBtn = target.closest('.btn-retry') as HTMLElement
      if (retryBtn) {
        const retryId = retryBtn.getAttribute('data-retry-id')
        if (retryId) {
          this.handleRetry(retryId)
        }
        return
      }

      // Copy response button
      const copyResponseBtn = target.closest('.btn-copy-response') as HTMLButtonElement
      if (copyResponseBtn) {
        const msgId = copyResponseBtn.getAttribute('data-msg-id')
        if (msgId) {
          this.handleCopyResponse(msgId, copyResponseBtn)
        }
        return
      }

      // Copy code block button
      const copyCodeBtn = target.closest('.btn-copy-code') as HTMLButtonElement
      if (copyCodeBtn) {
        this.handleCopyCode(copyCodeBtn)
        return
      }

      // Copy full fingerprint button
      const copyFpBtn = target.closest('.btn-copy-fp') as HTMLButtonElement
      if (copyFpBtn) {
        const fp = copyFpBtn.getAttribute('data-fp') || ''
        this.handleCopyFingerprint(fp, copyFpBtn)
        return
      }
    })

    // Delegated clicks inside conversation list (switching and deleting)
    this.conversationListEl?.addEventListener('click', (e) => {
      const target = e.target as HTMLElement

      // Delete conversation button
      const deleteBtn = target.closest('.btn-delete-conv') as HTMLElement
      if (deleteBtn) {
        e.stopPropagation()
        const convId = deleteBtn.getAttribute('data-conv-id')
        if (convId) {
          this.handleDeleteConversation(convId)
        }
        return
      }

      // Switch conversation item
      const itemEl = target.closest('.conversation-item') as HTMLElement
      if (itemEl) {
        const convId = itemEl.getAttribute('data-conv-id')
        if (convId) {
          this.handleSwitchConversation(convId)
        }
      }
    })
  }

  private autoResizeTextarea(): void {
    if (!this.chatTextarea) return
    this.chatTextarea.style.height = 'auto'
    const nextHeight = Math.min(this.chatTextarea.scrollHeight, 180)
    this.chatTextarea.style.height = `${Math.max(nextHeight, 44)}px`
  }

  private toggleMobileSidebar(): void {
    const isOpen = this.sidebarEl.classList.contains('mobile-open')
    if (isOpen) {
      this.closeMobileSidebar()
    } else {
      this.sidebarEl.classList.add('mobile-open')
      this.sidebarBackdropEl.classList.add('mobile-open')
    }
  }

  private closeMobileSidebar(): void {
    this.sidebarEl?.classList.remove('mobile-open')
    this.sidebarBackdropEl?.classList.remove('mobile-open')
  }

  public handleConnectionStateChange(info: ConnectionInfo): void {
    this.connectionInfo = info
    let text = 'Disconnected'

    switch (info.state) {
      case 'connecting':
        text = 'Connecting...'
        break
      case 'connected':
        text = `Connected: ${info.namespace || 'default'}`
        this.metricsController.setNamespace(info.namespace)
        this.metricsController.refresh().catch(() => {})
        this.syncConversationsFromServer().catch(() => {})
        break
      case 'unauthorized':
        text = 'Unauthorized (401)'
        break
      case 'error':
        text = 'Gateway Offline'
        break
    }

    this.updateConnectionBadge(info.state, text)
  }

  public updateConnectionBadge(state: ConnectionState, text: string): void {
    this.connectionInfo.state = state
    this.connectionBadgeEl?.setAttribute('data-state', state)
    if (this.connectionTextEl) {
      this.connectionTextEl.textContent = text
    }
  }

  public setMode(mode: 'single-turn' | 'multi-turn'): void {
    this.mode = mode
    this.modeSingleBtn?.classList.toggle('active', mode === 'single-turn')
    this.modeMultiBtn?.classList.toggle('active', mode === 'multi-turn')
    this.renderTurnNotice()
  }

  private renderTurnNotice(): void {
    if (!this.turnNoticeEl) return
    if (this.mode === 'single-turn') {
      this.turnNoticeEl.innerHTML =
        '<span class="notice-icon">⚡</span><span><strong>Single-Turn Query Mode:</strong> Eligible for deterministic exact matching and HNSW semantic response caching.</span>'
    } else {
      this.turnNoticeEl.innerHTML =
        '<span class="notice-icon">ℹ️</span><span><strong>Multi-Turn Conversation Mode:</strong> Server-managed conversation history in PostgreSQL with bounded context window. Excluded from semantic caching to prevent context ambiguity.</span>'
    }
  }

  public async syncConversationsFromServer(): Promise<void> {
    if (!this.apiClient.hasToken()) return
    try {
      const resp = await this.apiClient.listConversations(50, 0)
      if (resp && Array.isArray(resp.conversations)) {
        if (resp.conversations.length > 0) {
          this.conversationManager.replaceConversationsFromServer(resp.conversations)
          await this.loadMessagesForActiveConversation()
        } else {
          try {
            const created = await this.apiClient.createConversation('New Conversation')
            if (created && created.id) {
              this.conversationManager.replaceConversationsFromServer([created])
            }
          } catch {
            // Non-blocking fallback
          }
        }
        this.renderConversationList()
        this.renderMessages()
      }
    } catch {
      // Non-blocking fallback when PostgreSQL is not configured
    }
  }

  public async loadMessagesForActiveConversation(): Promise<void> {
    const activeConv = this.conversationManager.getActiveConversation()
    if (!activeConv || !this.apiClient.hasToken()) return

    try {
      const resp = await this.apiClient.getConversationMessages(activeConv.id, 100, 0)
      if (resp && Array.isArray(resp.messages) && resp.messages.length > 0) {
        const chatMessages: ChatMessage[] = []
        const rawMsgs = resp.messages

        // Collect all completed turn IDs
        const completedTurnIds = new Set<string>()
        for (const m of rawMsgs) {
          if (m.turn_status === 'completed' && m.turn_id) {
            completedTurnIds.add(m.turn_id)
          }
        }

        for (let i = 0; i < rawMsgs.length; i++) {
          const m = rawMsgs[i]
          const meta = (m.metadata || {}) as Record<string, unknown>
          const isFailed = m.turn_status === 'failed' || m.turn_status === 'cancelled'
          const isUser = m.role === 'user'

          if (isUser && isFailed) {
            const isCompletedTurn = completedTurnIds.has(m.turn_id)
            const userMsg: ChatMessage = {
              id: m.id,
              role: 'user',
              content: m.content,
              timestamp: new Date(m.created_at).getTime() || Date.now(),
              idempotencyKey: (meta.idempotency_key as string) || undefined,
              originalOptions: (meta.options as ChatMessage['originalOptions']) || undefined,
              turnId: m.turn_id,
              turnStatus: m.turn_status || undefined,
              turnErrorCode: m.turn_error_code || undefined,
              isFailedAttempt: true,
            }
            chatMessages.push(userMsg)

            // Check if there is an assistant reply following this message
            const nextMsg = rawMsgs[i + 1]
            const hasDirectAssistantReply =
              nextMsg && nextMsg.role === 'assistant' && nextMsg.turn_id === m.turn_id

            // Check if any later user turn in rawMsgs retried this prompt successfully
            let subsequentlyRetried = false
            for (let j = i + 1; j < rawMsgs.length; j++) {
              if (rawMsgs[j].role === 'user' && rawMsgs[j].content === m.content) {
                subsequentlyRetried = true
                break
              }
            }

            if (!hasDirectAssistantReply && !isCompletedTurn && !subsequentlyRetried) {
              const errReason = m.turn_error_code || 'Generation failed or lease expired'
              const assistantError: ChatMessage = {
                id: `err-${m.id}`,
                role: 'assistant',
                content: `Error: Generation did not complete (${errReason}).`,
                error: `Generation did not complete (${errReason}).`,
                timestamp: (new Date(m.created_at).getTime() || Date.now()) + 500,
                turnId: m.turn_id,
                turnStatus: m.turn_status || undefined,
                turnErrorCode: m.turn_error_code || undefined,
              }
              chatMessages.push(assistantError)
            }
          } else {
            const chatMsg: ChatMessage = {
              id: m.id,
              role: m.role,
              content: m.content,
              timestamp: new Date(m.created_at).getTime() || Date.now(),
              idempotencyKey: (meta.idempotency_key as string) || undefined,
              originalOptions: (meta.options as ChatMessage['originalOptions']) || undefined,
              turnId: m.turn_id,
              turnStatus: m.turn_status || undefined,
              turnErrorCode: m.turn_error_code || undefined,
            }
            if (m.role === 'assistant') {
              // Preserve existing responseMeta if available locally, or reconstruct from message metadata
              const existingMatch = activeConv.messages.find(
                (ex) =>
                  (ex.turnId && ex.turnId === m.turn_id && ex.role === 'assistant') ||
                  ex.id === m.id
              )
              if (existingMatch?.responseMeta) {
                chatMsg.responseMeta = existingMatch.responseMeta
              } else if (meta && (meta.model || meta.cache_type || meta.fingerprint)) {
                chatMsg.responseMeta = {
                  response: m.content,
                  cached: (meta.cached as boolean) ?? false,
                  cache_type: (meta.cache_type as QueryResponse['cache_type']) ?? 'none',
                  model: (meta.model as string) ?? '',
                  latency_ms: (meta.latency_ms as number) ?? 0,
                  fingerprint: (meta.fingerprint as string) ?? '',
                  conversation_id: activeConv.id,
                  turn_id: m.turn_id,
                  context_status: (meta.context_status as QueryResponse['context_status']) ?? null,
                  created_at: m.created_at,
                  cache_hit_count: 0,
                  usage: (meta.usage as QueryResponse['usage']) ?? {
                    prompt_tokens: 0,
                    completion_tokens: 0,
                    total_tokens: 0,
                  },
                }
              }
            }
            chatMessages.push(chatMsg)
          }
        }
        this.conversationManager.setConversationMessages(activeConv.id, chatMessages)
      }
    } catch {
      // Non-blocking fallback
    }
  }

  public async handleNewChat(): Promise<void> {
    if (this.connectionInfo.state === 'connected' && this.apiClient.hasToken()) {
      try {
        const created = await this.apiClient.createConversation('New Conversation')
        if (created && created.id) {
          this.conversationManager.createConversationWithId(
            created.id,
            created.title || 'New Conversation'
          )
          this.renderConversationList()
          this.renderMessages()
          if (this.chatTextarea) {
            this.chatTextarea.value = ''
            this.autoResizeTextarea()
            this.chatTextarea.focus()
          }
          this.closeMobileSidebar()
          return
        }
      } catch {
        // Fallback to local session
      }
    }
    this.conversationManager.createConversation('New Conversation')
    this.renderConversationList()
    this.renderMessages()
    if (this.chatTextarea) {
      this.chatTextarea.value = ''
      this.autoResizeTextarea()
      this.chatTextarea.focus()
    }
    this.closeMobileSidebar()
  }

  public async handleSwitchConversation(id: string): Promise<void> {
    const success = this.conversationManager.setActiveConversation(id)
    if (success) {
      if (this.apiClient.hasToken()) {
        await this.loadMessagesForActiveConversation()
      }
      this.renderConversationList()
      this.renderMessages()
      this.closeMobileSidebar()
      this.chatTextarea?.focus()
    }
  }

  public async handleDeleteConversation(id: string): Promise<void> {
    if (this.connectionInfo.state === 'connected' && this.apiClient.hasToken()) {
      try {
        await this.apiClient.deleteConversation(id)
      } catch (err) {
        console.error('Failed to delete conversation on server:', err)
        return
      }
    }

    // Server deletion succeeded or offline: remove local session without auto-creating placeholder
    this.conversationManager.deleteConversation(id, false)
    const remaining = this.conversationManager.getConversations()

    if (remaining.length === 0) {
      if (this.connectionInfo.state === 'connected' && this.apiClient.hasToken()) {
        try {
          const created = await this.apiClient.createConversation('New Conversation')
          if (created && created.id) {
            this.conversationManager.createConversationWithId(
              created.id,
              created.title || 'New Conversation'
            )
          }
        } catch {
          this.conversationManager.createConversation('New Conversation')
        }
      } else {
        this.conversationManager.createConversation('New Conversation')
      }
    }

    this.renderConversationList()
    this.renderMessages()
  }

  public async handleSend(): Promise<void> {
    const text = this.chatTextarea.value.trim()
    if (!text || this.isGenerating) return

    const activeConv = this.conversationManager.getActiveConversation()
    const idempotencyKey = crypto.randomUUID()
    const bypass = this.bypassCheckbox.checked

    const userMessage: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: text,
      timestamp: Date.now(),
      idempotencyKey,
      originalOptions: {
        bypass_cache: bypass,
      },
    }

    this.conversationManager.addMessage(activeConv.id, userMessage)
    this.chatTextarea.value = ''
    this.autoResizeTextarea()

    this.isGenerating = true
    this.sendBtn.disabled = true
    this.renderConversationList()
    this.renderMessages()

    const payload: QueryRequestPayload = {
      bypass_cache: bypass,
    }

    if (this.mode === 'single-turn') {
      // Single-turn mode sends prompt and omits conversation_id for semantic cache eligibility
      payload.prompt = text
    } else {
      // Multi-turn mode sends conversation_id and prompt; conversation history is reconstructed server-side
      if (this.connectionInfo.state === 'connected' && this.apiClient.hasToken()) {
        try {
          await this.apiClient.getConversation(activeConv.id)
        } catch (e: unknown) {
          const err = e as { status?: number }
          if (err?.status === 404) {
            try {
              await this.apiClient.createConversation(
                activeConv.title || 'New Conversation',
                activeConv.id
              )
            } catch {
              // Ignore if already created
            }
          }
        }
      }
      payload.conversation_id = activeConv.id
      payload.prompt = text
    }

    try {
      const response = await this.apiClient.sendQuery(payload, idempotencyKey)
      const assistantMessage: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: response.response,
        timestamp: Date.now(),
        responseMeta: response,
      }
      this.conversationManager.addMessage(activeConv.id, assistantMessage)

      // Silently refresh metrics in background
      this.metricsController.refresh().catch(() => {})
    } catch (err) {
      let errMsg = 'Failed to execute query.'
      if (err instanceof GatewayApiError) {
        errMsg = err.message
      } else if (err instanceof Error) {
        errMsg = err.message
      }

      const assistantError: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: `Error: ${errMsg}`,
        timestamp: Date.now(),
        error: errMsg,
      }
      this.conversationManager.addMessage(activeConv.id, assistantError)
    } finally {
      this.isGenerating = false
      this.sendBtn.disabled = false
      this.renderConversationList()
      this.renderMessages()
      this.chatTextarea?.focus()
    }
  }

  public async handleRetry(failedMsgId: string): Promise<void> {
    if (this.isGenerating) return

    const activeConv = this.conversationManager.getActiveConversation()
    const messages = activeConv.messages

    // Find the failed message
    const failedIndex = messages.findIndex((m) => m.id === failedMsgId)
    if (failedIndex === -1) return

    // Find the immediately preceding user message to retry, its original idempotency key, and original options
    let promptText = ''
    let retryIdempotencyKey: string | undefined
    let originalOptions: ChatMessage['originalOptions'] | undefined
    for (let i = failedIndex - 1; i >= 0; i--) {
      if (messages[i].role === 'user') {
        promptText = messages[i].content
        retryIdempotencyKey = messages[i].idempotencyKey
        originalOptions = messages[i].originalOptions
        break
      }
    }

    if (!promptText) return

    // Remove the error message from the conversation without duplicating the user turn
    this.conversationManager.removeMessage(activeConv.id, failedMsgId)

    this.isGenerating = true
    this.sendBtn.disabled = true
    this.renderMessages()

    const bypass =
      originalOptions?.bypass_cache !== undefined
        ? originalOptions.bypass_cache
        : this.bypassCheckbox.checked
    const payload: QueryRequestPayload = {
      bypass_cache: bypass,
    }

    if (this.mode === 'single-turn') {
      payload.prompt = promptText
    } else {
      payload.conversation_id = activeConv.id
      payload.prompt = promptText
    }

    try {
      // Reuse the preceding turn's idempotency key so server deduplicates / recovers cleanly
      const idempotencyKey = retryIdempotencyKey || crypto.randomUUID()
      const response = await this.apiClient.sendQuery(payload, idempotencyKey)

      if (
        this.mode !== 'single-turn' &&
        this.connectionInfo.state === 'connected' &&
        this.apiClient.hasToken()
      ) {
        // Reconcile from server so that the persisted attempt history matches exactly
        await this.loadMessagesForActiveConversation()
        const currentConv = this.conversationManager.getActiveConversation()
        const lastMsg = currentConv.messages[currentConv.messages.length - 1]
        if (lastMsg && lastMsg.role === 'assistant') {
          lastMsg.responseMeta = response
        } else {
          const assistantMessage: ChatMessage = {
            id: crypto.randomUUID(),
            role: 'assistant',
            content: response.response,
            timestamp: Date.now(),
            responseMeta: response,
          }
          this.conversationManager.addMessage(activeConv.id, assistantMessage)
        }
      } else {
        const assistantMessage: ChatMessage = {
          id: crypto.randomUUID(),
          role: 'assistant',
          content: response.response,
          timestamp: Date.now(),
          responseMeta: response,
        }
        this.conversationManager.addMessage(activeConv.id, assistantMessage)
      }
      this.metricsController.refresh().catch(() => {})
    } catch (err) {
      let errMsg = 'Failed to execute query.'
      if (err instanceof GatewayApiError) {
        errMsg = err.message
      } else if (err instanceof Error) {
        errMsg = err.message
      }

      const assistantError: ChatMessage = {
        id: crypto.randomUUID(),
        role: 'assistant',
        content: `Error: ${errMsg}`,
        timestamp: Date.now(),
        error: errMsg,
      }
      this.conversationManager.addMessage(activeConv.id, assistantError)
    } finally {
      this.isGenerating = false
      this.sendBtn.disabled = false
      this.renderConversationList()
      this.renderMessages()
    }
  }

  private handleCopyResponse(msgId: string, buttonEl: HTMLButtonElement): void {
    const activeConv = this.conversationManager.getActiveConversation()
    const msg = activeConv.messages.find((m) => m.id === msgId)
    if (!msg) return

    navigator.clipboard
      .writeText(msg.content)
      .then(() => {
        const originalText = buttonEl.innerHTML
        buttonEl.innerHTML = '✓ Copied!'
        buttonEl.classList.add('copied')
        setTimeout(() => {
          buttonEl.innerHTML = originalText
          buttonEl.classList.remove('copied')
        }, 2000)
      })
      .catch(() => {})
  }

  private handleCopyCode(buttonEl: HTMLButtonElement): void {
    const codeWrapper = buttonEl.closest('.code-block-wrapper')
    const codeEl = codeWrapper?.querySelector('code')
    if (!codeEl) return

    const codeText = codeEl.textContent || ''
    navigator.clipboard
      .writeText(codeText)
      .then(() => {
        const originalText = buttonEl.textContent
        buttonEl.textContent = 'Copied!'
        buttonEl.classList.add('copied')
        setTimeout(() => {
          buttonEl.textContent = originalText
          buttonEl.classList.remove('copied')
        }, 2000)
      })
      .catch(() => {})
  }

  private handleCopyFingerprint(fp: string, buttonEl: HTMLButtonElement): void {
    if (!fp) return
    navigator.clipboard
      .writeText(fp)
      .then(() => {
        const originalText = buttonEl.textContent
        buttonEl.textContent = 'Copied!'
        buttonEl.classList.add('copied')
        setTimeout(() => {
          buttonEl.textContent = originalText
          buttonEl.classList.remove('copied')
        }, 2000)
      })
      .catch(() => {})
  }

  private renderConversationList(): void {
    if (!this.conversationListEl) return
    const conversations = this.conversationManager.getConversations()
    const activeId = this.conversationManager.getActiveConversationId()

    if (conversations.length === 0) {
      this.conversationListEl.innerHTML = `
        <div class="sidebar-empty">
          <span>No conversations yet.</span>
        </div>
      `
      return
    }

    let html = ''
    for (const conv of conversations) {
      const isActive = conv.id === activeId
      const activeClass = isActive ? 'active' : ''
      const safeTitle = MarkdownRenderer.escapeHtml(conv.title)

      html += `
        <div class="conversation-item ${activeClass}" data-conv-id="${conv.id}" role="button" tabindex="0">
          <div class="conv-item-left">
            <span class="conv-icon">💬</span>
            <span class="conv-title" title="${safeTitle}">${safeTitle}</span>
          </div>
          <button class="btn-delete-conv" type="button" data-conv-id="${conv.id}" title="Delete conversation" aria-label="Delete conversation">
            ×
          </button>
        </div>
      `
    }

    this.conversationListEl.innerHTML = html
  }

  private renderMessages(): void {
    if (!this.messagesContainerEl) return
    const activeConv = this.conversationManager.getActiveConversation()
    const html = MessageRenderer.renderMessages(activeConv.messages, this.isGenerating)
    this.messagesContainerEl.innerHTML = html

    // Auto scroll to bottom
    this.messagesContainerEl.scrollTop = this.messagesContainerEl.scrollHeight
  }
}
