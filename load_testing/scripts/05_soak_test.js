/**
 * 05_soak_test.js
 * Parameterized endurance test to detect slow memory leaks, event loop degradation,
 * database connection pool exhaustion, or unbounded background worker queue growth.
 * Configured via SOAK_DURATION (e.g. '30m', '1h', '24h') and VUS.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, standardThresholds } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'
import { createPRNG, getWorkloadSeed, deterministicPick } from '../helpers/prng.js'

const soakDuration = __ENV.SOAK_DURATION || '2m'
const vus = parseInt(__ENV.VUS || '10', 10)
const seed = getWorkloadSeed()

export const options = {
  stages: [
    { duration: '30s', target: vus },
    { duration: soakDuration, target: vus },
    { duration: '30s', target: 0 },
  ],
  thresholds: standardThresholds,
}

const comparativeDataset = JSON.parse(open('../datasets/comparative_workload.json'))

export function setup() {
  console.log(`[Soak Test] Starting soak run: VUs=${vus}, Duration=${soakDuration}, Seed=${seed}`)
  const healthRes = http.get(`${BASE_URL}/health`)
  console.log(`[Soak Test] Initial Health: status=${healthRes.status}`)
  return { startTime: Date.now(), seed: seed }
}

export default function (data) {
  const prng = createPRNG(data.seed + __VU * 10000 + __ITER)
  const item = deterministicPick(comparativeDataset, prng)

  let payload = item.payload
  if (item.intent === 'unique_miss') {
    payload = {
      prompt: `${item.payload.prompt} [soak-${data.seed}-${__VU}-${Date.now()}]`,
      temperature: 0.0,
    }
  }

  const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: `Soak_${item.intent}` },
  })

  assertSuccessResponse(res, `Soak_${item.intent}`)
  recordResponseMetrics(res, {
    intent: item.intent,
    isBypass: item.intent === 'bypass',
    isIneligible: item.intent === 'ineligible',
    payload: payload,
  })

  sleep(0.2)
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000
  console.log(`[Soak Test] Completed soak run in ${elapsedSec.toFixed(1)}s.`)
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const metrics = adminRes.json()
      const agg = metrics.aggregate || metrics
      console.log(
        `[Soak Test] Final Total Requests: ${agg.request_and_cache?.total_authenticated_requests}`
      )
      console.log(`[Soak Test] Final Exact Cache Hits: ${agg.request_and_cache?.exact_cache_hits}`)
      console.log(
        `[Soak Test] Final Exact Cache Misses: ${agg.request_and_cache?.exact_cache_misses}`
      )
      console.log(`[Soak Test] Final Shadow Drops: ${agg.shadow?.shadow_queue_dropped_total}`)
    } catch (e) {}
  }
}
