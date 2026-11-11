/**
 * Zero-dependency Gateway API Client for IntelliCache Demonstration Chatbot.
 * Communicates exclusively with the FastAPI gateway endpoints.
 * Keeps client API tokens strictly in private memory closures — never in localStorage,
 * IndexedDB, sessionStorage, or cookies.
 */

import type {
  CacheStats,
  ConversationListResponse,
  ConversationMessagesResponse,
  ConversationSummary,
  NamespaceMetricsSnapshot,
  QueryRequestPayload,
  QueryResponse,
  RootStatusResponse,
  StreamCallbacks,
  StreamChunkPayload,
} from './types'

export class GatewayApiError extends Error {
  public readonly status: number
  public readonly detail?: string
  public readonly isAuthError: boolean
  public readonly isValidationError: boolean
  public readonly isRateLimit: boolean
  public readonly isServerError: boolean
  public readonly isNetworkError: boolean

  constructor(status: number, message: string, detail?: string) {
    super(message)
    this.name = 'GatewayApiError'
    this.status = status
    this.detail = detail
    this.isAuthError = status === 401 || status === 403
    this.isValidationError = status === 422
    this.isRateLimit = status === 429
    this.isServerError = status >= 500 && status < 600
    this.isNetworkError = status === 0
  }
}

export interface ConnectionTestResult {
  online: boolean
  authenticated: boolean
  namespace?: string
  serverVersion?: string
  configuredModel?: string
  cacheBackend?: string
  cacheEnabled?: boolean
  error?: string
}

/**
 * Resilient Server-Sent Events (SSE) Stream Parser.
 * Handles split JSON/event chunks across network reads, CRLF and LF line endings,
 * multiple events per chunk, [DONE] termination, comments, and empty lines.
 */
export class SseStreamParser {
  private buffer: string = ''
  private currentDataLines: string[] = []

  public feed(chunkText: string): { payloads: StreamChunkPayload[]; isDone: boolean } {
    this.buffer += chunkText
    const payloads: StreamChunkPayload[] = []
    let isDone = false

    const newlineRegex = /\r\n|\n|\r/g
    let lastIndex = 0
    let match: RegExpExecArray | null

    while ((match = newlineRegex.exec(this.buffer)) !== null) {
      const matchIndex = match.index
      const matchLen = match[0].length

      // If \r is the very last char, wait to see if \n follows in next chunk
      if (match[0] === '\r' && matchIndex === this.buffer.length - 1) {
        break
      }

      const line = this.buffer.slice(lastIndex, matchIndex)
      lastIndex = matchIndex + matchLen

      if (line === '') {
        if (this.currentDataLines.length > 0) {
          const combined = this.currentDataLines.join('\n').trim()
          this.currentDataLines = []
          if (combined === '[DONE]') {
            isDone = true
          } else if (combined) {
            try {
              const parsed = JSON.parse(combined) as StreamChunkPayload
              payloads.push(parsed)
            } catch {
              // Ignore malformed JSON payloads gracefully
            }
          }
        }
      } else if (line.startsWith('data:')) {
        const content = line.slice(5).replace(/^ /, '')
        this.currentDataLines.push(content)
      }
    }

    if (lastIndex > 0) {
      this.buffer = this.buffer.slice(lastIndex)
    }

    return { payloads, isDone }
  }

  public flush(): { payloads: StreamChunkPayload[]; isDone: boolean } {
    const payloads: StreamChunkPayload[] = []
    let isDone = false

    if (this.buffer.length > 0) {
      const line = this.buffer
      this.buffer = ''
      if (line.startsWith('data:')) {
        this.currentDataLines.push(line.slice(5).replace(/^ /, ''))
      }
    }

    if (this.currentDataLines.length > 0) {
      const combined = this.currentDataLines.join('\n').trim()
      this.currentDataLines = []
      if (combined === '[DONE]') {
        isDone = true
      } else if (combined) {
        try {
          const parsed = JSON.parse(combined) as StreamChunkPayload
          payloads.push(parsed)
        } catch {
          // Ignore malformed JSON payloads gracefully
        }
      }
    }

    return { payloads, isDone }
  }

  public reset(): void {
    this.buffer = ''
    this.currentDataLines = []
  }
}

export class GatewayApiClient {
  private baseUrl: string
  // Strictly memory-only token storage with ECMAScript runtime private field isolation (#clientToken).
  // Never written to disk, window.localStorage, IndexedDB, or exposed via public getters.
  #clientToken: string | null = null

