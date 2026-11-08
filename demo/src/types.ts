/**
 * TypeScript Contracts and Data Models for the IntelliCache Demonstration Chatbot.
 * Matches FastAPI backend models in app/api/routes.py, app/core/contracts.py,
 * and app/metrics/contracts.py.
 */

export interface MessageItem {
  role: 'user' | 'assistant' | 'system'
  content: string
}

export interface QueryRequestPayload {
  prompt?: string
  query?: string
  messages?: MessageItem[]
  system_prompt?: string
  model?: string
  temperature?: number
  max_tokens?: number
  stream?: boolean
  bypass_cache?: boolean
  conversation_id?: string
}

export interface UsageMetrics {
  prompt_tokens: number
  completion_tokens: number
  total_tokens: number
}

export interface QueryResponse {
  response: string
  cached: boolean
  model: string
  latency_ms: number
  usage: UsageMetrics
  cache_hit_count: number
  conversation_id?: string | null
  fingerprint: string
  created_at: string
  cache_type: 'exact' | 'semantic' | 'none' | string
}

export interface LatencySummary {
  sample_count: number
  min: number | null
  mean: number | null
  p50: number | null
  p95: number | null
  p99: number | null
  percentile_type: string
  small_sample_warning: boolean
}

export interface LatencyMetrics {
  total_request_latency_ms: LatencySummary
  exact_lookup_latency_ms: LatencySummary
  semantic_lookup_latency_ms: LatencySummary
  embedding_latency_ms?: LatencySummary
  vector_search_latency_ms?: LatencySummary
  candidate_policy_latency_ms?: LatencySummary
  provider_generation_latency_ms: LatencySummary
  exact_cache_write_latency_ms?: LatencySummary
  vector_indexing_latency_ms?: LatencySummary
  estimated_latency_saved_ms?: LatencySummary
}

export interface RequestAndCacheMetrics {
  total_authenticated_requests: number
  exact_cache_lookups: number
  exact_cache_hits: number
  exact_cache_misses: number
  cache_write_failures: number
  semantic_eligibility_checks: number
  semantic_eligibility_rejections: number
  semantic_lookups: number
  semantic_hits: number
  semantic_misses: number
  provider_generation_attempts: number
  provider_generation_successes: number
  provider_generation_failures: number
  cache_bypass_requests: number
  streaming_requests: number
  coalesced_joins: number
  coalesced_successes: number
  coalesced_failures: number
  request_cancellations: number
}

export interface ProviderAndSavingsMetrics {
  provider_calls_attempted: number
  provider_calls_succeeded: number
  provider_calls_failed: number
  provider_prompt_tokens_used: number
  provider_completion_tokens_used: number
  provider_total_tokens_used: number
  prompt_tokens_saved: number
  completion_tokens_saved: number
  total_tokens_saved: number
  provider_calls_avoided: number
}

export interface SemanticMetrics {
  semantic_eligibility_rejections: number
  semantic_lookup_attempts: number
  semantic_hits: number
  semantic_misses: number
  ambiguous_candidate_rejections: number
  incompatible_candidate_rejections: number
  embedding_failures: number
  vector_store_failures: number
  indexing_successes: number
  indexing_failures: number
}

export interface DerivedRates {
  exact_hit_rate: number
  semantic_hit_rate: number
  overall_cache_hit_rate: number
  coalesced_reuse_rate: number
}

export interface NamespaceMetricsSnapshot {
  scope: 'namespace'
  namespace: string
  collection_uptime_seconds: number
  timestamp: string
  request_and_cache: RequestAndCacheMetrics
  provider_and_savings: ProviderAndSavingsMetrics
  semantic: SemanticMetrics
  derived_rates: DerivedRates
  latencies: LatencyMetrics
  notes?: Record<string, string> | null
}

export interface CacheStats {
  backend: string
  namespace: string
  enabled: boolean
  entries: number
  max_entries: number
  ttl_seconds: number
  semantic?: Record<string, unknown> | null
}

export interface RootStatusResponse {
  service: string
  version: string
  status: string
  uptime_seconds: number
  configured_model: string
  cache_backend: string
  cache_namespace: string
  cache_enabled: boolean
  endpoints: Record<string, string>
}

export type ConnectionState = 'disconnected' | 'connecting' | 'connected' | 'unauthorized' | 'error'

export interface ConnectionInfo {
  state: ConnectionState
  gatewayUrl: string
  namespace?: string
  serverVersion?: string
  errorDetail?: string
}

export interface ChatMessage {
  id: string
  role: 'user' | 'assistant'
  content: string
  timestamp: number
  responseMeta?: QueryResponse
  error?: string
}
