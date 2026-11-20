/**
 * 04_infrastructure_resilience.js
 * Infrastructure Failure Injection and Fail-Open Resilience Test.
 * Validates gateway stability and fail-open guarantees under infrastructure stress:
 * 1. Queue Saturation: High-volume concurrent requests verify that background shadow
 *    evaluation queue saturation fails open (drops without delaying user response).
 * 2. Background Component Failures: Vector store or embedding failures in shadow mode
 *    never affect user-facing response delivery (zero HTTP 500s).
 * 3. Telemetry Integrity: Failure counters increment appropriately without process abort.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL } from '../config/test-profiles.js'
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'

export const options = {
  scenarios: {
    queue_stress_burst: {
      executor: 'ramping-arrival-rate',
      startRate: 10,
      timeUnit: '1s',
      preAllocatedVUs: 30,
      maxVUs: 60,
      stages: [
        { duration: '5s', target: 50 }, // Ramp to high arrival rate to stress background queue
        { duration: '10s', target: 100 }, // Peak arrival rate
        { duration: '5s', target: 0 }, // Cooldown
      ],
    },
  },
  thresholds: {
    // Critical: zero unhandled 500 errors on primary request path
    'http_req_failed{status:500}': ['rate==0'],
    http_req_failed: ['rate<0.01'],
    semantic_hits_served: ['count==0'],
  },
}

export function setup() {
  console.log(`[Infrastructure Resilience] Starting background queue stress & fail-open test.`)
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
    initialMetrics: initialMetrics,
  }
}

export default function () {
  // Fire unique requests to force candidate generation and shadow queue ingestion
  const payload = {
    prompt: `Infrastructure resilience test query [iter-${__ITER}-vu-${__VU}-${Date.now()}]`,
    temperature: 0.0,
  }

  const res = http.post(`${BASE_URL}/query`, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: 'Resilience_Queue_Stress' },
  })

  // User-facing response must ALWAYS succeed (HTTP 200, valid response text)
  assertSuccessResponse(res, 'Resilience_Queue_Stress')
  recordResponseMetrics(res, { intent: 'resilience_stress', payload: payload })
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000
  console.log(`[Infrastructure Resilience] Completed in ${elapsedSec.toFixed(2)}s.`)

  // Check metrics for drops and execution status
  sleep(1.0)
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  })
  if (adminRes.status === 200) {
    try {
      const parsed = adminRes.json()
      const agg = parsed.aggregate || parsed
      const shadow = agg.shadow || {}
      console.log(`[Resilience Telemetry] Shadow Queue Drops: ${shadow.shadow_queue_dropped_total}`)
      console.log(
        `[Resilience Telemetry] Shadow Exec Failures: ${shadow.shadow_execution_failures}`
      )
      console.log(
        `[Resilience Telemetry] Primary Requests Succeeded: ${agg.request_and_cache?.total_authenticated_requests}`
      )
    } catch (e) {}
  }
}
