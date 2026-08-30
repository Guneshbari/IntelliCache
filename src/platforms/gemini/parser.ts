/**
 * Pure DOM parsing and extraction utilities for Gemini (gemini.google.com) conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

import { logger } from '../../diagnostics'
import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'
import { GEMINI_SELECTORS } from './selectors'

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
    const parsed = new URL(url)
    const pathname = parsed.pathname

    // Match /app/{id} or /chat/{id} where id is non-empty and not just 'app'/'chat'
    const appMatch = pathname.match(/\/(?:app|chat)\/([a-zA-Z0-9_-]+)/)
    if (appMatch && appMatch[1]) {
      const id = appMatch[1].trim()
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

  if (!title) {
    return null
  }

  // Clean brand suffixes
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
 * Returns provider: 'google', name: string | null.
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

  return {
    provider: 'google',
    name: name ?? null,
  }
}

/**
 * Extracts message ID attribute (`data-message-id` or similar) from an element if present.
 * Returns null if not exposed by Gemini's DOM.
 */
export function extractMessageId(element: Element): string | null {
  const directId = element.getAttribute('data-message-id')
  if (directId && directId.trim()) {
    return directId.trim()
  }

  const childWithId = element.querySelector('[data-message-id]')
  if (childWithId) {
    const childId = childWithId.getAttribute('data-message-id')
    if (childId && childId.trim()) {
      return childId.trim()
    }
  }

  return null
}

/**
 * Extracts original source timestamp from `<time datetime="...">` or `data-timestamp`
 * if exposed by the platform DOM. Never fabricates timestamps.
 */
export function extractSourceTimestamp(element: Element): string | null {
  const timeEl = element.querySelector('time[datetime]')
  if (timeEl) {
    const dt = timeEl.getAttribute('datetime')
    if (dt && dt.trim()) {
      return dt.trim()
    }
  }
  const timestampAttr = element.getAttribute('data-timestamp')
  if (timestampAttr && timestampAttr.trim()) {
    return timestampAttr.trim()
  }
  const childWithTimestamp = element.querySelector('[data-timestamp]')
  if (childWithTimestamp) {
    const ts = childWithTimestamp.getAttribute('data-timestamp')
    if (ts && ts.trim()) {
      return ts.trim()
    }
  }
  return null
}

/**
 * Extracts raw user query text from a user turn element (<user-query>).
 * Strips UI controls and navigation while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  // Target .query-content inside user-query if available, else element itself
  const textContainer = element.querySelector(GEMINI_SELECTORS.USER_TEXT) || element
  const clone = textContainer.cloneNode(true) as Element

  // Strip UI controls, action toolbars, copy buttons, etc.
  const uiControls = clone.querySelectorAll(GEMINI_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((b) => b.remove())

  const rawText = clone.textContent || ''

  return rawText
    .replace(/\r\n|\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * Extracts assistant response text from a model response element (<model-response>).
 * Preserves code blocks with language annotations and strips interactive UI controls.
 */
