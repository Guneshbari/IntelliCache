/**
 * 02_shadow_evaluation.js
 * Evaluates the IntelliCache gateway under shadow evaluation mode.
 * Uses the IDENTICAL dataset, stages, seed, and distribution as 01_baseline_exact_cache.js
 * to ensure strict ceteris paribus comparability for overhead measurement.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, standardThresholds } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import {
  recordResponseMetrics,
  exactCacheHits,
  exactCacheMisses,
  exactHitLatency,
  cacheMissLatency,
} from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'
import { createPRNG, getWorkloadSeed, deterministicPick } from '../helpers/prng.js'

export { exactCacheHits, exactCacheMisses, exactHitLatency, cacheMissLatency }

const vus = parseInt(__ENV.VUS || '10', 10)
const duration = __ENV.DURATION || '20s'
const iterations = parseInt(__ENV.ITERATIONS || '0', 10)
const seed = getWorkloadSeed()

export const options =
  iterations > 0
    ? {
        scenarios: {
          fixed_shadow: {
            executor: 'per-vu-iterations',
            vus: vus,
            iterations: Math.floor(iterations / vus),
            maxDuration: '120s',
          },
        },
        thresholds: standardThresholds,
      }
    : {
        stages: [
          { duration: '3s', target: vus }, // Warm-up ramp
          { duration: duration, target: vus }, // Steady-state load
          { duration: '2s', target: 0 }, // Cooldown
        ],
        thresholds: standardThresholds,
      }

const comparativeDataset = JSON.parse(open('../datasets/comparative_workload.json'))

export function setup() {
  console.log(`[Shadow Load] VUs: ${vus}, Duration: ${duration}, Seed: ${seed}`)

  // 1. Warm up exact targets so they are primed in cache (identical to baseline)
  const warmupHeaders = getClientHeaders()
  const exactTargets = comparativeDataset.filter((item) => item.intent === 'exact_target')

  for (const item of exactTargets) {
    http.post(`${BASE_URL}/query`, JSON.stringify(item.payload), {
      headers: warmupHeaders,
      tags: { name: 'Warmup_Prime' },
    })
  }

  // 2. Capture shadow and request metric counters before run
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
    seed: seed,
    initialMetrics: initialMetrics,
  }
}

export default function (data) {
  // Deterministic PRNG per VU and iteration (identical formula to baseline)
  const prng = createPRNG(data.seed + __VU * 10000 + __ITER)
  const item = deterministicPick(comparativeDataset, prng)

  let payload = item.payload
  if (item.intent === 'unique_miss') {
    payload = {
      prompt: `${item.payload.prompt} [seed-${data.seed}-vu-${__VU}-iter-${__ITER}]`,
      temperature: 0.0,
    }
  }

  const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: `Shadow_${item.intent}` },
  })

  assertSuccessResponse(res, `Shadow_${item.intent}`)
  recordResponseMetrics(res, {
    intent: item.intent,
    isBypass: item.intent === 'bypass',
    isIneligible: item.intent === 'ineligible',
    payload: payload,
  })

  sleep(0.05)
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000
  console.log(`[Shadow Load] Complete. Total wall-clock: ${elapsedSec.toFixed(2)}s`)

  // Allow shadow background workers to drain pending queue items
  sleep(1.0)

  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const parsed = adminRes.json()
      const agg = parsed.aggregate || parsed
      const beforeShadow = data.initialMetrics.shadow || {}
      const afterShadow = agg.shadow || {}

      const deltaSampled =
        (afterShadow.shadow_requests_sampled || 0) - (beforeShadow.shadow_requests_sampled || 0)
      const deltaUnsampled =
        (afterShadow.shadow_requests_unsampled || 0) - (beforeShadow.shadow_requests_unsampled || 0)
      const deltaEligible =
        (afterShadow.shadow_requests_eligible || 0) - (beforeShadow.shadow_requests_eligible || 0)
      const deltaHypoHits =
        (afterShadow.shadow_hypothetical_hits || 0) - (beforeShadow.shadow_hypothetical_hits || 0)
      const deltaDrops =
        (afterShadow.shadow_queue_dropped_total || 0) -
        (beforeShadow.shadow_queue_dropped_total || 0)
      const deltaExecFailures =
        (afterShadow.shadow_execution_failures || 0) - (beforeShadow.shadow_execution_failures || 0)

      console.log(`[Shadow Telemetry] Sampled: ${deltaSampled}, Unsampled: ${deltaUnsampled}`)
      console.log(
        `[Shadow Telemetry] Eligible: ${deltaEligible}, Hypothetical Hits: ${deltaHypoHits}`
      )
      console.log(
        `[Shadow Telemetry] Queue Drops: ${deltaDrops}, Exec Failures: ${deltaExecFailures}`
      )
      if (agg.latencies) {
        console.log(
          `[Shadow Latencies] Embedding Mean: ${agg.latencies.shadow_embedding_latency_ms?.mean}ms`
        )
        console.log(
          `[Shadow Latencies] Retrieval Mean: ${agg.latencies.shadow_retrieval_latency_ms?.mean}ms`
        )
        console.log(
          `[Shadow Latencies] Policy Mean: ${agg.latencies.shadow_policy_latency_ms?.mean}ms`
        )
        console.log(
          `[Shadow Latencies] Execution Mean: ${agg.latencies.shadow_execution_latency_ms?.mean}ms`
        )
      }
    } catch (e) {}
  }
}
