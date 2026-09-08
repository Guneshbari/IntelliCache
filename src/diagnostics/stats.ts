/**
 * Diagnostic Statistics Store
 *
 * Provides lightweight in-memory counters for the testing phase to track
 * collector performance, turn extraction counts, and error frequency.
 *
 * NOTE: Diagnostic counters are held strictly in memory and do not affect
 * persistent application state or IndexedDB schemas.
 */

import type { DiagnosticCounters } from './types'

function createDefaultCounters(): DiagnosticCounters {
  return {
    domScans: 0,
    userTurnsFound: 0,
    assistantTurnsFound: 0,
    completePairs: 0,
    interactionsExtracted: 0,
    interactionsQueued: 0,
    interactionsSaved: 0,
    duplicates: 0,
    streamingDeferrals: 0,
    missingConversationIds: 0,
    extractionFailures: 0,
    persistenceFailures: 0,
  }
}

export class DiagnosticStats {
  private counters: DiagnosticCounters = createDefaultCounters()

  /**
   * Increments a diagnostic counter by a given amount (default: 1).
   */
  increment(counter: keyof DiagnosticCounters, amount = 1): void {
    this.counters[counter] += amount
  }

  /**
   * Retrieves the current value of a specific counter.
   */
  get(counter: keyof DiagnosticCounters): number {
    return this.counters[counter]
  }

  /**
   * Returns an immutable snapshot copy of all counters.
   */
  getSnapshot(): Readonly<DiagnosticCounters> {
    return { ...this.counters }
  }

  /**
   * Resets all counters back to zero.
   */
  reset(): void {
    this.counters = createDefaultCounters()
  }
}

export const diagnosticStats = new DiagnosticStats()
