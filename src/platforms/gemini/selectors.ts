/**
 * Selectors and DOM attribute definitions for Gemini Data Collection.
 *
 * NOTE: Prefer semantic custom elements (`user-query`, `model-response`)
 * and structured content paths (`.query-content`, `message-content .markdown`)
 * over volatile Angular-generated CSS class names (_ngcontent-*, _nghost-*, ng-tns-*)
 * to maximize extraction longevity across gemini.google.com updates.
 */

export const GEMINI_SELECTORS = {
  /**
   * User message turn element.
   * Targets Gemini <user-query> custom element (with semantic fallback).
   */
  USER_MESSAGE: 'user-query, [data-message-author-role="user"]',

  /**
   * User query text content wrapper inside <user-query>.
   */
  USER_TEXT:
    '.query-content, [id^="user-query-content"], user-query-content, .user-query-container, .query-text',

  /**
   * Model (Gemini assistant) response turn element.
   * Targets Gemini <model-response> custom element (with semantic fallback).
   */
  ASSISTANT_MESSAGE: 'model-response, [data-message-author-role="assistant"]',

  /**
   * Assistant markdown text container within <model-response>.
   */
  ASSISTANT_TEXT: '.markdown, message-content, .response-container-content, response-container',

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
    'button[aria-label="Stop response generation"]',
    'button[data-testid="stop-button"]',
    'model-response.streaming',
    'model-response.loading',
    'model-response.animating',
    'mat-spinner',
    'sparkle-spinner',
    'span.blinking-cursor',
  ].join(', '),

  /**
   * Stop button specific selectors.
   */
  STOP_BUTTON: [
    'button[aria-label="Stop response"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop response generation"]',
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
