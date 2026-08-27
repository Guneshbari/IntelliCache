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
 * 3. Collapsing multiple horizontal spaces and tabs into a single space.
 * 4. Trimming horizontal whitespace around line breaks.
 * 5. Collapsing 3+ consecutive newlines to double newlines.
 * 6. Trimming outer leading and trailing whitespace.
 */
export function normalizeTextForFingerprint(text: string): string {
  if (!text) {
    return ''
  }

  return text
    .normalize('NFC')
    .replace(/\r\n|\r/g, '\n')
    .replace(/[ \t]+/g, ' ')
    .replace(/[ \t]*\n[ \t]*/g, '\n')
    .replace(/\n{3,}/g, '\n\n')
    .trim()
}
