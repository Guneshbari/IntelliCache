/**
 * Pure DOM parsing and extraction utilities for Claude (claude.ai) conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

import { logger } from '../../diagnostics'
import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'
import {
  cleanQueryText,
  cleanResponseText,
  extractMessageId,
  extractSourceTimestamp,
  formatCodeBlock,
  normalizeExtractedText,
  pairTurnsIntoInteractions as sharedPairTurns,
} from '../shared/parser-utils'
import { CLAUDE_SELECTORS } from './selectors'

export { extractMessageId, extractSourceTimestamp }

/**
 * Extracts the UUID or slug conversation ID from Claude URLs.
 * Examples:
 * - https://claude.ai/chat/6a8617f8-ce44-83ee-b5b6-72eb43d13516 -> "6a8617f8-ce44-83ee-b5b6-72eb43d13516"
 * - https://claude.ai/project/proj_123/chat/abc-456 -> "abc-456"
 */
export function extractConversationIdFromUrl(url: string): string | null {
  try {
    const pathname = new URL(url).pathname
    const match = pathname.match(/\/chat\/([a-zA-Z0-9_-]+)/)
    return match?.[1] ?? null
  } catch {
    return null
  }
}

/**
 * Extracts conversation title from page title or DOM header.
 * Strips standard brand suffixes like " - Claude" or " | Claude".
 */
export function extractConversationTitle(docOrElement: Document | Element): string | null {
  let title = ''

  if ('title' in docOrElement && typeof docOrElement.title === 'string') {
    title = docOrElement.title
  } else if (docOrElement.ownerDocument && typeof docOrElement.ownerDocument.title === 'string') {
    title = docOrElement.ownerDocument.title
  }

  if (!title) return null

  const cleaned = title
    .replace(/\s*-\s*Claude$/i, '')
    .replace(/\s*\|\s*Claude$/i, '')
    .trim()

  if (
    !cleaned ||
    cleaned.toLowerCase() === 'claude' ||
    cleaned.toLowerCase() === 'new chat' ||
    cleaned.toLowerCase() === 'untitled'
  ) {
    return null
  }

  return cleaned
}

/**
 * Extracts model provider and name from the Claude interface if available.
 */
export function extractModelInfo(root: Document | Element): {
  provider: string | null
  name: string | null
} {
  const modelSwitcher = root.querySelector(CLAUDE_SELECTORS.MODEL_SELECTOR)
  let name: string | null = null

  if (modelSwitcher) {
    const text = modelSwitcher.textContent?.trim()
    if (text && text.length > 0 && text.length < 50) {
      name = text
    }
  }

  return { provider: 'claude', name }
}

/**
 * Extracts raw user query text from a user turn element.
 * Strips UI controls and navigation while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  const clone = element.cloneNode(true) as Element
  clone.querySelectorAll(CLAUDE_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((b) => b.remove())
  return cleanQueryText(normalizeExtractedText(clone.textContent || ''))
}

/**
 * Extracts assistant response text while preserving code blocks with language annotations.
 * Strips interactive action buttons, copy buttons, and toolbars.
 */
export function extractAssistantResponseText(element: Element): string {
  const clone = element.cloneNode(true) as Element

  clone.querySelectorAll(CLAUDE_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((el) => el.remove())

  const ownerDoc = element.ownerDocument || document
  clone.querySelectorAll(CLAUDE_SELECTORS.CODE_BLOCK).forEach((pre) => {
    formatCodeBlock(pre, ownerDoc)
  })

  clone.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li').forEach((p) => {
    p.textContent = `${p.textContent || ''}\n`
  })

  return cleanResponseText(normalizeExtractedText(clone.textContent || ''))
}

/**
 * Checks whether the page as a whole is actively generating / streaming.
 */
export function isPageGenerating(root: Document | Element): boolean {
  if (root.querySelector(CLAUDE_SELECTORS.STOP_BUTTON) !== null) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `Active generation detected: stop button present ('${CLAUDE_SELECTORS.STOP_BUTTON}')`
    )
    return true
  }

  if (root.querySelector(CLAUDE_SELECTORS.STREAMING_INDICATORS) !== null) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `Active generation detected: streaming indicator present ('${CLAUDE_SELECTORS.STREAMING_INDICATORS}')`
    )
    return true
  }

  return false
}

