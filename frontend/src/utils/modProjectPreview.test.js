import assert from 'node:assert/strict'
import test from 'node:test'
import { JSDOM } from 'jsdom'
import { createProjectDetailsCache, installedProjectId } from './modProjectPreview.js'
import { createModrinthMarkdownRenderer, safeProjectUrl } from './modrinthMarkdown.js'

test('installed previews trust manifests then hash matches, including disabled files, never filenames', () => {
  assert.equal(installedProjectId({ filename: 'sodium.jar', slug: 'sodium', id: 'random' }), null)
  assert.equal(installedProjectId(null), null)
  assert.equal(installedProjectId({ modrinth: {}, modrinthGuess: { projectId: 'hash' } }), 'hash')
  assert.equal(installedProjectId({ modrinth: { projectId: 'record' }, modrinthGuess: { projectId: 'hash' } }), 'record')
  assert.equal(installedProjectId({ disabled: true, filename: 'renamed.jar.disabled', modrinthGuess: { projectId: 'hash' } }), 'hash')
  assert.equal(installedProjectId({ modrinth: { slug: 'legacy-slug' } }), 'legacy-slug')
})

test('details cache is lazy, reuses successful details, expires and stays bounded', async () => {
  let time = 0
  const calls = []
  const load = createProjectDetailsCache(async (id) => {
    calls.push(id)
    return { id, body: '# Description' }
  }, { now: () => time, ttl: 100, limit: 2 })
  assert.deepEqual(calls, [])
  await load('a')
  await load('a')
  assert.deepEqual(calls, ['a'])
  time = 100
  await load('a')
  await load('b')
  await load('c')
  await load('a')
  assert.deepEqual(calls, ['a', 'a', 'b', 'c', 'a'])
})

test('failed and aborted requests do not poison retries or cache', async () => {
  let count = 0
  const load = createProjectDetailsCache(async () => {
    if (++count === 1) throw new Error('Rate limited')
    return { id: 'a' }
  })
  await assert.rejects(load('a'), /Rate limited/)
  const controller = new AbortController()
  controller.abort()
  await load('a', { signal: controller.signal })
  await load('a')
  assert.equal(count, 3)
})

const window = new JSDOM('').window
const render = createModrinthMarkdownRenderer(window)
function documentFor(markdown) {
  return new JSDOM(render(markdown)).window.document
}

test('Markdown supports readable headings, lists, code, tables, links and responsive images', () => {
  const doc = documentFor('# Sodium\n\n**Fast** rendering.\n\n- Item\n\n```js\nalert("text only")\n```\n\n| A | B |\n| - | - |\n| x | y |\n\n[Wiki](/mod/sodium)\n\n![Example](https://cdn.modrinth.com/example.png)')
  for (const tag of ['h1', 'strong', 'ul', 'pre code', 'table', 'a', 'img']) assert.ok(doc.querySelector(tag), tag)
  assert.equal(doc.querySelector('a').href, 'https://modrinth.com/mod/sodium')
  assert.equal(doc.querySelector('a').rel, 'noopener noreferrer')
  assert.equal(doc.querySelector('a').target, '_blank')
  assert.equal(doc.querySelector('img').getAttribute('loading'), 'lazy')
  assert.equal(doc.querySelector('img').getAttribute('referrerpolicy'), 'no-referrer')
})

test('remote HTML, scripts, events, iframes, SVG, styles and scriptable URLs cannot execute', () => {
  const doc = documentFor(`
<script>alert(1)</script><iframe src="https://evil.example"></iframe>
<img src=x onerror=alert(1)><svg onload=alert(1)></svg>
<a href="javascript:alert(1)" style="color:red">raw</a>
[bad](javascript:alert%281%29)
[bad](vbscript:alert%281%29)
![bad](data:image/svg+xml;base64,abc)
[encoded](jav&#x61;script:alert%281%29)
[link](https://example.com "onmouseover=alert(1)")
`)
  assert.equal(doc.querySelector('script, iframe, svg, style'), null)
  for (const element of doc.querySelectorAll('*')) {
    for (const attr of element.attributes) {
      assert.ok(!/^on|^style$|^id$/i.test(attr.name))
      if (['href', 'src'].includes(attr.name)) assert.match(attr.value, /^https?:\/\//)
    }
  }
  assert.ok(doc.body.textContent.includes('<script>'))
})

test('URL policy resolves Modrinth-relative content without accepting script, data or credentials', () => {
  assert.equal(safeProjectUrl('/data/image.png'), 'https://modrinth.com/data/image.png')
  assert.equal(safeProjectUrl('//cdn.modrinth.com/image.png'), 'https://cdn.modrinth.com/image.png')
  for (const value of ['javascript:alert(1)', 'java\nscript:alert(1)', 'data:text/html,test', 'file:///etc/passwd', 'https://user:pass@example.com', '', null]) {
    assert.equal(safeProjectUrl(value), null, String(value))
  }
  assert.equal(render(null), '')
})
