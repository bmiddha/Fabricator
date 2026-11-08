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
  assert.match(document.body.textContent, /not confirmed delivered/)
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
  assert.match(document.body.textContent, /do not have permission to read/)
})

test('stopped, unsupported and unattributed chat disable sending honestly', async () => {
  globalThis.fetch = async () => response({ items: [], ready: false, canSend: false, sender: null })
  mount()
  await settle()
  assert(document.querySelector('input').disabled)
  assert.match(document.body.textContent, /stopped or still starting/)
  assert.match(document.body.textContent, /authentication is disabled/)
})

test('unsupported chat explains parsing limits', async () => {
  globalThis.fetch = async () => response({ items: [], supported: false, ready: false, canSend: false })
  mount()
  await settle()
  assert.match(document.body.textContent, /custom\/plugin formats are not parsed/)
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
  assert.deepEqual([...document.querySelectorAll('.server-chat__message > p')].map(p => p.textContent),
    ['first saved', 'second saved', 'third saved', 'fourth saved'])
  assert.equal(document.querySelector('.server-chat__transcript button'), null)
  assert.match(document.body.textContent, /saved across restarts for up to 7 days/)
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
