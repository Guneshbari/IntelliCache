/**
 * Text metric calculation utilities for calculating character lengths,
 * UTF-8 byte counts, and handling nullable token estimates.
 */

import type { InteractionTextMetrics } from './types'

/**
 * Calculates UTF-8 byte length for a given string without allocating memory buffers.
 * Non-string input is treated as empty (returns 0) to stay total on the hot path;
 * callers that need strictness should validate before measuring.
 */
export function calculateUtf8Bytes(text: string): number {
  if (typeof text !== 'string' || !text) return 0
  let bytes = 0
  const len = text.length
  for (let i = 0; i < len; i++) {
    const code = text.charCodeAt(i)
    if (code <= 0x7f) {
      bytes += 1
    } else if (code <= 0x7ff) {
      bytes += 2
    } else if (code >= 0xd800 && code <= 0xdbff) {
      // High surrogate: check if followed by valid low surrogate (surrogate pair)
      if (i + 1 < len) {
        const nextCode = text.charCodeAt(i + 1)
        if (nextCode >= 0xdc00 && nextCode <= 0xdfff) {
          bytes += 4
          i++
          continue
        }
      }
      bytes += 3 // Unpaired surrogate replacement (U+FFFD is 3 bytes in UTF-8)
    } else if (code >= 0xdc00 && code <= 0xdfff) {
      bytes += 3 // Unpaired low surrogate
    } else {
      bytes += 3
    }
  }
  return bytes
}

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
