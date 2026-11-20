/**
 * Custom k6 metrics definitions.
 * Distinguishes exact hits, exact misses, explicit bypass, and guards invariant violations.
 */

import { Counter, Trend } from 'k6/metrics'

// Counters
export const exactCacheHits = new Counter('exact_cache_hits')
export const exactCacheMisses = new Counter('exact_cache_misses')
export const bypassRequests = new Counter('bypass_requests')
export const ineligibleRequests = new Counter('ineligible_requests')

// Critical Invariant: semantic hits served to user MUST BE 0!
export const semanticHitsServed = new Counter('semantic_hits_served')

// Latency trends
export const exactHitLatency = new Trend('exact_hit_latency_ms', true)
export const cacheMissLatency = new Trend('cache_miss_latency_ms', true)
export const gatewayProcessingLatency = new Trend('gateway_reported_latency_ms', true)

export function recordResponseMetrics(res) {
  if (!res || res.status !== 200) {
    return
  }

  try {
    const body = res.json()
    if (!body) return

    if (body.latency_ms !== undefined) {
      gatewayProcessingLatency.add(body.latency_ms)
    }

    if (body.cached === true && body.cache_type === 'exact') {
      exactCacheHits.add(1)
      exactHitLatency.add(res.timings.duration)
    } else if (body.cached === true && body.cache_type === 'semantic') {
      // INVARIANT BREACH! Semantic hit served to user!
      semanticHitsServed.add(1)
    } else {
      exactCacheMisses.add(1)
      cacheMissLatency.add(res.timings.duration)
    }
  } catch (e) {
    // Non-JSON response
  }
}
