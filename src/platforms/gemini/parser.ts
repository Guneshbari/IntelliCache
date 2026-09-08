/**
 * Pure DOM parsing and extraction utilities for Gemini (gemini.google.com) conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

import { logger } from '../../diagnostics'
import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'
import {
  extractMessageId,
  extractSourceTimestamp,
  formatCodeBlock,
  normalizeExtractedText,
  pairTurnsIntoInteractions as sharedPairTurns,
} from '../shared/parser-utils'
import { GEMINI_SELECTORS } from './selectors'

export { extractMessageId, extractSourceTimestamp }

/**
 * Extracts the conversation ID from Gemini URLs.
 * Examples:
 * - https://gemini.google.com/app/6a8617f8ce4483ee -> "6a8617f8ce4483ee"
 * - https://gemini.google.com/chat/abc-456 -> "abc-456"
 *
 * For /app with no ID (or root URLs), returns null.
 */
export function extractConversationIdFromUrl(url: string): string | null {
  try {
    const pathname = new URL(url).pathname
    const match = pathname.match(/\/(?:app|chat)\/([a-zA-Z0-9_-]+)/)
    if (match?.[1]) {
      const id = match[1].trim()
      if (id.length > 0 && id !== 'app' && id !== 'chat') {
        return id
      }
    }
    return null
  } catch {
    return null
  }
}

/**
 * Extracts conversation title from page title or DOM header.
 * Strips standard brand suffixes like " - Gemini" or " - Google Gemini".
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
    .replace(/\s*-\s*Google\s+Gemini$/i, '')
    .replace(/\s*-\s*Gemini$/i, '')
    .replace(/\s*\|\s*Gemini$/i, '')
    .trim()

  if (
    !cleaned ||
    cleaned.toLowerCase() === 'gemini' ||
    cleaned.toLowerCase() === 'google gemini' ||
    cleaned.toLowerCase() === 'new chat' ||
    cleaned.toLowerCase() === 'untitled'
  ) {
    return null
  }

  return cleaned
}

/**
 * Extracts model provider and name from the Gemini interface if available.
 */
export function extractModelInfo(root: Document | Element): {
  provider: string | null
  name: string | null
} {
  const modelSwitcher = root.querySelector(GEMINI_SELECTORS.MODEL_SELECTOR)
  let name: string | null = null

  if (modelSwitcher) {
    const text = modelSwitcher.textContent?.trim()
    if (text && text.length > 0 && text.length < 50) {
      name = text
    }
  }

  return { provider: 'google', name }
}

/**
 * Extracts raw user query text from a user turn element (<user-query>).
 * Strips UI controls and navigation while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  const textContainer = element.querySelector(GEMINI_SELECTORS.USER_TEXT) || element
  const clone = textContainer.cloneNode(true) as Element
  clone.querySelectorAll(GEMINI_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((b) => b.remove())
  return normalizeExtractedText(clone.textContent || '')
}

/**
 * Extracts assistant response text from a model response element (<model-response>).
 * Preserves code blocks with language annotations and strips interactive UI controls.
 */
export function extractAssistantResponseText(element: Element): string {
  const contentContainer = element.querySelector(GEMINI_SELECTORS.ASSISTANT_TEXT) || element
  const clone = contentContainer.cloneNode(true) as Element

  clone.querySelectorAll(GEMINI_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((el) => el.remove())

  const ownerDoc = element.ownerDocument || document
  clone.querySelectorAll(GEMINI_SELECTORS.CODE_BLOCK).forEach((pre) => {
    formatCodeBlock(pre, ownerDoc)
  })

  clone.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li').forEach((p) => {
    p.textContent = `${p.textContent || ''}\n`
  })

  return normalizeExtractedText(clone.textContent || '')
}

/**
 * Checks whether the page as a whole is actively generating / streaming.
 */
