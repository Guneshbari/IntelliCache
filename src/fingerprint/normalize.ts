/**
 * Text normalization utilities strictly dedicated to deterministic fingerprint generation.
 *
 * IMPORTANT: Normalization is applied solely during hashing. Stored raw query and response
 * text must NEVER be mutated or altered by this function.
 */

/**
 * Normalizes text for deterministic fingerprinting:
 * 1. Unicode NFC normalization.
 * 2. Unification of carriage returns and newlines (\r\n -> \n).
 * 3. Normalizes non-breaking spaces to regular spaces; strips zero-width
 *    characters (ZWSP/ZWNJ/ZWJ/BOM) that otherwise cause near-duplicate
 *    prompts to hash differently across platforms.
 * 4. Collapsing multiple horizontal spaces and tabs into a single space.
 * 5. Trimming horizontal whitespace around line breaks.
 * 6. Collapsing 3+ consecutive newlines to double newlines.
 * 7. Trimming outer leading and trailing whitespace.
 */
const RE_NBSP = /\u00A0/g
const RE_ZERO_WIDTH = /\u200B|\u200C|\u200D|\uFEFF/g
const RE_CRLF = /\r\n|\r/g
const RE_HORIZONTAL_SPACES = /[ \t]+/g
const RE_WS_AROUND_NL = /[ \t]*\n[ \t]*/g
const RE_MULTI_NL = /\n{3,}/g

export function normalizeTextForFingerprint(text: string): string {
  if (typeof text !== 'string' || !text) {
    return ''
  }

  return text
    .normalize('NFC')
    .replace(RE_NBSP, ' ')
    .replace(RE_ZERO_WIDTH, '')
    .replace(RE_CRLF, '\n')
    .replace(RE_HORIZONTAL_SPACES, ' ')
    .replace(RE_WS_AROUND_NL, '\n')
    .replace(RE_MULTI_NL, '\n\n')
    .trim()
}
