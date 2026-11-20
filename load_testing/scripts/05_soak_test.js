/**
 * 05_soak_test.js
 * Configurable long-running soak test script for stability and resource leak detection.
 * Supports configurable durations: e.g. 30m, 1h, 24h via SOAK_DURATION environment variable.
 * Default execution profile runs a brief test if SOAK_DURATION is not explicitly set.
 *
 * NOTE: As per Phase 7 instructions, a full 24-hour soak test is NOT automatically
 * executed during CI or implementation turns; this script is provided for operators.
 */

import http from 'k6/http';
import { sleep } from 'k6';
import { BASE_URL, standardThresholds } from '../config/test-profiles.js';
import { getClientHeaders, getAdminHeaders } from '../helpers/auth.js';
import { recordResponseMetrics } from '../helpers/metrics.js';
import { assertSuccessResponse } from '../helpers/assertions.js';

const soakDuration = __ENV.SOAK_DURATION || '2m'; // Default to 2m for quick verification if not specified
const vus = parseInt(__ENV.VUS || '10', 10);

export const options = {
  stages: [
    { duration: '30s', target: vus },
    { duration: soakDuration, target: vus },
    { duration: '30s', target: 0 },
  ],
  thresholds: standardThresholds,
};

const mixedDataset = JSON.parse(open('../datasets/mixed_workload.json'));

export function setup() {
  console.log(`[Soak Test] Starting soak run: VUs=${vus}, Duration=${soakDuration}`);
  // Log baseline health and memory before soak
  const healthRes = http.get(`${BASE_URL}/health`);
  console.log(`[Soak Test] Initial Health: status=${healthRes.status}`);
  return { startTime: Date.now() };
}

export default function () {
  const url = `${BASE_URL}/query`;
  const item = mixedDataset[Math.floor(Math.random() * mixedDataset.length)];

  let payload = item.payload;
  if (item.intent === 'unique_miss') {
    payload = {
      prompt: `${item.payload.prompt} [soak-${__VU}-${Date.now()}-${Math.random()}]`,
      temperature: 0.0,
    };
  }

  const res = http.post(url, JSON.stringify(payload), {
    headers: getClientHeaders(),
    tags: { name: `Soak_${item.intent}` },
  });

  assertSuccessResponse(res, `Soak_${item.intent}`);
  recordResponseMetrics(res);

  sleep(0.2);
}

export function teardown(data) {
  const elapsedSec = (Date.now() - data.startTime) / 1000;
  console.log(`[Soak Test] Completed soak run in ${elapsedSec.toFixed(1)}s.`);
  const adminRes = http.get(`${BASE_URL}/admin/metrics`, {
    headers: getAdminHeaders(),
  });
  if (adminRes.status === 200) {
    try {
      const metrics = adminRes.json();
      const agg = metrics.aggregate || metrics;
      console.log(`[Soak Test] Final Total Requests: ${agg.request_and_cache?.total_authenticated_requests}`);
      console.log(`[Soak Test] Final Exact Cache Hits: ${agg.request_and_cache?.exact_cache_hits}`);
      console.log(`[Soak Test] Final Exact Cache Misses: ${agg.request_and_cache?.exact_cache_misses}`);
      console.log(`[Soak Test] Final Shadow Drops: ${agg.shadow?.shadow_queue_dropped_total}`);
    } catch (e) {}
  }
}
