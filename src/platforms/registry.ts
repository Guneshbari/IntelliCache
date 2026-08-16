/**
 * Platform Adapter Registry
 *
 * Provides central registration and URL-based discovery for platform collectors.
 */

import { ChatGPTAdapter } from './chatgpt/adapter'
import type { PlatformAdapter } from './types'

const adapters: PlatformAdapter[] = [new ChatGPTAdapter()]

/**
 * Registers an adapter instance into the global registry.
 */
export function registerAdapter(adapter: PlatformAdapter): void {
  const existingIndex = adapters.findIndex((a) => a.platform === adapter.platform)
  if (existingIndex >= 0) {
    adapters[existingIndex] = adapter
  } else {
    adapters.push(adapter)
  }
}

/**
 * Finds the first registered platform adapter that can handle the given URL.
 */
export function getAdapterForUrl(url: string): PlatformAdapter | null {
  for (const adapter of adapters) {
    if (adapter.canHandle(url)) {
      return adapter
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
