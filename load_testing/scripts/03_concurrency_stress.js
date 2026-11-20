/**
 * 03_concurrency_stress.js
 * Stresses the gateway under high concurrent load:
 * - Simultaneous bursts of identical queries (testing SingleFlight leader/follower coordination)
 * - Rapid arrival of unique queries to test shadow queue capacity
 * - Verification that queue saturation drops tasks gracefully without failing user requests
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, stressProfile } from '../config/test-profiles.js'
import { getClientHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse } from '../helpers/assertions.js'

export const options = stressProfile

const sharedPrompt = 'High concurrency test query for SingleFlight evaluation.'

export default function () {
  const url = `${BASE_URL}/query`

  // 70% of requests query identical shared prompt concurrently (testing SingleFlight coalescing)
  // 30% of requests query unique prompts (stressing shadow queue ingestion)
  const isIdentical = Math.random() < 0.7
  const payload = isIdentical
    ? { prompt: sharedPrompt, temperature: 0.0 }
    : {
        prompt: `Unique stress prompt [vu-${__VU}-${Date.now()}-${Math.random()}]`,
        temperature: 0.0,
      }

  const tag = isIdentical ? 'Stress_Identical_SingleFlight' : 'Stress_Unique_Miss'
  const res = http.post(url, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: tag },
  })

  assertSuccessResponse(res, tag)
  recordResponseMetrics(res)

  sleep(0.05)
}
