// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import type { ChatMessage } from '../demo/src/types'
import { ConversationManager } from '../demo/src/ui/conversation-manager'

describe('ConversationManager (In-Memory Session Model)', () => {
  let manager: ConversationManager

  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    document.cookie = ''
    manager = new ConversationManager()
  })

  it('initializes with a default conversation session and valid UUID', () => {
    const active = manager.getActiveConversation()
    expect(active).toBeDefined()
    expect(active.id).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i)
    expect(active.title).toBe('New Conversation')
    expect(active.messages).toEqual([])
    expect(manager.getConversations().length).toBe(1)
  })

  it('creates new independent conversations with unique IDs', () => {
    const session1 = manager.getActiveConversation()
    const session2 = manager.createConversation('Second Conversation')

    expect(session2.id).not.toBe(session1.id)
    expect(session2.title).toBe('Second Conversation')
    expect(manager.getActiveConversationId()).toBe(session2.id)
    expect(manager.getConversations().length).toBe(2)
  })

  it('switches between conversations and preserves separate message histories', () => {
    const conv1 = manager.getActiveConversation()
    const msg1: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: 'Question 1 in Conv 1',
      timestamp: Date.now(),
    }
    manager.addMessage(conv1.id, msg1)

    const conv2 = manager.createConversation('Conv 2')
    const msg2: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: 'Question 2 in Conv 2',
      timestamp: Date.now(),
    }
    manager.addMessage(conv2.id, msg2)

    expect(manager.getMessages(conv1.id).length).toBe(1)
    expect(manager.getMessages(conv1.id)[0].content).toBe('Question 1 in Conv 1')

    expect(manager.getMessages(conv2.id).length).toBe(1)
    expect(manager.getMessages(conv2.id)[0].content).toBe('Question 2 in Conv 2')

    // Switch back to Conv 1
    const switched = manager.setActiveConversation(conv1.id)
    expect(switched).toBe(true)
    expect(manager.getActiveConversation().id).toBe(conv1.id)
    expect(manager.getActiveConversation().messages[0].content).toBe('Question 1 in Conv 1')
  })

  it('automatically derives conversation title from the first user message', () => {
    const conv = manager.getActiveConversation()
    expect(conv.title).toBe('New Conversation')

    const firstMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: 'How does PostgreSQL pgvector perform vector similarity searches?',
      timestamp: Date.now(),
    }
    manager.addMessage(conv.id, firstMsg)

    expect(conv.title).toBe('How does PostgreSQL pgvector perform...')

    // Second message should NOT overwrite derived title
    const secondMsg: ChatMessage = {
      id: crypto.randomUUID(),
      role: 'user',
      content: 'Another question that should not change title',
      timestamp: Date.now(),
    }
    manager.addMessage(conv.id, secondMsg)
    expect(conv.title).toBe('How does PostgreSQL pgvector perform...')
  })

  it('formats titles cleanly by stripping extra whitespace, newlines, and truncating', () => {
    expect(ConversationManager.formatTitle('Short title')).toBe('Short title')
    expect(ConversationManager.formatTitle('Line 1\nLine 2\r\nLine 3')).toBe('Line 1 Line 2 Line 3')
    expect(ConversationManager.formatTitle('   Extra   spaces   here   ')).toBe('Extra spaces here')
    expect(
      ConversationManager.formatTitle(
        'This is an exceptionally long prompt intended to test maximum title truncation length'
      )
    ).toBe('This is an exceptionally long prompt...')
  })

  it('handles deletion of the active conversation and selects next remaining session', () => {
    const conv1 = manager.getActiveConversation()
    const conv2 = manager.createConversation('Conversation 2')

    expect(manager.getActiveConversationId()).toBe(conv2.id)

    // Delete active conv2
    const deleted = manager.deleteConversation(conv2.id)
    expect(deleted).toBe(true)
    expect(manager.getActiveConversationId()).toBe(conv1.id)
    expect(manager.getConversations().length).toBe(1)
  })

  it('handles deletion of the last remaining conversation by creating a fresh one', () => {
    const conv1 = manager.getActiveConversation()
    const deleted = manager.deleteConversation(conv1.id)
    expect(deleted).toBe(true)

    const current = manager.getActiveConversation()
    expect(current).toBeDefined()
    expect(current.id).not.toBe(conv1.id)
    expect(current.title).toBe('New Conversation')
    expect(manager.getConversations().length).toBe(1)
  })

  it('supports removing specific messages or the last message for retry handling', () => {
    const conv = manager.getActiveConversation()
    const msg1: ChatMessage = { id: 'm1', role: 'user', content: 'Q1', timestamp: 100 }
    const msg2: ChatMessage = {
      id: 'm2',
      role: 'assistant',
      content: 'Error: Gateway offline',
      timestamp: 200,
      error: 'Gateway offline',
    }
    manager.addMessage(conv.id, msg1)
    manager.addMessage(conv.id, msg2)

    expect(manager.getMessages(conv.id).length).toBe(2)

    // Remove specific message
    const removed = manager.removeMessage(conv.id, 'm2')
    expect(removed).toBe(true)
    expect(manager.getMessages(conv.id).length).toBe(1)
    expect(manager.getMessages(conv.id)[0].id).toBe('m1')

    // Remove last message
    const last = manager.removeLastMessage(conv.id)
    expect(last?.id).toBe('m1')
    expect(manager.getMessages(conv.id).length).toBe(0)
  })

  it('strictly preserves memory-only semantics without touching persistent browser storage', () => {
    const conv = manager.createConversation('Confidential Research Query')
    manager.addMessage(conv.id, {
      id: crypto.randomUUID(),
      role: 'user',
      content: 'Sensitive architectural prompt text',
      timestamp: Date.now(),
    })

    // Strict assertion: Storage must remain completely empty
    expect(localStorage.length).toBe(0)
    expect(sessionStorage.length).toBe(0)
    expect(document.cookie).toBe('')
  })
})
