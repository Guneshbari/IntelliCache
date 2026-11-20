/**
 * 02_shadow_evaluation.js
 * Evaluates gateway throughput, latency, and overhead with semantic shadow evaluation enabled.
 * Uses identical mixed workload to compare fairly against baseline exact-cache run.
 * Gathers shadow metrics via /admin/metrics endpoint during teardown.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, standardThresholds } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'

export const options = {
  stages: [
    { duration: '5s', target: parseInt(__ENV.VUS || '10', 10) },
    { duration: __ENV.DURATION || '20s', target: parseInt(__ENV.VUS || '10', 10) },
    { duration: '5s', target: 0 },
  ],
  thresholds: standardThresholds,
}

const mixedDataset = JSON.parse(open('../datasets/mixed_workload.json'))

export function setup() {
  // Prime exact cache targets so exact hit ratio remains comparable
  const url = `${BASE_URL}/query`
  const exactTargets = mixedDataset.filter((d) => d.intent === 'exact_hit')
  for (const item of exactTargets) {
    http.post(url, JSON.stringify(item.payload), {
      headers: getClientHeaders(),
      tags: { name: 'Setup_Prime_Cache' },
    })
  }

  // Record initial telemetry from admin metrics
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  let initialMetrics = {}
  if (adminRes.status === 200) {
    try {
      initialMetrics = adminRes.json()
    } catch (e) {}
  }
  return { initialMetrics }
}

export default function () {
  const url = `${BASE_URL}/query`

  // Pick an item from mixed dataset
  const index = Math.floor(Math.random() * mixedDataset.length)
  const item = mixedDataset[index]

  let payload = item.payload
  // If unique miss, ensure fresh prompt
  if (item.intent === 'unique_miss') {
    payload = {
      prompt: `${item.payload.prompt} [vu-${__VU}-${Date.now()}-${Math.random()}]`,
      temperature: 0.0,
    }
  }

  const tag = `ShadowWorkload_${item.intent}`
  const res = http.post(url, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: tag },
  })

  assertSuccessResponse(res, tag)
  recordResponseMetrics(res)

  sleep(0.1)
}

export function teardown(data) {
  // Scrape final telemetry to record shadow evaluation activity
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const finalMetrics = adminRes.json()
      const agg = finalMetrics.aggregate || finalMetrics
      console.log('--- Post-Test Shadow Telemetry Snapshot ---')
      if (agg.shadow) {
        console.log(`Shadow Total Requests: ${agg.shadow.shadow_requests_total}`)
        console.log(`Shadow Sampled Requests: ${agg.shadow.shadow_requests_sampled}`)
        console.log(`Shadow Eligible Requests: ${agg.shadow.shadow_requests_eligible}`)
        console.log(`Shadow Hypothetical Hits: ${agg.shadow.shadow_hypothetical_hits}`)
        console.log(`Shadow Dropped Requests: ${agg.shadow.shadow_queue_dropped_total}`)
      }
      if (agg.request_and_cache) {
        console.log(`Exact Cache Lookups: ${agg.request_and_cache.exact_cache_lookups}`)
        console.log(`Exact Cache Hits: ${agg.request_and_cache.exact_cache_hits}`)
        console.log(`Exact Cache Misses: ${agg.request_and_cache.exact_cache_misses}`)
      }
    } catch (e) {}
  }
}
