import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'sessionStorage', 'history', 'location']) {
  globalThis[key] = dom.window[key]
}
const vite = await createServer({
  configFile: false, root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'directory-dom-components',
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
const { createApp, h, nextTick } = await import('vue')
const { createPinia } = await import('pinia')
const { createRouter, createMemoryHistory } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Directory = (await vite.ssrLoadModule('/src/views/PlayersDirectory.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  globalThis.fetch = originalFetch
})
async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
const uid = '12345678-1234-4234-8234-123456789abc'
const player = {
  id: `minecraft:${uid}`, uuid: uid, namespace: 'minecraft', name: 'Current',
  names: ['Former', 'Current'], lastSeen: null,
  servers: [{ id: 'a', name: 'Survival', joined: null, whitelisted: true, online: null, lastSeen: null }],
}
const fixture = () => ({
  players: [player], total: 1, offset: 0, limit: 50,
  servers: [{ id: 'a', name: 'Survival', whitelist: 'available', online: 'unavailable', history: 'partial' }],
})
async function mount(path = '/players') {
  const router = createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/players', name: 'PlayersDirectory', component: Directory },
      { path: '/players/:playerId', name: 'PlayerDetail', component: Directory },
      { path: '/server/:id/players', name: 'ServerPlayers', component: { render: () => null } },
    ],
  })
  await router.push(path)
  const app = createApp({ render: () => h(Directory) })
  app.use(createPinia()).use(i18n).use(router)
  const auth = app.runWithContext(() => useAuthStore())
  auth.oidcMode = true
  auth.user = { permissions: [], servers: { a: { permissions: ['server.view', 'players.read'] } } }
  app.mount(document.body.appendChild(document.createElement('div')))
  apps.push(app)
  await settle()
  return { router, auth }
}
function requests(handler = url => url.includes('/api/players?') ? fixture() : player) {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    const value = await handler(url)
    return new Response(JSON.stringify(value), { headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}
function refresh() {
  const button = [...document.querySelectorAll('button')].find(b => b.textContent.includes('Refresh'))
  button.click()
}

test('read-only directory shows counts, explicit coverage and working detail navigation', async () => {
  const calls = requests()
  const { router } = await mount()
  assert.match(document.body.textContent, /1 players in this view/)
  assert.match(document.body.textContent, /History is partial/)
  assert.match(document.body.textContent, /Unavailable/)
  assert.equal(document.querySelector('tbody tr td:nth-child(2)').textContent, '0')
  const link = document.querySelector('tbody a')
  link.click()
  await settle()
  assert.equal(router.currentRoute.value.name, 'PlayerDetail')
  assert.match(document.body.textContent, /Former, Current/)
  assert.equal(document.querySelector('tbody tr td:nth-child(2)').textContent, 'Unknown')
  assert.equal(document.querySelector('tbody tr td:nth-child(3)').textContent, 'Yes')
  assert.equal(document.querySelector('tbody a').getAttribute('href'), '/server/a/players')
  assert.ok(calls.every(c => c.options.method === 'GET'))
  assert.ok(calls.some(c => c.url.includes(encodeURIComponent(player.id))))
})

test('search, relationship and server filters use backend-scoped query parameters', async () => {
  const calls = requests()
  await mount()
  const input = document.querySelector('input')
  input.value = 'Former'
  input.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { bubbles: true }))
  await settle()
  const selects = document.querySelectorAll('select')
  selects[0].value = 'whitelisted'
  selects[0].dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  selects[1].value = 'a'
  selects[1].dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  const query = new URL(calls.at(-1).url, 'http://localhost').searchParams
  assert.equal(query.get('q'), 'Former')
  assert.equal(query.get('relation'), 'whitelisted')
  assert.equal(query.get('server'), 'a')
  assert.equal(query.get('offset'), '0')
})

test('empty and failed requests do not masquerade as never joined', async () => {
  requests(() => ({ players: [], total: 0, offset: 0, limit: 50, servers: [] }))
  await mount()
  assert.match(document.body.textContent, /No game servers with player-read access/)
  assert.match(document.body.textContent, /does not mean a player never joined/)
  globalThis.fetch = async () => new Response(JSON.stringify({ error: 'Directory unavailable' }), {
    status: 503, headers: { 'Content-Type': 'application/json' },
  })
  refresh()
  await settle()
  assert.equal(document.querySelector('[role=alert]').textContent, 'Directory unavailable')
  assert.equal(document.querySelector('tbody'), null)
})

test('permission revision discards previous data and stale responses', async () => {
  requests()
  const { auth } = await mount()
  let resolveStale
  requests(() => new Promise(resolve => { resolveStale = resolve }))
  refresh()
  await settle()
  requests(() => ({ players: [], total: 0, offset: 0, limit: 50, servers: [] }))
  auth.user = null
  auth.revision++
  await settle()
  resolveStale(fixture())
  await settle()
  assert.doesNotMatch(document.body.textContent, /Current/)
  assert.match(document.body.textContent, /No game servers with player-read access/)
})
