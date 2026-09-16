/**
 * Pure storage formatting helpers for IntelliCache.
 *
 * Zero-dependency leaf module (no Dexie, no DOM): safe to import from the
 * popup, content scripts, service worker, and tests alike.
 */

/** Binary byte units used across the dashboard. 1 KiB = 1024 bytes. */
const BYTE_UNITS = ['B', 'KiB', 'MiB', 'GiB'] as const

let cachedEncoder: TextEncoder | null = null

function getEncoder(): TextEncoder | null {
  if (typeof TextEncoder !== 'undefined') {
    if (!cachedEncoder) {
      cachedEncoder = new TextEncoder()
    }
    return cachedEncoder
  }
  return null
}

/**
 * Manual UTF-8 length fallback for runtimes without TextEncoder.
 * Mirrors the counting semantics of `calculateUtf8Bytes` in database/metrics.
 */
function manualUtf8Length(text: string): number {
  let bytes = 0
  const len = text.length
  for (let i = 0; i < len; i++) {
    const code = text.charCodeAt(i)
    if (code <= 0x7f) {
      bytes += 1
    } else if (code <= 0x7ff) {
      bytes += 2
    } else if (code >= 0xd800 && code <= 0xdbff) {
      if (i + 1 < len) {
        const next = text.charCodeAt(i + 1)
        if (next >= 0xdc00 && next <= 0xdfff) {
          bytes += 4
          i++
          continue
        }
      }
      bytes += 3
    } else {
      bytes += 3
    }
  }
  return bytes
}

/**
 * Returns the UTF-8 byte length of a value.
 * Non-string values (null, undefined, numbers, objects) contribute 0 bytes so
 * callers can measure nullable persisted fields without per-field guards.
 */
export function utf8ByteLength(value: unknown): number {
  if (typeof value !== 'string' || value.length === 0) {
    return 0
  }
  const encoder = getEncoder()
  if (encoder) {
    return encoder.encode(value).byteLength
  }
  return manualUtf8Length(value)
}

/**
 * Formats a raw byte count with binary units (B, KiB, MiB, GiB).
 * Raw values are never rounded before calculation — only the displayed unit
 * value is shortened (integers shown whole, fractions with one decimal).
 * Non-finite or negative input renders as '0 B'.
 */
export function formatBytes(bytes: number): string {
  if (typeof bytes !== 'number' || !Number.isFinite(bytes) || bytes < 0) {
    return '0 B'
  }
  if (bytes < 1024) {
    return `${Math.floor(bytes)} B`
  }
  let value = bytes
  let unit = 0
  while (value >= 1024 && unit < BYTE_UNITS.length - 1) {
    value /= 1024
    unit++
  }
  const rounded = Math.round(value * 10) / 10
  const display = Number.isInteger(rounded) ? rounded.toFixed(0) : rounded.toFixed(1)
  return `${display} ${BYTE_UNITS[unit]}`
}
