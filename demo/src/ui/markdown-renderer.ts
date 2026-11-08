/**
 * Safe Markdown Parser and Code Renderer for the IntelliCache Demonstration Chatbot.
 *
 * Combines:
 * - Marked for robust CommonMark and GFM parsing (headings, lists, tables, code blocks).
 * - Highlight.js for syntax highlighting of programming languages.
 * - DOMPurify for strict HTML sanitization and XSS prevention.
 *
 * Security guarantees:
 * - Model-generated raw HTML is neutralized and escaped.
 * - Insecure URI schemes (javascript:, vbscript:, data:) are rejected.
 * - All external links open with target="_blank" and rel="noopener noreferrer".
 * - User messages are strictly escaped as plain text without passing through Markdown.
 */

import DOMPurify from 'dompurify'
import hljs from 'highlight.js'
import { marked } from 'marked'

// Polyfill Happy-DOM Node.prototype.nodeName getter bug in test environments
if (typeof window !== 'undefined' && window.Node && window.Node.prototype) {
  const nodeNameDesc = Object.getOwnPropertyDescriptor(window.Node.prototype, 'nodeName')
  const testEl = window.document?.createElement?.('div')
  if (testEl && nodeNameDesc?.get && nodeNameDesc.get.call(testEl) === '') {
    Object.defineProperty(window.Node.prototype, 'nodeName', {
      get(this: Node) {
        return (
          (this as Element).tagName ||
          (this.nodeType === 3 ? '#text' : this.nodeType === 8 ? '#comment' : '')
        )
      },
      configurable: true,
    })
  }
}

export class MarkdownRenderer {
  private static initialized: boolean = false

  public static init(): void {
    if (this.initialized) return

    marked.use({
      gfm: true,
      breaks: true,
      renderer: {
        link({ href, title, text }) {
          const cleanHref = (href || '').trim()
          // Reject dangerous protocols
          if (/^(?:javascript|vbscript|data):/i.test(cleanHref)) {
            return text
          }
          const titleAttr = title ? ` title="${MarkdownRenderer.escapeHtml(title)}"` : ''
          return `<a href="${cleanHref}" target="_blank" rel="noopener noreferrer"${titleAttr}>${text}</a>`
        },
        code({ text, lang }) {
          const rawLang = (lang || '').trim().toLowerCase()
          const validLang = rawLang && hljs.getLanguage(rawLang) ? rawLang : ''
          let highlighted: string

          if (validLang) {
            try {
              highlighted = hljs.highlight(text, {
                language: validLang,
                ignoreIllegals: true,
              }).value
            } catch {
              highlighted = MarkdownRenderer.escapeHtml(text)
            }
          } else {
            highlighted = MarkdownRenderer.escapeHtml(text)
          }

          const displayLang = validLang || rawLang || 'plaintext'
          return `<div class="code-block-wrapper"><div class="code-block-header"><span class="code-lang">${MarkdownRenderer.escapeHtml(displayLang)}</span><button class="btn-copy-code" type="button" aria-label="Copy code to clipboard">Copy</button></div><pre><code class="hljs ${validLang ? `language-${validLang}` : ''}">${highlighted}</code></pre></div>`
        },
      },
    })

    this.initialized = true
  }

  /**
   * Parses markdown and returns sanitized HTML safe for insertion into the DOM.
   */
  public static renderMarkdown(content: string): string {
    this.init()
    if (!content) return ''

    // Pre-strip script and dangerous executable embed tags to neutralize execution before DOM parsing
    const preCleaned = content
      .replace(/<\s*script\b[^>]*>[\s\S]*?<\s*\/\s*script\s*>/gi, '')
      .replace(/<\s*script\b[^>]*>/gi, '')
      .replace(/<\s*iframe\b[^>]*>[\s\S]*?<\s*\/\s*iframe\s*>/gi, '')
      .replace(/<\s*iframe\b[^>]*>/gi, '')

    const rawParsed = marked.parse(preCleaned) as string

    const purify =
      typeof DOMPurify === 'function' && !('sanitize' in DOMPurify)
        ? (DOMPurify as unknown as (win?: Window) => typeof DOMPurify)(
            typeof window !== 'undefined' ? window : undefined
          )
        : DOMPurify

    const sanitized = purify.sanitize(rawParsed, {
      ALLOWED_TAGS: [
        'h1',
        'h2',
        'h3',
        'h4',
        'h5',
        'h6',
        'p',
        'pre',
        'code',
        'div',
        'span',
        'button',
        'table',
        'thead',
        'tbody',
        'tr',
        'th',
        'td',
        'ul',
        'ol',
        'li',
        'blockquote',
        'hr',
        'a',
        'strong',
        'em',
        'del',
        'b',
        'i',
        'br',
        'sub',
        'sup',
      ],
      ALLOWED_ATTR: ['class', 'href', 'title', 'target', 'rel', 'type', 'aria-label', 'align'],
      ALLOWED_URI_REGEXP: /^(?:(?:https?|mailto):|[^a-z]|[a-z+.-]+(?:[^a-z+.:-]|$))/i,
      FORBID_TAGS: ['script', 'iframe', 'object', 'embed', 'form', 'style'],
      FORBID_ATTR: ['onerror', 'onload', 'onclick', 'onmouseover', 'onfocus', 'onblur'],
    })

    return sanitized
  }

  /**
   * Escapes plain text for safe insertion into HTML without formatting.
   * Used for user messages and literal labels.
   */
  public static escapeHtml(str: string): string {
    return (str || '')
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#39;')
  }
}
