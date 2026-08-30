import { beforeEach, describe, expect, it } from 'vitest'
import { DiagnosticStats, diagnosticStats, type DiagnosticCounters } from '../src/diagnostics'

describe('Diagnostic Statistics Store Unit Tests', () => {
  let stats: DiagnosticStats

  beforeEach(() => {
    stats = new DiagnosticStats()
    diagnosticStats.reset()
  })

  it('initializes all 12 diagnostic counters to zero', () => {
    const snapshot = stats.getSnapshot()
    const expectedKeys: (keyof DiagnosticCounters)[] = [
      'domScans',
      'userTurnsFound',
      'assistantTurnsFound',
      'completePairs',
      'interactionsExtracted',
      'interactionsQueued',
      'interactionsSaved',
      'duplicates',
      'streamingDeferrals',
      'missingConversationIds',
      'extractionFailures',
      'persistenceFailures',
    ]

    expect(Object.keys(snapshot)).toHaveLength(12)
    for (const key of expectedKeys) {
      expect(snapshot[key]).toBe(0)
      expect(stats.get(key)).toBe(0)
    }
  })

  it('increments each counter individually by default amount (1)', () => {
    stats.increment('domScans')
    stats.increment('userTurnsFound')
    stats.increment('assistantTurnsFound')
    stats.increment('completePairs')
    stats.increment('interactionsExtracted')
    stats.increment('interactionsQueued')
    stats.increment('interactionsSaved')
    stats.increment('duplicates')
    stats.increment('streamingDeferrals')
    stats.increment('missingConversationIds')
    stats.increment('extractionFailures')
    stats.increment('persistenceFailures')

    const snapshot = stats.getSnapshot()
    for (const key of Object.keys(snapshot) as (keyof DiagnosticCounters)[]) {
      expect(snapshot[key]).toBe(1)
    }
  })

  it('supports custom increment amounts', () => {
    stats.increment('userTurnsFound', 5)
    stats.increment('assistantTurnsFound', 5)
    stats.increment('completePairs', 5)

    expect(stats.get('userTurnsFound')).toBe(5)
    expect(stats.get('assistantTurnsFound')).toBe(5)
    expect(stats.get('completePairs')).toBe(5)

    stats.increment('userTurnsFound', 3)
    expect(stats.get('userTurnsFound')).toBe(8)
  })

  it('provides immutable snapshots that do not mutate with subsequent increments', () => {
    stats.increment('domScans', 2)
    const snapshot1 = stats.getSnapshot()
    expect(snapshot1.domScans).toBe(2)

    stats.increment('domScans', 3)
    const snapshot2 = stats.getSnapshot()
    expect(snapshot1.domScans).toBe(2)
    expect(snapshot2.domScans).toBe(5)
  })

  it('resets all counters back to zero correctly', () => {
    stats.increment('domScans', 10)
    stats.increment('interactionsSaved', 7)
    stats.increment('duplicates', 3)

    expect(stats.get('domScans')).toBe(10)
    expect(stats.get('interactionsSaved')).toBe(7)

    stats.reset()

    const snapshot = stats.getSnapshot()
    for (const key of Object.keys(snapshot) as (keyof DiagnosticCounters)[]) {
      expect(snapshot[key]).toBe(0)
    }
  })

  it('global diagnosticStats singleton operates correctly across operations', () => {
    diagnosticStats.increment('domScans', 1)
    diagnosticStats.increment('userTurnsFound', 2)

    expect(diagnosticStats.get('domScans')).toBe(1)
    expect(diagnosticStats.get('userTurnsFound')).toBe(2)

    diagnosticStats.reset()
    expect(diagnosticStats.get('domScans')).toBe(0)
    expect(diagnosticStats.get('userTurnsFound')).toBe(0)
  })
})
