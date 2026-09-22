/**
 * Pure DOM parsing and extraction utilities for ChatGPT conversations.
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
import { CHATGPT_SELECTORS } from './selectors'

export { extractMessageId, extractSourceTimestamp }

/**
 * Extracts the UUID or slug conversation ID from ChatGPT URLs.
 * Examples:
 * - https://chatgpt.com/c/6789abcd-1234-5678-90ab-cdef12345678 -> "6789abcd-1234-5678-90ab-cdef12345678"
 * - https://chat.openai.com/c/abc-123 -> "abc-123"
 * - https://chatgpt.com/g/g-p-123-custom/c/456-def -> "456-def"
 */
export function extractConversationIdFromUrl(url: string): string | null {
  try {
    const pathname = new URL(url).pathname
    const match = pathname.match(/\/c\/([a-zA-Z0-9_-]+)/)
    return match?.[1] ?? null
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

  if (!title) return null

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

  return { provider: 'openai', name }
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

  clone.querySelectorAll(CHATGPT_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((b) => b.remove())

  // Strip accessibility / speaker headings (e.g. <h5>You said:</h5>)
  clone.querySelectorAll('h5, h6').forEach((h) => {
    if (/said/i.test(h.textContent || '') || h.classList.contains('sr-only')) {
      h.remove()
    }
  })

  const textContainer = clone.querySelector(CHATGPT_SELECTORS.USER_TEXT) || clone
  return cleanQueryText(normalizeExtractedText(textContainer.textContent || ''))
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

  clone.querySelectorAll(CHATGPT_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((el) => el.remove())

  // Strip accessibility / speaker headings (e.g. <h6>ChatGPT said:</h6>)
  clone.querySelectorAll('h5, h6').forEach((h) => {
    if (/said/i.test(h.textContent || '') || h.classList.contains('sr-only')) {
      h.remove()
    }
  })

  const markdownContainer = clone.querySelector(CHATGPT_SELECTORS.ASSISTANT_TEXT) || clone
  const ownerDoc = element.ownerDocument || document

  markdownContainer.querySelectorAll(CHATGPT_SELECTORS.CODE_BLOCK).forEach((pre) => {
    formatCodeBlock(pre, ownerDoc)
  })

  markdownContainer.querySelectorAll('p, h1, h2, h3, h4, h5, h6, li').forEach((p) => {
    p.textContent = `${p.textContent || ''}\n`
  })

  return cleanResponseText(normalizeExtractedText(markdownContainer.textContent || ''))
}

/**
 * Checks whether the page as a whole is actively generating / streaming.
 */
export function isPageGenerating(root: Document | Element): boolean {
  if (root.querySelector(CHATGPT_SELECTORS.STOP_BUTTON) !== null) {
    logger.debug(
      'Parser',
      'CHATGPT',
      `Active generation detected: stop button present ('${CHATGPT_SELECTORS.STOP_BUTTON}')`
    )
    return true
  }

  if (root.querySelector(CHATGPT_SELECTORS.STREAMING_INDICATORS) !== null) {
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
  if (
    turnElement.classList.contains('result-streaming') ||
    turnElement.classList.contains('streaming') ||
    turnElement.querySelector('span.streaming-cursor, .result-streaming, .streaming') !== null
  ) {
    return true
  }

  const context = root || turnElement.ownerDocument || document
  return isPageGenerating(context)
}

/**
 * Detects whether the current page represents a guest / logged-out ChatGPT session.
 */
export function isChatGPTGuestSession(root: Document | Element): boolean {
  try {
    const doc =
      root instanceof Document
        ? root
        : root.ownerDocument || (typeof document !== 'undefined' ? document : null)
    if (!doc && !(root instanceof Element)) return false

    const hasGuestIndicator =
      (root instanceof Element && root.querySelector(CHATGPT_SELECTORS.GUEST_INDICATORS) !== null) ||
      (doc ? doc.querySelector(CHATGPT_SELECTORS.GUEST_INDICATORS) !== null : false)
    const hasLoggedInProfile =
      (root instanceof Element && root.querySelector(CHATGPT_SELECTORS.LOGGED_IN_INDICATORS) !== null) ||
      (doc ? doc.querySelector(CHATGPT_SELECTORS.LOGGED_IN_INDICATORS) !== null : false)

    if (hasGuestIndicator && !hasLoggedInProfile) {
      return true
    }

    // Also check for login / sign up text, hrefs, or testids in buttons or links when not logged in
    if (!hasLoggedInProfile) {
      const scope = root instanceof Element ? root : doc
      const checkAuth = (el: Element) => {
        const txt = el.textContent?.trim().toLowerCase() || ''
        const href = el.getAttribute('href')?.toLowerCase() || ''
        const testId = el.getAttribute('data-testid')?.toLowerCase() || ''
        return (
          /\b(log\s*in|sign\s*up|sign\s*in)\b/i.test(txt) ||
          href.includes('/auth') ||
          href.includes('login') ||
          testId.includes('login') ||
          testId.includes('signup')
        )
      }
      const authLinks = Array.from(scope?.querySelectorAll('button, a') || []).filter(checkAuth)
      if (authLinks.length > 0) {
        return true
      }
      if (doc && doc !== scope) {
        const docAuthLinks = Array.from(doc.querySelectorAll('button, a')).filter(checkAuth)
        if (docAuthLinks.length > 0) {
          return true
        }
      }
    }

    return false
  } catch {
    return false
  }
}

/**
 * Determines whether an element represents a user turn, assistant turn, or neither.
 * Supports standard data attributes as well as semantic structure found in guest / logged-out mode.
 */
function detectTurnRole(turnEl: Element): 'user' | 'assistant' | null {
  const roleAttr = turnEl.getAttribute('data-message-author-role')
  if (roleAttr === 'user') return 'user'
  if (roleAttr === 'assistant') return 'assistant'

  if (typeof turnEl.matches === 'function') {
    if (turnEl.matches(CHATGPT_SELECTORS.USER_ROLE)) return 'user'
    if (turnEl.matches(CHATGPT_SELECTORS.ASSISTANT_ROLE)) return 'assistant'
  }

  if (turnEl.querySelector(CHATGPT_SELECTORS.USER_ROLE)) return 'user'
  if (turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE)) return 'assistant'

  // Heading check (e.g. <h5>You said:</h5>, <h6>ChatGPT said:</h6>)
  const headings = Array.from(turnEl.querySelectorAll('h5, h6, h2, h3, h4'))
  for (const h of headings) {
    const text = h.textContent?.toLowerCase() || ''
    if (/you said/i.test(text)) return 'user'
    if (/chatgpt said|assistant said/i.test(text)) return 'assistant'
  }

  // Content-based check: markdown or copy button indicates assistant response
  if (
    turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_TEXT) !== null ||
    turnEl.querySelector(
      'button[data-testid="copy-turn-action-button"], button[aria-label="Copy"]'
    ) !== null
  ) {
    return 'assistant'
  }

  // Pre-wrap text without assistant markdown indicates user prompt
  if (turnEl.querySelector(CHATGPT_SELECTORS.USER_TEXT) !== null) {
    return 'user'
  }

  return null
}

/**
 * Extracts raw conversation turns (User and Assistant) in document order from a root container.
 * Specifically prevents nested articles or embedded views from creating duplicate turns.
 */
export function extractConversationTurns(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  let turnContainers = Array.from(root.querySelectorAll(CHATGPT_SELECTORS.TURN_ARTICLE))
  logger.debug(
    'Parser',
    'CHATGPT',
    `Turn article query found ${turnContainers.length} container(s) matching '${CHATGPT_SELECTORS.TURN_ARTICLE}'`
  )

  // In guest mode or newer builds, turns may be bare <article> elements without data-testid="conversation-turn-*"
  if (turnContainers.length === 0) {
    const bareArticles = Array.from(root.querySelectorAll('main article, article')).filter(
      (el) => !el.closest('.embedded-canvas-view')
    )
    if (bareArticles.length > 0) {
      turnContainers = bareArticles
      logger.debug(
        'Parser',
        'CHATGPT',
        `Discovered ${turnContainers.length} bare article container(s) for turn extraction`
      )
    }
  }

  if (turnContainers.length > 0) {
    for (const turnEl of turnContainers) {
      const role = detectTurnRole(turnEl)

      if (role === 'user') {
        const userEl =
          turnEl.getAttribute('data-message-author-role') === 'user'
            ? turnEl
            : turnEl.querySelector(CHATGPT_SELECTORS.USER_ROLE) || turnEl
        turns.push({
          role: 'user',
          element: userEl,
          text: extractUserQueryText(userEl),
          messageId: extractMessageId(userEl),
          sourceTimestamp: extractSourceTimestamp(userEl),
          isStreaming: false,
        })
      } else if (role === 'assistant') {
        const asstEl =
          turnEl.getAttribute('data-message-author-role') === 'assistant'
            ? turnEl
            : turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE) || turnEl
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
  }

  // Fallback: If container extraction yielded 0 turns (or no containers were found),
  // search directly by data-message-author-role or message text containers
  if (turns.length === 0) {
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
          parent.getAttribute('data-message-author-role') === 'assistant' ||
          (typeof parent.matches === 'function' &&
            (parent.matches(CHATGPT_SELECTORS.USER_ROLE) ||
              parent.matches(CHATGPT_SELECTORS.ASSISTANT_ROLE)))
        ) {
          return false
        }
        parent = parent.parentElement
      }
      return true
    })

    for (const el of topRoleElements) {
      const role =
        detectTurnRole(el) || (el.getAttribute('data-message-author-role') as 'user' | 'assistant' | null)
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
    logger.debug('Parser', 'CHATGPT', 'DOM scan completed: 0 conversation turns found.')
  } else if (userCount === 0) {
    logger.debug(
      'Parser',
      'CHATGPT',
      `DOM scan completed: 0 user turns found (${asstCount} assistant turns found).`
    )
  } else if (asstCount === 0) {
    logger.debug(
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
  const interactions = sharedPairTurns('chatgpt', turns, context)

  if (interactions.length === 0 && turns.length > 0) {
    logger.debug(
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
