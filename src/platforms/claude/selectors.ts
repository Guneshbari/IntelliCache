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
   * Claude uses `data-testid="user-message"` on user turn wrappers.
   */
  USER_MESSAGE: '[data-testid="user-message"]',

  /**
   * Assistant (Claude) response containers.
   * Claude renders assistant responses within elements that carry the
   * `.font-claude-message` class or within the content grid structure.
   * We use a broad but semantically-grounded selector set.
   */
  ASSISTANT_MESSAGE: ['[data-testid="assistant-message"]', '.font-claude-message'].join(', '),

  /**
   * A conversation "turn" wrapper that encloses both a user message and the
   * subsequent assistant response. Claude renders these as sibling block
   * elements inside the scrollable conversation container.
   *
   * We identify turns by their user-message or assistant-message children.
   * There is no single consistent turn-level data-testid on Claude.ai, so
   * extraction iterates direct role elements rather than wrapper articles.
   */
  TURN_WRAPPER: '[data-testid="user-message"], [data-testid="assistant-message"]',

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
