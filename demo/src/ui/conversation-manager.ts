/**
 * In-Memory Conversation Session Manager for the IntelliCache Demonstration Chatbot.
 *
 * Implements a strict memory-only conversation session model.
 * NO conversation titles, IDs, prompts, or assistant responses are ever written to
 * window.localStorage, window.sessionStorage, IndexedDB, or cookies.
 */

import type { ChatMessage, ConversationSession, ConversationSummary } from '../types'

export class ConversationManager {
  private sessions: Map<string, ConversationSession> = new Map()
  private activeId: string

  constructor() {
    const initialSession = this.createNewSession('New Conversation')
    this.sessions.set(initialSession.id, initialSession)
    this.activeId = initialSession.id
  }

  private createNewSession(title: string = 'New Conversation'): ConversationSession {
    const id = crypto.randomUUID()
    const now = Date.now()
    return {
      id,
      title,
      createdAt: now,
      updatedAt: now,
      messages: [],
    }
  }

  public createConversation(title?: string): ConversationSession {
    const session = this.createNewSession(title || 'New Conversation')
    this.sessions.set(session.id, session)
    this.activeId = session.id
    return session
  }

  public createConversationWithId(id: string, title?: string): ConversationSession {
    const now = Date.now()
    const session: ConversationSession = {
      id,
      title: title || 'New Conversation',
      createdAt: now,
      updatedAt: now,
      messages: [],
    }
    this.sessions.set(session.id, session)
    this.activeId = session.id
    return session
  }

  public getConversations(): ConversationSession[] {
    return Array.from(this.sessions.values()).sort((a, b) => b.updatedAt - a.updatedAt)
  }

  public getActiveConversation(): ConversationSession {
    let session = this.sessions.get(this.activeId)
    if (!session) {
      session = this.createConversation()
    }
    return session
  }

  public getActiveConversationId(): string {
    return this.activeId
  }

  public setActiveConversation(id: string): boolean {
    if (this.sessions.has(id)) {
      this.activeId = id
      return true
    }
    return false
  }

  public deleteConversation(id: string): boolean {
    if (!this.sessions.has(id)) {
      return false
    }

    this.sessions.delete(id)

    // If the active conversation was deleted, switch to the most recently updated,
    // or create a new clean conversation if none remain.
    if (this.activeId === id) {
      const remaining = this.getConversations()
      if (remaining.length > 0) {
        this.activeId = remaining[0].id
      } else {
        const fresh = this.createConversation()
        this.activeId = fresh.id
      }
    }
    return true
  }

  public clearAll(): void {
    this.sessions.clear()
    const fresh = this.createConversation()
    this.activeId = fresh.id
  }

  public hasConversation(id: string): boolean {
    return this.sessions.has(id)
  }

  public setConversationsFromServer(conversations: ConversationSummary[]): void {
    if (!conversations || conversations.length === 0) return

    for (const c of conversations) {
      const existing = this.sessions.get(c.id)
      const updatedAt = new Date(c.updated_at || c.created_at).getTime()
      const createdAt = new Date(c.created_at).getTime()
      if (existing) {
        existing.title = c.title || existing.title
        existing.updatedAt = updatedAt
      } else {
        this.sessions.set(c.id, {
          id: c.id,
          title: c.title || 'New Conversation',
          createdAt: Number.isFinite(createdAt) ? createdAt : Date.now(),
          updatedAt: Number.isFinite(updatedAt) ? updatedAt : Date.now(),
          messages: [],
        })
      }
    }

    if (!this.sessions.has(this.activeId) && conversations.length > 0) {
      this.activeId = conversations[0].id
    }
  }

  public setConversationMessages(id: string, messages: ChatMessage[]): void {
    const session = this.sessions.get(id)
    if (session) {
      session.messages = [...messages]
      session.updatedAt = Date.now()
    }
  }

  public addMessage(convId: string, message: ChatMessage): void {
    const session = this.sessions.get(convId)
    if (!session) return

    session.messages.push(message)
    session.updatedAt = Date.now()

    // Auto-derive title from first user message if title is still default
    if (
      message.role === 'user' &&
      (session.title === 'New Conversation' || session.title === 'New Chat')
    ) {
      this.updateTitleFromFirstMessage(convId, message.content)
    }
  }

  public removeLastMessage(convId: string): ChatMessage | undefined {
    const session = this.sessions.get(convId)
    if (!session || session.messages.length === 0) return undefined

    const removed = session.messages.pop()
    session.updatedAt = Date.now()
    return removed
  }

  public removeMessage(convId: string, messageId: string): boolean {
    const session = this.sessions.get(convId)
    if (!session) return false

    const initialLen = session.messages.length
    session.messages = session.messages.filter((m) => m.id !== messageId)
    if (session.messages.length !== initialLen) {
      session.updatedAt = Date.now()
      return true
    }
    return false
  }

  public getMessages(convId: string): ChatMessage[] {
    const session = this.sessions.get(convId)
    return session ? [...session.messages] : []
  }

  public updateTitleFromFirstMessage(convId: string, text: string): void {
    const session = this.sessions.get(convId)
    if (!session) return

    const title = ConversationManager.formatTitle(text)
    if (title) {
      session.title = title
    }
  }

  public static formatTitle(text: string, maxLength: number = 36): string {
    const cleaned = (text || '')
      .replace(/[\r\n\t]+/g, ' ')
      .replace(/\s+/g, ' ')
      .trim()

    if (!cleaned) return 'New Conversation'

    if (cleaned.length <= maxLength) {
      return cleaned
    }

    return `${cleaned.slice(0, maxLength).trim()}...`
  }
}