/**
 * Checks whether an assistant turn is actively streaming / generating.
 */
export function isTurnStreaming(turnElement: Element, root?: Document | Element): boolean {
  if (
    turnElement.classList.contains('streaming') ||
    turnElement.classList.contains('animate-pulse') ||
    turnElement.getAttribute('data-is-streaming') === 'true' ||
    turnElement.querySelector(CLAUDE_SELECTORS.STREAMING_INDICATORS) !== null
  ) {
    return true
  }

  const context = root || turnElement.ownerDocument || document
  return isPageGenerating(context)
}

/**
 * Extracts raw conversation turns (User and Assistant) in document order from a root container.
 */
export function extractConversationTurns(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  const elements = Array.from(
    root.querySelectorAll(`${CLAUDE_SELECTORS.USER_MESSAGE}, ${CLAUDE_SELECTORS.ASSISTANT_MESSAGE}`)
  )

  const topElements = elements.filter((el) => {
    let parent = el.parentElement
    while (parent && parent !== root) {
      if (
        parent.matches?.(CLAUDE_SELECTORS.USER_MESSAGE) ||
        parent.matches?.(CLAUDE_SELECTORS.ASSISTANT_MESSAGE)
      ) {
        return false
      }
      parent = parent.parentElement
    }
    return true
  })

  logger.debug(
    'Parser',
    'CLAUDE',
    `Message elements query: matched=${elements.length}, topLevel=${topElements.length}`
  )

  for (const el of topElements) {
    const isUser = el.matches?.(CLAUDE_SELECTORS.USER_MESSAGE)
    const isAssistant = el.matches?.(CLAUDE_SELECTORS.ASSISTANT_MESSAGE)

    if (isUser) {
      const text = extractUserQueryText(el)
      if (text.length > 0) {
        turns.push({
          role: 'user',
          element: el,
          text,
          messageId: extractMessageId(el),
          sourceTimestamp: extractSourceTimestamp(el),
          isStreaming: false,
        })
      }
    } else if (isAssistant) {
      turns.push({
        role: 'assistant',
        element: el,
        text: extractAssistantResponseText(el),
        messageId: extractMessageId(el),
        sourceTimestamp: extractSourceTimestamp(el),
        isStreaming: isTurnStreaming(el, root),
      })
    }
  }

  const userCount = turns.filter((t) => t.role === 'user').length
  const asstCount = turns.filter((t) => t.role === 'assistant').length
  logger.debug(
    'Parser',
    'CLAUDE',
    `Turn extraction complete: total=${turns.length}, userTurns=${userCount}, assistantTurns=${asstCount}`
  )

  if (turns.length === 0) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `DOM scan completed: 0 conversation turns found matching '${CLAUDE_SELECTORS.USER_MESSAGE}' / '${CLAUDE_SELECTORS.ASSISTANT_MESSAGE}'.`
    )
  } else if (userCount === 0) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `DOM scan completed: 0 user turns found (${asstCount} assistant turns found).`
    )
  } else if (asstCount === 0) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `DOM scan completed: 0 assistant turns found (${userCount} user turns found).`
    )
  }

  return turns
}

/**
 * Pairs sequential user queries and assistant responses into complete interactions.
 * Ensures that partial or streaming assistant responses are NOT paired.
 */
export function pairTurnsIntoInteractions(
  turns: RawMessageTurn[],
  context: {
    conversationId: string | null
    title: string | null
    model: { provider: string | null; name: string | null }
    captureContext?: CaptureContext
    observedAt?: string
  }
): ExtractedInteraction[] {
  const interactions = sharedPairTurns('claude', turns, context)

  if (interactions.length === 0 && turns.length > 0) {
    logger.debug(
      'Parser',
      'CLAUDE',
      `Failed to form any complete user/assistant pairs from ${turns.length} turns.`
    )
  } else {
    logger.debug(
      'Parser',
      'CLAUDE',
      `Pairing complete: formed ${interactions.length} complete interaction pair(s).`
    )
  }

  return interactions
}
