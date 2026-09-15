/**
 * Platform Adapter Registry
 *
 * Provides central registration and URL-based discovery for platform collectors.
 */

import { ChatGPTAdapter } from './chatgpt/adapter'
import { ClaudeAdapter } from './claude/adapter'
import { GeminiAdapter } from './gemini/adapter'
import type { PlatformAdapter } from './types'

const adapters: PlatformAdapter[] = [new ChatGPTAdapter(), new ClaudeAdapter(), new GeminiAdapter()]

/**
 * Registers an adapter instance into the global registry.
 * Validates adapter shape so a buggy caller cannot poison URL discovery.
 */
export function registerAdapter(adapter: PlatformAdapter): void {
  if (
    !adapter ||
    typeof adapter !== 'object' ||
    typeof adapter.platform !== 'string' ||
    typeof adapter.canHandle !== 'function'
  ) {
    throw new TypeError('registerAdapter: adapter must expose platform and canHandle(url)')
  }
  const existingIndex = adapters.findIndex((a) => a.platform === adapter.platform)
  if (existingIndex >= 0) {
    adapters[existingIndex] = adapter
  } else {
    adapters.push(adapter)
  }
}

/**
 * Finds the first registered platform adapter that can handle the given URL.
 * A throwing canHandle never breaks discovery — it is treated as "cannot handle".
 */
export function getAdapterForUrl(url: string): PlatformAdapter | null {
  for (const adapter of adapters) {
    try {
      if (adapter.canHandle(url)) {
        return adapter
      }
    } catch {
      continue
    }
  }
  return null
}

/**
 * Returns all currently registered platform adapters.
 */
export function getAllAdapters(): PlatformAdapter[] {
  return [...adapters]
}
