// Polyfill Happy-DOM Node.prototype.nodeName getter bug in test environments
if (typeof window !== 'undefined' && window.Node && window.Node.prototype) {
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
