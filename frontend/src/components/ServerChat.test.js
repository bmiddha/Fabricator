import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'sessionStorage']) {
  globalThis[key] = dom.window[key]
}
const vite = await createServer({
  configFile: false, root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'chat-dom-components',
    async load(id) {
      if (!id.endsWith('.vue')) return
      const { descriptor } = parse(await readFile(id, 'utf8'))
      const script = compileScript(descriptor, { id, genDefaultAs: '__component' })
      const template = compileTemplate({
        id, filename: id, source: descriptor.template.content,
        compilerOptions: { bindingMetadata: script.bindings },
      }).code.replace('export function render', 'function render')
      return `${script.content}\n${template}\n__component.render = render\nexport default __component`
    },
  }],
})
const { createApp, h, nextTick, ref } = await import('vue')
const { createPinia } = await import('pinia')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Chat = (await vite.ssrLoadModule('/src/components/ServerChat.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  globalThis.fetch = originalFetch
})
async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
function mount(permissions = ['chat.read', 'chat.send']) {
  const props = ref({ serverId: 'a' })
  const app = createApp({ render: () => h(Chat, props.value) })
  app.use(createPinia())
  app.use(i18n)
  let auth
  app.runWithContext(() => {
    auth = useAuthStore()
    auth.oidcMode = true
    auth.user = { permissions: ['server.view', ...permissions] }
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  return { props, auth }
}
const item = { id: 'first', kind: 'player', sender: '<script>Alex</script>',
  message: '<img src=x onerror=alert(1)>', ts: '2026-11-08T00:00:00Z' }
function response(overrides = {}) {
  return Response.json({ items: [item], supported: true, ready: true, running: true,
    sender: 'Alice', canSend: true, ...overrides })
}
function submit(message = 'Hello players') {
  const input = document.querySelector('input')
  input.value = message
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
}

test('chat reads only its endpoint, escapes content and sends no identity override', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options])
    return options.method === 'POST'
      ? Response.json({ item: { ...item, id: 'sent', kind: 'fabricator', sender: 'Alice', message: 'Hello players', status: 'submitted' } }, { status: 202 })
      : response()
  }
  mount()
  await settle()
  assert.equal(calls[0][0], '/api/servers/a/chat')
  assert.equal(document.querySelector('.server-chat__message img'), null)
  assert.match(document.body.textContent, /<script>Alex<\/script>/)
  submit()
  await settle()
  assert.deepEqual(JSON.parse(calls[1][1].body), { message: 'Hello players' })
  assert.equal(document.querySelector('input').value, '')
  assert.match(document.body.textContent, /Fabricator · Alice/)
  assert.equal(document.querySelector('.server-chat__identity').textContent.trim(), 'portal - Alice')
})

test('read-only and console-only users cannot send or read through console grants', async () => {
  globalThis.fetch = async () => response({ canSend: false })
  mount(['chat.read'])
  await settle()
  assert.equal(document.querySelector('form'), null)
  assert.match(document.body.textContent, /Read-only chat/)
})

test('no chat read permission does not fetch or expose a composer', async () => {
  globalThis.fetch = () => { throw new Error('No request permitted') }
  mount(['server.console.execute', 'chat.send'])
  await settle()
  assert.equal(document.querySelector('form'), null)
  assert.match(document.body.textContent, /do not have access/)
})

test('stopped, unsupported and unattributed chat disable sending honestly', async () => {
  globalThis.fetch = async () => response({ items: [], ready: false, canSend: false, sender: null })
  mount()
  await settle()
  assert(document.querySelector('input').disabled)
  assert.match(document.body.textContent, /Server is not ready/)
  assert.equal(document.querySelector('.server-chat__identity').textContent.trim(), 'portal')
})

test('unsupported chat remains explicit without instructional boilerplate', async () => {
  globalThis.fetch = async () => response({ items: [], supported: false, ready: false, canSend: false })
  mount()
  await settle()
  assert.match(document.body.textContent, /Chat is not supported for this server/)
  assert(document.querySelector('button').disabled)
})

