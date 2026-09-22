/**
 * Shared DOM parsing utilities for platform-specific parsers.
 *
 * These functions are platform-agnostic and were previously duplicated
 * verbatim across the chatgpt, claude, and gemini parsers.
 */

import { cleanQueryText, cleanResponseText } from '../../shared/text-cleaner'
import type { CaptureContext } from '../../shared/types'
import type { ExtractedInteraction, RawMessageTurn } from '../types'

export { cleanQueryText, cleanResponseText }

/**
 * Extracts message ID attribute (`data-message-id`) from an element if present.
 * Checks the element itself first, then its descendants.
 */
export function extractMessageId(element: Element): string | null {
  if (!element || typeof element.getAttribute !== 'function') {
    return null
  }

  const directId = element.getAttribute('data-message-id')
  if (directId?.trim()) {
    return directId.trim()
  }

  const childWithId = element.querySelector('[data-message-id]')
  if (childWithId) {
    const childId = childWithId.getAttribute('data-message-id')
    if (childId?.trim()) {
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
  if (!element || typeof element.getAttribute !== 'function') {
    return null
  }

  const timeEl = element.querySelector('time[datetime]')
  if (timeEl) {
    const dt = timeEl.getAttribute('datetime')
    if (dt?.trim()) {
      return dt.trim()
    }
  }

  const timestampAttr = element.getAttribute('data-timestamp')
  if (timestampAttr?.trim()) {
    return timestampAttr.trim()
  }

  const childWithTimestamp = element.querySelector('[data-timestamp]')
  if (childWithTimestamp) {
    const ts = childWithTimestamp.getAttribute('data-timestamp')
    if (ts?.trim()) {
      return ts.trim()
    }
  }

  return null
}

/**
 * Formats a `<pre><code>` code block into a markdown fenced-code-block string.
 * Detects language from `language-*` class, replaces the `<pre>` node in place.
 *
 * WARNING: mutates the passed node via `replaceWith`. Callers MUST pass a
 * detached clone (e.g. from `cloneNode(true)`), never a live-DOM node —
 * calling this on live page DOM would destroy the visible code block.
 *
 * @param pre - The `<pre>` element (detached clone) to transform.
 * @param ownerDocument - Optional document used to create text nodes (defaults to pre.ownerDocument).
 */
export function formatCodeBlock(pre: Element, ownerDocument?: Document): void {
  if (!pre) return
  const doc =
    ownerDocument || pre.ownerDocument || (typeof document !== 'undefined' ? document : null)
  if (!doc) return

  const codeElement = pre.querySelector('code')
  const rawCode = codeElement ? codeElement.textContent || '' : pre.textContent || ''

  let lang = ''
  const classAttr = codeElement?.getAttribute('class') || ''
  if (classAttr) {
    const match = classAttr.match(/language-([a-zA-Z0-9_-]+)/)
    if (match?.[1]) {
      lang = match[1]
    }
  }

  const formattedBlock = `\n\`\`\`${lang}\n${rawCode.trim()}\n\`\`\`\n`
  const textNode = doc.createTextNode(formattedBlock)
  pre.replaceWith(textNode)
}

/**
 * Normalizes raw textContent extracted from a DOM clone:
 * - Unifies CRLF/CR → LF
 * - Collapses 3+ consecutive newlines to 2
 * - Trims surrounding whitespace
 *
 * Non-string input yields '' (defensive: extraction clones can be nullish
 * mid-mutation when the page re-renders under the observer).
 */
export function normalizeExtractedText(rawText: string): string {
  if (typeof rawText !== 'string' || !rawText) {
    return ''
  }
  return rawText
    .replace(/\r\n|\r/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}

/**
 * Pairs sequential user/assistant RawMessageTurns into ExtractedInteractions.
 * Streaming or empty assistant turns are skipped WITHOUT consuming the pending
 * user turn, so a later completed assistant response still pairs (previously the
 * pending user was dropped on any skipped assistant, losing valid pairs like
 * U2+A2 in `U1,U2,A1(streaming),A2(complete)`).
 * A newer non-empty user turn overwrites an unpaired pending user (regeneration
 * branch semantics). Only the `platform` literal differs between callers.
 */
export function pairTurnsIntoInteractions(
  platform: ExtractedInteraction['platform'],
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
  let pairIndex = 0

  for (const turn of turns) {
    if (turn.role === 'user') {
      // Empty user turns are ignored and never overwrite a pending user.
      if (turn.text.length > 0) {
        if (pendingUserTurn) {
          pendingUserTurn = {
            ...turn,
            messageId: pendingUserTurn.messageId || turn.messageId,
            text: `${pendingUserTurn.text}\n\n${turn.text}`,
            sourceTimestamp: pendingUserTurn.sourceTimestamp || turn.sourceTimestamp,
          }
        } else {
          pendingUserTurn = turn
        }
      }
    } else if (turn.role === 'assistant' && pendingUserTurn) {
      if (!turn.isStreaming) {
        const queryText = cleanQueryText(pendingUserTurn.text)
        const responseText = cleanResponseText(turn.text)
        if (queryText.length > 0 && responseText.length > 0) {
          interactions.push({
            platform,
            conversationId: context.conversationId,
            messageId: turn.messageId,
            userMessageId: pendingUserTurn.messageId,
            model: context.model,
            queryText,
            responseText,
            conversationTitle: context.title,
            observedAt,
            sourceTimestamp: turn.sourceTimestamp ?? pendingUserTurn.sourceTimestamp ?? null,
            captureContext,
            turnIndex: pairIndex,
          })
          pendingUserTurn = null
          pairIndex++
        }
      }
      // Skipped (streaming/empty) assistants intentionally keep pendingUserTurn.
    }
  }

  return interactions
}
