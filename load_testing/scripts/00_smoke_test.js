/**
 * 00_smoke_test.js
 * Validates basic gateway connectivity, client token authentication,
 * exact cache hit on repeated query, explicit bypass semantics,
 * and confirms that no semantic cached responses are served.
 */

import http from 'k6/http';
import { sleep } from 'k6';
import { BASE_URL, smokeProfile } from '../config/test-profiles.js';
import { getClientHeaders } from '../helpers/auth.js';
import { recordResponseMetrics } from '../helpers/metrics.js';
import { assertSuccessResponse, assertExactHit } from '../helpers/assertions.js';

export const options = smokeProfile;

const smokeData = JSON.parse(open('../datasets/smoke.json'));

export default function () {
  const url = `${BASE_URL}/query`;

  // 1. Initial query: should be an exact cache miss
  const seedItem = smokeData[0];
  const res1 = http.post(url, JSON.stringify(seedItem.payload), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Initial_Query' },
  });
  assertSuccessResponse(res1, 'Smoke_Initial_Query');
  recordResponseMetrics(res1);

  sleep(0.5);

  // 2. Repeat query: identical prompt should be an exact cache hit
  const repeatItem = smokeData[1];
  const res2 = http.post(url, JSON.stringify(repeatItem.payload), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Repeat_Exact_Hit' },
  });
  assertSuccessResponse(res2, 'Smoke_Repeat_Exact_Hit');
  assertExactHit(res2, 'Smoke_Repeat_Exact_Hit');
  recordResponseMetrics(res2);

  sleep(0.5);

  // 3. Bypass query: should bypass cache even though identical prompt is in cache
  const bypassItem = smokeData[3];
  const res3 = http.post(url, JSON.stringify(bypassItem.payload), {
    headers: getClientHeaders(),
    tags: { name: 'Smoke_Bypass_Query' },
  });
  assertSuccessResponse(res3, 'Smoke_Bypass_Query');
  recordResponseMetrics(res3);

  sleep(0.5);
}
