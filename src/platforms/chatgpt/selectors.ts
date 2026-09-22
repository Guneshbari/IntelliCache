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
  TURN_ARTICLE: [
    'article[data-testid^="conversation-turn-"]',
    'div[data-testid^="conversation-turn-"]',
    'div[class*="group/conversation-turn"]',
    '[class*="group/conversation-turn"]',
    '[data-testid^="conversation-turn-"]',
  ].join(', '),

  /**
   * Role-based message identifiers.
   */
  USER_ROLE:
    '[data-message-author-role="user"], [data-testid*="user-message"], [data-testid*="user_message"]',
  ASSISTANT_ROLE:
    '[data-message-author-role="assistant"], [data-testid*="assistant-message"], [data-testid*="assistant_message"]',

  /**
   * Assistant turn Copy action button anchor.
   * Scoped to the completed response action bar in guest and authenticated modes.
   */
  ASSISTANT_COPY_ANCHOR: [
    'button[aria-label="Copy response"]',
    'button[aria-label="Copy"]',
    'button[aria-label="Copy to clipboard"]',
    'button[data-testid="copy-turn-action-button"]',
    '[data-testid="copy-turn-action-button"]',
  ].join(', '),

  /**
   * Guest / unauthenticated session indicators.
   * Buttons, banners, or links present when user is chatting as a guest without login.
   */
  GUEST_INDICATORS: [
    'button[data-testid="login-button"]',
    'button[data-testid="signup-button"]',
    'a[href*="/auth/login"]',
    'a[href*="login"]',
    'a[href*="signup"]',
    '[data-testid="logged-out-banner"]',
    '[data-testid="stay-logged-out-button"]',
    'button[data-testid="welcome-login-button"]',
    '[data-testid="login-banner"]',
    '[data-testid="unauth-banner"]',
  ].join(', '),

  /**
   * Authenticated profile indicators (to ensure we don't misclassify logged-in users).
   * Note: 'button[aria-label*="Open account menu"]' alone is not included here because real
   * guest ChatGPT DOM also exposes an account menu button for logged-out users to log in or sign up.
   * Dynamic account menu checks are handled in isChatGPTGuestSession().
   */
  LOGGED_IN_INDICATORS: [
    '[data-testid="profile-button"]',
    '[data-testid="accounts-profile-button"]',
    'button[aria-label*="User profile"]',
    '.avatar-user',
    '[data-testid="user-avatar"]',
  ].join(', '),

  /**
   * User message text containers.
   * Prioritizes the message text element (.whitespace-pre-wrap) over outer wrappers.
   * Note: div[class*="content"] removed to avoid matching generic parent layout wrappers.
   */
  USER_TEXT:
    '.whitespace-pre-wrap, [class*="whitespace-pre-wrap"], div[class*="text-message"], [data-message-author-role="user"]',

  /**
   * Assistant response text and markdown containers.
   */
  ASSISTANT_TEXT: '[data-assistant-markdown], .markdown, .prose, div[class*="markdown"]',

  /**
   * Code block containers.
   */
  CODE_BLOCK: 'pre',

  /**
   * Interactive UI elements that must be stripped from response and query text.
   * Includes screen-reader labels (.sr-only), buttons, edit triggers, and toolbars.
   */
  UI_CONTROLS_TO_EXCLUDE: [
    'button',
    '[role="button"]',
    'time',
    '.sr-only',
    '[class*="sr-only"]',
    '[aria-hidden="true"]',
    '[data-testid="copy-turn-action-button"]',
    '[data-testid="good-response-turn-action-button"]',
    '[data-testid="bad-response-turn-action-button"]',
    '[data-testid="voice-play-turn-action-button"]',
    '[data-testid="web-search-sources"]',
    '[data-testid="edit-message-button"]',
    '[role="toolbar"]',
    '[role="dialog"]',
    '[role="tooltip"]',
    '.gizmo-shadow-stroke',
    'svg',
    'form',
    'nav',
    'aside',
  ].join(', '),

  /**
   * Streaming / generation in-progress class indicators.
   * NOTE: Does NOT include persistent thought containers like .result-thinking.
   */
  STREAMING_INDICATORS: '.result-streaming, .streaming, span.streaming-cursor',

  /**
   * Stop button specific selectors.
   * Only targets generation stop buttons to avoid matching audio/TTS or voice stop buttons.
   */
  STOP_BUTTON: [
    'button[data-testid="stop-button"]',
    'button[aria-label="Stop generating"]',
    'button[aria-label="Stop streaming"]',
    'button[data-testid="fruitjuice-stop-button"]',
  ].join(', '),

  /**
   * Model selector button / header dropdown.
   * NOTE: Removed broad `button[id^="radix-"]` to prevent selecting arbitrary Radix buttons.
   */
  MODEL_SWITCHER:
    'button[data-testid="model-switcher-dropdown-button"], [data-testid="model-selector-dropdown"], button[data-testid="model-switcher"]',
} as const
