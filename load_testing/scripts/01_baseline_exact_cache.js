/**
 * 01_baseline_exact_cache.js
 * Evaluates API performance and exact-cache behavior with shadow evaluation disabled.
 * Measures latency percentiles (P50, P90, P95, P99), throughput, and exact hit rate.
 */

import http from 'k6/http';
import { sleep } from 'k6';
import { BASE_URL, standardThresholds } from '../config/test-profiles.js';
import { getClientHeaders } from '../helpers/auth.js';
import { recordResponseMetrics } from '../helpers/metrics.js';
import { assertSuccessResponse } from '../helpers/assertions.js';

export const options = {
  stages: [
    { duration: '5s', target: parseInt(__ENV.VUS || '10', 10) },
    { duration: __ENV.DURATION || '20s', target: parseInt(__ENV.VUS || '10', 10) },
    { duration: '5s', target: 0 },
  ],
  thresholds: standardThresholds,
};

const dataset = JSON.parse(open('../datasets/baseline_workload.json'));

export function setup() {
  // Prime the exact cache with the 10 exact_target queries so subsequent calls hit
  const url = `${BASE_URL}/query`;
  const targets = dataset.filter((d) => d.group === 'exact_target');
  for (const item of targets) {
    http.post(url, JSON.stringify(item.payload), {
      headers: getClientHeaders(),
      tags: { name: 'Setup_Prime_Cache' },
    });
  }
}

export default function () {
  const url = `${BASE_URL}/query`;

  // Select a query following realistic baseline distribution:
  // 60% probability of exact target (should hit exact cache)
  // 30% probability of unique miss
  // 10% probability of bypass request
  const rand = Math.random();
  let item;

  if (rand < 0.60) {
    const targets = dataset.filter((d) => d.group === 'exact_target');
    item = targets[Math.floor(Math.random() * targets.length)];
  } else if (rand < 0.90) {
    const misses = dataset.filter((d) => d.group === 'unique_miss');
    const baseMiss = misses[Math.floor(Math.random() * misses.length)];
    // Ensure uniqueness for miss by appending VU and iteration timestamp
    item = {
      group: 'unique_miss',
      payload: {
        prompt: `${baseMiss.payload.prompt} [query-${__VU}-${Date.now()}-${Math.random()}]`,
        temperature: 0.0,
      },
    };
  } else {
    const bypasses = dataset.filter((d) => d.group === 'bypass');
    item = bypasses[Math.floor(Math.random() * bypasses.length)];
  }

  const tag = `Query_${item.group}`;
  const res = http.post(url, JSON.stringify(item.payload), {
    headers: getClientHeaders(),
    tags: { name: tag },
  });

  assertSuccessResponse(res, tag);
  recordResponseMetrics(res);

  sleep(0.1);
}
