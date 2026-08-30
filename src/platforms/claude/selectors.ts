/**
 * Selectors and DOM attribute definitions for Claude Data Collection.
 *
 * NOTE: Prefer semantic attributes (`data-testid`, `aria-*`, `role`) over
 * volatile CSS class names to maximize extraction longevity across Claude.ai
 * React app updates.
 *
 * Claude's DOM structure is not fully documented. Selectors are designed to
 * be resilient and fail gracefully (returning null) when elements are absent.
 */

export const CLAUDE_SELECTORS = {
  /**
   * User message containers.
   * Primary: data-testid="user-message" (standard Claude UI)
   * Fallback: transcript-row[data-perf-row="human"] (observed in Claude transcript DOM)
   */
  USER_MESSAGE: [
    '[data-testid="user-message"]',
    '[data-testid="transcript-row"][data-perf-row="human"]',
  ].join(', '),

  /**
   * Assistant (Claude) response containers.
   * Primary: data-testid="assistant-message"
   * Secondary: .font-claude-message
   * Fallback: transcript-row[data-perf-row="assistant"] (observed in Claude transcript DOM)
   */
  ASSISTANT_MESSAGE: [
    '[data-testid="assistant-message"]',
    '.font-claude-message',
    '[data-testid="transcript-row"][data-perf-row="assistant"]',
  ].join(', '),

  /**
   * Transcript list container (observed in real Claude DOM via inspection).
   * Claude renders the full conversation inside this container.
   */
  TRANSCRIPT_LIST: '[data-testid="transcript-list"]',

  /**
   * Individual transcript row elements (observed in real Claude DOM).
   * data-perf-row attribute distinguishes human vs. assistant rows.
   */
  TRANSCRIPT_ROW: '[data-testid="transcript-row"]',

  /**
   * User transcript rows — data-perf-row="human"
   */
  TRANSCRIPT_USER_ROW: '[data-testid="transcript-row"][data-perf-row="human"]',

  /**
   * Assistant transcript rows — data-perf-row="assistant"
   */
  TRANSCRIPT_ASSISTANT_ROW: '[data-testid="transcript-row"][data-perf-row="assistant"]',

  /**
   * Code block containers.
   * Claude renders code inside standard `<pre><code>` pairs; copy buttons
   * sit outside the `<code>` element.
   */
  CODE_BLOCK: 'pre',
  CODE_ELEMENT: 'pre code, code',

  /**
   * Interactive UI elements that must be stripped from extracted text.
   * Includes copy buttons, action toolbars, feedback icons, and navigation.
   */
  UI_CONTROLS_TO_EXCLUDE: [
    'button',
    'svg',
    'form',
    'nav',
    'aside',
    '[aria-label="Copy"]',
    '[data-testid="copy-button"]',
    '[role="toolbar"]',
    '[role="navigation"]',
  ].join(', '),

  /**
   * Streaming / generation in-progress indicators.
   * When any of these are present, the response is considered incomplete.
   *
   * Claude shows a stop/cancel button during generation and may render
   * streaming-state class names or a progress indicator.
   */
  STREAMING_INDICATORS: [
    'button[aria-label="Stop Response"]',
    'button[data-testid="stop-button"]',
    '[data-testid="streaming-indicator"]',
    '.streaming',
    '.animate-pulse',
  ].join(', '),

  /**
   * Stop button selector (subset of STREAMING_INDICATORS focused on the
   * interactive button that disappears when generation completes).
   */
  STOP_BUTTON: ['button[aria-label="Stop Response"]', 'button[data-testid="stop-button"]'].join(
    ', '
  ),

  /**
   * Model selector trigger button.
   * Claude exposes the active model name via a dropdown trigger button.
   * Returns null gracefully when absent.
   */
  MODEL_SELECTOR: [
    '[data-testid="model-selector-trigger"]',
    'button[data-testid="model-selector"]',
  ].join(', '),

  /**
   * Timestamp selector.
   * Claude does not consistently expose `<time datetime="...">` in its DOM.
   * Included for forward compatibility if Claude adds timestamps.
   */
  TIMESTAMP: 'time[datetime], [data-timestamp]',
} as const
