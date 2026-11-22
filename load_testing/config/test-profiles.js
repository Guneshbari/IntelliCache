/**
 * Reusable execution profiles, thresholds, and options for k6 load testing.
 */

export const BASE_URL = __ENV.BASE_URL || 'http://127.0.0.1:8000'
export const CLIENT_TOKEN =
  __ENV.CLIENT_TOKEN ||
  __ENV.GATEWAY_CLIENT_API_KEY ||
  __ENV.CLIENT_API_TOKEN ||
  'test-client-token'
export const ADMIN_TOKEN =
  __ENV.ADMIN_TOKEN || __ENV.ADMIN_API_KEY || __ENV.ADMIN_API_TOKEN || 'test-admin-token'

// Conservative stop-condition thresholds:
// 1. HTTP failures must remain below 1%
// 2. Exact-cache latency P95 must remain < 50ms
// 3. Overall latency P95 must remain < 500ms
// 4. Zero unexpected semantic hits served to user (invariance gate)
export const standardSummaryTrendStats = [
  'avg',
  'min',
  'med',
  'max',
  'p(90)',
  'p(95)',
  'p(99)',
  'count',
]

export const standardThresholds = {
  http_req_failed: ['rate<0.01'],
  http_req_duration: ['p(95)<500', 'p(99)<1000'],
  exact_cache_hits: ['count>=0'],
  exact_cache_misses: ['count>=0'],
  exact_hit_latency_ms: ['p(95)<500'],
  cache_miss_latency_ms: ['p(95)<1000'],
  invalid_requests: ['count==0'],
  semantic_hits_served: ['count==0'], // Must remain strictly 0!
}

export const smokeProfile = {
  vus: 1,
  iterations: 1,
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}

export const lowLoadProfile = {
  vus: 5,
  duration: __ENV.DURATION || '20s',
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}

export const mediumLoadProfile = {
  vus: 10,
  duration: __ENV.DURATION || '30s',
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}

export const highLoadProfile = {
  stages: [
    { duration: '10s', target: 10 },
    { duration: '20s', target: 25 },
    { duration: '20s', target: 50 },
    { duration: '10s', target: 0 },
  ],
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}

export const arrivalRateProfile = {
  scenarios: {
    constant_rate: {
      executor: 'constant-arrival-rate',
      rate: parseInt(__ENV.TARGET_RPS || '20', 10),
      timeUnit: '1s',
      duration: __ENV.DURATION || '30s',
      preAllocatedVUs: 10,
      maxVUs: 50,
    },
  },
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}

export const stressProfile = {
  stages: [
    { duration: '10s', target: 10 },
    { duration: '20s', target: 30 },
    { duration: '20s', target: 50 },
    { duration: '10s', target: 0 },
  ],
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: {
    http_req_failed: ['rate<0.05'], // Max 5% failure during extreme stress
    semantic_hits_served: ['count==0'],
  },
}

export const soakProfile = {
  stages: [
    { duration: '1m', target: 10 },
    { duration: __ENV.SOAK_DURATION || '10m', target: 10 },
    { duration: '1m', target: 0 },
  ],
  summaryTrendStats: standardSummaryTrendStats,
  thresholds: standardThresholds,
}
