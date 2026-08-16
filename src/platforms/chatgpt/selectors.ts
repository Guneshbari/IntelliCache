/**
 * Selectors and DOM attribute definitions for ChatGPT Data Collection.
 *
 * NOTE: Prefer semantic attributes (`data-message-author-role`, `data-testid`, `role`, `article`)
 * over volatile generated Tailwind CSS class names to maximize extraction longevity.
 */

export const CHATGPT_SELECTORS = {
  /**
   * Main conversation turn elements.
   * Matches specific conversation-turn articles or wrappers with data-testid.
   * Excludes bare `article` to prevent nested embedded/canvas articles from double-counting.
   */
  TURN_ARTICLE:
    'article[data-testid^="conversation-turn-"], div[data-testid^="conversation-turn-"]',

  /**
   * Role-based message identifiers.
   */
  USER_ROLE: '[data-message-author-role="user"]',
  ASSISTANT_ROLE: '[data-message-author-role="assistant"]',

  /**
   * User message text containers.
   * Prioritize semantic attributes and structural classes over generic styling utilities.
   */
  USER_TEXT: 'div[class*="text-message"], div[class*="content"], [data-message-author-role="user"]',

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
    'time',
    '[data-testid="copy-turn-action-button"]',
    '[data-testid="good-response-turn-action-button"]',
    '[data-testid="bad-response-turn-action-button"]',
    '[data-testid="voice-play-turn-action-button"]',
    '[data-testid="web-search-sources"]',
    '[data-testid="edit-message-button"]',
    '[role="toolbar"]',
    '.gizmo-shadow-stroke',
    'svg',
    'form',
    'nav',
    'aside',
  ].join(', '),

  /**
   * Streaming / generation in-progress indicators.
   * If any of these are present, the response is considered incomplete.
   */
  STREAMING_INDICATORS: [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    'button[aria-label="Stop"]',
    'button[data-testid="fruitjuice-stop-button"]',
    '.result-streaming',
    '.streaming',
    'span.streaming-cursor',
    '.result-thinking',
  ].join(', '),

  /**
   * Stop button specific selectors.
   */
  STOP_BUTTON: [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    'button[aria-label="Stop"]',
    'button[data-testid="fruitjuice-stop-button"]',
  ].join(', '),

  /**
   * Send button indicators confirming readiness.
   */
  SEND_BUTTON:
    'button[data-testid="send-button"], button[aria-label="Send prompt"], [data-testid="fruitjuice-send-button"]',

  /**
   * Model selector button / header dropdown.
   * NOTE: Removed broad `button[id^="radix-"]` to prevent selecting arbitrary Radix buttons.
   */
  MODEL_SWITCHER:
    'button[data-testid="model-switcher-dropdown-button"], [data-testid="model-selector-dropdown"], button[data-testid="model-switcher"]',

  /**
   * Original timestamp metadata selector if rendered by ChatGPT.
   */
  TIMESTAMP: 'time[datetime], [data-timestamp]',
} as const
