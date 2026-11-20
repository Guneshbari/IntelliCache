/**
 * 03_concurrency_stress.js
 * SingleFlight Concurrency & Coalescing Verification.
 * Issues synchronized bursts of concurrent identical cold requests using an explicit
 * wall-clock barrier to guarantee all 20 VUs burst at the exact same millisecond.
 * Enforces automated hard threshold gating for exact request counts, provider calls,
 * coalesced follower joins, and provider calls avoided.
 */

import http from 'k6/http'
import { check, sleep } from 'k6'
import exec from 'k6/execution'
import { Counter } from 'k6/metrics'
import { BASE_URL } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'

const concurrentVUs = parseInt(__ENV.CONCURRENT_VUS || '20', 10)
const burstRounds = parseInt(__ENV.BURST_ROUNDS || '3', 10)
const burstIntervalSec = 2.5

export const sfAssertionErrors = new Counter('sf_assertion_errors')

export const options = {
  scenarios: {
    synchronized_burst: {
      executor: 'per-vu-iterations',
      vus: concurrentVUs,
      iterations: burstRounds,
      maxDuration: '30s',
      exec: 'executeBurst',
    },
    verification_probe: {
      executor: 'per-vu-iterations',
      vus: 1,
      iterations: 1,
      startTime: `${Math.ceil(burstRounds * burstIntervalSec + 2.0)}s`,
      maxDuration: '10s',
      exec: 'verifySingleFlight',
    },
  },
  thresholds: {
    http_req_failed: ['rate==0'],
    sf_assertion_errors: ['count==0'],
    semantic_hits_served: ['count==0'],
  },
}

export function setup() {
  const runId = `sf-${Date.now()}`
  console.log(
    `[SingleFlight Test] Starting with ${concurrentVUs} concurrent VUs across ${burstRounds} burst rounds.`
  )
  console.log(`[SingleFlight Test] Run Identifier: ${runId}`)

  // Capture metrics baseline before burst traffic
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

  // Pre-calculate exact wall-clock start times for each burst round (barrier coordination)
  const now = Date.now()
  const roundStartTimes = []
  for (let r = 0; r < burstRounds; r++) {
    roundStartTimes.push(now + 1500 + Math.floor(r * burstIntervalSec * 1000))
  }

  return {
    startTime: now,
    runId: runId,
    roundStartTimes: roundStartTimes,
    initialMetrics: initialMetrics,
  }
}

/**
 * Executes synchronized burst requests.
 * All VUs synchronize against the pre-calculated wall-clock barrier for each round.
 */
export function executeBurst(data) {
  // 1. Barrier synchronization: sleep until exact target timestamp for this round
  const roundIdx = exec.vu.iterationInScenario
  const targetTime = data.roundStartTimes[roundIdx]
  const now = Date.now()
  if (targetTime > now) {
    sleep((targetTime - now) / 1000.0)
  }

  // In each round, every VU targets the exact same cold prompt
  const roundColdPrompt = `Explain Byzantine Fault Tolerance in distributed systems. [burst-${data.runId}-round-${roundIdx}]`

  const payload = {
    prompt: roundColdPrompt,
    temperature: 0.0,
  }

  // Fire synchronized request
  const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: `SingleFlight_Burst_Round_${roundIdx}` },
  })

  assertSuccessResponse(res, `SingleFlight_Burst_Round_${roundIdx}`)
  recordResponseMetrics(res, {
    intent: 'cold_burst',
    payload: payload,
  })
}

/**
 * Automated Verification Probe: runs as a dedicated post-burst scenario.
 * Queries admin metrics and asserts on exact SingleFlight coalescing invariants.
 */
export function verifySingleFlight(data) {
  // Brief sleep to ensure all burst requests and logging have settled
  sleep(0.5)

  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })

  const expectedTotalRequests = concurrentVUs * burstRounds
  const expectedProviderCalls = burstRounds
  const expectedFollowerJoins = (concurrentVUs - 1) * burstRounds
  const expectedCallsAvoided = (concurrentVUs - 1) * burstRounds

  if (adminRes.status !== 200) {
    sfAssertionErrors.add(1)
    console.error(`[SingleFlight Probe] Failed to fetch admin metrics: status ${adminRes.status}`)
    return
  }

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

    console.log(`\n================ SINGLEFLIGHT AUTOMATED ASSERTION ================`)
    console.log(
      `Total Requests:      ${deltaRequests} / ${expectedTotalRequests} (Expected: ${expectedTotalRequests})`
    )
    console.log(
      `Provider Invocations: ${deltaProviderCalls} / ${expectedProviderCalls} (Expected: ${expectedProviderCalls})`
    )
    console.log(
      `Coalesced Joins:     ${deltaCoalescedJoins} / ${expectedFollowerJoins} (Expected: ${expectedFollowerJoins})`
    )
    console.log(
      `Coalesced Successes: ${deltaCoalescedSuccess} / ${expectedFollowerJoins} (Expected: ${expectedFollowerJoins})`
    )
    console.log(
      `Calls Avoided:       ${deltaCallsAvoided} / ${expectedCallsAvoided} (Expected: ${expectedCallsAvoided})`
    )
    console.log(`==================================================================\n`)

    const checksPassed = check(agg, {
      'SingleFlight: total requests match expected': () => deltaRequests === expectedTotalRequests,
      'SingleFlight: provider calls match expected burst rounds': () =>
        deltaProviderCalls === expectedProviderCalls,
      'SingleFlight: follower joins match expected': () =>
        deltaCoalescedJoins === expectedFollowerJoins,
      'SingleFlight: follower successes match expected': () =>
        deltaCoalescedSuccess === expectedFollowerJoins,
      'SingleFlight: provider calls avoided match expected': () =>
        deltaCallsAvoided === expectedCallsAvoided,
    })

    if (!checksPassed) {
      sfAssertionErrors.add(1)
      console.error(
        `[SingleFlight Probe] HARD ASSERTION FAILED: metrics did not match exact expected coalescing numbers.`
      )
    }
  } catch (err) {
    sfAssertionErrors.add(1)
    console.error(`[SingleFlight Probe] Assertion evaluation error: ${err}`)
  }
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000
  console.log(`[SingleFlight Test] Execution finished in ${elapsedSec.toFixed(2)}s.`)
}
