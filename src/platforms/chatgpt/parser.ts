/**
 * Pure DOM parsing and extraction utilities for ChatGPT conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

import { logger } from '../../diagnostics'
import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'
import { CHATGPT_SELECTORS } from './selectors'

/**
 * Extracts the UUID or slug conversation ID from ChatGPT URLs.
 * Examples:
 * - https://chatgpt.com/c/6789abcd-1234-5678-90ab-cdef12345678 -> "6789abcd-1234-5678-90ab-cdef12345678"
 * - https://chat.openai.com/c/abc-123 -> "abc-123"
 * - https://chatgpt.com/g/g-p-123-custom/c/456-def -> "456-def"
 */
export function extractConversationIdFromUrl(url: string): string | null {
  try {
    const parsed = new URL(url)
    const pathname = parsed.pathname

    // Match /c/{id}
    const standardMatch = pathname.match(/\/c\/([a-zA-Z0-9_-]+)/)
    if (standardMatch && standardMatch[1]) {
      return standardMatch[1]
    }

    return null
  } catch {
    return null
  }
}

/**
 * Extracts conversation title from page title or DOM header.
 * Strips standard brand suffixes like " - ChatGPT".
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

  // Clean brand suffix
  const cleaned = title.replace(/\s*-\s*ChatGPT$/i, '').trim()

  if (!cleaned || cleaned.toLowerCase() === 'chatgpt' || cleaned.toLowerCase() === 'new chat') {
    return null
  }

  return cleaned
}

/**
 * Extracts model provider and name from the ChatGPT interface if available.
 *
 * NOTE: Strictly avoids matching arbitrary Radix UI buttons (`button[id^="radix-"]`).
 * If a reliable model switcher cannot be found, returns null for model name.
 */
export function extractModelInfo(root: Document | Element): {
  provider: string | null
  name: string | null
} {
  const modelSwitcher = root.querySelector(CHATGPT_SELECTORS.MODEL_SWITCHER)
  let name: string | null = null

  if (modelSwitcher) {
    const text = modelSwitcher.textContent?.trim()
    if (text && text.length > 0 && text.length < 50) {
      name = text
    }
  }

  return {
    provider: 'openai',
    name: name ?? null,
  }
}

/**
 * Extracts message ID attribute (`data-message-id`) from an element if present.
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
 * Extracts raw user query text from a user turn element.
 * Strips UI controls, navigation, and edit controls while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  const userContainer =
    element.getAttribute('data-message-author-role') === 'user'
      ? element
      : element.querySelector(CHATGPT_SELECTORS.USER_ROLE) || element

  const clone = userContainer.cloneNode(true) as Element

  // Strip UI controls, edit buttons, action toolbars, forms, and navigation
  const uiControls = clone.querySelectorAll(CHATGPT_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((b) => b.remove())

  // Look for text wrapper
  const textContainer = clone.querySelector(CHATGPT_SELECTORS.USER_TEXT) || clone
  const rawText = textContainer.textContent || ''

  return rawText
    .replace(/\r\n|\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * Extracts assistant response text while preserving code blocks with language annotations.
 * Strips interactive action buttons, feedback icons, and citations.
 */
export function extractAssistantResponseText(element: Element): string {
  const asstContainer =
    element.getAttribute('data-message-author-role') === 'assistant'
      ? element
      : element.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE) || element

  const clone = asstContainer.cloneNode(true) as Element

  // Strip UI controls (copy buttons, feedback icons, citations, toolbars)
  const uiControls = clone.querySelectorAll(CHATGPT_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((el) => el.remove())

  // Locate main markdown container
  const markdownContainer = clone.querySelector(CHATGPT_SELECTORS.ASSISTANT_TEXT) || clone

  // Format code blocks before getting textContent
  const codeBlocks = markdownContainer.querySelectorAll(CHATGPT_SELECTORS.CODE_BLOCK)
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
  const paragraphs = markdownContainer.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li')
  paragraphs.forEach((p) => {
    p.textContent = `${p.textContent || ''}\n`
  })

  const rawText = markdownContainer.textContent || ''

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
  const stopButton = root.querySelector(CHATGPT_SELECTORS.STOP_BUTTON)
  if (stopButton !== null) {
    logger.debug(
      'Parser',
      'CHATGPT',
      `Active generation detected: stop button present ('${CHATGPT_SELECTORS.STOP_BUTTON}')`
    )
    return true
  }

  // 2. Check for active streaming or thinking classes across the page
  const streamingEl = root.querySelector(
    '.result-streaming, .streaming, span.streaming-cursor, .result-thinking'
  )
  if (streamingEl !== null) {
    logger.debug(
      'Parser',
      'CHATGPT',
      'Active generation detected: streaming/thinking indicator present'
    )
    return true
  }

  return false
}

/**
 * Checks whether an assistant turn is actively streaming / generating.
 */
export function isTurnStreaming(turnElement: Element, root?: Document | Element): boolean {
  // 1. Check if turn itself has streaming classes or cursor
  if (
    turnElement.classList.contains('result-streaming') ||
    turnElement.classList.contains('streaming') ||
    turnElement.querySelector('span.streaming-cursor, .result-streaming, .streaming') !== null
  ) {
    return true
  }

  // 2. Check if global stop button or page streaming is active
  const context = root || turnElement.ownerDocument || document
  return isPageGenerating(context)
}

