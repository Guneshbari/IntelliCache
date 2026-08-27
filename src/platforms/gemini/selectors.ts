/**
 * Selectors and DOM attribute definitions for Gemini Data Collection.
 *
 * NOTE: Prefer semantic attributes (`user-query`, `model-response`, `data-testid`, `aria-*`, `role`)
 * over volatile Angular/Material CSS class names to maximize extraction longevity across
 * gemini.google.com updates.
 *
 * Gemini's DOM structure is designed to be resilient and fail gracefully (returning null)
 * when elements are absent.
 */

export const GEMINI_SELECTORS = {
  /**
   * User message containers.
   * Gemini custom element or class wrapper for user turns.
   */
  USER_MESSAGE: [
    'user-query',
    '[data-message-author-role="user"]',
    '.user-query-container',
    'div[class*="user-query"]',
    '.query-text',
  ].join(', '),

  /**
   * Model (Gemini assistant) response containers.
   * Gemini custom element or class wrapper for model responses.
   */
  ASSISTANT_MESSAGE: [
    'model-response',
    '[data-message-author-role="assistant"]',
    '.model-response-container',
    'div[class*="model-response"]',
    'response-container',
    'div[class*="response-container"]',
    '.markdown',
  ].join(', '),

  /**
   * Code block containers.
   */
  CODE_BLOCK: 'pre',
  CODE_ELEMENT: 'pre code, code',

  /**
   * Interactive UI elements that must be stripped from extracted text.
   * Includes copy buttons, feedback buttons, export controls, and icons.
   */
  UI_CONTROLS_TO_EXCLUDE: [
    'button',
    'svg',
    'form',
    'nav',
    'aside',
    'mat-icon',
    '[role="toolbar"]',
    '[aria-label="Copy"]',
    '[aria-label="Copy code"]',
    '[aria-label="Good response"]',
    '[aria-label="Bad response"]',
    '.citation',
    '.sources-list',
    'response-feedback',
  ].join(', '),

  /**
   * Streaming / generation in-progress indicators.
   * When any of these are present, the response is considered incomplete.
   */
  STREAMING_INDICATORS: [
    'button[aria-label="Stop response"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop"]',
    'button[data-testid="stop-button"]',
    '.loading',
    '.animating',
    '.streaming',
    'mat-spinner',
    'sparkle-spinner',
    'div[class*="loading"]',
    'span.blinking-cursor',
  ].join(', '),

  /**
   * Stop button specific selectors.
   */
  STOP_BUTTON: [
    'button[aria-label="Stop response"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop"]',
    'button[data-testid="stop-button"]',
  ].join(', '),

  /**
   * Model selector trigger button.
   */
  MODEL_SELECTOR: [
    '[data-testid="model-selector"]',
    'button[aria-label*="Gemini"]',
    'div[class*="model-select"]',
  ].join(', '),

  /**
   * Original timestamp metadata selector if rendered.
   */
  TIMESTAMP: 'time[datetime], [data-timestamp]',
} as const