  constructor(initialBaseUrl: string = 'http://127.0.0.1:8000') {
    this.baseUrl = this.normalizeUrl(initialBaseUrl)
  }

  public setBaseUrl(url: string): void {
    this.baseUrl = this.normalizeUrl(url)
  }

  public getBaseUrl(): string {
    return this.baseUrl
  }

  public setClientToken(token: string | null): void {
    const trimmed = token?.trim()
    this.#clientToken = trimmed && trimmed.length > 0 ? trimmed : null
  }

  public hasToken(): boolean {
    return this.#clientToken !== null && this.#clientToken.length > 0
  }

  public clearToken(): void {
    this.#clientToken = null
  }

  private normalizeUrl(url: string): string {
    const trimmed = (url || '').trim()
    if (!trimmed) {
      return 'http://127.0.0.1:8000'
    }
    return trimmed.replace(/\/+$/, '')
  }

  private buildHeaders(includeAuth: boolean = true): Record<string, string> {
    const headers: Record<string, string> = {
      Accept: 'application/json',
      'Content-Type': 'application/json',
    }

    if (includeAuth && this.#clientToken) {
      headers['X-Client-Token'] = this.#clientToken
    }

    return headers
  }

  private async handleFetchError(err: unknown, endpoint: string): Promise<never> {
    if (err instanceof GatewayApiError) {
      throw err
    }
    const errorMsg = err instanceof Error ? err.message : String(err)
    throw new GatewayApiError(
      0,
      `Network connection failed when reaching gateway at ${this.baseUrl}${endpoint}. Ensure the server is running and CORS allows this origin. (${errorMsg})`,
      errorMsg
    )
  }

  private async parseResponse<T>(res: Response, endpoint: string): Promise<T> {
    if (!res.ok) {
      let errorDetail: string | undefined
      try {
        const json = await res.json()
        errorDetail = typeof json.detail === 'string' ? json.detail : JSON.stringify(json.detail)
      } catch {
        try {
          errorDetail = await res.text()
        } catch {
          errorDetail = undefined
        }
      }

      const friendlyMessage = this.formatErrorMessage(res.status, endpoint, errorDetail)
      throw new GatewayApiError(res.status, friendlyMessage, errorDetail)
    }

    return (await res.json()) as T
  }

  private formatErrorMessage(status: number, endpoint: string, detail?: string): string {
    switch (status) {
      case 401:
        return detail
          ? `Unauthorized: ${detail}`
          : 'Unauthorized: Invalid or missing client API token. Please configure a valid client token in Connection Settings.'
      case 403:
        return detail
          ? `Forbidden: ${detail}`
          : 'Forbidden: You do not have permission to access this resource or endpoint.'
      case 422:
        return detail
          ? `Validation Error: ${detail}`
          : 'Validation Error: The request payload is malformed or exceeds size limits.'
      case 429:
        return detail
          ? `Provider Rate Limit Exceeded: ${detail}`
          : 'Provider Rate Limit Exceeded: Upstream provider is temporarily throttling requests. Please retry shortly.'
      case 502:
        return detail
          ? `Bad Gateway: ${detail}`
          : 'Bad Gateway: Upstream LLM provider returned an unhandled error.'
      case 503:
        return detail
          ? `Service Unavailable: ${detail}`
          : 'Service Unavailable: Upstream LLM provider connection failed or leader request was cancelled.'
      case 500:
        return detail
          ? `Internal Gateway Error: ${detail}`
          : 'Internal Gateway Error: An unexpected error occurred on the IntelliCache gateway.'
      default:
        return detail
          ? `Gateway Error (${status}): ${detail}`
          : `Gateway returned HTTP status ${status} on ${endpoint}.`
    }
  }