export function extractAssistantResponseText(element: Element): string {
  // Target message-content .markdown or .markdown inside model-response if available
  const contentContainer = element.querySelector(GEMINI_SELECTORS.ASSISTANT_TEXT) || element
  const clone = contentContainer.cloneNode(true) as Element

  // Strip UI controls (copy buttons, feedback icons, toolbars)
  const uiControls = clone.querySelectorAll(GEMINI_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((el) => el.remove())

  // Format code blocks before getting textContent
  const codeBlocks = clone.querySelectorAll(GEMINI_SELECTORS.CODE_BLOCK)
  codeBlocks.forEach((pre) => {
    const codeElement = pre.querySelector('code')
    const rawCode = codeElement ? codeElement.textContent || '' : pre.textContent || ''

    // Detect language from class (e.g. "language-python" -> "python")
    let lang = ''
    if (codeElement && codeElement.className) {
      const match = codeElement.className.match(/language-([a-zA-Z0-9_-]+)/)
      if (match && match[1]) {
        lang = match[1]
      }
    }

    // Replace <pre> with a formatted text node
    const formattedBlock = `\n\`\`\`${lang}\n${rawCode.trim()}\n\`\`\`\n`
    const textNode = (element.ownerDocument || document).createTextNode(formattedBlock)
    pre.replaceWith(textNode)
  })

  // Format paragraph line breaks properly
  const paragraphs = clone.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li')
  paragraphs.forEach((p) => {
    p.textContent = `${p.textContent || ''}\n`
  })

  const rawText = clone.textContent || ''

  // Normalize excessive newlines from block concatenation
  return rawText
    .replace(/\r\n|\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * Checks whether the page as a whole is actively generating / streaming.
 */
export function isPageGenerating(root: Document | Element): boolean {
  // 1. Check for stop button
  const stopButton = root.querySelector(GEMINI_SELECTORS.STOP_BUTTON)
  if (stopButton !== null) {
    logger.debug(
      'Parser',
      'GEMINI',
      `Active generation detected: stop button present ('${GEMINI_SELECTORS.STOP_BUTTON}')`
    )
    return true
  }

  // 2. Check for streaming indicators
  const streamingEl = root.querySelector(GEMINI_SELECTORS.STREAMING_INDICATORS)
  if (streamingEl !== null) {
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

  // Query all user and assistant message containers in document order
  const elements = Array.from(
    root.querySelectorAll(`${GEMINI_SELECTORS.USER_MESSAGE}, ${GEMINI_SELECTORS.ASSISTANT_MESSAGE}`)
  )

  // Filter out any elements that are nested inside another matched turn element
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
    logger.warn(
      'Parser',
      'GEMINI',
      `DOM scan completed: 0 conversation turns found matching '${GEMINI_SELECTORS.USER_MESSAGE}' / '${GEMINI_SELECTORS.ASSISTANT_MESSAGE}'.`
    )
  } else if (userCount === 0) {
    logger.warn(
      'Parser',
      'GEMINI',
      `DOM scan completed: 0 user turns found (${asstCount} assistant turns found).`
    )
  } else if (asstCount === 0) {
    logger.warn(
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
  const interactions: ExtractedInteraction[] = []
  let pendingUserTurn: RawMessageTurn | null = null

  const captureContext = context.captureContext ?? 'on_generate'
  const observedAt = context.observedAt ?? new Date().toISOString()

  let userTextsCount = 0
  let assistantTextsCount = 0

  for (const turn of turns) {
    if (turn.role === 'user') {
      if (turn.text.length > 0) {
        userTextsCount++
        pendingUserTurn = turn
      }
    } else if (turn.role === 'assistant' && pendingUserTurn) {
      if (turn.text.length > 0) {
        assistantTextsCount++
      }
      // Only pair if response is non-empty and NOT streaming
      if (!turn.isStreaming && turn.text.length > 0 && pendingUserTurn.text.length > 0) {
        interactions.push({
          platform: 'gemini',
          conversationId: context.conversationId,
          messageId: turn.messageId, // Assistant message ID (or null)
          userMessageId: pendingUserTurn.messageId, // User message ID (or null)
          model: context.model,
          queryText: pendingUserTurn.text,
          responseText: turn.text,
          conversationTitle: context.title,
          observedAt,
          sourceTimestamp: turn.sourceTimestamp ?? pendingUserTurn.sourceTimestamp ?? null,
          captureContext,
        })
      }
      // Reset pending user turn once consumed or attempted
      pendingUserTurn = null
    }
  }

  const userQueriesCount = turns.filter((t) => t.role === 'user').length
  const modelResponsesCount = turns.filter((t) => t.role === 'assistant').length

  logger.info(
    'Parser',
    'GEMINI',
    `DOM diagnostics | userQueries=${userQueriesCount} | modelResponses=${modelResponsesCount} | userTexts=${userTextsCount} | assistantTexts=${assistantTextsCount} | completePairs=${interactions.length}`
  )

  if (interactions.length === 0 && turns.length > 0) {
    logger.warn(
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
