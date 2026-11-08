// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { GatewayApiClient, GatewayApiError } from '../demo/src/api-client'
import type {
  CacheStats,
  NamespaceMetricsSnapshot,
  QueryResponse,
  RootStatusResponse,
} from '../demo/src/types'

describe('GatewayApiClient (Demo Chatbot)', () => {
  let client: GatewayApiClient

  beforeEach(() => {
    vi.restoreAllMocks()
    localStorage.clear()
    sessionStorage.clear()
    client = new GatewayApiClient('http://127.0.0.1:8000')
  })

  describe('Initialization and URL Handling', () => {
    it('initializes with default gateway URL and normalizes trailing slashes', () => {
      expect(client.getBaseUrl()).toBe('http://127.0.0.1:8000')

      const clientWithSlash = new GatewayApiClient('http://localhost:8000///')
      expect(clientWithSlash.getBaseUrl()).toBe('http://localhost:8000')
    })

    it('updates gateway base URL dynamically', () => {
      client.setBaseUrl('http://gateway.internal:9000/')
      expect(client.getBaseUrl()).toBe('http://gateway.internal:9000')
    })

    it('falls back to default if empty string is supplied', () => {
      client.setBaseUrl('')
      expect(client.getBaseUrl()).toBe('http://127.0.0.1:8000')
    })
  })

  describe('Security & Privacy: In-Memory Token Management', () => {
    it('manages client token strictly in memory without persistent storage side-effects', () => {
      expect(client.hasToken()).toBe(false)
      expect(client.getClientToken()).toBeNull()

      client.setClientToken('tenant-secret-token-xyz')
      expect(client.hasToken()).toBe(true)
      expect(client.getClientToken()).toBe('tenant-secret-token-xyz')

      // Critical Security Requirement: Ensure token is never written to browser storage
      expect(localStorage.getItem('token')).toBeNull()
      expect(localStorage.getItem('client_token')).toBeNull()
      expect(localStorage.getItem('X-Client-Token')).toBeNull()
      expect(sessionStorage.getItem('token')).toBeNull()
      expect(document.cookie).toBe('')

      client.clearToken()
      expect(client.hasToken()).toBe(false)
      expect(client.getClientToken()).toBeNull()
    })

    it('ignores empty whitespace-only tokens', () => {
      client.setClientToken('   ')
      expect(client.hasToken()).toBe(false)
      expect(client.getClientToken()).toBeNull()
    })
  })

  describe('Query Execution (POST /query)', () => {
    it('attaches X-Client-Token authentication header when configured', async () => {
      client.setClientToken('valid-tenant-token')

      const mockResponse: QueryResponse = {
        response: 'Python asyncio uses an event loop to manage asynchronous tasks.',
        cached: false,
        model: 'llama-3.3-70b-versatile',
        latency_ms: 1240.5,
        usage: { prompt_tokens: 15, completion_tokens: 30, total_tokens: 45 },
        cache_hit_count: 0,
        fingerprint: 'sha256_mock_fp_1234567890abcdef',
        created_at: new Date().toISOString(),
        cache_type: 'none',
      }

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.sendQuery({
        prompt: 'Explain Python asyncio event loop',
      })

      expect(fetchSpy).toHaveBeenCalledTimes(1)
      const [url, init] = fetchSpy.mock.calls[0]
      expect(url).toBe('http://127.0.0.1:8000/query')
      expect(init?.method).toBe('POST')
      const headers = init?.headers as Record<string, string>
      expect(headers['X-Client-Token']).toBe('valid-tenant-token')
      expect(headers['Content-Type']).toBe('application/json')
      expect(result.response).toContain('asyncio uses an event loop')
      expect(result.cached).toBe(false)
      expect(result.cache_type).toBe('none')
    })

    it('correctly handles exact cache hit responses', async () => {
      client.setClientToken('valid-tenant-token')

      const mockExactHit: QueryResponse = {
        response: 'Cached exact answer for JavaScript variable declarations.',
        cached: true,
        model: 'llama-3.3-70b-versatile',
        latency_ms: 11.2,
        usage: { prompt_tokens: 20, completion_tokens: 40, total_tokens: 60 },
        cache_hit_count: 4,
        fingerprint: 'sha256_exact_fp_9876543210fedcba',
        created_at: new Date().toISOString(),
        cache_type: 'exact',
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockExactHit), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.sendQuery({
        prompt: 'Difference between let and const in JavaScript',
      })

      expect(result.cached).toBe(true)
      expect(result.cache_type).toBe('exact')
      expect(result.cache_hit_count).toBe(4)
      expect(result.latency_ms).toBe(11.2)
    })

    it('correctly handles semantic cache hit responses', async () => {
      client.setClientToken('valid-tenant-token')

      const mockSemanticHit: QueryResponse = {
        response: 'Semantically reused answer for pgvector similarity indexing.',
        cached: true,
        model: 'llama-3.3-70b-versatile',
        latency_ms: 48.7,
        usage: { prompt_tokens: 25, completion_tokens: 50, total_tokens: 75 },
        cache_hit_count: 2,
        fingerprint: 'sha256_semantic_fp_abcdef1234567890',
        created_at: new Date().toISOString(),
        cache_type: 'semantic',
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockSemanticHit), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.sendQuery({
        prompt: 'How to index vectors with pgvector in PostgreSQL?',
      })

      expect(result.cached).toBe(true)
      expect(result.cache_type).toBe('semantic')
      expect(result.latency_ms).toBe(48.7)
    })

    it('forwards bypass_cache=true to the gateway', async () => {
      client.setClientToken('valid-tenant-token')

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            response: 'Fresh provider answer bypassing cache.',
            cached: false,
            model: 'llama-3.3-70b-versatile',
            latency_ms: 1100.0,
            usage: { prompt_tokens: 10, completion_tokens: 15, total_tokens: 25 },
            cache_hit_count: 0,
            fingerprint: 'sha256_fresh_fp',
            created_at: new Date().toISOString(),
            cache_type: 'none',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )

      await client.sendQuery({
        prompt: 'Explain Python asyncio',
        bypass_cache: true,
      })

      const [, init] = fetchSpy.mock.calls[0]
      const body = JSON.parse(init?.body as string)
      expect(body.bypass_cache).toBe(true)
      expect(body.prompt).toBe('Explain Python asyncio')
    })

    it('supports multi-turn payload with conversation_id and messages', async () => {
      client.setClientToken('valid-tenant-token')

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            response: 'Multi-turn assistant continuation.',
            cached: false,
            model: 'llama-3.3-70b-versatile',
            latency_ms: 950.0,
            usage: { prompt_tokens: 50, completion_tokens: 30, total_tokens: 80 },
            cache_hit_count: 0,
            conversation_id: 'conv-uuid-1234',
            fingerprint: 'sha256_multi_fp',
            created_at: new Date().toISOString(),
            cache_type: 'none',
          }),
          { status: 200, headers: { 'Content-Type': 'application/json' } }
        )
      )

      await client.sendQuery({
        conversation_id: 'conv-uuid-1234',
        messages: [
          { role: 'user', content: 'What is asyncio?' },
          { role: 'assistant', content: 'Asyncio is a concurrency library.' },
          { role: 'user', content: 'Can you give a code snippet?' },
        ],
      })

      const [, init] = fetchSpy.mock.calls[0]
      const body = JSON.parse(init?.body as string)
      expect(body.conversation_id).toBe('conv-uuid-1234')
      expect(body.messages).toHaveLength(3)
      expect(body.messages[2].content).toBe('Can you give a code snippet?')
    })
  })

  describe('HTTP Error Mapping and Diagnostics', () => {
    it('handles HTTP 401 Unauthorized with descriptive authentication message', async () => {
      client.setClientToken('bad-token')
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: 'Unauthorized: Invalid or missing client API token.' }),
          { status: 401, headers: { 'Content-Type': 'application/json' } }
        )
      )

      let caughtError: GatewayApiError | null = null
      try {
        await client.sendQuery({ prompt: 'test query' })
      } catch (err) {
        caughtError = err as GatewayApiError
      }

      expect(caughtError).not.toBeNull()
      expect(caughtError).toBeInstanceOf(GatewayApiError)
      expect(caughtError?.status).toBe(401)
      expect(caughtError?.isAuthError).toBe(true)
      expect(caughtError?.isServerError).toBe(false)
      expect(caughtError?.message).toContain('Unauthorized')
    })

    it('handles HTTP 403 Forbidden', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: 'Forbidden: Invalid administrative token.' }), {
          status: 403,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      try {
        await client.sendQuery({ prompt: 'test query' })
      } catch (err) {
        const apiErr = err as GatewayApiError
        expect(apiErr.status).toBe(403)
        expect(apiErr.isAuthError).toBe(true)
        expect(apiErr.message).toContain('Forbidden')
      }
    })

    it('handles HTTP 422 Validation Error', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: "At least one of 'prompt', 'query', or 'messages' must be provided.",
          }),
          { status: 422, headers: { 'Content-Type': 'application/json' } }
        )
      )

      try {
        await client.sendQuery({})
      } catch (err) {
        const apiErr = err as GatewayApiError
        expect(apiErr.status).toBe(422)
        expect(apiErr.isValidationError).toBe(true)
        expect(apiErr.message).toContain('Validation')
      }
    })

    it('handles HTTP 429 Provider Rate Limit', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({ detail: 'Provider Rate Limit Exceeded: Upstream quota reached.' }),
          { status: 429, headers: { 'Content-Type': 'application/json' } }
        )
      )

      try {
        await client.sendQuery({ prompt: 'rate limit query' })
      } catch (err) {
        const apiErr = err as GatewayApiError
        expect(apiErr.status).toBe(429)
        expect(apiErr.isRateLimit).toBe(true)
        expect(apiErr.message).toContain('Rate Limit')
      }
    })

    it('handles HTTP 500, 502, 503 Server Errors', async () => {
      // 503 Provider unavailable / leader cancelled
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            detail: 'Upstream leader request was cancelled. Please retry.',
          }),
          { status: 503, headers: { 'Content-Type': 'application/json' } }
        )
      )

      try {
        await client.sendQuery({ prompt: 'concurrency cancelled query' })
      } catch (err) {
        const apiErr = err as GatewayApiError
        expect(apiErr.status).toBe(503)
        expect(apiErr.isServerError).toBe(true)
        expect(apiErr.message).toContain('cancelled')
      }
    })

    it('handles network failure (server down or offline) gracefully', async () => {
      vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('Failed to fetch'))

      try {
        await client.sendQuery({ prompt: 'offline query' })
      } catch (err) {
        const apiErr = err as GatewayApiError
        expect(apiErr.status).toBe(0)
        expect(apiErr.isNetworkError).toBe(true)
        expect(apiErr.message).toContain('Network connection failed')
      }
    })
  })

  describe('Connection Testing (testConnection)', () => {
    it('returns offline when gateway root endpoint fails', async () => {
      vi.spyOn(globalThis, 'fetch').mockRejectedValueOnce(new TypeError('Failed to fetch'))

      const result = await client.testConnection()
      expect(result.online).toBe(false)
      expect(result.authenticated).toBe(false)
      expect(result.error).toContain('Network connection failed')
    })

    it('returns online but unauthenticated when token is missing', async () => {
      const mockRoot: RootStatusResponse = {
        service: 'IntelliCache Gateway',
        version: '1.0.0',
        status: 'online',
        uptime_seconds: 120.5,
        configured_model: 'llama-3.3-70b-versatile',
        cache_backend: 'postgres',
        cache_namespace: 'default',
        cache_enabled: true,
        endpoints: {},
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockRoot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.testConnection()
      expect(result.online).toBe(true)
      expect(result.authenticated).toBe(false)
      expect(result.serverVersion).toBe('1.0.0')
      expect(result.error).toContain('Provide a client API token')
    })

    it('returns online and authenticated when valid token is verified via /metrics/me', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockRoot: RootStatusResponse = {
        service: 'IntelliCache Gateway',
        version: '1.0.0',
        status: 'online',
        uptime_seconds: 120.5,
        configured_model: 'llama-3.3-70b-versatile',
        cache_backend: 'postgres',
        cache_namespace: 'default',
        cache_enabled: true,
        endpoints: {},
      }

      const mockMetrics: Partial<NamespaceMetricsSnapshot> = {
        scope: 'namespace',
        namespace: 'tenant_alpha',
        collection_uptime_seconds: 120.5,
        timestamp: new Date().toISOString(),
      }

      vi.spyOn(globalThis, 'fetch')
        .mockResolvedValueOnce(
          new Response(JSON.stringify(mockRoot), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        )
        .mockResolvedValueOnce(
          new Response(JSON.stringify(mockMetrics), {
            status: 200,
            headers: { 'Content-Type': 'application/json' },
          })
        )

      const result = await client.testConnection()
      expect(result.online).toBe(true)
      expect(result.authenticated).toBe(true)
      expect(result.namespace).toBe('tenant_alpha')
      expect(result.serverVersion).toBe('1.0.0')
    })
  })

  describe('Namespace Metrics Retrieval (GET /metrics/me)', () => {
    it('retrieves and parses actual namespace metrics payload fields', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockSnapshot: NamespaceMetricsSnapshot = {
        scope: 'namespace',
        namespace: 'tenant_alpha',
        collection_uptime_seconds: 450.0,
        timestamp: new Date().toISOString(),
        request_and_cache: {
          total_authenticated_requests: 12,
          exact_cache_lookups: 10,
          exact_cache_hits: 6,
          exact_cache_misses: 4,
          cache_write_failures: 0,
          semantic_eligibility_checks: 4,
          semantic_eligibility_rejections: 1,
          semantic_lookups: 3,
          semantic_hits: 2,
          semantic_misses: 1,
          provider_generation_attempts: 2,
          provider_generation_successes: 2,
          provider_generation_failures: 0,
          cache_bypass_requests: 1,
          streaming_requests: 0,
          coalesced_joins: 3,
          coalesced_successes: 3,
          coalesced_failures: 0,
          request_cancellations: 0,
        },
        provider_and_savings: {
          provider_calls_attempted: 2,
          provider_calls_succeeded: 2,
          provider_calls_failed: 0,
          provider_prompt_tokens_used: 120,
          provider_completion_tokens_used: 350,
          provider_total_tokens_used: 470,
          prompt_tokens_saved: 480,
          completion_tokens_saved: 1400,
          total_tokens_saved: 1880,
          provider_calls_avoided: 8,
        },
        semantic: {
          semantic_eligibility_rejections: 1,
          semantic_lookup_attempts: 3,
          semantic_hits: 2,
          semantic_misses: 1,
          ambiguous_candidate_rejections: 0,
          incompatible_candidate_rejections: 0,
          embedding_failures: 0,
          vector_store_failures: 0,
          indexing_successes: 2,
          indexing_failures: 0,
        },
        derived_rates: {
          exact_hit_rate: 0.6,
          semantic_hit_rate: 0.2,
          overall_cache_hit_rate: 0.8,
          coalesced_reuse_rate: 0.25,
        },
        latencies: {
          total_request_latency_ms: {
            sample_count: 12,
            min: 8.5,
            mean: 110.2,
            p50: 12.0,
            p95: 980.0,
            p99: 1200.0,
            percentile_type: 'exact_rolling',
            small_sample_warning: false,
          },
          exact_lookup_latency_ms: {
            sample_count: 10,
            min: 1.2,
            mean: 2.1,
            p50: 2.0,
            p95: 3.5,
            p99: 4.0,
            percentile_type: 'exact_rolling',
            small_sample_warning: false,
          },
          semantic_lookup_latency_ms: {
            sample_count: 3,
            min: 25.0,
            mean: 32.4,
            p50: 30.0,
            p95: 42.0,
            p99: 45.0,
            percentile_type: 'exact_rolling',
            small_sample_warning: true,
          },
          provider_generation_latency_ms: {
            sample_count: 2,
            min: 850.0,
            mean: 920.0,
            p50: 920.0,
            p95: 990.0,
            p99: 990.0,
            percentile_type: 'exact_rolling',
            small_sample_warning: true,
          },
        },
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockSnapshot), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const metrics = await client.fetchNamespaceMetrics()
      expect(metrics.namespace).toBe('tenant_alpha')
      expect(metrics.request_and_cache.exact_cache_hits).toBe(6)
      expect(metrics.request_and_cache.semantic_hits).toBe(2)
      expect(metrics.request_and_cache.coalesced_joins).toBe(3)
      expect(metrics.provider_and_savings.total_tokens_saved).toBe(1880)
      expect(metrics.derived_rates.overall_cache_hit_rate).toBe(0.8)
      expect(metrics.latencies.exact_lookup_latency_ms.mean).toBe(2.1)
    })
  })

  describe('Cache Statistics Retrieval (GET /cache/stats)', () => {
    it('retrieves and parses cache stats payload', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockStats: CacheStats = {
        backend: 'postgres',
        namespace: 'tenant_alpha',
        enabled: true,
        entries: 42,
        max_entries: 1000,
        ttl_seconds: 3600,
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockStats), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const stats = await client.fetchCacheStats()
      expect(stats.backend).toBe('postgres')
      expect(stats.entries).toBe(42)
      expect(stats.enabled).toBe(true)
    })
  })
})