  /**
   * Tests connection to the gateway.
   * Checks gateway reachability via GET / and authenticates client token via GET /metrics/me.
   */
  public async testConnection(): Promise<ConnectionTestResult> {
    let rootInfo: RootStatusResponse
    try {
      const res = await fetch(`${this.baseUrl}/`, {
        method: 'GET',
        headers: this.buildHeaders(false),
      })
      rootInfo = await this.parseResponse<RootStatusResponse>(res, '/')
    } catch (err) {
      let message = err instanceof Error ? err.message : String(err)
      if (err instanceof GatewayApiError) {
        message = err.message
      } else {
        message = `Network connection failed when reaching gateway at ${this.baseUrl}/. (${message})`
      }
      return {
        online: false,
        authenticated: false,
        error: message,
      }
    }

    // Gateway is online. If token is provided, verify client token against /metrics/me
    if (!this.#clientToken) {
      return {
        online: true,
        authenticated: false,
        serverVersion: rootInfo.version,
        configuredModel: rootInfo.configured_model,
        cacheBackend: rootInfo.cache_backend,
        cacheEnabled: rootInfo.cache_enabled,
        error: 'Gateway is online. Provide a client API token to authenticate.',
      }
    }

    try {
      const metrics = await this.fetchNamespaceMetrics()
      return {
        online: true,
        authenticated: true,
        namespace: metrics.namespace,
        serverVersion: rootInfo.version,
        configuredModel: rootInfo.configured_model,
        cacheBackend: rootInfo.cache_backend,
        cacheEnabled: rootInfo.cache_enabled,
      }
    } catch (err) {
      const apiErr = err instanceof GatewayApiError ? err : new GatewayApiError(0, String(err))
      return {
        online: true,
        authenticated: false,
        serverVersion: rootInfo.version,
        configuredModel: rootInfo.configured_model,
        cacheBackend: rootInfo.cache_backend,
        cacheEnabled: rootInfo.cache_enabled,
        error: apiErr.message,
      }
    }
  }

  /**
   * Sends a query request to POST /query on the IntelliCache Gateway.
   * Handles single-turn (prompt) and server-managed multi-turn (conversation_id + prompt) payloads.
   * Attaches Idempotency-Key header when idempotencyKey is supplied or when conversation_id is active.
   */
  public async sendQuery(
    payload: QueryRequestPayload,
    idempotencyKey?: string
  ): Promise<QueryResponse> {
    const endpoint = '/query'
    try {
      const headers = this.buildHeaders(true)
      const effectiveKey =
        idempotencyKey || (payload.conversation_id ? crypto.randomUUID() : undefined)
      if (effectiveKey) {
        headers['Idempotency-Key'] = effectiveKey
      }

      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(payload),
      })
      return await this.parseResponse<QueryResponse>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Sends an optional streaming query request via SSE to POST /query.
   * Handles progressive chunk delivery, [DONE] termination, status_code 409 conflict,
   * error payloads over HTTP 200, AbortSignal cancellation, and reconstructs authoritative QueryResponse.
   */
  public async sendStreamQuery(
    payload: QueryRequestPayload,
    idempotencyKey?: string,
    callbacks?: StreamCallbacks,
    signal?: AbortSignal
  ): Promise<QueryResponse> {
    const endpoint = '/query'
    try {
      const headers = this.buildHeaders(true)
      headers['Accept'] = 'text/event-stream, application/json'

      const effectiveKey =
        idempotencyKey || (payload.conversation_id ? crypto.randomUUID() : undefined)
      if (effectiveKey) {
        headers['Idempotency-Key'] = effectiveKey
      }

      const streamPayload: QueryRequestPayload = {
        ...payload,
        stream: true,
      }

      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'POST',
        headers,
        body: JSON.stringify(streamPayload),
        signal,
      })

      if (!res.ok) {
        return await this.parseResponse<QueryResponse>(res, endpoint)
      }

      if (!res.body) {
        throw new GatewayApiError(0, 'Gateway response body stream is not available.')
      }

      const reader = res.body.getReader()
      const decoder = new TextDecoder('utf-8')
      const parser = new SseStreamParser()

      let accumulatedText = ''
      let finalChunk: StreamChunkPayload | null = null

      try {
        while (true) {
          const { done, value } = await reader.read()
          if (done) {
            const flushed = parser.flush()
            for (const chunk of flushed.payloads) {
              if (chunk.error || (chunk.status_code && chunk.status_code >= 400)) {
                throw new GatewayApiError(
                  chunk.status_code || 500,
                  chunk.error || `Stream failed with status ${chunk.status_code}`,
                  chunk.error
                )
              }
              callbacks?.onChunk?.(chunk)
              if (chunk.delta) {
                accumulatedText += chunk.delta
                callbacks?.onDelta?.(chunk.delta)
              }
              if (chunk.done) {
                finalChunk = chunk
              }
            }
            break
          }

          const chunkText = decoder.decode(value, { stream: true })
          const { payloads, isDone } = parser.feed(chunkText)

          for (const chunk of payloads) {
            if (chunk.error || (chunk.status_code && chunk.status_code >= 400)) {
              throw new GatewayApiError(
                chunk.status_code || 500,
                chunk.error || `Stream failed with status ${chunk.status_code}`,
                chunk.error
              )
            }

            callbacks?.onChunk?.(chunk)

            if (chunk.delta) {
              accumulatedText += chunk.delta
              callbacks?.onDelta?.(chunk.delta)
            }

            if (chunk.done) {
              finalChunk = chunk
            }
          }

          if (isDone) {
            break
          }
        }
      } finally {
        reader.releaseLock?.()
      }

