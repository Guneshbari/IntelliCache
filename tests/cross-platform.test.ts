// @vitest-environment happy-dom
import { describe, expect, it } from 'vitest'
import { namespaceConversationId } from '../src/database/types'
import { pairTurnsIntoInteractions as pairChatGPTTurns } from '../src/platforms/chatgpt/parser'
import { pairTurnsIntoInteractions as pairClaudeTurns } from '../src/platforms/claude/parser'
import { pairTurnsIntoInteractions as pairGeminiTurns } from '../src/platforms/gemini/parser'
import { getAllAdapters, getAdapterForUrl } from '../src/platforms/registry'
import type { RawMessageTurn } from '../src/platforms/types'
import { detectPlatformFromUrl } from '../src/shared/messages'

describe('Cross-Platform Normalization & Contract Tests', () => {
  it('ensures ChatGPT, Claude, and Gemini all produce the unified ExtractedInteraction contract', () => {
    const rawTurnUser: RawMessageTurn = {
      role: 'user',
      element: document.createElement('div'),
      text: 'What is semantic caching?',
      messageId: 'u-common-1',
      sourceTimestamp: '2026-08-20T03:00:00Z',
      isStreaming: false,
    }

    const rawTurnAsst: RawMessageTurn = {
      role: 'assistant',
      element: document.createElement('div'),
      text: 'It is a technique to cache responses by meaning rather than exact string matching.',
      messageId: 'a-common-1',
      sourceTimestamp: '2026-08-20T03:00:05Z',
      isStreaming: false,
    }

    const chatgptInteraction = pairChatGPTTurns([rawTurnUser, rawTurnAsst], {
      conversationId: 'conv-100',
      title: 'Semantic Caching',
      model: { provider: 'openai', name: 'GPT-4o' },
      captureContext: 'on_generate',
      observedAt: '2026-08-20T03:00:06Z',
    })[0]

    const claudeInteraction = pairClaudeTurns([rawTurnUser, rawTurnAsst], {
      conversationId: 'conv-100',
      title: 'Semantic Caching',
      model: { provider: 'claude', name: 'Claude 3.5 Sonnet' },
      captureContext: 'on_generate',
      observedAt: '2026-08-20T03:00:06Z',
    })[0]

    const geminiInteraction = pairGeminiTurns([rawTurnUser, rawTurnAsst], {
      conversationId: 'conv-100',
      title: 'Semantic Caching',
      model: { provider: 'google', name: 'Gemini 1.5 Pro' },
      captureContext: 'on_generate',
      observedAt: '2026-08-20T03:00:06Z',
    })[0]

    // Verify all required fields exist across all platform interactions
    const all = [chatgptInteraction, claudeInteraction, geminiInteraction]
    const expectedPlatforms = ['chatgpt', 'claude', 'gemini']

    all.forEach((item, index) => {
      expect(item.platform).toBe(expectedPlatforms[index])
      expect(item.conversationId).toBe('conv-100')
      expect(item.userMessageId).toBe('u-common-1')
      expect(item.messageId).toBe('a-common-1')
      expect(item.queryText).toBe('What is semantic caching?')
      expect(item.responseText).toBe(
        'It is a technique to cache responses by meaning rather than exact string matching.'
      )
      expect(item.conversationTitle).toBe('Semantic Caching')
      expect(item.observedAt).toBe('2026-08-20T03:00:06Z')
      expect(item.sourceTimestamp).toBe('2026-08-20T03:00:05Z')
      expect(item.captureContext).toBe('on_generate')
      expect(item.model).toBeDefined()
      expect(item.model.provider).toBeDefined()
    })
  })

  it('guarantees identical raw conversation IDs across platforms never collide due to namespacing', () => {
    const rawId = 'shared-conv-uuid-12345'

    const chatgptNamespaced = namespaceConversationId('chatgpt', rawId)
    const claudeNamespaced = namespaceConversationId('claude', rawId)
    const geminiNamespaced = namespaceConversationId('gemini', rawId)

    expect(chatgptNamespaced).toBe('chatgpt:shared-conv-uuid-12345')
    expect(claudeNamespaced).toBe('claude:shared-conv-uuid-12345')
    expect(geminiNamespaced).toBe('gemini:shared-conv-uuid-12345')

    // All distinct
    const set = new Set([chatgptNamespaced, claudeNamespaced, geminiNamespaced])
    expect(set.size).toBe(3)
  })

  it('correctly maps URLs to their respective platform adapters in the registry', () => {
    const chatgptAdapter = getAdapterForUrl('https://chatgpt.com/c/123')
    expect(chatgptAdapter?.platform).toBe('chatgpt')

    const chatgptLegacy = getAdapterForUrl('https://chat.openai.com/c/456')
    expect(chatgptLegacy?.platform).toBe('chatgpt')

    const claudeAdapter = getAdapterForUrl('https://claude.ai/chat/789')
    expect(claudeAdapter?.platform).toBe('claude')

    const geminiAdapter = getAdapterForUrl('https://gemini.google.com/app/abc')
    expect(geminiAdapter?.platform).toBe('gemini')

    const unsupported = getAdapterForUrl('https://bing.com/chat')
    expect(unsupported).toBeNull()
  })

  it('verifies detectPlatformFromUrl accurately categorizes all supported and unsupported hosts', () => {
    expect(detectPlatformFromUrl('https://chatgpt.com/c/1')).toBe('chatgpt')
    expect(detectPlatformFromUrl('https://chat.openai.com/c/2')).toBe('chatgpt')
    expect(detectPlatformFromUrl('https://claude.ai/chat/3')).toBe('claude')
    expect(detectPlatformFromUrl('https://gemini.google.com/app/4')).toBe('gemini')
    expect(detectPlatformFromUrl('https://google.com')).toBe('unknown')
    expect(detectPlatformFromUrl('https://github.com')).toBe('unknown')
    expect(detectPlatformFromUrl('invalid-url-string')).toBe('unknown')
  })

  it('confirms all 3 adapters are registered in registry', () => {
    const adapters = getAllAdapters()
    const platforms = adapters.map((a) => a.platform)
    expect(platforms).toContain('chatgpt')
    expect(platforms).toContain('claude')
    expect(platforms).toContain('gemini')
  })
})
