/**
 * Custom k6 metrics definitions and classification.
 * Strictly separates:
 * - exact_cache_hits: Cache hits served from exact cache
 * - exact_cache_misses: Eligible queries evaluated against exact cache that missed
 * - bypass_requests: Queries with explicit bypass_cache flag set
 * - ineligible_requests: Queries ineligible for caching (streaming, temperature > 0, etc.)
 * - invalid_requests: HTTP 4xx / 5xx error responses
 * - semantic_hits_served: Critical invariant counter (MUST BE 0)
 */

import { Counter, Trend } from 'k6/metrics'

// Counters
export const exactCacheHits = new Counter('exact_cache_hits')
export const exactCacheMisses = new Counter('exact_cache_misses')
export const bypassRequests = new Counter('bypass_requests')
export const ineligibleRequests = new Counter('ineligible_requests')
export const invalidRequests = new Counter('invalid_requests')

// Workload vs Lifecycle Counters (Stage 3.4.1.4.3)
export const workloadRequests = new Counter('workload_requests')
export const warmupRequests = new Counter('warmup_requests')
export const adminRequests = new Counter('admin_requests')

// Critical Invariant: semantic hits served to user MUST BE 0!
export const semanticHitsServed = new Counter('semantic_hits_served')

// Latency trends
export const exactHitLatency = new Trend('exact_hit_latency_ms', true)
export const cacheMissLatency = new Trend('cache_miss_latency_ms', true)
export const gatewayProcessingLatency = new Trend('gateway_reported_latency_ms', true)

// Workload vs Lifecycle Latency Trends (Stage 3.4.1.4.3)
export const workloadReqLatency = new Trend('workload_req_duration_ms', true)
export const warmupReqLatency = new Trend('warmup_req_duration_ms', true)
export const adminReqLatency = new Trend('admin_req_duration_ms', true)
export const workloadStartTime = new Trend('workload_start_time', false)
export const workloadEndTime = new Trend('workload_end_time', false)

/**
 * Classifies and records response metrics.
 * @param {object} res - k6 HTTP response
 * @param {object} context - Request metadata: { isBypass, isIneligible, intent, payload }
 */
export function recordResponseMetrics(res, context = {}) {
  if (!res) return

  // 1. Invalid / Error HTTP responses
  if (res.status >= 400) {
    invalidRequests.add(1)
    return
  }

  // 2. Successful HTTP 200 responses
  try {
    const body = res.json()
    if (!body) return

    if (body.latency_ms !== undefined) {
      gatewayProcessingLatency.add(body.latency_ms)
    }

    // Critical Invariant Check
    if (body.cached === true && body.cache_type === 'semantic') {
      semanticHitsServed.add(1)
      return
    }

    // Exact Cache Hit
    if (body.cached === true && body.cache_type === 'exact') {
      exactCacheHits.add(1)
      exactHitLatency.add(res.timings.duration)
      return
    }

    // Explicit Bypass Request
    const isBypass =
      context.isBypass === true ||
      context.intent === 'bypass' ||
      (context.payload && context.payload.bypass_cache === true)

    if (isBypass) {
      bypassRequests.add(1)
      return
    }

    // Ineligible Request (e.g. temperature > 0, streaming, complex ineligible prompt)
    const isIneligible =
      context.isIneligible === true ||
      context.intent === 'ineligible' ||
      (context.payload &&
        context.payload.temperature !== undefined &&
        context.payload.temperature > 0.0)

    if (isIneligible) {
      ineligibleRequests.add(1)
      return
    }

    // Eligible Exact Cache Miss
    exactCacheMisses.add(1)
    cacheMissLatency.add(res.timings.duration)
  } catch (e) {
    invalidRequests.add(1)
  }
}