test('send validation, rate limits and failed submission preserve the draft', async () => {
  let posts = 0
  globalThis.fetch = async (url, options) => {
    if (options.method === 'POST') {
      posts++
      return Response.json({ error: 'Chat rate limit reached', retryAfter: 60 }, { status: 429 })
    }
    return response()
  }
  mount()
  await settle()
  submit('hello\u202estop')
  await settle()
  assert.equal(posts, 0)
  assert.match(document.querySelector('[role=alert]').textContent, /without controls/)
  submit('retry later')
  await settle()
  assert.equal(posts, 1)
  assert.equal(document.querySelector('input').value, 'retry later')
  assert(document.querySelector('button').disabled)
  assert.match(document.querySelector('[role=alert]').textContent, /rate limit reached/)
})

test('switches drop retained messages and ignore late responses from another server', async () => {
  let resolveFirst
  globalThis.fetch = async url => url.includes('/a/')
    ? new Promise(resolve => { resolveFirst = resolve })
    : response({ items: [{ ...item, id: 'b', sender: 'Bob', message: 'Server B only' }] })
  const { props } = mount()
  await settle()
  props.value.serverId = 'b'
  await settle()
  resolveFirst(response({ items: [{ ...item, message: 'private old response' }] }))
  await settle()
  assert.match(document.body.textContent, /Server B only/)
  assert.doesNotMatch(document.body.textContent, /private old response/)
})

test('auth revision and permission revocation clear retained chat immediately', async () => {
  globalThis.fetch = async () => response()
  const { auth } = mount()
  await settle()
  assert.match(document.body.textContent, /<img src=x/)
  auth.user = { permissions: [] }
  auth.revision++
  await settle()
  assert.doesNotMatch(document.body.textContent, /<img src=x/)
  assert.equal(document.querySelector('input'), null)
})

test('failed reads are explicit and no stale transcript remains', async () => {
  globalThis.fetch = async () => Response.json({ error: 'permission revoked' }, { status: 403 })
  mount()
  await settle()
  assert.equal(document.querySelector('[role=log]'), null)
  assert.match(document.querySelector('[role=alert]').textContent, /permission revoked/)
  assert(document.querySelector('input').disabled)
})

test('saved history pages are prepended in capture order without losing recent messages', async () => {
  const calls = []
  globalThis.fetch = async url => {
    calls.push(url)
    if (url.includes('before=')) return response({
      items: [{ ...item, id: 1, message: 'first saved' }, { ...item, id: 2, message: 'second saved' }],
      nextBefore: null,
    })
    return response({ items: [{ ...item, id: 3, message: 'third saved' },
      { ...item, id: 4, message: 'fourth saved' }], nextBefore: 3, latestId: 4 })
  }
  mount()
  await settle()
  document.querySelector('.server-chat__transcript button').click()
  await settle()
  assert.match(calls[1], /before=3&limit=200/)
  assert.deepEqual([...document.querySelectorAll('.server-chat__bubble')].map(p => p.textContent),
    ['first saved', 'second saved', 'third saved', 'fourth saved'])
  assert.equal(document.querySelector('.server-chat__transcript button'), null)
  assert.doesNotMatch(document.body.textContent, /7 days|10,000|not confirmed delivered/)
})

test('delta polling updates persisted outcomes and does not duplicate replayed items', async () => {
  const originalTimeout = globalThis.setTimeout
  let poll
  globalThis.setTimeout = (fn, delay, ...args) => {
    if (delay === 2500) { poll = fn; return -1 }
    return originalTimeout(fn, delay, ...args)
  }
  try {
    const calls = []
    globalThis.fetch = async url => {
      calls.push(url)
      if (url.includes('after=')) return response({
        items: [{ ...item, id: 2, message: 'new saved' }],
        outcomes: [{ id: 1, status: 'submitted' }], latestId: 2,
      })

      return response({ items: [{ ...item, kind: 'fabricator', sender: 'Alice',
        id: 1, status: 'pending', message: 'original send' }], latestId: 1 })
    }
    mount()
    await settle()
    assert.match(document.body.textContent, /Submission outcome unknown/)
    await poll()
    await settle()
    assert.match(calls[1], /after=1&limit=200/)
    assert.doesNotMatch(document.body.textContent, /Submission outcome unknown/)
    assert.equal(document.querySelectorAll('.server-chat__message').length, 2)
    assert.match(document.body.textContent, /Submitted/)
  } finally {
    globalThis.setTimeout = originalTimeout
  }
})

