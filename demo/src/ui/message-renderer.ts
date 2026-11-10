/**
 * Message and Cache Performance Diagnostics Renderer for the IntelliCache Demonstration Chatbot.
 *
 * Renders user turns as safe escaped plain text, assistant turns as sanitized Markdown,
 * and attaches authoritative cache diagnostics cards with exact/semantic/provider badges.
 */

import type { ChatMessage, QueryResponse } from '../types'
import { MarkdownRenderer } from './markdown-renderer'

export class MessageRenderer {
  /**
   * Renders the complete conversation history or the empty welcome state.
   */
  public static renderMessages(messages: ChatMessage[], isGenerating: boolean): string {
    if (messages.length === 0 && !isGenerating) {
      return this.renderEmptyState()
    }

    let html = ''
    for (const msg of messages) {
      if (msg.role === 'user') {
        html += this.renderUserMessage(msg)
      } else if (msg.error) {
        html += this.renderErrorMessage(msg)
      } else {
        html += this.renderAssistantMessage(msg)
      }
    }

    if (isGenerating) {
      html += this.renderLoadingIndicator()
    }

    return html
  }

  private static renderEmptyState(): string {
    return `
      <div class="empty-state">
        <div class="empty-badge">INTELLICACHE ARCHITECTURE DEMO</div>
        <h2 class="empty-title">Intelligent AI Semantic Response Gateway</h2>
        <p class="empty-desc">
          Test real-time Exact-Match SHA-256 caching, Vector Semantic Similarity reuse, and process-local SingleFlight coalescing against live LLM providers.
        </p>

        <div class="sample-prompts-section">
          <div class="sample-prompts-label">Interactive Test Scenarios</div>

          <div class="sample-group">
            <div class="sample-group-title">
              <span class="sample-group-icon">⚡</span>
              <span>1. Exact Cache Hit (Deterministic SHA-256 Match)</span>
            </div>
            <div class="prompt-chips">
              <button type="button" class="prompt-chip" data-prompt="Explain Python asyncio event loop and task scheduling">
                Explain Python asyncio event loop and task scheduling
              </button>
              <button type="button" class="prompt-chip" data-prompt="What is the difference between let, const, and var in JavaScript?">
                What is the difference between let, const, and var in JavaScript?
              </button>
            </div>
          </div>

          <div class="sample-group">
            <div class="sample-group-title">
              <span class="sample-group-icon">🧠</span>
              <span>2. Semantic Cache Hit (pgvector HNSW Cosine Rephrasing)</span>
            </div>
            <div class="prompt-chips">
              <button type="button" class="prompt-chip" data-prompt="Describe how tasks and the event loop work in Python asyncio">
                Describe how tasks and the event loop work in Python asyncio
              </button>
              <button type="button" class="prompt-chip" data-prompt="How do let, const, and var differ in modern JavaScript?">
                How do let, const, and var differ in modern JavaScript?
              </button>
              <button type="button" class="prompt-chip" data-prompt="How does PostgreSQL pgvector perform approximate nearest neighbor search?">
                How does PostgreSQL pgvector perform approximate nearest neighbor search?
              </button>
            </div>
          </div>
        </div>
      </div>
    `
  }

  private static renderUserMessage(msg: ChatMessage): string {
    const timeStr = this.formatTime(msg.timestamp)
    // Strictly escape user content to ensure it is always rendered as plain text
    const escapedContent = MarkdownRenderer.escapeHtml(msg.content)
    const attemptBadge = msg.isFailedAttempt
      ? '<span class="author-badge attempt-badge" title="This generation attempt did not complete">Failed Attempt</span>'
      : ''

    return `
      <div class="message-row user ${msg.isFailedAttempt ? 'user-failed-attempt' : ''}" id="msg-${msg.id}">
        <div class="message-header">
          <div class="author-info">
            <span class="author-badge user-badge">You</span>
            ${attemptBadge}
          </div>
          <span class="message-timestamp">${timeStr}</span>
        </div>
        <div class="message-bubble user-bubble">${escapedContent}</div>
      </div>
    `
  }

  private static renderAssistantMessage(msg: ChatMessage): string {
    const timeStr = this.formatTime(msg.timestamp)
    const meta = msg.responseMeta
    const modelName = meta?.model ? MarkdownRenderer.escapeHtml(meta.model) : 'IntelliCache'
    const formattedBody = MarkdownRenderer.renderMarkdown(msg.content)

    return `
      <div class="message-row assistant" id="msg-${msg.id}">
        <div class="message-header">
          <div class="assistant-meta">
            <span class="author-badge assistant-badge">⚡ IntelliCache</span>
            <span class="model-pill">${modelName}</span>
          </div>
          <div class="message-header-right">
            <span class="message-timestamp">${timeStr}</span>
            <button class="btn-copy-response" type="button" data-msg-id="${msg.id}" title="Copy response markdown to clipboard" aria-label="Copy response">
              📋 Copy
            </button>
          </div>
        </div>

        <div class="message-bubble assistant-bubble">${formattedBody}</div>

        ${meta ? this.renderCacheDiagnostics(meta) : ''}
      </div>
    `
  }

