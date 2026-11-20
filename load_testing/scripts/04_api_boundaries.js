/**
 * 04_api_boundaries.js
 * API Boundary, Input Validation, and Client Error Handling Test.
 * Validates gateway rejection and status contracts for:
 * - Missing required fields (HTTP 422)
 * - Missing client authentication token (HTTP 401)
 * - Invalid client authentication token (HTTP 401)
 * - Oversized payload (HTTP 422 / 400)
 * - Explicit bypass handling (HTTP 200 with cached: false)
 * - Zero unhandled internal server errors (0 HTTP 500s)
 */

import http from 'k6/http'
import { sleep } from 'k6'
import { BASE_URL } from '../config/test-profiles.js'
import { getClientHeaders } from '../helpers/auth.js'
import { recordResponseMetrics } from '../helpers/metrics.js'
import { assertSuccessResponse, assertExpectedError } from '../helpers/assertions.js'

export const options = {
  vus: 5,
  duration: '15s',
  thresholds: {
    // Critical: zero unhandled 500 errors
    'http_req_failed{status:500}': ['rate==0'],
    semantic_hits_served: ['count==0'],
  },
}

export default function () {
  const url = `${BASE_URL}/query`

  // 1. Missing authentication token from external client -> Expect 401
  {
    const res = http.post(url, JSON.stringify({ prompt: 'Test missing auth token' }), {
      headers: {
        'Content-Type': 'application/json',
        'X-Forwarded-For': '203.0.113.19',
      },
      tags: { name: 'Boundary_Missing_Auth' },
    })
    assertExpectedError(res, 401, 'Boundary_Missing_Auth')
    recordResponseMetrics(res)
  }

  // 2. Invalid authentication token -> Expect 401
  {
    const res = http.post(url, JSON.stringify({ prompt: 'Test invalid auth token' }), {
      headers: {
        'Content-Type': 'application/json',
        'X-Client-Token': 'totally-invalid-client-token-12345',
      },
      tags: { name: 'Boundary_Invalid_Auth' },
    })
    assertExpectedError(res, 401, 'Boundary_Invalid_Auth')
    recordResponseMetrics(res)
  }

  // 3. Malformed payload: missing prompt, query, and messages -> Expect 422
  {
    const res = http.post(url, JSON.stringify({ temperature: 0.7 }), {
      headers: getClientHeaders(),
      tags: { name: 'Boundary_Malformed_Payload' },
    })
    assertExpectedError(res, 422, 'Boundary_Malformed_Payload')
    recordResponseMetrics(res)
  }

  // 4. Oversized prompt payload (exceeding 131,072 max limit) -> Expect 422
  {
    const oversizedPrompt = 'X'.repeat(140000)
    const res = http.post(url, JSON.stringify({ prompt: oversizedPrompt, temperature: 0.0 }), {
      headers: getClientHeaders(),
      tags: { name: 'Boundary_Oversized_Payload' },
    })
    assertExpectedError(res, 422, 'Boundary_Oversized_Payload')
    recordResponseMetrics(res)
  }

  // 5. Valid query with explicit cache bypass -> Expect 200 with cached: false
  {
    const res = http.post(
      url,
      JSON.stringify({
        prompt: 'What is the velocity of light in vacuum?',
        bypass_cache: true,
        temperature: 0.0,
      }),
      {
        headers: getClientHeaders(),
        tags: { name: 'Boundary_Bypass_Fallback' },
      }
    )
    assertSuccessResponse(res, 'Boundary_Bypass_Fallback')
    recordResponseMetrics(res, { isBypass: true, intent: 'bypass' })
  }

  sleep(0.1)
}