export function isPageGenerating(root: Document | Element): boolean {
  if (root.querySelector(GEMINI_SELECTORS.STOP_BUTTON) !== null) {
    logger.debug(
      'Parser',
      'GEMINI',
      `Active generation detected: stop button present ('${GEMINI_SELECTORS.STOP_BUTTON}')`
    )
    return true
  }

  if (root.querySelector(GEMINI_SELECTORS.STREAMING_INDICATORS) !== null) {
    logger.debug(
      'Parser',
      'GEMINI',
      `Active generation detected: streaming indicator present ('${GEMINI_SELECTORS.STREAMING_INDICATORS}')`
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
    turnElement.classList.contains('loading') ||
    turnElement.classList.contains('animating') ||
    turnElement.querySelector(GEMINI_SELECTORS.STREAMING_INDICATORS) !== null
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

  // Count raw DOM elements for diagnostics
  const userQueryElements = Array.from(root.querySelectorAll('user-query'))
  const modelResponseElements = Array.from(root.querySelectorAll('model-response'))

  const elements = Array.from(
    root.querySelectorAll(`${GEMINI_SELECTORS.USER_MESSAGE}, ${GEMINI_SELECTORS.ASSISTANT_MESSAGE}`)
  )

  const topElements = elements.filter((el) => {
    let parent = el.parentElement
    while (parent && parent !== root) {
      if (
        parent.matches?.(GEMINI_SELECTORS.USER_MESSAGE) ||
        parent.matches?.(GEMINI_SELECTORS.ASSISTANT_MESSAGE)
      ) {
        return false
      }
      parent = parent.parentElement
    }
    return true
  })

  let userTextsCount = 0
  let assistantTextsCount = 0

  for (const el of topElements) {
    const isUser = el.matches?.(GEMINI_SELECTORS.USER_MESSAGE)
    const isAssistant = el.matches?.(GEMINI_SELECTORS.ASSISTANT_MESSAGE)

    if (isUser) {
      const text = extractUserQueryText(el)
      if (text.length > 0) {
        userTextsCount++
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
      const text = extractAssistantResponseText(el)
      if (text.length > 0) {
        assistantTextsCount++
      }
      turns.push({
        role: 'assistant',
        element: el,
        text,
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
    'GEMINI',
    `DOM element counts | userQueries=${userQueryElements.length} | modelResponses=${modelResponseElements.length} | topLevelElements=${topElements.length} | userTexts=${userTextsCount} | assistantTexts=${assistantTextsCount} | extractedUserTurns=${userCount} | extractedAssistantTurns=${asstCount}`
  )

  if (turns.length === 0) {
    logger.debug(
      'Parser',
      'GEMINI',
      `DOM scan completed: 0 conversation turns found matching '${GEMINI_SELECTORS.USER_MESSAGE}' / '${GEMINI_SELECTORS.ASSISTANT_MESSAGE}'.`
    )
  } else if (userCount === 0) {
    logger.debug(
      'Parser',
      'GEMINI',
      `DOM scan completed: 0 user turns found (${asstCount} assistant turns found).`
    )
  } else if (asstCount === 0) {
    logger.debug(
      'Parser',
      'GEMINI',
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
  const userQueriesCount = turns.filter((t) => t.role === 'user').length
  const modelResponsesCount = turns.filter((t) => t.role === 'assistant').length

  const interactions = sharedPairTurns('gemini', turns, context)

  logger.info(
    'Parser',
    'GEMINI',
    `DOM diagnostics | userQueries=${userQueriesCount} | modelResponses=${modelResponsesCount} | completePairs=${interactions.length}`
  )

  if (interactions.length === 0 && turns.length > 0) {
    logger.debug(
      'Parser',
      'GEMINI',
      `Failed to form any complete user/assistant pairs from ${turns.length} turns.`
    )
  } else {
    logger.debug(
      'Parser',
      'GEMINI',
      `Pairing complete: formed ${interactions.length} complete interaction pair(s).`
    )
  }

  return interactions
}
