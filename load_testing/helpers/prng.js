/**
 * Deterministic Pseudo-Random Number Generator (PRNG) for reproducible load testing.
 * Uses the Mulberry32 algorithm for uniform, seedable 32-bit random number generation.
 */

export function createPRNG(seed = 1337) {
  let s = (typeof seed === 'number' ? seed : parseInt(seed, 10)) >>> 0
  return function nextFloat() {
    s = (s + 0x6d2b79f5) | 0
    let t = Math.imul(s ^ (s >>> 15), 1 | s)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

export function getWorkloadSeed() {
  if (typeof __ENV !== 'undefined' && __ENV.WORKLOAD_SEED) {
    return parseInt(__ENV.WORKLOAD_SEED, 10)
  }
  return 1337
}

export function deterministicPick(array, prng) {
  if (!array || array.length === 0) return null
  const idx = Math.floor(prng() * array.length)
  return array[idx]
}

export function deterministicShuffle(array, seed = 1337) {
  const prng = createPRNG(seed)
  const result = [...array]
  for (let i = result.length - 1; i > 0; i--) {
    const j = Math.floor(prng() * (i + 1))
    ;[result[i], result[j]] = [result[j], result[i]]
  }
  return result
}