/**
 * Extracts raw conversation turns (User and Assistant) in document order from a root container.
 * Specifically prevents nested articles or embedded views from creating duplicate turns.
 */
export function extractConversationTurns(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  // 1. Primary: query turn containers with data-testid^="conversation-turn-"
  const turnContainers = Array.from(root.querySelectorAll(CHATGPT_SELECTORS.TURN_ARTICLE))
  logger.debug(
    'Parser',
    'CHATGPT',
    `Turn article query found ${turnContainers.length} container(s) matching '${CHATGPT_SELECTORS.TURN_ARTICLE}'`
  )

  if (turnContainers.length > 0) {
    for (const turnEl of turnContainers) {
      const isUser =
        turnEl.getAttribute('data-message-author-role') === 'user' ||
        turnEl.querySelector(CHATGPT_SELECTORS.USER_ROLE) !== null
      const isAssistant =
        turnEl.getAttribute('data-message-author-role') === 'assistant' ||
        turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE) !== null

      if (isUser) {
        const userEl =
          turnEl.getAttribute('data-message-author-role') === 'user'
            ? turnEl
            : turnEl.querySelector(CHATGPT_SELECTORS.USER_ROLE)!
        turns.push({
          role: 'user',
          element: userEl,
          text: extractUserQueryText(userEl),
          messageId: extractMessageId(userEl),
          sourceTimestamp: extractSourceTimestamp(userEl),
          isStreaming: false,
        })
      } else if (isAssistant) {
        const asstEl =
          turnEl.getAttribute('data-message-author-role') === 'assistant'
            ? turnEl
            : turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE)!
        turns.push({
          role: 'assistant',
          element: asstEl,
          text: extractAssistantResponseText(asstEl),
          messageId: extractMessageId(asstEl),
          sourceTimestamp: extractSourceTimestamp(asstEl),
          isStreaming: isTurnStreaming(asstEl, root),
        })
      }
    }
  } else {
    // 2. Fallback: Search directly by data-message-author-role (filtering out nested role elements)
    const roleElements = Array.from(
      root.querySelectorAll(`${CHATGPT_SELECTORS.USER_ROLE}, ${CHATGPT_SELECTORS.ASSISTANT_ROLE}`)
    )
    logger.debug(
      'Parser',
      'CHATGPT',
      `Fallback role elements query found ${roleElements.length} candidate(s)`
    )

    const topRoleElements = roleElements.filter((el) => {
      let parent = el.parentElement
      while (parent && parent !== root) {
        if (
          parent.getAttribute('data-message-author-role') === 'user' ||
          parent.getAttribute('data-message-author-role') === 'assistant'
        ) {
          return false // Exclude nested role element
        }
        parent = parent.parentElement
      }
      return true
    })

    for (const el of topRoleElements) {
      const role = el.getAttribute('data-message-author-role')
      if (role === 'user') {
        turns.push({
          role: 'user',
          element: el,
          text: extractUserQueryText(el),
          messageId: extractMessageId(el),
          sourceTimestamp: extractSourceTimestamp(el),
          isStreaming: false,
        })
      } else if (role === 'assistant') {
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
  }

  const userCount = turns.filter((t) => t.role === 'user').length
  const asstCount = turns.filter((t) => t.role === 'assistant').length
  logger.debug(
    'Parser',
    'CHATGPT',
    `Turn extraction complete: total=${turns.length}, userTurns=${userCount}, assistantTurns=${asstCount}`
  )

  if (turns.length === 0) {
    logger.warn('Parser', 'CHATGPT', 'DOM scan completed: 0 conversation turns found.')
  } else if (userCount === 0) {
    logger.warn(
      'Parser',
      'CHATGPT',
      `DOM scan completed: 0 user turns found (${asstCount} assistant turns found).`
    )
  } else if (asstCount === 0) {
    logger.warn(
      'Parser',
      'CHATGPT',
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

  for (const turn of turns) {
    if (turn.role === 'user') {
      if (turn.text.length > 0) {
        pendingUserTurn = turn
      }
    } else if (turn.role === 'assistant' && pendingUserTurn) {
      // Only pair if response is non-empty and NOT streaming
      if (!turn.isStreaming && turn.text.length > 0 && pendingUserTurn.text.length > 0) {
        interactions.push({
          platform: 'chatgpt',
          conversationId: context.conversationId,
          messageId: turn.messageId, // Assistant message ID
          userMessageId: pendingUserTurn.messageId, // User message ID
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

  if (interactions.length === 0 && turns.length > 0) {
    logger.warn(
      'Parser',
      'CHATGPT',
      `Failed to form any complete user/assistant pairs from ${turns.length} turns.`
    )
  } else {
    logger.debug(
      'Parser',
      'CHATGPT',
      `Pairing complete: formed ${interactions.length} complete interaction pair(s).`
    )
  }

  return interactions
}
