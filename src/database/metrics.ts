/**
 * Text metric calculation utilities for calculating character lengths,
 * UTF-8 byte counts, and handling nullable token estimates.
 */

import { calculateUtf8Bytes } from '../shared/storage-format'
import type { InteractionTextMetrics } from './types'

export { calculateUtf8Bytes }

/**
 * Constructs a complete InteractionTextMetrics object with exact character
 * count and UTF-8 byte count.
 *
 * `characters` counts UTF-16 code units (`text.length`), matching the stored
 * contract asserted across the test suite — not grapheme clusters. Display code
 * that must not split emoji should slice via `Array.from(text)` instead.
 */
export function calculateTextMetrics(
  text: string,
  estimatedTokens?: number | null
): InteractionTextMetrics {
  if (typeof text !== 'string') {
    throw new TypeError('calculateTextMetrics: text must be a string')
  }
  return {
    text,
    characters: text.length,
    bytes: calculateUtf8Bytes(text),
    estimated_tokens: estimatedTokens !== undefined ? estimatedTokens : null,
  }
}