test('player heads reuse the existing avatar with fixed size, privacy and failure fallback', async () => {
  globalThis.fetch = async () => response({ items: [{ ...item, id: 1, sender: 'Alex' }] })
  mount()
  await settle()
  const head = document.querySelector('.server-chat__message .player-avatar')
  const img = head.querySelector('img')
  assert.equal(img.getAttribute('src'), 'https://mc-heads.net/avatar/Alex/32')
  assert.equal(img.getAttribute('referrerpolicy'), 'no-referrer')
  assert.equal(img.getAttribute('loading'), 'lazy')
  assert.equal(head.style.width, '32px')
  assert.equal(head.style.height, '32px')
  img.dispatchEvent(new dom.window.Event('error'))
  await settle()
  assert.equal(head.querySelector('img'), null)
  assert.equal(head.textContent, 'A')
  assert.equal(head.style.width, '32px')
})

test('consecutive messages group by actual sender and source without assigning panel users Minecraft heads', async () => {
  const ts = '2026-11-08T00:00:00Z'
  globalThis.fetch = async () => response({ items: [
    { ...item, id: 1, sender: 'Alex', message: 'first', ts },
    { ...item, id: 2, sender: 'Alex', message: 'second', ts },
    { ...item, id: 3, sender: 'Bob', message: 'third', ts },
    { ...item, id: 4, kind: 'fabricator', sender: 'Alice', message: 'from the portal', ts, status: 'submitted' },
    { ...item, id: 5, kind: 'player', sender: 'Alice', message: 'same name, different source', ts },
    { ...item, id: 6, kind: 'player', sender: 'Alice', message: 'five minutes later', ts: '2026-11-08T00:05:00Z' },
    { ...item, id: 7, kind: 'player', sender: 'Alice', message: 'older timestamp', ts },
  ] })
  mount()
  await settle()
  const rows = [...document.querySelectorAll('.server-chat__message')]
  assert(rows[1].classList.contains('is-continued'))
  assert.equal(rows[1].querySelector('img'), null)
  assert(rows[1].querySelector('.server-chat__author').classList.contains('server-chat__sr-only'))
  assert(!rows[2].classList.contains('is-continued'))
  assert(rows[3].classList.contains('is-own'))
  assert.equal(rows[3].querySelector('img'), null)
  assert.equal(rows[3].querySelector('.server-chat__portal-avatar').textContent, 'F')
  assert(!rows[4].classList.contains('is-continued'))
  assert(!rows[4].classList.contains('is-own'))
  assert(!rows[5].classList.contains('is-continued'))
  assert(!rows[6].classList.contains('is-continued'))
})

test('send icon stays inside the message box and sending preserves the viewport and composer nodes', async () => {
  let finish
  globalThis.fetch = async (url, options) => options.method === 'POST'
    ? new Promise(resolve => { finish = resolve })
    : response()
  mount()
  await settle()
  const viewport = document.querySelector('.server-chat__viewport')
  const form = document.querySelector('form')
  const input = document.querySelector('input')
  const button = document.querySelector('.server-chat__send')
  assert.equal(button.getAttribute('aria-label'), 'Send message')
  assert.equal(button.parentElement, input.parentElement)
  assert(button.querySelector('svg'))
  assert.doesNotMatch(document.body.textContent, /Sending as|7 days|10,000|not confirmed delivered/)
  submit('Message from the portal')
  await settle()
  assert.equal(button.getAttribute('aria-busy'), 'true')
  assert(button.querySelector('.server-chat__send-spinner'))
  assert.equal(document.querySelector('.server-chat__viewport'), viewport)
  assert.equal(document.querySelector('form'), form)
  finish(Response.json({ item: { ...item, id: 'sent', kind: 'fabricator', sender: 'Alice',
    message: 'Message from the portal', status: 'submitted' } }, { status: 202 }))
  await settle()
  assert.equal(document.querySelector('.server-chat__viewport'), viewport)
  assert.equal(document.querySelector('form'), form)
  assert.equal(document.querySelector('.server-chat__send'), button)
  assert.equal(button.getAttribute('aria-busy'), 'false')
})

