/**
 * Pure DOM parsing and extraction utilities for Claude (claude.ai) conversations.
 *
 * Implements pure functions that can be tested in isolation using static HTML fixtures.
 */

import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'
import { CLAUDE_SELECTORS } from './selectors'

/**
 * Extracts the UUID or slug conversation ID from Claude URLs.
 * Examples:
 * - https://claude.ai/chat/6a8617f8-ce44-83ee-b5b6-72eb43d13516 -> "6a8617f8-ce44-83ee-b5b6-72eb43d13516"
 * - https://claude.ai/project/proj_123/chat/abc-456 -> "abc-456"
 */
export function extractConversationIdFromUrl(url: string): string | null {
  try {
    const parsed = new URL(url)
    const pathname = parsed.pathname

    // Match /chat/{id}
    const standardMatch = pathname.match(/\/chat\/([a-zA-Z0-9_-]+)/)
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
 * Strips standard brand suffixes like " - Claude" or " | Claude".
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
 * Returns provider: 'claude', name: string | null.
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

  return {
    provider: 'claude',
    name: name ?? null,
  }
}

/**
 * Extracts message ID attribute (`data-message-id` or similar) from an element if present.
 * Returns null if not exposed by Claude's DOM.
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
 * Strips UI controls and navigation while preserving multiline formatting.
 */
export function extractUserQueryText(element: Element): string {
  const clone = element.cloneNode(true) as Element

  // Strip UI controls, action toolbars, copy buttons, etc.
  const uiControls = clone.querySelectorAll(CLAUDE_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((b) => b.remove())

  const rawText = clone.textContent || ''

  return rawText
    .replace(/\r\n|\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * Extracts assistant response text while preserving code blocks with language annotations.
 * Strips interactive action buttons, copy buttons, and toolbars.
 */
export function extractAssistantResponseText(element: Element): string {
  const clone = element.cloneNode(true) as Element

  // Strip UI controls (copy buttons, feedback icons, toolbars)
  const uiControls = clone.querySelectorAll(CLAUDE_SELECTORS.UI_CONTROLS_TO_EXCLUDE)
  uiControls.forEach((el) => el.remove())

  // Format code blocks before getting textContent
  const codeBlocks = clone.querySelectorAll(CLAUDE_SELECTORS.CODE_BLOCK)
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
  const stopButton = root.querySelector(CLAUDE_SELECTORS.STOP_BUTTON)
  if (stopButton !== null) {
    return true
  }

  // 2. Check for streaming indicators
  const streamingEl = root.querySelector(CLAUDE_SELECTORS.STREAMING_INDICATORS)
  if (streamingEl !== null) {
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

  // Query all user and assistant message containers in document order
  const elements = Array.from(
    root.querySelectorAll(`${CLAUDE_SELECTORS.USER_MESSAGE}, ${CLAUDE_SELECTORS.ASSISTANT_MESSAGE}`)
  )

  // Filter out any elements that are nested inside another matched turn element
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
      const text = extractAssistantResponseText(el)
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
          platform: 'claude',
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

  return interactions
}