      return {
        response: accumulatedText,
        cached: typeof finalChunk?.cached === 'boolean' ? finalChunk.cached : null,
        cache_type: (finalChunk?.cache_type as QueryResponse['cache_type']) ?? 'none',
        model: finalChunk?.model || payload.model || '',
        latency_ms: typeof finalChunk?.latency_ms === 'number' ? finalChunk.latency_ms : null,
        usage: finalChunk?.usage ? finalChunk.usage : null,
        cache_hit_count:
          typeof finalChunk?.cache_hit_count === 'number' ? finalChunk.cache_hit_count : null,
        conversation_id: finalChunk?.conversation_id || payload.conversation_id || null,
        turn_id: finalChunk?.turn_id || null,
        context_status: (finalChunk?.context_status as QueryResponse['context_status']) || null,
        fingerprint: finalChunk?.fingerprint || '',
        created_at: new Date().toISOString(),
        idempotent_replay: finalChunk?.idempotent_replay,
      }
    } catch (err: unknown) {
      if (signal?.aborted || (err instanceof Error && err.name === 'AbortError')) {
        throw new GatewayApiError(0, 'Generation stopped by user.', 'ABORTED')
      }
      if (err instanceof GatewayApiError) {
        throw err
      }
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Lists conversations owned by the authenticated client via GET /conversations.
   */
  public async listConversations(
    limit: number = 50,
    offset: number = 0
  ): Promise<ConversationListResponse> {
    const endpoint = `/conversations?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'GET',
        headers: this.buildHeaders(true),
      })
      return await this.parseResponse<ConversationListResponse>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Creates a new persistent conversation via POST /conversations.
   */
  public async createConversation(title?: string, id?: string): Promise<ConversationSummary> {
    const endpoint = '/conversations'
    try {
      const body: Record<string, string> = {}
      if (title) body.title = title
      if (id) body.id = id

      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'POST',
        headers: this.buildHeaders(true),
        body: JSON.stringify(body),
      })
      return await this.parseResponse<ConversationSummary>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Retrieves conversation metadata via GET /conversations/{id}.
   */
  public async getConversation(id: string): Promise<ConversationSummary> {
    const endpoint = `/conversations/${encodeURIComponent(id)}`
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'GET',
        headers: this.buildHeaders(true),
      })
      return await this.parseResponse<ConversationSummary>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Retrieves messages for a conversation in chronological order via GET /conversations/{id}/messages.
   */
  public async getConversationMessages(
    id: string,
    limit: number = 100,
    offset: number = 0
  ): Promise<ConversationMessagesResponse> {
    const endpoint = `/conversations/${encodeURIComponent(id)}/messages?limit=${encodeURIComponent(limit)}&offset=${encodeURIComponent(offset)}`
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'GET',
        headers: this.buildHeaders(true),
      })
      return await this.parseResponse<ConversationMessagesResponse>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Deletes a conversation and its turns and messages via DELETE /conversations/{id}.
   */
  public async deleteConversation(id: string): Promise<boolean> {
    const endpoint = `/conversations/${encodeURIComponent(id)}`
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'DELETE',
        headers: this.buildHeaders(true),
      })
      await this.parseResponse<Record<string, unknown>>(res, endpoint)
      return true
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Retrieves runtime metrics for the client's authenticated namespace via GET /metrics/me.
   */
  public async fetchNamespaceMetrics(): Promise<NamespaceMetricsSnapshot> {
    const endpoint = '/metrics/me'
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'GET',
        headers: this.buildHeaders(true),
      })
      return await this.parseResponse<NamespaceMetricsSnapshot>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }

  /**
   * Retrieves cache statistics via GET /cache/stats.
   */
  public async fetchCacheStats(): Promise<CacheStats> {
    const endpoint = '/cache/stats'
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'GET',
        headers: this.buildHeaders(true),
      })
      return await this.parseResponse<CacheStats>(res, endpoint)
    } catch (err) {
      return await this.handleFetchError(err, endpoint)
    }
  }
}
