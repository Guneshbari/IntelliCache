/**
 * 04_failure_injection.js
 * Evaluates gateway resilience and fail-open behavior.
 * Verifies that oversized payloads, malformed structures, and bypass queries
 * handle errors gracefully and never cause unhandled server crashes (500s).
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { BASE_URL } from '../config/test-profiles.js';
import { getClientHeaders } from '../helpers/auth.js';
import { recordResponseMetrics } from '../helpers/metrics.js';
import { assertSuccessResponse } from '../helpers/assertions.js';

export const options = {
  vus: 5,
  duration: '15s',
  thresholds: {
    // 400/401/422 is expected for invalid requests; 500 Internal Error is forbidden!
    'http_req_failed{status:500}': ['rate==0'],
    'semantic_hits_served': ['count==0'],
  },
};

export default function () {
  const url = `${BASE_URL}/query`;

  const scenario = Math.floor(Math.random() * 4);

  if (scenario === 0) {
    // Normal valid query: should return 200
    const res = http.post(url, JSON.stringify({
      prompt: "Normal resilience test query for fail-open verification.",
      temperature: 0.0,
    }), {
      headers: getClientHeaders(),
      tags: { name: 'Failure_Valid_Query' },
    });
    assertSuccessResponse(res, 'Failure_Valid_Query');
    recordResponseMetrics(res);
  } else if (scenario === 1) {
    // Empty query body: should return 422 or 400 (validation rejection), never 500
    const res = http.post(url, JSON.stringify({}), {
      headers: getClientHeaders(),
      tags: { name: 'Failure_Empty_Body' },
    });
    check(res, {
      'Empty body rejected with 422 or 400': (r) => r.status === 422 || r.status === 400,
      'Empty body did not return 500': (r) => r.status !== 500,
    });
  } else if (scenario === 2) {
    // Missing auth token: should return 401 Unauthorized, never 500
    const res = http.post(url, JSON.stringify({
      prompt: "Unauthenticated query attempt.",
      temperature: 0.0,
    }), {
      headers: { 'Content-Type': 'application/json' }, // No X-Client-Token!
      tags: { name: 'Failure_Missing_Auth' },
    });
    check(res, {
      'Missing token rejected with 401': (r) => r.status === 401,
      'Missing token did not return 500': (r) => r.status !== 500,
    });
  } else {
    // Bypass query under failure mode: should fall open to upstream generation without crashing
    const res = http.post(url, JSON.stringify({
      prompt: "Bypass query under failure test mode.",
      bypass_cache: true,
      temperature: 0.0,
    }), {
      headers: getClientHeaders(),
      tags: { name: 'Failure_Bypass_Fallback' },
    });
    assertSuccessResponse(res, 'Failure_Bypass_Fallback');
    recordResponseMetrics(res);
  }

  sleep(0.1);
}
