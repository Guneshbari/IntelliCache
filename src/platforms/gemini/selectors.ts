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
   * Targets Gemini <user-query> custom element (with semantic and guest mode fallbacks).
   */
  USER_MESSAGE:
    'user-query, [data-message-author-role="user"], .user-query, .user-query-container, [data-query-id], [data-testid*="user-query"]',

  /**
   * User query text content wrapper inside <user-query>.
   */
  USER_TEXT:
    '.query-content, [id^="user-query-content"], user-query-content, .user-query-container, .query-text, p',

  /**
   * Model (Gemini assistant) response turn element.
   * Targets Gemini <model-response> custom element (with semantic and guest mode fallbacks).
   */
  ASSISTANT_MESSAGE:
    'model-response, [data-message-author-role="assistant"], response-container, .model-response, .response-container, [data-response-id], [data-testid*="model-response"], message-content',

  /**
   * Assistant markdown text container within <model-response>.
   */
  ASSISTANT_TEXT:
    '.markdown, message-content, .response-container-content, response-container, .model-response-text',

  /**
   * Guest / unauthenticated session indicators.
   * Elements present when user is using Gemini without logging into a Google account.
   */
  GUEST_INDICATORS: [
    'a[href*="accounts.google.com/ServiceLogin"]',
    'a[href*="accounts.google.com/AccountChooser"]',
    'a[aria-label*="Sign in"]',
    'button[aria-label*="Sign in"]',
    '[data-testid*="sign-in"]',
    '[data-testid*="signin"]',
  ].join(', '),

  /**
   * Logged-in Google account indicators.
   */
  LOGGED_IN_INDICATORS: [
    'a[href*="myaccount.google.com"]',
    '[aria-label*="Google Account:"]',
    'a[aria-label*="Google Account"]',
  ].join(', '),

  /**
   * Code block containers.
   */
  CODE_BLOCK: 'pre',

  /**
   * Interactive UI elements that must be stripped from extracted text.
   * Includes copy buttons, feedback buttons, export controls, and icons.
   */
  UI_CONTROLS_TO_EXCLUDE: [
    'button',
    '[role="button"]',
    '.sr-only',
    '[class*="sr-only"]',
    '[aria-hidden="true"]',
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
   *
   * NOTE: spinner entries (`mat-spinner`, `sparkle-spinner`) are intentionally
   * retained alongside the stop-button selectors as a conservative fallback —
   * a false positive only delays capture (with exponential backoff in
   * BaseAdapter), while a false negative would persist a truncated response.
   */
  STREAMING_INDICATORS: [
    'button[aria-label="Stop response"]',
    'button[aria-label="Stop Response"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop response generation"]',
    'button[aria-label="Stop"]',
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
    'button[aria-label="Stop Response"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop response generation"]',
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
} as const
