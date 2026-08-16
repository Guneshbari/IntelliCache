/**
 * Selectors and DOM attribute definitions for ChatGPT Data Collection.
 *
 * NOTE: Prefer semantic attributes (`data-message-author-role`, `data-testid`, `role`, `article`)
 * over volatile generated Tailwind CSS class names to maximize extraction longevity.
 */

export const CHATGPT_SELECTORS = {
  /**
   * Main conversation turn elements.
   * Matches articles or conversation-turn wrappers.
   */
  TURN_ARTICLE:
    'article[data-testid^="conversation-turn-"], article, div[data-testid^="conversation-turn-"]',

  /**
   * Role-based message identifiers.
   */
  USER_ROLE: '[data-message-author-role="user"]',
  ASSISTANT_ROLE: '[data-message-author-role="assistant"]',

  /**
   * User message text containers.
   */
  USER_TEXT: '.whitespace-pre-wrap, div[class*="text-message"], [data-message-author-role="user"]',

  /**
   * Assistant response text and markdown containers.
   */
  ASSISTANT_TEXT: '.markdown, .prose, div[class*="markdown"]',

  /**
   * Code block containers and code elements.
   */
  CODE_BLOCK: 'pre',
  CODE_ELEMENT: 'pre code, code',
  CODE_HEADER: '.flex.items-center.text-xs, div[class*="bg-token-main-surface-tertiary"]',

  /**
   * Interactive UI elements that must be stripped from response text.
   */
  UI_CONTROLS_TO_EXCLUDE: [
    'button',
    '[data-testid="copy-turn-action-button"]',
    '[data-testid="good-response-turn-action-button"]',
    '[data-testid="bad-response-turn-action-button"]',
    '[data-testid="voice-play-turn-action-button"]',
    '[data-testid="web-search-sources"]',
    '.gizmo-shadow-stroke',
    'svg',
    'form',
  ].join(', '),

  /**
   * Streaming / generation in-progress indicators.
   * If any of these are present, the response is considered incomplete.
   */
  STREAMING_INDICATORS: [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    '.result-streaming',
    '.streaming',
    'span.streaming-cursor',
  ].join(', '),

  /**
   * Send button indicators confirming readiness.
   */
  SEND_BUTTON:
    'button[data-testid="send-button"], button[aria-label="Send prompt"], [data-testid="fruitjuice-send-button"]',

  /**
   * Model selector button / header dropdown.
   */
  MODEL_SWITCHER:
    'button[data-testid="model-switcher-dropdown-button"], [data-testid="model-selector-dropdown"], button[id^="radix-"]',
} as const
