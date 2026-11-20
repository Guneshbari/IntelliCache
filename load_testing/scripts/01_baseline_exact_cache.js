/**
 * 01_baseline_exact_cache.js
 * Measures baseline performance of the IntelliCache gateway with exact-match caching.
 * Uses reproducible deterministic query selection and warms up targets during setup.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, standardThresholds } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'
import { createPRNG, getWorkloadSeed, deterministicPick } from '../helpers/prng.js'

const vus = parseInt(__ENV.VUS || '10', 10)
const duration = __ENV.DURATION || '20s'
const seed = getWorkloadSeed()

export const options = {
  stages: [
    { duration: '3s', target: vus }, // Warm-up ramp
    { duration: duration, target: vus }, // Steady-state load
    { duration: '2s', target: 0 }, // Cooldown
  ],
  thresholds: standardThresholds,
}

const comparativeDataset = JSON.parse(open('../datasets/comparative_workload.json'))

export function setup() {
  console.log(`[Baseline Load] VUs: ${vus}, Duration: ${duration}, Seed: ${seed}`)

  // 1. Warm up exact targets so they are primed in cache
  const warmupHeaders = getClientHeaders()
  const exactTargets = comparativeDataset.filter((item) => item.intent === 'exact_target')

  for (const item of exactTargets) {
    http.post(`${BASE_URL}/query`, JSON.stringify(item.payload), {
      headers: warmupHeaders,
      tags: { name: 'Warmup_Prime' },
    })
  }

  // 2. Capture baseline metric counters before run
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
  // Deterministic PRNG per VU and iteration
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
    tags: { name: `Baseline_${item.intent}` },
  })

  assertSuccessResponse(res, `Baseline_${item.intent}`)
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
  console.log(`[Baseline Load] Complete. Total wall-clock: ${elapsedSec.toFixed(2)}s`)

  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const parsed = adminRes.json()
      const agg = parsed.aggregate || parsed
      const beforeReq = data.initialMetrics.request_and_cache || {}
      const afterReq = agg.request_and_cache || {}

      const deltaReqs =
        (afterReq.total_authenticated_requests || 0) - (beforeReq.total_authenticated_requests || 0)
      const deltaHits = (afterReq.exact_cache_hits || 0) - (beforeReq.exact_cache_hits || 0)
      const deltaMisses = (afterReq.exact_cache_misses || 0) - (beforeReq.exact_cache_misses || 0)

      console.log(`[Baseline Telemetry] Total Reqs Delta: ${deltaReqs}`)
      console.log(`[Baseline Telemetry] Exact Hits: ${deltaHits}, Exact Misses: ${deltaMisses}`)
    } catch (e) {}
  }
}
