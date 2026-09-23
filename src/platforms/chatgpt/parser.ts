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

const RE_CHATGPT_CONV_ID = /\/(?:c|uc)\/([a-zA-Z0-9_-]+)/
const RE_CHATGPT_TITLE_STRIP = /\s*-\s*ChatGPT$/i

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
    const match = pathname.match(RE_CHATGPT_CONV_ID)
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

  const cleaned = title.replace(RE_CHATGPT_TITLE_STRIP, '').trim()
  const lower = cleaned.toLowerCase()

  if (
    !cleaned ||
    lower === 'chatgpt' ||
    lower === 'new chat' ||
    lower.startsWith('chatgpt: chat, work, create') ||
    lower.includes('chat, work, create & code with ai') ||
    lower.includes('get answers. find inspiration')
  ) {
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

const RE_SAID_HEADING = /said/i
const RE_AUTH_WORDS = /\b(log\s*in|sign\s*up|sign\s*in)\b/i

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

  // Strip accessibility / speaker headings (e.g. <h4>You said:</h4>, <h5>You said:</h5>)
  clone.querySelectorAll('h4, h5, h6').forEach((h) => {
    if (RE_SAID_HEADING.test(h.textContent || '') || h.classList.contains('sr-only')) {
      h.remove()
    }
  })

  // Strip UI controls except for elements that ARE the message text.
  // In production guest DOM, the user message text is inside a <button> element.
  // We remove buttons that have an aria-label (action buttons) but preserve unlabelled
  // buttons that carry message content.
  clone.querySelectorAll(CHATGPT_SELECTORS.UI_CONTROLS_TO_EXCLUDE).forEach((b) => {
    // Preserve <button> elements that have NO aria-label — they are message text containers
    if (b.tagName === 'BUTTON' && !b.hasAttribute('aria-label')) {
      return
    }
    b.remove()
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

  // Strip accessibility / speaker headings (e.g. <h4>ChatGPT said:</h4>, <h6>ChatGPT said:</h6>)
  clone.querySelectorAll('h4, h5, h6').forEach((h) => {
    if (RE_SAID_HEADING.test(h.textContent || '') || h.classList.contains('sr-only')) {
      h.remove()
    }
  })

  const markdownContainer =
    typeof clone.matches === 'function' && clone.matches(CHATGPT_SELECTORS.ASSISTANT_TEXT)
      ? clone
      : clone.querySelector(CHATGPT_SELECTORS.ASSISTANT_TEXT) || clone
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

    const scope = root instanceof Element ? root : doc
    if (!scope) return false

    // Check for login / sign up text, hrefs, or testids in buttons, links, or aria-labels
    const checkAuth = (el: Element): boolean => {
      const txt = el.textContent?.trim() || ''
      const aria = el.getAttribute('aria-label') || ''
      if (RE_AUTH_WORDS.test(txt) || RE_AUTH_WORDS.test(aria)) {
        return true
      }
      const href = el.getAttribute('href')?.toLowerCase() || ''
      if (href.includes('/auth') || href.includes('login') || href.includes('signup')) {
        return true
      }
      const testId = el.getAttribute('data-testid')?.toLowerCase() || ''
      return testId.includes('login') || testId.includes('signup')
    }

    // 1. Explicit guest indicators in selectors
    let hasGuestIndicator = scope.querySelector(CHATGPT_SELECTORS.GUEST_INDICATORS) !== null
    if (!hasGuestIndicator && doc && doc !== scope && doc.contains(scope)) {
      hasGuestIndicator = doc.querySelector(CHATGPT_SELECTORS.GUEST_INDICATORS) !== null
    }

    // 2. Discover any login/signup buttons or links across scope with early exit
    let hasAuthLinks = false
    const authElements = scope.querySelectorAll('button, a, [role="button"]')
    for (let i = 0; i < authElements.length; i++) {
      if (checkAuth(authElements[i])) {
        hasAuthLinks = true
        break
      }
    }
    if (!hasAuthLinks && doc && doc !== scope && doc.contains(scope)) {
      const docAuth = doc.querySelectorAll('button, a, [role="button"]')
      for (let i = 0; i < docAuth.length; i++) {
        if (checkAuth(docAuth[i])) {
          hasAuthLinks = true
          break
        }
      }
    }

    // 3. Discover authenticated profile controls
    let hasStrongProfile = false
    const profileCandidates = scope.querySelectorAll(CHATGPT_SELECTORS.LOGGED_IN_INDICATORS)
    for (let i = 0; i < profileCandidates.length; i++) {
      if (!checkAuth(profileCandidates[i])) {
        hasStrongProfile = true
        break
      }
    }
    if (!hasStrongProfile && doc && doc !== scope && doc.contains(scope)) {
      const docProfiles = doc.querySelectorAll(CHATGPT_SELECTORS.LOGGED_IN_INDICATORS)
      for (let i = 0; i < docProfiles.length; i++) {
        if (!checkAuth(docProfiles[i])) {
          hasStrongProfile = true
          break
        }
      }
    }

    // 4. Distinguish guest account menu from authenticated account menu:
    let hasAccountMenuAsProfile = false
    const accountMenuBtn =
      scope.querySelector('button[aria-label*="Open account menu"]') ||
      (doc && doc !== scope && doc.contains(scope)
        ? doc.querySelector('button[aria-label*="Open account menu"]')
        : null)
    if (accountMenuBtn && !hasAuthLinks && !hasGuestIndicator && !checkAuth(accountMenuBtn)) {
      hasAccountMenuAsProfile = true
    }

    const hasLoggedInProfile = hasStrongProfile || hasAccountMenuAsProfile

    if (hasGuestIndicator || hasAuthLinks) {
      if (!hasLoggedInProfile) {
        return true
      }
    }

    if (!hasLoggedInProfile && (hasGuestIndicator || hasAuthLinks)) {
      return true
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
  const headings = [
    ...(typeof turnEl.matches === 'function' &&
    turnEl.matches('h5, h6, h2, h3, h4, [class*="sr-only"]')
      ? [turnEl]
      : []),
    ...Array.from(turnEl.querySelectorAll('h5, h6, h2, h3, h4, [class*="sr-only"]')),
  ]
  for (const h of headings) {
    const text = h.textContent?.toLowerCase() || ''
    if (/you said/i.test(text)) return 'user'
    if (/chatgpt said|assistant said/i.test(text)) return 'assistant'
  }

  // Content-based check: markdown or prose indicates assistant response
  if (
    (typeof turnEl.matches === 'function' && turnEl.matches(CHATGPT_SELECTORS.ASSISTANT_TEXT)) ||
    turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_TEXT) !== null
  ) {
    return 'assistant'
  }

  // Pre-wrap text without assistant markdown indicates user prompt
  if (
    (typeof turnEl.matches === 'function' && turnEl.matches(CHATGPT_SELECTORS.USER_TEXT)) ||
    turnEl.querySelector(CHATGPT_SELECTORS.USER_TEXT) !== null
  ) {
    return 'user'
  }

  // Fallback: assistant action buttons (e.g. copy response button)
  if (turnEl.querySelector(CHATGPT_SELECTORS.ASSISTANT_COPY_ANCHOR) !== null) {
    return 'assistant'
  }

  // Fallback: user action button (e.g. copy message button)
  if (turnEl.querySelector('button[aria-label="Copy message"]') !== null) {
    return 'user'
  }

  return null
}

/**
 * Guest-mode turn extraction fallback using the confirmed assistant Copy action anchor.
 *
 * In modern unauthenticated ChatGPT guest sessions (such as /uc/<uuid>), OpenAI strips
 * data-testid conversation-turn attributes, data-message-author-role attributes, and
 * article tags. However, every completed assistant response reliably renders an accessible
 * Copy action button (`button[aria-label="Copy"]` or `[data-testid="copy-turn-action-button"]`)
 * inside its action toolbar, with the user query occupying the immediately preceding
 * conversational row.
 */
export function extractGuestTurnsFromCopyAnchors(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  const mainEl =
    (root instanceof Document
      ? root.querySelector('main')
      : typeof root.matches === 'function' && root.matches('main')
        ? root
        : root.querySelector('main')) || (root instanceof Document ? root.body : root)

  if (!mainEl) return turns

  // Discover candidate Copy buttons inside the primary chat container
  const copyButtons = Array.from(
    mainEl.querySelectorAll(CHATGPT_SELECTORS.ASSISTANT_COPY_ANCHOR)
  ).filter((btn) => {
    // Exclude buttons in navigation, sidebars, composer form, dialogs, modals, and canvas
    if (
      btn.closest('nav, aside, form, [role="navigation"], [role="dialog"], .embedded-canvas-view')
    ) {
      return false
    }
    // Exclude code-block copy buttons (<pre>, <code>, code headers)
    if (btn.closest('pre, code, [class*="code-block"], [class*="code-header"], .code-block')) {
      return false
    }
    const aria = btn.getAttribute('aria-label')?.toLowerCase() || ''
    if (aria.includes('copy code')) {
      return false
    }
    return true
  })

  if (copyButtons.length === 0) return turns

  const processedAssistantRows = new Set<Element>()
  const pairs: {
    userEl: Element
    assistantEl: Element
    userText: string
    assistantText: string
  }[] = []

  for (const copyBtn of copyButtons) {
    let asstRow: Element | null = null
    let userRow: Element | null = null

    // Climb upward from copyBtn to find the assistant response container
    let current: Element | null = copyBtn.parentElement
    while (current && current !== mainEl) {
      // Production guest DOM (OL/LI structure with atomic CSS) uses list items as turn boundaries.
      // Each <LI> is a conversational turn: the assistant's <LI> has the preceding user <LI> as
      // its previousElementSibling. Detect this before the parent-stop guard fires.
      if (current.tagName === 'LI') {
        let prevLi = current.previousElementSibling
        while (prevLi && prevLi.tagName !== 'LI') {
          prevLi = prevLi.previousElementSibling
        }
        if (prevLi && prevLi.tagName === 'LI' && !copyButtons.some((b) => prevLi!.contains(b))) {
          userRow = prevLi
          asstRow = current
          break
        }
      }

      // Check if current contains assistant response content (data-assistant-markdown, markdown, prose, p, pre, list, blockquote)
      const hasContent =
        current.querySelector(
          '[data-assistant-markdown], .markdown, .prose, p, pre, ul, ol, blockquote'
        ) !== null ||
        (typeof current.matches === 'function' &&
          current.matches(
            '[data-assistant-markdown], .markdown, .prose, p, pre, ul, ol, blockquote'
          ))

      if (hasContent) {
        // Look for the conversational element immediately preceding current
        let prev = current.previousElementSibling
        while (prev) {
          const txt = prev.textContent?.trim() || ''
          if (txt.length === 0) {
            prev = prev.previousElementSibling
            continue
          }
          if (
            prev.matches?.('form, [role="toolbar"], nav, aside') ||
            prev.querySelector('form, textarea')
          ) {
            prev = prev.previousElementSibling
            continue
          }
          // Skip speaker heading labels (e.g. <h4>You said:</h4> / <h4>ChatGPT said:</h4>)
          // These are accessibility labels attached to the response container, not user query rows.
          if (prev.matches?.('h1, h2, h3, h4, h5, h6')) {
            prev = prev.previousElementSibling
            continue
          }
          if (copyButtons.some((b) => prev!.contains(b))) {
            // Previous element contains a copy button — it is another assistant turn, not a user query
            break
          }
          // Found preceding conversational candidate
          userRow = prev
          asstRow = current
          break
        }

        if (asstRow && userRow) {
          break
        }
      }

      // If current.parentElement contains another copy button from copyButtons,
      // stop climbing to avoid merging multiple turns
      if (
        current.parentElement &&
        copyButtons.some((other) => other !== copyBtn && current!.parentElement!.contains(other))
      ) {
        break
      }

      current = current.parentElement
    }

    if (!asstRow || !userRow || processedAssistantRows.has(asstRow)) {
      continue
    }

    const asstText = extractAssistantResponseText(asstRow)
    const userText = extractUserQueryText(userRow)

    if (asstText.length > 0 && userText.length > 0) {
      processedAssistantRows.add(asstRow)
      pairs.push({
        userEl: userRow,
        assistantEl: asstRow,
        userText,
        assistantText: asstText,
      })
    }
  }

  // Construct RawMessageTurn objects in exact document order
  for (const pair of pairs) {
    turns.push({
      role: 'user',
      element: pair.userEl,
      text: pair.userText,
      messageId: extractMessageId(pair.userEl),
      sourceTimestamp: extractSourceTimestamp(pair.userEl),
      isStreaming: false,
    })
    turns.push({
      role: 'assistant',
      element: pair.assistantEl,
      text: pair.assistantText,
      messageId: extractMessageId(pair.assistantEl),
      sourceTimestamp: extractSourceTimestamp(pair.assistantEl),
      isStreaming: false,
    })
  }

  return turns
}

/**
 * Extracts raw conversation turns (User and Assistant) in document order from a root container.
 * Specifically prevents nested articles or embedded views from creating duplicate turns.
 */
export function extractConversationTurns(root: Document | Element): RawMessageTurn[] {
  const turns: RawMessageTurn[] = []

  let turnContainers = Array.from(root.querySelectorAll(CHATGPT_SELECTORS.TURN_ARTICLE)).filter(
    (el) => !el.closest('.embedded-canvas-view, nav, aside')
  )
  logger.debug(
    'Parser',
    'CHATGPT',
    `Turn article query found ${turnContainers.length} container(s) matching '${CHATGPT_SELECTORS.TURN_ARTICLE}'`
  )

  // In guest mode or newer builds, turns may be bare <article> elements without data-testid="conversation-turn-*"
  // An article qualifies as a turn container only if it contains verifiable message evidence
  if (turnContainers.length === 0) {
    const bareArticles = Array.from(root.querySelectorAll('main article, article')).filter((el) => {
      if (el.closest('.embedded-canvas-view, nav, aside')) return false
      return (
        el.hasAttribute('data-message-author-role') ||
        el.querySelector('[data-message-author-role]') !== null ||
        el.querySelector(CHATGPT_SELECTORS.USER_ROLE) !== null ||
        el.querySelector(CHATGPT_SELECTORS.ASSISTANT_ROLE) !== null ||
        detectTurnRole(el) !== null
      )
    })
    if (bareArticles.length > 0) {
      turnContainers = bareArticles
      logger.debug(
        'Parser',
        'CHATGPT',
        `Discovered ${turnContainers.length} bare article container(s) for turn extraction`
      )
    }
  }

  // Next structural fallback: look for conversation-turn class containers
  if (turnContainers.length === 0) {
    const classTurns = Array.from(
      root.querySelectorAll('main [class*="conversation-turn"], [class*="conversation-turn"]')
    ).filter((el) => !el.closest('.embedded-canvas-view, nav, aside'))
    if (classTurns.length > 0) {
      turnContainers = classTurns
      logger.debug(
        'Parser',
        'CHATGPT',
        `Discovered ${turnContainers.length} conversation-turn class container(s) for turn extraction`
      )
    }
  }

  // Next speaker-heading fallback: look for headings like "You said:", "ChatGPT said:"
  if (turnContainers.length === 0) {
    const speakerHeadings = Array.from(root.querySelectorAll('h5, h6, [class*="sr-only"]')).filter(
      (el) => {
        if (el.closest('.embedded-canvas-view, nav, aside')) return false
        const txt = el.textContent?.toLowerCase() || ''
        return /you said|chatgpt said|assistant said/i.test(txt)
      }
    )
    if (speakerHeadings.length > 0) {
      const headingContainers: Element[] = []
      for (const h of speakerHeadings) {
        const parentContainer = h.closest(
          'article, [class*="group/conversation-turn"], [class*="conversation-turn"], div.w-full, div'
        )
        if (
          parentContainer &&
          parentContainer !== root &&
          parentContainer !== (root instanceof Document ? root.body : root) &&
          !headingContainers.includes(parentContainer)
        ) {
          headingContainers.push(parentContainer)
        }
      }
      if (headingContainers.length > 0) {
        turnContainers = headingContainers
        logger.debug(
          'Parser',
          'CHATGPT',
          `Discovered ${turnContainers.length} speaker-heading container(s) for turn extraction`
        )
      }
    }
  }

  // Next modern guest DOM fallback: identify sibling message rows inside primary chat viewport
  if (turnContainers.length === 0) {
    const mainEl =
      (root instanceof Document
        ? root.querySelector('main')
        : typeof root.matches === 'function' && root.matches('main')
          ? root
          : root.querySelector('main')) || (root instanceof Document ? root.body : root)

    if (mainEl) {
      // Discover candidate message content elements (data-assistant-markdown, .whitespace-pre-wrap, .markdown, .prose)
      // Strictly exclude navigation, sidebars, composer form, toolbars, dialogs, and canvas views
      const contentNodes = Array.from(
        mainEl.querySelectorAll(
          '[data-assistant-markdown], .whitespace-pre-wrap, [class*="whitespace-pre-wrap"], div[class*="text-message"], .markdown, .prose, div[class*="markdown"]'
        )
      ).filter(
        (el) =>
          !el.closest(
            'nav, aside, form, [role="navigation"], [role="toolbar"], [role="dialog"], .embedded-canvas-view'
          )
      )

      // Exclude nested content nodes (e.g. .prose inside .markdown, or .whitespace-pre-wrap inside assistant code blocks)
      const isInsideAssistantMarkdown = (el: Element): boolean => {
        let p = el.parentElement
        while (p && p !== mainEl) {
          if (
            (typeof p.matches === 'function' && p.matches(CHATGPT_SELECTORS.ASSISTANT_TEXT)) ||
            p.hasAttribute('data-assistant-markdown') ||
            p.classList.contains('markdown') ||
            p.classList.contains('prose')
          ) {
            return true
          }
          p = p.parentElement
        }
        return false
      }

      const topContentNodes = contentNodes.filter((el) => {
        if (
          (typeof el.matches === 'function' && el.matches(CHATGPT_SELECTORS.ASSISTANT_TEXT)) ||
          el.classList.contains('markdown') ||
          el.classList.contains('prose')
        ) {
          return !isInsideAssistantMarkdown(el)
        }
        return !isInsideAssistantMarkdown(el)
      })

      if (topContentNodes.length > 0) {
        // Map each content node to its logical message row container inside mainEl.
        // A message row is the highest ancestor below mainEl that contains ONLY this message
        // and none of the other distinct topContentNodes.
        const candidateRows: Element[] = []
        for (const contentEl of topContentNodes) {
          let row = contentEl
          let current: Element | null = contentEl.parentElement
          while (current && current !== mainEl) {
            const containsOther = topContentNodes.some(
              (other) => other !== contentEl && current!.contains(other)
            )
            if (containsOther) {
              break
            }
            row = current
            current = current.parentElement
          }
          if (!candidateRows.includes(row) && !candidateRows.some((r) => r.contains(row))) {
            const existingChildIdx = candidateRows.findIndex((r) => row.contains(r))
            if (existingChildIdx >= 0) {
              candidateRows[existingChildIdx] = row
            } else {
              candidateRows.push(row)
            }
          }
        }

        if (candidateRows.length > 0) {
          turnContainers = candidateRows
          logger.debug(
            'Parser',
            'CHATGPT',
            `Discovered ${turnContainers.length} guest message row container(s) for turn extraction`
          )
        }
      }
    }
  }

  // Deduplicate nested containers so parent-child duplicates don't produce double turns
  if (turnContainers.length > 1) {
    turnContainers = turnContainers.filter((el) => {
      let parent = el.parentElement
      while (parent && parent !== root) {
        if (turnContainers.includes(parent)) {
          const parentRole = detectTurnRole(parent)
          if (parentRole !== null) {
            return false
          }
        }
        parent = parent.parentElement
      }
      return true
    })
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

  // If the heuristic found containers but the extraction produced an UNBALANCED result
  // (0 user turns OR 0 assistant turns), it cannot form any complete pairs.
  // This is the typical failure for production OL/LI guest DOM where:
  //   - The sibling heuristic matches .prose inside assistant LI only → 0 user rows
  //   - OR the OL container gets classified as user (contains Copy message) → 0 assistant rows
  // Clearing allows the dedicated copy-anchor extraction to run with correct LI-boundary logic.
  if (turns.length > 0) {
    const userTurnCount = turns.filter((t) => t.role === 'user').length
    const asstTurnCount = turns.filter((t) => t.role === 'assistant').length
    if (userTurnCount === 0 || asstTurnCount === 0) {
      logger.debug(
        'Parser',
        'CHATGPT',
        `Container extraction yielded unbalanced turns (user=${userTurnCount}, asst=${asstTurnCount}) — clearing for copy-anchor fallback.`
      )
      turns.length = 0
    }
  }

  // Fallback 1: Guest-mode anchor extraction using the confirmed assistant Copy action button
  if (turns.length === 0) {
    const guestTurns = extractGuestTurnsFromCopyAnchors(root)
    if (guestTurns.length > 0) {
      logger.debug(
        'Parser',
        'CHATGPT',
        `Discovered ${guestTurns.length} turn(s) using guest assistant Copy action anchor.`
      )
      return guestTurns
    }
  }

  // Fallback 2: If container extraction yielded 0 turns (or no containers were found),
  // search directly by data-message-author-role or message text containers
  if (turns.length === 0) {
    const roleElements = Array.from(
      root.querySelectorAll(
        `${CHATGPT_SELECTORS.USER_ROLE}, ${CHATGPT_SELECTORS.ASSISTANT_ROLE}, ${CHATGPT_SELECTORS.USER_TEXT}, ${CHATGPT_SELECTORS.ASSISTANT_TEXT}`
      )
    ).filter((el) => !el.closest('.embedded-canvas-view, nav, aside'))

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
              parent.matches(CHATGPT_SELECTORS.ASSISTANT_ROLE))) ||
          roleElements.includes(parent)
        ) {
          return false
        }
        parent = parent.parentElement
      }
      return true
    })

    for (const el of topRoleElements) {
      const role =
        detectTurnRole(el) ||
        (el.getAttribute('data-message-author-role') as 'user' | 'assistant' | null)
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
