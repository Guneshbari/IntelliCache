/**
 * Text Cleaning and Sanitization Utilities
 *
 * Provides shared sanitization for user queries and assistant responses:
 * - Strips accessibility / screen-reader speaker prefixes (e.g. "You said:", "ChatGPT said:")
 * - Deduplicates repeated phrase artifacts caused by dual DOM nodes or voice dictation
 * - Normalizes whitespace and line breaks
 */

const RE_REPEATED_PHRASE = /^(.{3,}?)(?:(?:[\s,.;:!?]+)\1)+$/i
const RE_HAS_WHITESPACE = /\s/
const RE_USER_PREFIX = /^\s*(?:you\s+said|user|human)\s*[:：\-–—]?\s*/i
const RE_SPLIT_LINES = /\r?\n/
const RE_ASSISTANT_PREFIX = /^\s*(?:chatgpt|claude|gemini|assistant)\s*said\s*[:：\-–—]?\s*/i

/**
 * Deduplicates exact or case-insensitive repeated phrase segments.
 * Only triggers if the repeating candidate contains whitespace (multi-word) or
 * exceeds 10 characters to avoid breaking intentional single-word repetitions
 * such as "no no no" or "bye bye".
 */
export function deduplicateRepeatedPhrase(text: string): string {
  if (!text || text.length < 8) return text
  const match = text.match(RE_REPEATED_PHRASE)
  if (match) {
    const candidate = match[1].trim()
    if (RE_HAS_WHITESPACE.test(candidate) || candidate.length > 10) {
      return candidate
    }
  }
  return text
}

/**
 * Cleans extracted user query text:
 * 1. Strips accessibility / screen-reader prefixes (e.g. "You said:", "you said")
 * 2. Deduplicates repeated phrases or mirrored text blocks (e.g. "Make the video longer make the video longer")
 * 3. Normalizes consecutive identical lines
 */
export function cleanQueryText(raw: string): string {
  if (typeof raw !== 'string' || !raw) return ''
  let text = raw.trim()

  // 1. Strip screen-reader / speaker prefixes (e.g. "You said:", "you said", "User:", "Human:")
  text = text.replace(RE_USER_PREFIX, '').trim()

  // 2. Check for exact or case-insensitive duplicate whole string (e.g. "foo bar foo bar")
  text = deduplicateRepeatedPhrase(text)

  // 3. Deduplicate consecutive identical lines (multi-line scrape artifacts)
  const lines = text.split(RE_SPLIT_LINES)
  if (lines.length >= 2) {
    const deduped: string[] = []
    for (let i = 0; i < lines.length; i++) {
      const current = lines[i]
      const prev = deduped[deduped.length - 1]
      if (
        current.trim() &&
        prev !== undefined &&
        prev.trim() &&
        current.trim().toLowerCase() === prev.trim().toLowerCase()
      ) {
        continue
      }
      deduped.push(current)
    }
    text = deduped.join('\n')
  }

  // 4. Re-check full duplication after line deduplication
  text = deduplicateRepeatedPhrase(text)

  return text
}

/**
 * Cleans extracted assistant response text:
 * 1. Strips accessibility / assistant speaker prefixes (e.g. "ChatGPT said:", "Claude said:", "Gemini said:")
 */
export function cleanResponseText(raw: string): string {
  if (typeof raw !== 'string' || !raw) return ''
  let text = raw.trim()

  // Strip screen-reader / assistant speaker prefixes (e.g. "ChatGPT said:", "Claude said:", "Assistant said:")
  text = text.replace(RE_ASSISTANT_PREFIX, '').trim()

  return text
}
