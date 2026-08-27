/**
 * Text metric calculation utilities for calculating character lengths,
 * UTF-8 byte counts, and handling nullable token estimates.
 */

import type { InteractionTextMetrics } from './types'

const textEncoder = new TextEncoder()

/**
 * Calculates UTF-8 byte length for a given string without modifying the string.
 */
export function calculateUtf8Bytes(text: string): number {
  return textEncoder.encode(text).length
}

/**
 * Constructs a complete InteractionTextMetrics object with exact character
 * count and UTF-8 byte count.
 */
export function calculateTextMetrics(
  text: string,
  estimatedTokens?: number | null
): InteractionTextMetrics {
  return {
    text,
    characters: text.length,
    bytes: calculateUtf8Bytes(text),
    estimated_tokens: estimatedTokens !== undefined ? estimatedTokens : null,
  }
}
