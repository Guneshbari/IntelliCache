/**
 * Zero-dependency Gateway API Client for IntelliCache Demonstration Chatbot.
 * Communicates exclusively with the FastAPI gateway endpoints.
 * Keeps client API tokens strictly in private memory closures — never in localStorage,
 * IndexedDB, sessionStorage, or cookies.
 */

import type {
  CacheStats,
  NamespaceMetricsSnapshot,
  QueryRequestPayload,
  QueryResponse,
  RootStatusResponse,
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

export class GatewayApiClient {
  private baseUrl: string
  // Strictly memory-only token storage. Never written to disk, window.localStorage, or IndexedDB.
  private clientToken: string | null = null

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
    this.clientToken = trimmed && trimmed.length > 0 ? trimmed : null
  }

  public getClientToken(): string | null {
    return this.clientToken
  }

  public hasToken(): boolean {
    return this.clientToken !== null && this.clientToken.length > 0
  }

  public clearToken(): void {
    this.clientToken = null
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

    if (includeAuth && this.clientToken) {
      headers['X-Client-Token'] = this.clientToken
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
    if (!this.clientToken) {
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
   * Handles single-turn (prompt) and multi-turn (messages) payloads.
   */
  public async sendQuery(payload: QueryRequestPayload): Promise<QueryResponse> {
    const endpoint = '/query'
    try {
      const res = await fetch(`${this.baseUrl}${endpoint}`, {
        method: 'POST',
        headers: this.buildHeaders(true),
        body: JSON.stringify(payload),
      })
      return await this.parseResponse<QueryResponse>(res, endpoint)
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