  private static renderCacheDiagnostics(meta: QueryResponse): string {
    let badgeClass = 'provider'
    let badgeLabel = '🌐 Upstream Provider Generation'
    let latencyClass = 'latency-standard'

    if (meta.cached === null || meta.cached === undefined) {
      badgeClass = 'unknown'
      badgeLabel = 'ℹ️ Diagnostic History Unavailable'
      latencyClass = 'latency-standard'
    } else if (meta.idempotent_replay) {
      badgeClass = 'replay'
      badgeLabel = '🔁 Idempotent Replay'
      latencyClass = 'latency-fast'
    } else if (meta.cached && meta.cache_type === 'exact') {
      badgeClass = 'exact'
      badgeLabel = '⚡ Exact Cache Hit'
      latencyClass = 'latency-fast'
    } else if (meta.cached && meta.cache_type === 'semantic') {
      badgeClass = 'semantic'
      badgeLabel = '🧠 Semantic Cache Hit'
      latencyClass = 'latency-fast'
    }

    const shortFp = meta.fingerprint ? `${meta.fingerprint.slice(0, 16)}...` : 'n/a'
    const totalTokens = meta.usage?.total_tokens ?? '—'
    const promptTokens = meta.usage?.prompt_tokens ?? '—'
    const compTokens = meta.usage?.completion_tokens ?? '—'
    const formattedLatency =
      meta.latency_ms !== undefined && meta.latency_ms !== null
        ? `${meta.latency_ms.toFixed(1)} ms`
        : '—'
    const formattedHitCount =
      meta.cache_hit_count !== undefined && meta.cache_hit_count !== null
        ? String(meta.cache_hit_count)
        : '—'

    return `
      <div class="cache-diagnostics-container">
        <div class="cache-compact-strip">
          <span class="cache-badge ${badgeClass}">${badgeLabel}</span>
          <span class="cache-latency-pill ${latencyClass}">${formattedLatency}</span>
          <details class="diagnostics-details">
            <summary class="diagnostics-summary">
              <span>Inspect Cache Metadata</span>
              <span class="details-chevron">▾</span>
            </summary>
            <div class="diagnostics-card">
              <div class="diagnostics-grid">
                <div class="diag-item">
                  <span class="diag-label">Configured Model:</span>
                  <span class="diag-value" title="${MarkdownRenderer.escapeHtml(meta.model)}">${MarkdownRenderer.escapeHtml(meta.model)}</span>
                </div>
                <div class="diag-item">
                  <span class="diag-label">Cache Hit Count:</span>
                  <span class="diag-value highlight-accent">${formattedHitCount}</span>
                </div>
                <div class="diag-item">
                  <span class="diag-label">Total Tokens:</span>
                  <span class="diag-value">${totalTokens}</span>
                </div>
                <div class="diag-item">
                  <span class="diag-label">Prompt / Completion:</span>
                  <span class="diag-value">${promptTokens} / ${compTokens}</span>
                </div>
                ${
                  meta.context_status
                    ? `
                <div class="diag-item">
                  <span class="diag-label">Context Status:</span>
                  <span class="diag-value highlight-accent">${MarkdownRenderer.escapeHtml(meta.context_status)}</span>
                </div>`
                    : ''
                }
                ${
                  meta.turn_id
                    ? `
                <div class="diag-item">
                  <span class="diag-label">Turn ID:</span>
                  <span class="diag-value" title="${MarkdownRenderer.escapeHtml(meta.turn_id)}">${MarkdownRenderer.escapeHtml(meta.turn_id.slice(0, 8))}...</span>
                </div>`
                    : ''
                }
                <div class="diag-item diag-full">
                  <span class="diag-label">SHA-256 Fingerprint:</span>
                  <div class="fingerprint-row">
                    <code class="fingerprint-code" title="${MarkdownRenderer.escapeHtml(meta.fingerprint)}">${MarkdownRenderer.escapeHtml(shortFp)}</code>
                    <button class="btn-copy-fp" type="button" data-fp="${MarkdownRenderer.escapeHtml(meta.fingerprint)}" title="Copy full SHA-256 fingerprint">
                      Copy
                    </button>
                  </div>
                </div>
              </div>
            </div>
          </details>
        </div>
      </div>
    `
  }

  private static renderErrorMessage(msg: ChatMessage): string {
    const timeStr = this.formatTime(msg.timestamp)
    const errorText = MarkdownRenderer.escapeHtml(msg.error || msg.content)

    return `
      <div class="message-row assistant error-row" id="msg-${msg.id}">
        <div class="message-header">
          <span class="author-badge error-badge">⚠️ Gateway Error</span>
          <span class="message-timestamp">${timeStr}</span>
        </div>
        <div class="error-callout">
          <div class="error-message">${errorText}</div>
          <div class="error-actions">
            <button class="btn-retry" type="button" data-retry-id="${msg.id}" aria-label="Retry query">
              🔄 Retry Query
            </button>
          </div>
        </div>
      </div>
    `
  }

  private static renderLoadingIndicator(): string {
    return `
      <div class="message-row assistant loading-row" id="msg-loading">
        <div class="message-header">
          <span class="author-badge assistant-badge">⚡ IntelliCache</span>
          <span class="model-pill">processing</span>
        </div>
        <div class="loading-bubble">
          <div class="loading-dots">
            <span class="dot"></span>
            <span class="dot"></span>
            <span class="dot"></span>
          </div>
          <span class="loading-text">Executing query on IntelliCache gateway...</span>
        </div>
      </div>
    `
  }

  private static formatTime(timestamp: number): string {
    return new Date(timestamp).toLocaleTimeString([], {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
    })
  }
}
