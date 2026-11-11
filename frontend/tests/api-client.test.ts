// @vitest-environment happy-dom
import { beforeEach, describe, expect, it, vi } from 'vitest'
import {
  GatewayApiClient,
  GatewayApiError,
  SseStreamParser,
  validateStreamChunkPayload,
} from '../src/api-client'
import type {
  CacheStats,
  NamespaceMetricsSnapshot,
  QueryResponse,
  RootStatusResponse,
} from '../src/types'

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
    it('manages client token strictly in memory with private runtime field protection', () => {
      expect(client.hasToken()).toBe(false)

      client.setClientToken('tenant-secret-token-xyz')
      expect(client.hasToken()).toBe(true)

      // Verified: Property is a true JavaScript runtime private field (#clientToken)
      // and is not exposed as a public property or getter on the instance
      expect((client as unknown as Record<string, unknown>)['clientToken']).toBeUndefined()
      expect((client as unknown as Record<string, unknown>)['#clientToken']).toBeUndefined()

      // Critical Security Requirement: Ensure token is never written to browser storage
      expect(localStorage.getItem('token')).toBeNull()
      expect(localStorage.getItem('client_token')).toBeNull()
      expect(localStorage.getItem('X-Client-Token')).toBeNull()
      expect(sessionStorage.getItem('token')).toBeNull()
      expect(document.cookie).toBe('')

      client.clearToken()
      expect(client.hasToken()).toBe(false)
    })

    it('ignores empty whitespace-only tokens', () => {
      client.setClientToken('   ')
      expect(client.hasToken()).toBe(false)
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
    it('retrieves and parses actual backend cache stats payload', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockStats: CacheStats = {
        total_entries: 42,
        max_entries: 1000,
        hits: 15,
        misses: 5,
        coalesced_requests: 3,
        total_requests: 20,
        hit_rate: 0.75,
        total_tokens_saved: 540,
        total_latency_saved_ms: 12500.5,
        ttl_seconds: 3600,
        enabled: true,
        backend: 'postgres',
        semantic: {
          lookups: 5,
          hits: 2,
        },
      }

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockStats), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const stats = await client.fetchCacheStats()
      expect(stats.backend).toBe('postgres')
      expect(stats.total_entries).toBe(42)
      expect(stats.max_entries).toBe(1000)
      expect(stats.hits).toBe(15)
      expect(stats.misses).toBe(5)
      expect(stats.hit_rate).toBe(0.75)
      expect(stats.total_tokens_saved).toBe(540)
      expect(stats.enabled).toBe(true)
      expect(stats.semantic?.hits).toBe(2)
    })
  })

  describe('Persistent Conversation Management API', () => {
    it('attaches Idempotency-Key header when sending query with conversation_id', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockResponse: QueryResponse = {
        response: 'Server managed response',
        cached: false,
        model: 'llama-3.3-70b-versatile',
        latency_ms: 100.0,
        usage: { prompt_tokens: 10, completion_tokens: 10, total_tokens: 20 },
        cache_hit_count: 0,
        conversation_id: 'conv-uuid-1',
        turn_id: 'turn-uuid-1',
        context_status: 'complete',
        fingerprint: 'sha256_mock_fp',
        created_at: new Date().toISOString(),
        cache_type: 'none',
      }

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockResponse), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      await client.sendQuery(
        { prompt: 'Hello in conversation', conversation_id: 'conv-uuid-1' },
        'explicit-idempotency-key'
      )

      expect(fetchSpy).toHaveBeenCalledTimes(1)
      const [, init] = fetchSpy.mock.calls[0]
      const headers = init?.headers as Record<string, string>
      expect(headers['Idempotency-Key']).toBe('explicit-idempotency-key')
      expect(headers['X-Client-Token']).toBe('tenant-alpha-token')
    })

    it('lists conversations via GET /conversations', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockList = {
        conversations: [
          {
            id: 'c-1',
            title: 'Chat 1',
            cache_namespace: 'tenant_alpha',
            created_at: '2026-11-10T00:00:00Z',
            updated_at: '2026-11-10T00:01:00Z',
            last_message_at: '2026-11-10T00:01:00Z',
          },
        ],
        total: 1,
      }

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockList), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.listConversations(20, 0)
      expect(fetchSpy).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/conversations?limit=20&offset=0',
        expect.objectContaining({ method: 'GET' })
      )
      expect(result.total).toBe(1)
      expect(result.conversations[0].id).toBe('c-1')
    })

    it('creates a conversation via POST /conversations', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockCreated = {
        id: 'c-new',
        title: 'New Chat Title',
        cache_namespace: 'tenant_alpha',
        created_at: '2026-11-10T00:00:00Z',
        updated_at: '2026-11-10T00:00:00Z',
        last_message_at: '2026-11-10T00:00:00Z',
      }

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockCreated), {
          status: 201,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.createConversation('New Chat Title')
      expect(fetchSpy).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/conversations',
        expect.objectContaining({
          method: 'POST',
          body: JSON.stringify({ title: 'New Chat Title' }),
        })
      )
      expect(result.id).toBe('c-new')
    })

    it('fetches messages via GET /conversations/{id}/messages', async () => {
      client.setClientToken('tenant-alpha-token')

      const mockMessages = {
        conversation_id: 'c-1',
        messages: [
          {
            id: 'm-1',
            conversation_id: 'c-1',
            turn_id: 't-1',
            sequence_number: 1,
            role: 'user',
            content: 'Hello',
            created_at: '2026-11-10T00:00:00Z',
          },
        ],
        total: 1,
      }

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify(mockMessages), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const result = await client.getConversationMessages('c-1')
      expect(fetchSpy).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/conversations/c-1/messages?limit=100&offset=0',
        expect.objectContaining({ method: 'GET' })
      )
      expect(result.messages.length).toBe(1)
      expect(result.messages[0].content).toBe('Hello')
    })

    it('deletes conversation via DELETE /conversations/{id}', async () => {
      client.setClientToken('tenant-alpha-token')

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify({ status: 'ok', deleted: true, conversation_id: 'c-1' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const success = await client.deleteConversation('c-1')
      expect(fetchSpy).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/conversations/c-1',
        expect.objectContaining({ method: 'DELETE' })
      )
      expect(success).toBe(true)
    })
  })

  describe('SseStreamParser (Resilient SSE Parsing)', () => {
    it('handles an event split across multiple network chunks', () => {
      const parser = new SseStreamParser()

      const res1 = parser.feed('data: {"delta": "Hello')
      expect(res1.payloads).toEqual([])
      expect(res1.isDone).toBe(false)

      const res2 = parser.feed(' world"}\n\n')
      expect(res2.payloads).toEqual([{ delta: 'Hello world' }])
      expect(res2.isDone).toBe(false)
    })

    it('handles multiple events in one network chunk', () => {
      const parser = new SseStreamParser()
      const chunk = 'data: {"delta": "part1"}\n\ndata: {"delta": "part2"}\n\n'
      const { payloads, isDone } = parser.feed(chunk)

      expect(payloads.length).toBe(2)
      expect(payloads[0]).toEqual({ delta: 'part1' })
      expect(payloads[1]).toEqual({ delta: 'part2' })
      expect(isDone).toBe(false)
    })

    it('handles CRLF (\\r\\n) and LF (\\n) line endings', () => {
      const parser = new SseStreamParser()
      const crlfChunk = 'data: {"delta": "line1"}\r\n\r\ndata: {"delta": "line2"}\n\n'
      const { payloads } = parser.feed(crlfChunk)

      expect(payloads).toEqual([{ delta: 'line1' }, { delta: 'line2' }])
    })

    it('recognises [DONE] termination', () => {
      const parser = new SseStreamParser()
      const chunk = 'data: {"delta": "final"}\n\ndata: [DONE]\n\n'
      const { payloads, isDone } = parser.feed(chunk)

      expect(payloads).toEqual([{ delta: 'final' }])
      expect(isDone).toBe(true)
    })

    it('throws GatewayApiError on malformed non-empty data events rather than silently discarding them', () => {
      const parser = new SseStreamParser()
      const chunk = 'data: {invalid json\n\ndata: {"delta": "valid"}\n\n'
      expect(() => parser.feed(chunk)).toThrow(GatewayApiError)
    })

    describe('validateStreamChunkPayload (Strict Runtime SSE Frame Validation)', () => {
      it('rejects null, array, and primitive JSON payloads', () => {
        expect(() => validateStreamChunkPayload(null)).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload([1, 2, 3])).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload('string-payload')).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload(12345)).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload(true)).toThrow(GatewayApiError)
      })

      it('rejects non-string delta', () => {
        expect(() => validateStreamChunkPayload({ delta: 123 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ delta: ['array'] })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ delta: { nested: true } })).toThrow(
          GatewayApiError
        )
      })

      it('rejects non-boolean done flags', () => {
        expect(() => validateStreamChunkPayload({ done: 'false' })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ done: 'true' })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ done: 0 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ done: 1 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ done: null })).toThrow(GatewayApiError)
      })

      it('rejects non-string error field', () => {
        expect(() => validateStreamChunkPayload({ error: 404 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ error: { msg: 'bad' } })).toThrow(GatewayApiError)
      })

      it('rejects invalid status_code', () => {
        expect(() => validateStreamChunkPayload({ status_code: '500' })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ status_code: 99 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ status_code: 600 })).toThrow(GatewayApiError)
        expect(() => validateStreamChunkPayload({ status_code: 200.5 })).toThrow(GatewayApiError)
      })

      it('accepts valid intermediate and terminal frames', () => {
        const intermediate = validateStreamChunkPayload({ delta: 'hello', done: false })
        expect(intermediate.delta).toBe('hello')
        expect(intermediate.done).toBe(false)

        const terminal = validateStreamChunkPayload({
          done: true,
          cached: false,
          cache_type: 'none',
          latency_ms: 120,
        })
        expect(terminal.done).toBe(true)
        expect(terminal.cached).toBe(false)
        expect(terminal.latency_ms).toBe(120)
      })
    })

    it('ignores comments and handles flush at stream end', () => {
      const parser = new SseStreamParser()
      parser.feed(': heartbeat\n\ndata: {"delta": "trailing"}')
      const flushed = parser.flush()

      expect(flushed.payloads).toEqual([{ delta: 'trailing' }])
    })
  })

  describe('GatewayApiClient.sendStreamQuery (Optional SSE Streaming)', () => {
    function createMockStream(chunks: string[]): ReadableStream<Uint8Array> {
      const encoder = new TextEncoder()
      let index = 0
      return new ReadableStream({
        pull(controller) {
          if (index < chunks.length) {
            controller.enqueue(encoder.encode(chunks[index++]))
          } else {
            controller.close()
          }
        },
      })
    }

    function createByteStream(chunks: Uint8Array[]): ReadableStream<Uint8Array> {
      let index = 0
      return new ReadableStream({
        pull(controller) {
          if (index < chunks.length) {
            controller.enqueue(chunks[index++])
          } else {
            controller.close()
          }
        },
      })
    }

    function createMonitoredMockStream(chunks: string[]): {
      stream: ReadableStream<Uint8Array>
      cancelSpy: ReturnType<typeof vi.fn>
      releaseLockSpy: ReturnType<typeof vi.fn>
    } {
      const cancelSpy = vi.fn().mockResolvedValue(undefined)
      const releaseLockSpy = vi.fn()
      const rawStream = createMockStream(chunks)
      const originalGetReader = rawStream.getReader.bind(rawStream)

      vi.spyOn(rawStream, 'getReader').mockImplementation((() => {
        const reader = originalGetReader()
        const origCancel = reader.cancel.bind(reader)
        const origRelease = reader.releaseLock?.bind(reader)

        reader.cancel = async (reason?: unknown) => {
          cancelSpy(reason)
          return origCancel(reason)
        }
        if (origRelease) {
          reader.releaseLock = () => {
            releaseLockSpy()
            origRelease()
          }
        }
        return reader
      }) as unknown as typeof rawStream.getReader)

      return { stream: rawStream, cancelSpy, releaseLockSpy }
    }

    it('sends streaming request with stream=true and required headers', async () => {
      client.setClientToken('stream-token-123')

      const sseChunks = [
        'data: {"delta": "Streamed response.", "done": false}\n\n',
        'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 250, "model": "llama-3.3-70b", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "fingerprint": "fp_stream_123"}\n\n',
        'data: [DONE]\n\n',
      ]

      const fetchSpy = vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const deltas: string[] = []
      const result = await client.sendStreamQuery(
        { prompt: 'Tell me about streaming' },
        'custom-idemp-key',
        {
          onDelta: (d) => deltas.push(d),
        }
      )

      expect(fetchSpy).toHaveBeenCalledWith(
        'http://127.0.0.1:8000/query',
        expect.objectContaining({
          method: 'POST',
          headers: expect.objectContaining({
            Accept: 'text/event-stream, application/json',
            'X-Client-Token': 'stream-token-123',
            'Idempotency-Key': 'custom-idemp-key',
          }),
          body: JSON.stringify({ prompt: 'Tell me about streaming', stream: true }),
        })
      )

      expect(deltas).toEqual(['Streamed response.'])
      expect(result.response).toBe('Streamed response.')
      expect(result.cached).toBe(false)
      expect(result.cache_type).toBe('none')
      expect(result.latency_ms).toBe(250)
      expect(result.fingerprint).toBe('fp_stream_123')
      expect(result.model).toBe('llama-3.3-70b')
      expect(result.usage).toEqual({ prompt_tokens: 10, completion_tokens: 5, total_tokens: 15 })
    })

    it('reconstructs authoritative QueryResponse with final metadata including hit counts', async () => {
      const sseChunks = [
        'data: {"delta": "Exact hit", "done": false, "cached": true, "cache_type": "exact"}\n\n',
        'data: {"delta": "", "done": true, "cached": true, "cache_type": "exact", "cache_hit_count": 3, "latency_ms": 4.5, "model": "llama-3.3-70b", "fingerprint": "fp_exact_hit", "context_status": "complete"}\n\n',
        'data: [DONE]\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const result = await client.sendStreamQuery({ prompt: 'Cached query' })
      expect(result.response).toBe('Exact hit')
      expect(result.cached).toBe(true)
      expect(result.cache_type).toBe('exact')
      expect(result.cache_hit_count).toBe(3)
      expect(result.latency_ms).toBe(4.5)
      expect(result.context_status).toBe('complete')
      expect(result.fingerprint).toBe('fp_exact_hit')
    })

    it('handles HTTP error responses by throwing GatewayApiError', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify({ detail: 'Conversation turn conflict' }), {
          status: 409,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      await expect(client.sendStreamQuery({ prompt: 'Conflict test' })).rejects.toThrow(
        GatewayApiError
      )
    })

    it('handles SSE error payloads with status_code 409 as GatewayApiError', async () => {
      const sseChunks = [
        'data: {"error": "Turn \'turn-1\' lost its lease during generation.", "status_code": 409, "done": true}\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Lease loss test' })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.status).toBe(409)
      expect(caughtErr?.message).toContain("Turn 'turn-1' lost its lease")
    })

    it('distinguishes user cancellation (AbortError) as cancellation GatewayApiError', async () => {
      const controller = new AbortController()

      vi.spyOn(globalThis, 'fetch').mockImplementationOnce(() => {
        const err = new Error('The operation was aborted.')
        err.name = 'AbortError'
        return Promise.reject(err)
      })

      controller.abort()

      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery(
          { prompt: 'Cancel test' },
          undefined,
          undefined,
          controller.signal
        )
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('ABORTED')
      expect(caughtErr?.message).toContain('Generation stopped by user.')
    })

    it('correctly parses idempotent replay responses in streaming format', async () => {
      const sseChunks = [
        'data: {"delta": "Previous completed response", "done": false, "fingerprint": "idempotent_replay"}\n\n',
        'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 0.0, "fingerprint": "idempotent_replay", "idempotent_replay": true}\n\n',
        'data: [DONE]\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const result = await client.sendStreamQuery({ prompt: 'Replay query' })
      expect(result.response).toBe('Previous completed response')
      expect(result.idempotent_replay).toBe(true)
      expect(result.fingerprint).toBe('idempotent_replay')
      expect(result.cached).toBe(false)
    })

    it('invokes onDone exactly once with the reconstructed QueryResponse on successful stream', async () => {
      const sseChunks = [
        'data: {"delta": "Hello ", "done": false}\n\n',
        'data: {"delta": "world!", "done": false}\n\n',
        'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 150, "model": "llama-3.3-70b", "fingerprint": "fp_done_test"}\n\n',
        'data: [DONE]\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const onDoneSpy = vi.fn()
      const result = await client.sendStreamQuery({ prompt: 'Callback test' }, undefined, {
        onDone: onDoneSpy,
      })

      expect(result.response).toBe('Hello world!')
      expect(onDoneSpy).toHaveBeenCalledTimes(1)
      expect(onDoneSpy).toHaveBeenCalledWith(result)
    })

    it('rejects incomplete stream that reaches EOF without authoritative final done=true payload', async () => {
      const sseChunks = [
        'data: {"delta": "Partial output that cuts off abruptly", "done": false}\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const onDoneSpy = vi.fn()
      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Truncated stream' }, undefined, {
          onDone: onDoneSpy,
        })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('INCOMPLETE_STREAM')
      expect(caughtErr?.message).toContain('Incomplete stream response')
      expect(onDoneSpy).not.toHaveBeenCalled()
    })

    it('rejects incomplete stream with done=true payload but EOF before [DONE] marker', async () => {
      const sseChunks = [
        'data: {"delta": "Complete text", "done": false}\n\n',
        'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 100, "model": "test", "fingerprint": "fp1"}\n\n',
        // EOF without data: [DONE]
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const onDoneSpy = vi.fn()
      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Missing DONE' }, undefined, {
          onDone: onDoneSpy,
        })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('INCOMPLETE_STREAM')
      expect(onDoneSpy).not.toHaveBeenCalled()
    })

    it('rejects incomplete stream with [DONE] marker but missing authoritative done=true payload', async () => {
      const sseChunks = [
        'data: {"delta": "Only delta without final chunk", "done": false}\n\n',
        'data: [DONE]\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const onDoneSpy = vi.fn()
      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Missing final payload' }, undefined, {
          onDone: onDoneSpy,
        })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('INCOMPLETE_STREAM')
      expect(onDoneSpy).not.toHaveBeenCalled()
    })

    it('rejects stream with malformed non-empty data event as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
      const sseChunks = [
        'data: {"delta": "Initial delta", "done": false}\n\n',
        'data: {corrupted-unparseable-json\n\n',
      ]

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createMockStream(sseChunks), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const onDoneSpy = vi.fn()
      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Malformed json' }, undefined, {
          onDone: onDoneSpy,
        })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
      expect(caughtErr?.message).toContain('Malformed SSE data event payload')
      expect(onDoneSpy).not.toHaveBeenCalled()
    })

    it('reconstructs multi-byte UTF-8 character split across network chunks correctly', async () => {
      // 🚀 is 4 bytes: [0xF0, 0x9F, 0x9A, 0x80] in UTF-8
      const enc = new TextEncoder()
      const prefix = enc.encode('data: {"delta": "Rocket: ')
      const rocketBytes = enc.encode('🚀') // length 4: [240, 159, 154, 128]
      const suffix = enc.encode('", "done": false}\n\n')

      // Chunk 1: prefix + first 2 bytes of rocket
      const chunk1 = new Uint8Array(prefix.length + 2)
      chunk1.set(prefix, 0)
      chunk1.set(rocketBytes.slice(0, 2), prefix.length)

      // Chunk 2: remaining 2 bytes of rocket + suffix
      const chunk2 = new Uint8Array(2 + suffix.length)
      chunk2.set(rocketBytes.slice(2, 4), 0)
      chunk2.set(suffix, 2)

      const chunk3 = enc.encode(
        'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 50, "model": "test", "fingerprint": "fp_utf8"}\n\n'
      )
      const chunk4 = enc.encode('data: [DONE]\n\n')

      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(createByteStream([chunk1, chunk2, chunk3, chunk4]), {
          status: 200,
          headers: { 'Content-Type': 'text/event-stream' },
        })
      )

      const deltas: string[] = []
      const result = await client.sendStreamQuery({ prompt: 'Split UTF-8' }, undefined, {
        onDelta: (d) => deltas.push(d),
      })

      expect(result.response).toBe('Rocket: 🚀')
      expect(deltas.join('')).toBe('Rocket: 🚀')
    })

    it('rejects response with invalid Content-Type without calling onDone', async () => {
      vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
        new Response(JSON.stringify({ response: 'Should have been SSE' }), {
          status: 200,
          headers: { 'Content-Type': 'application/json' },
        })
      )

      const onDoneSpy = vi.fn()
      let caughtErr: GatewayApiError | null = null
      try {
        await client.sendStreamQuery({ prompt: 'Wrong content type' }, undefined, {
          onDone: onDoneSpy,
        })
      } catch (e) {
        caughtErr = e as GatewayApiError
      }

      expect(caughtErr).not.toBeNull()
      expect(caughtErr?.detail).toBe('INVALID_CONTENT_TYPE')
      expect(caughtErr?.message).toContain("Expected 'text/event-stream' Content-Type")
      expect(onDoneSpy).not.toHaveBeenCalled()
    })

    describe('Stage 2.0.2 Strict Frame Validation & Reader Lifecycle', () => {
      it('rejects a stream frame with done="false" string as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: {"delta": "test", "done": "false"}\n\n', 'data: [DONE]\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'String false done' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(caughtErr?.message).toContain('StreamChunkPayload.done must be a boolean')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('rejects a stream frame with done="true" string as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: {"delta": "", "done": "true"}\n\n', 'data: [DONE]\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'String true done' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(caughtErr?.message).toContain('StreamChunkPayload.done must be a boolean')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('rejects a stream frame with numeric done value as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: {"delta": "test", "done": 1}\n\n', 'data: [DONE]\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'Numeric done' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('rejects null JSON payload as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: null\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'Null payload' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(caughtErr?.message).toContain('SSE payload must be a non-null JSON object')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('rejects array or primitive JSON payload as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: ["not", "an", "object"]\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'Array payload' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('rejects non-string delta as STREAM_PROTOCOL_ERROR without calling onDone', async () => {
        const sseChunks = ['data: {"delta": 12345, "done": false}\n\n']
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const onDoneSpy = vi.fn()
        let caughtErr: GatewayApiError | null = null
        try {
          await client.sendStreamQuery({ prompt: 'Number delta' }, undefined, {
            onDone: onDoneSpy,
          })
        } catch (e) {
          caughtErr = e as GatewayApiError
        }
        expect(caughtErr).not.toBeNull()
        expect(caughtErr?.detail).toBe('STREAM_PROTOCOL_ERROR')
        expect(caughtErr?.message).toContain('StreamChunkPayload.delta must be a string')
        expect(onDoneSpy).not.toHaveBeenCalled()
      })

      it('accepts valid intermediate frame with done=false and invokes onDelta', async () => {
        const sseChunks = [
          'data: {"delta": "Alpha ", "done": false}\n\n',
          'data: {"delta": "Beta", "done": false}\n\n',
          'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 100, "model": "test-m", "fingerprint": "fp_int"}\n\n',
          'data: [DONE]\n\n',
        ]
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(createMockStream(sseChunks), {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const deltas: string[] = []
        const onDoneSpy = vi.fn()
        const res = await client.sendStreamQuery({ prompt: 'Intermediate test' }, undefined, {
          onDelta: (d) => deltas.push(d),
          onDone: onDoneSpy,
        })
        expect(res.response).toBe('Alpha Beta')
        expect(deltas).toEqual(['Alpha ', 'Beta'])
        expect(onDoneSpy).toHaveBeenCalledTimes(1)
      })

      it('releases the reader lock on normal EOF completion without cancel', async () => {
        const sseChunks = [
          'data: {"delta": "Done at EOF", "done": false}\n\n',
          'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 50, "model": "test", "fingerprint": "fp_eof"}\n\n',
          'data: [DONE]',
        ]
        const { stream, cancelSpy, releaseLockSpy } = createMonitoredMockStream(sseChunks)
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const res = await client.sendStreamQuery({ prompt: 'EOF test' })
        expect(res.response).toBe('Done at EOF')
        expect(cancelSpy).not.toHaveBeenCalled()
        expect(releaseLockSpy).toHaveBeenCalled()
      })

      it('releases the reader lock when cancelled via AbortSignal', async () => {
        const abortController = new AbortController()
        const cancelSpy = vi.fn().mockResolvedValue(undefined)
        const releaseLockSpy = vi.fn()
        const rawStream = new ReadableStream<Uint8Array>({
          async pull() {
            abortController.abort()
            const err = new Error('The operation was aborted')
            err.name = 'AbortError'
            throw err
          },
        })
        const originalGetReader = rawStream.getReader.bind(rawStream)
        vi.spyOn(rawStream, 'getReader').mockImplementation((() => {
          const reader = originalGetReader()
          const origRelease = reader.releaseLock?.bind(reader)
          reader.cancel = async (reason?: unknown) => {
            cancelSpy(reason)
            return undefined
          }
          if (origRelease) {
            reader.releaseLock = () => {
              releaseLockSpy()
              origRelease()
            }
          }
          return reader
        }) as unknown as typeof rawStream.getReader)

        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(rawStream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )

        await expect(
          client.sendStreamQuery(
            { prompt: 'Abort test' },
            undefined,
            undefined,
            abortController.signal
          )
        ).rejects.toThrow()
        expect(releaseLockSpy).toHaveBeenCalled()
      })

      it('cancels unread stream body when early loop termination occurs upon [DONE] marker', async () => {
        const sseChunks = [
          'data: {"delta": "Early break", "done": false}\n\n',
          'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 60, "model": "test", "fingerprint": "fp_early"}\n\n',
          'data: [DONE]\n\n',
          'data: {"delta": "Unread trailing chunk that should never be read"}\n\n',
        ]
        const { stream, cancelSpy, releaseLockSpy } = createMonitoredMockStream(sseChunks)
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const res = await client.sendStreamQuery({ prompt: 'Early break test' })
        expect(res.response).toBe('Early break')
        expect(cancelSpy).toHaveBeenCalledTimes(1)
        expect(releaseLockSpy).toHaveBeenCalledTimes(1)
      })

      it('cancels reader and releases lock when [DONE] is received without final payload', async () => {
        const sseChunks = [
          'data: {"delta": "Missing final", "done": false}\n\n',
          'data: [DONE]\n\n',
          'data: {"delta": "trailing"}\n\n',
        ]
        const { stream, cancelSpy, releaseLockSpy } = createMonitoredMockStream(sseChunks)
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        await expect(client.sendStreamQuery({ prompt: 'Missing final test' })).rejects.toThrow(
          GatewayApiError
        )
        expect(cancelSpy).toHaveBeenCalledTimes(1)
        expect(releaseLockSpy).toHaveBeenCalledTimes(1)
      })

      it('cancels unread reader and releases lock on malformed payload error', async () => {
        const sseChunks = [
          'data: {"delta": 12345, "done": false}\n\n',
          'data: {"delta": "trailing"}\n\n',
        ]
        const { stream, cancelSpy, releaseLockSpy } = createMonitoredMockStream(sseChunks)
        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        await expect(client.sendStreamQuery({ prompt: 'Malformed cancel test' })).rejects.toThrow(
          GatewayApiError
        )
        expect(cancelSpy).toHaveBeenCalledTimes(1)
        expect(releaseLockSpy).toHaveBeenCalledTimes(1)
      })

      it('guarantees releaseLock executes even if reader.cancel() throws an error', async () => {
        const sseChunks = [
          'data: {"delta": "Cancel throws", "done": false}\n\n',
          'data: {"delta": "", "done": true, "cached": false, "cache_type": "none", "latency_ms": 70, "model": "test", "fingerprint": "fp_ct"}\n\n',
          'data: [DONE]\n\n',
          'data: {"delta": "unread"}\n\n',
        ]
        const { stream, cancelSpy, releaseLockSpy } = createMonitoredMockStream(sseChunks)
        cancelSpy.mockRejectedValue(new Error('Underlying transport cancel failed'))

        vi.spyOn(globalThis, 'fetch').mockResolvedValueOnce(
          new Response(stream, {
            status: 200,
            headers: { 'Content-Type': 'text/event-stream' },
          })
        )
        const res = await client.sendStreamQuery({ prompt: 'Cancel throw test' })
        expect(res.response).toBe('Cancel throws')
        expect(cancelSpy).toHaveBeenCalledTimes(1)
        expect(releaseLockSpy).toHaveBeenCalledTimes(1)
      })
    })
  })
})
