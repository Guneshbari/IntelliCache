/**
 * Pure DOM parsing and extraction utilities for ChatGPT conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

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
 * Extracts raw user query text from a user turn element.
 * Strips UI controls (e.g. edit buttons) while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  const clone = element.cloneNode(true) as Element

  // Strip buttons or edit controls
  const buttons = clone.querySelectorAll('button, svg')
  buttons.forEach((b) => b.remove())

  // Look for text wrapper
  const textContainer = clone.querySelector(CHATGPT_SELECTORS.USER_TEXT)
  const rawText = textContainer ? textContainer.textContent : clone.textContent

  return (rawText || '').trim()
}

/**
 * Extracts assistant response text while preserving code blocks with language annotations.
 * Strips interactive action buttons, feedback icons, and citations.
 */
export function extractAssistantResponseText(element: Element): string {
  const clone = element.cloneNode(true) as Element

  // Strip UI controls (copy buttons, feedback icons, citations)
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
 * Checks whether an assistant turn is actively streaming / generating.
 */
export function isTurnStreaming(turnElement: Element, root?: Document | Element): boolean {
  // Check if turn itself has streaming classes or cursor
  if (
    turnElement.classList.contains('result-streaming') ||
    turnElement.classList.contains('streaming') ||
    turnElement.querySelector('span.streaming-cursor, .result-streaming') !== null
  ) {
    return true
  }

  // Check if global stop button is present in root/document
  const context = root || turnElement.ownerDocument || document
  const stopButton = context.querySelector(
    'button[data-testid="stop-button"], button[aria-label="Stop generating"], button[aria-label="Stop streaming"]'
  )

  return stopButton !== null
}

/**
 * Extracts raw conversation turns (User and Assistant) in document order from a root container.
 */
export function extractConversationTurns(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  // Query articles or role-based elements
  const articles = Array.from(root.querySelectorAll(CHATGPT_SELECTORS.TURN_ARTICLE))

  if (articles.length > 0) {
    for (const article of articles) {
      const isUser =
        article.querySelector(CHATGPT_SELECTORS.USER_ROLE) !== null ||
        article.getAttribute('data-message-author-role') === 'user'
      const isAssistant =
        article.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE) !== null ||
        article.getAttribute('data-message-author-role') === 'assistant'

      if (isUser) {
        turns.push({
          role: 'user',
          element: article,
          text: extractUserQueryText(article),
          messageId: extractMessageId(article),
          isStreaming: false,
        })
      } else if (isAssistant) {
        turns.push({
          role: 'assistant',
          element: article,
          text: extractAssistantResponseText(article),
          messageId: extractMessageId(article),
          isStreaming: isTurnStreaming(article, root),
        })
      }
    }
    return turns
  }

  // Fallback: search directly by data-message-author-role
  const roleElements = Array.from(
    root.querySelectorAll(`${CHATGPT_SELECTORS.USER_ROLE}, ${CHATGPT_SELECTORS.ASSISTANT_ROLE}`)
  )

  for (const el of roleElements) {
    const role = el.getAttribute('data-message-author-role')
    if (role === 'user') {
      turns.push({
        role: 'user',
        element: el,
        text: extractUserQueryText(el),
        messageId: extractMessageId(el),
        isStreaming: false,
      })
    } else if (role === 'assistant') {
      turns.push({
        role: 'assistant',
        element: el,
        text: extractAssistantResponseText(el),
        messageId: extractMessageId(el),
        isStreaming: isTurnStreaming(el, root),
      })
    }
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
  }
): ExtractedInteraction[] {
  const interactions: ExtractedInteraction[] = []
  let pendingUserTurn: RawMessageTurn | null = null

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
          messageId: turn.messageId,
          model: context.model,
          queryText: pendingUserTurn.text,
          responseText: turn.text,
          conversationTitle: context.title,
          observedAt: new Date().toISOString(),
        })
      }
      // Reset pending user turn once consumed or attempted
      pendingUserTurn = null
    }
  }

  return interactions
}
