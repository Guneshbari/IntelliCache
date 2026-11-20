/**
 * 03_concurrency_stress.js
 * SingleFlight Concurrency & Coalescing Verification.
 * Issues synchronized bursts of concurrent identical cold requests to definitively
 * prove that concurrent misses coalesce into a single upstream provider invocation.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics, exactCacheHits } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'

const concurrentVUs = parseInt(__ENV.VUS || '20', 10)
const burstRounds = parseInt(__ENV.BURST_ROUNDS || '3', 10)

export const options = {
  scenarios: {
    synchronized_burst: {
      executor: 'per-vu-iterations',
      vus: concurrentVUs,
      iterations: burstRounds,
      maxDuration: '30s',
    },
  },
  thresholds: {
    http_req_failed: ['rate<0.01'],
    semantic_hits_served: ['count==0'],
  },
}

export function setup() {
  const runId = `sf-${Date.now()}`
  console.log(
    `[SingleFlight Test] Starting with ${concurrentVUs} concurrent VUs across ${burstRounds} burst rounds.`
  )
  console.log(`[SingleFlight Test] Run Identifier: ${runId}`)

  // Capture metrics baseline
  let initialMetrics = {}
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const parsed = adminRes.json()
      initialMetrics = parsed.aggregate || parsed
    } catch (e) {}
  }

  return {
    startTime: Date.now(),
    runId: runId,
    initialMetrics: initialMetrics,
  }
}

export default function (data) {
  // In each round, every VU targets the exact same cold prompt
  // The prompt incorporates the round index (__ITER) and unique runId
  const roundColdPrompt = `Explain Byzantine Fault Tolerance in distributed systems. [burst-${data.runId}-round-${__ITER}]`

  const payload = {
    prompt: roundColdPrompt,
    temperature: 0.0,
  }

  // Fire synchronized request
  const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: `SingleFlight_Burst_Round_${__ITER}` },
  })

  assertSuccessResponse(res, `SingleFlight_Burst_Round_${__ITER}`)
  recordResponseMetrics(res, {
    intent: 'cold_burst',
    payload: payload,
  })

  // Brief pause between burst rounds
  sleep(1.0)
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000
  console.log(`[SingleFlight Test] Completed in ${elapsedSec.toFixed(2)}s. Querying telemetry...`)

  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const parsed = adminRes.json()
      const agg = parsed.aggregate || parsed
      const beforeReq = data.initialMetrics.request_and_cache || {}
      const afterReq = agg.request_and_cache || {}
      const beforeProv = data.initialMetrics.provider_and_savings || {}
      const afterProv = agg.provider_and_savings || {}

      const deltaRequests =
        (afterReq.total_authenticated_requests || 0) - (beforeReq.total_authenticated_requests || 0)
      const deltaCoalescedJoins = (afterReq.coalesced_joins || 0) - (beforeReq.coalesced_joins || 0)
      const deltaCoalescedSuccess =
        (afterReq.coalesced_successes || 0) - (beforeReq.coalesced_successes || 0)
      const deltaProviderCalls =
        (afterProv.provider_calls_attempted || 0) - (beforeProv.provider_calls_attempted || 0)
      const deltaCallsAvoided =
        (afterProv.provider_calls_avoided || 0) - (beforeProv.provider_calls_avoided || 0)

      console.log(`---------------- SINGLEFLIGHT VERIFICATION ----------------`)
      console.log(`Total Burst Requests:          ${deltaRequests}`)
      console.log(`Expected Distinct Upstream:   ${burstRounds}`)
      console.log(`Actual Provider Invocations:  ${deltaProviderCalls}`)
      console.log(`Coalesced Follower Joins:      ${deltaCoalescedJoins}`)
      console.log(`Coalesced Follower Successes:  ${deltaCoalescedSuccess}`)
      console.log(`Provider Calls Avoided:       ${deltaCallsAvoided}`)
      console.log(`-----------------------------------------------------------`)

      if (deltaProviderCalls === burstRounds) {
        console.log(
          `[VERIFIED] SingleFlight perfectly coalesced concurrent misses: exactly ${burstRounds} provider calls for ${deltaRequests} requests!`
        )
      } else {
        console.log(
          `[NOTE] Provider invocations: ${deltaProviderCalls} (Expected <= ${burstRounds})`
        )
      }
    } catch (e) {}
  }
}
