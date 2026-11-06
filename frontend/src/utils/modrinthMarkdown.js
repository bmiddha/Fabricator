import MarkdownIt from 'markdown-it'
import createDOMPurify from 'dompurify'

const markdown = new MarkdownIt({ html: false, linkify: true })
const allowedTags = [
  'p', 'br', 'hr', 'h1', 'h2', 'h3', 'h4', 'h5', 'h6', 'blockquote',
  'ul', 'ol', 'li', 'strong', 'em', 's', 'pre', 'code', 'a', 'img',
  'table', 'thead', 'tbody', 'tr', 'th', 'td',
]

export function safeProjectUrl(value, base = 'https://modrinth.com/') {
  if (typeof value !== 'string' || !value.trim()) return null
  try {
    const url = new URL(value, base)
    return ['https:', 'http:'].includes(url.protocol) && !url.username && !url.password ? url.href : null
  } catch {
    return null
  }
}

/** Raw HTML is displayed as text; generated markup receives a second, strict allowlist. */
export function createModrinthMarkdownRenderer(window) {
  const purify = createDOMPurify(window)
  purify.addHook('afterSanitizeAttributes', (node) => {
    if (node.tagName === 'A') {
      const href = safeProjectUrl(node.getAttribute('href'))
      if (href) {
        node.setAttribute('href', href)
        node.setAttribute('target', '_blank')
        node.setAttribute('rel', 'noopener noreferrer')
      } else {
        node.removeAttribute('href')
      }
    }
    if (node.tagName === 'IMG') {
      const src = safeProjectUrl(node.getAttribute('src'))
      if (src) node.setAttribute('src', src)
      else node.removeAttribute('src')
      node.setAttribute('loading', 'lazy')
      node.setAttribute('referrerpolicy', 'no-referrer')
    }
  })
  return (body) => purify.sanitize(markdown.render(typeof body === 'string' ? body : ''), {
    ALLOWED_TAGS: allowedTags,
    ALLOWED_ATTR: ['href', 'src', 'alt', 'title', 'start'],
    ALLOW_DATA_ATTR: false,
    ALLOW_ARIA_ATTR: false,
  })
}
