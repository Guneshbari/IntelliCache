/**
 * Common verification assertions for IntelliCache responses.
 */

import { check } from 'k6'

export function assertSuccessResponse(res, scenarioName = 'query') {
  return check(res, {
    [`${scenarioName}: status is 200`]: (r) => r.status === 200,
    [`${scenarioName}: response has valid JSON`]: (r) => {
      try {
        return r.json() !== null
      } catch (e) {
        return false
      }
    },
    [`${scenarioName}: has response text`]: (r) => {
      try {
        const body = r.json()
        return body && typeof body.response === 'string' && body.response.length > 0
      } catch (e) {
        return false
      }
    },
    [`${scenarioName}: has fingerprint`]: (r) => {
      try {
        const body = r.json()
        return body && typeof body.fingerprint === 'string' && body.fingerprint.length > 0
      } catch (e) {
        return false
      }
    },
    [`${scenarioName}: semantic cache NOT served to user`]: (r) => {
      try {
        const body = r.json()
        // Invariant: cached must NEVER be true with cache_type='semantic'
        return !(body && body.cached === true && body.cache_type === 'semantic')
      } catch (e) {
        return true
      }
    },
  })
}

export function assertExactHit(res, scenarioName = 'exact_hit') {
  return check(res, {
    [`${scenarioName}: cached is true and exact`]: (r) => {
      try {
        const body = r.json()
        return body && body.cached === true && body.cache_type === 'exact'
      } catch (e) {
        return false
      }
    },
    [`${scenarioName}: latency is sub-100ms`]: (r) => r.timings.duration < 100,
  })
}

export function assertExactMiss(res, scenarioName = 'exact_miss') {
  return check(res, {
    [`${scenarioName}: cached is false on miss`]: (r) => {
      try {
        const body = r.json()
        return body && body.cached === false && body.cache_type === 'none'
      } catch (e) {
        return false
      }
    },
  })
}

export function assertBypass(res, scenarioName = 'bypass') {
  return check(res, {
    [`${scenarioName}: cached is false on bypass`]: (r) => {
      try {
        const body = r.json()
        return body && body.cached === false && body.cache_type === 'none'
      } catch (e) {
        return false
      }
    },
  })
}

export function assertExpectedError(res, expectedStatus, scenarioName = 'error_check') {
  return check(res, {
    [`${scenarioName}: status matches expected ${expectedStatus}`]: (r) =>
      r.status === expectedStatus,
  })
}