test('group timestamps survive minute rollover and split at time, sender, source, event and local date boundaries', async () => {
  const stamp = (hour, minute, second, day = 7) => new Date(2026, 10, day, hour, minute, second).toISOString()
  const entries = [
    { sender: 'Alex', ts: stamp(12, 34, 50) },
    { sender: 'Alex', ts: stamp(12, 35, 10) },
    { sender: 'Alex', ts: stamp(12, 40, 9) },
    { sender: 'Alex', ts: stamp(12, 45, 9) },
    { sender: 'Bob', ts: stamp(12, 45, 10) },
    { sender: 'Bob', kind: 'fabricator', ts: stamp(12, 45, 11) },
    { sender: 'Bob', kind: 'system', ts: stamp(12, 45, 12) },
    { sender: 'Bob', kind: 'fabricator', ts: stamp(12, 45, 13) },
    { sender: 'Alex', ts: stamp(23, 59, 59) },
    { sender: 'Alex', ts: stamp(0, 0, 1, 8) },
  ]
  globalThis.fetch = async () => response({
    items: entries.map((entry, index) => ({ ...item, id: index + 1, message: `message ${index}`, ...entry })),
  })
  mount()
  await settle()
  const rows = [...document.querySelectorAll('.server-chat__message')]
  assert.deepEqual(rows.map(row => row.classList.contains('is-continued')),
    [false, true, true, false, false, false, false, false, false, false])
  assert.deepEqual(rows.map(row => Boolean(row.querySelector('.server-chat__meta > time'))),
    [false, false, true, true, true, true, true, true, true, true])
  assert.equal(rows[2].querySelector('.server-chat__meta > time').getAttribute('datetime'), entries[2].ts)
})

test('each grouped bubble exposes its exact time through native focusable and tappable disclosure', async () => {
  const ts = '2026-11-08T00:35:07.321Z'
  globalThis.fetch = async () => response({ items: [
    { ...item, id: 1, sender: 'Alex', message: 'first', ts },
    { ...item, id: 2, sender: 'Alex', message: 'second', ts },
  ] })
  mount()
  await settle()
  assert.equal(document.querySelectorAll('.server-chat__meta > time').length, 1)
  for (const details of document.querySelectorAll('.server-chat__message-details')) {
    const summary = details.querySelector('summary')
    const exact = details.querySelector('.server-chat__exact-time')
    assert.equal(details.open, false)
    assert.equal(summary.getAttribute('tabindex'), '0')
    assert.equal(summary.getAttribute('aria-describedby'), exact.id)
    assert.match(summary.title, /321/)
    assert.equal(exact.getAttribute('datetime'), ts)
    assert.equal(exact.textContent, ts)
    summary.focus()
    assert.equal(document.activeElement, summary)
    summary.click()
    await settle()
    assert.equal(details.open, true)
    summary.click()
    await settle()
    assert.equal(details.open, false)
  }
})

test('collapsing timestamps never hides individual send failures or uncertain outcomes', async () => {
  globalThis.fetch = async () => response({ items: [
    { ...item, id: 1, kind: 'fabricator', sender: 'Alice', status: 'failed' },
    { ...item, id: 2, kind: 'fabricator', sender: 'Alice', status: 'pending' },
    { ...item, id: 3, kind: 'fabricator', sender: 'Alice', status: 'submitted' },
  ] })
  mount()
  await settle()
  assert.equal(document.querySelectorAll('.server-chat__meta > time').length, 1)
  const outcomes = [...document.querySelectorAll('.server-chat__meta > span')].map(el => el.textContent)
  assert.deepEqual(outcomes, ['Submission failed; delivery uncertain', 'Submission outcome unknown', 'Submitted'])
})
