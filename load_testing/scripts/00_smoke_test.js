/**
 * 00_smoke_test.js
 * Validates basic gateway connectivity, client token authentication,
 * exact cache miss on initial seed, exact cache hit on repeated query,
 * exact cache miss on distinct query, explicit bypass semantics,
 * and confirms that no semantic cached responses are served.
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL, smokeProfile } from '../config/test-profiles.js'
import { getClientHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import {
  assertSuccessResponse,
  assertExactHit,
  assertExactMiss,
  assertBypass,
} from '../helpers/assertions.js'

export const options = smokeProfile

const smokeData = JSON.parse(open('../datasets/smoke.json'))

export default function () {
  const url = `${BASE_URL}/query`
  const runToken = `${Date.now()}-${Math.floor(Math.random() * 10000)}`

  // 1. Initial seed query: MUST be an exact cache miss
  const seedItem = smokeData[0]
  const seedPrompt = `${seedItem.payload.prompt} [smoke-seed-${runToken}]`
  const res1 = http.post(url, JSON.stringify({ ...seedItem.payload, prompt: seedPrompt }), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Initial_Seed' },
  })
  assertSuccessResponse(res1, 'Smoke_Initial_Seed')
  assertExactMiss(res1, 'Smoke_Initial_Seed')
  recordResponseMetrics(res1, { intent: 'seed', payload: seedItem.payload })

  sleep(0.3)

  // 2. Repeat query: identical prompt MUST be an exact cache hit
  const repeatItem = smokeData[1]
  const repeatPrompt = seedPrompt // Exact match with item 1
  const res2 = http.post(url, JSON.stringify({ ...repeatItem.payload, prompt: repeatPrompt }), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Repeat_Exact_Hit' },
  })
  assertSuccessResponse(res2, 'Smoke_Repeat_Exact_Hit')
  assertExactHit(res2, 'Smoke_Repeat_Exact_Hit')
  recordResponseMetrics(res2, { intent: 'repeat', payload: repeatItem.payload })

  sleep(0.3)

  // 3. Unique miss query: new prompt MUST be an exact cache miss
  const missItem = smokeData[2]
  const missPrompt = `${missItem.payload.prompt} [smoke-miss-${runToken}]`
  const res3 = http.post(url, JSON.stringify({ ...missItem.payload, prompt: missPrompt }), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Unique_Miss' },
  })
  assertSuccessResponse(res3, 'Smoke_Unique_Miss')
  assertExactMiss(res3, 'Smoke_Unique_Miss')
  recordResponseMetrics(res3, { intent: 'miss', payload: missItem.payload })

  sleep(0.3)

  // 4. Bypass query: explicit bypass flag MUST bypass cache
  const bypassItem = smokeData[3]
  const bypassPrompt = seedPrompt // Same prompt as seeded, but with bypass_cache: true
  const res4 = http.post(
    url,
    JSON.stringify({
      ...bypassItem.payload,
      prompt: bypassPrompt,
      bypass_cache: true,
    }),
    {
      headers: getClientHeaders(),
      tags: { name: 'Smoke_Bypass_Query' },
    }
  )
  assertSuccessResponse(res4, 'Smoke_Bypass_Query')
  assertBypass(res4, 'Smoke_Bypass_Query')
  recordResponseMetrics(res4, {
    isBypass: true,
    intent: 'bypass',
    payload: { ...bypassItem.payload, bypass_cache: true },
  })

  sleep(0.3)
}
