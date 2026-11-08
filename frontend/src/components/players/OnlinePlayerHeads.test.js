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
  configFile: false, root: fileURLToPath(new URL('../../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'online-heads-dom-components',
    async load(id) {
      if (id.endsWith('/InstanceTransfers.vue') || id.endsWith('/PerformanceDiagnostics.vue')) {
        return 'export default { render: () => null }'
      }
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
const { createApp, h, nextTick, reactive } = await import('vue')
const { createPinia } = await import('pinia')
const { createRouter, createMemoryHistory } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
const Heads = (await vite.ssrLoadModule('/src/components/players/OnlinePlayerHeads.vue')).default
const Overview = (await vite.ssrLoadModule('/src/views/server/ServerOverviewPage.vue')).default
const apps = []
const originalFetch = globalThis.fetch
const originalSetTimeout = globalThis.setTimeout
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  globalThis.fetch = originalFetch
  globalThis.setTimeout = originalSetTimeout
})
async function settle() {
  for (let i = 0; i < 8; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
const uid = '12345678-1234-4234-8234-123456789abc'
const fixture = (count = 13) => ({
  players: Array.from({ length: count }, (_, i) => ({
    name: `Player${i}`, uuid: i === 0 ? uid : null,
    id: i === 0 ? `minecraft:${uid}` : null, namespace: i === 0 ? 'minecraft' : null,
  })), total: count, status: 'partial', offlineMode: false,
})
function requests(handler = () => fixture()) {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    return new Response(JSON.stringify(await handler(url)), { headers: { 'Content-Type': 'application/json' } })
  }
  return calls
}
async function mount({ canRead = true, status = 'running', overview = false, loader = 'vanilla' } = {}) {
  const props = reactive({ serverId: 'a', status, loader })
  const router = createRouter({
    history: createMemoryHistory(),
    routes: ['ServerPlayers', 'PlayerDetail', 'ServerConsole', 'ServerMods', 'ServerOverview'].map(name => ({
      path: name === 'PlayerDetail' ? '/players/:playerId' : `/server/:id/${name}`,
      name, component: { render: () => null },
    })),
  })
  await router.push({ name: 'ServerOverview', params: { id: 'a' } })
  const app = createApp({ render: () => h(overview ? Overview : Heads, overview ? {} : props) })
  app.use(createPinia()).use(i18n).use(router)
  const auth = app.runWithContext(() => useAuthStore())
  auth.oidcMode = true
  auth.managed = true
  auth.user = { permissions: [], servers: { a: { permissions: [
    'server.view', ...(canRead ? ['players.read'] : []),
    ...(overview ? ['server.logs.read', 'server.metrics.read', 'mods.read'] : []),
  ] } } }
  const store = app.runWithContext(() => useServerStore())
  store.currentServerId = 'a'
  store.server = { id: 'a', loader, status, port: 25565, runtime: { status, uptime: '1h', players: { online: 13 } } }
  store.logs.stdout = [{ text: 'Existing overview log' }]
  store.installedMods = [{ path: 'mods/demo.jar', name: 'ExistingMod', version: '1.0' }]
  app.mount(document.body.appendChild(document.createElement('div')))
  apps.push(app)
  await settle()
  return { props, router, auth, store }
}

test('at most ten accessible heads, remainder link and public-UUID-only provider requests', async () => {
  const calls = requests()
  const { router } = await mount()
  assert.equal(document.querySelectorAll('.online-heads li').length, 10)
  assert.equal(document.querySelectorAll('.player-avatar').length, 10)
  assert.equal(document.querySelectorAll('img').length, 1)
  const image = document.querySelector('img')
  assert.equal(image.getAttribute('src'), `https://mc-heads.net/avatar/${uid}/24`)
  assert.equal(image.getAttribute('referrerpolicy'), 'no-referrer')
  assert.equal(document.querySelectorAll('.player-avatar__initial').length, 10)
  image.dispatchEvent(new dom.window.Event('load'))
  await nextTick()
  assert.equal(document.querySelectorAll('.player-avatar__initial').length, 9)
  assert.equal(document.querySelector('.online-heads li a').getAttribute('title'), 'Player0')
  assert.equal(document.querySelector('.online-heads__remaining').textContent, '+3')
  assert.equal(document.querySelector('.online-heads__remaining').getAttribute('aria-label'), '+3 more online')
  assert.equal(document.querySelector('.online-heads li a').getAttribute('aria-label'), 'Player0')
  assert.ok(calls.every(c => c.options.method === 'GET' && c.url.includes('/api/servers/a/players/preview')))
  document.querySelector('.online-heads__remaining').click()
  await settle()
  assert.equal(router.currentRoute.value.name, 'ServerPlayers')
  assert.equal(router.currentRoute.value.params.id, 'a')
})

test('offline, unknown and invalid UUIDs use local initials; image error falls back', async () => {
  requests(() => ({ ...fixture(3), players: [
    { name: 'PrivateOffline', uuid: 'bb77495a-a91a-326f-af64-fba8c7b43e4d', id: 'offline:a:uuid', namespace: 'offline:a' },
    { name: 'Unresolved', uuid: null, id: null, namespace: null },
    { name: 'Invalid', uuid: 'PRIVATE_NAME', id: null, namespace: 'minecraft' },
  ] }))
  await mount()
  assert.equal(document.querySelectorAll('img').length, 0)
  assert.equal(document.querySelectorAll('.player-avatar__initial').length, 3)
  apps.splice(0).forEach(app => app.unmount())
  requests(() => ({ ...fixture(1), offlineMode: true }))
  await mount()
  assert.equal(document.querySelectorAll('img').length, 0)
  apps.splice(0).forEach(app => app.unmount())
  requests(() => fixture(1))
  await mount()
  document.querySelector('img').dispatchEvent(new dom.window.Event('error'))
  await nextTick()
  assert.equal(document.querySelector('img'), null)
})

test('no player-read grant or proxy means no names, heads, links or requests', async () => {
  const calls = requests()
  await mount({ canRead: false })
  assert.equal(document.querySelector('.online-heads'), null)
  assert.equal(calls.length, 0)
  apps.splice(0).forEach(app => app.unmount())
  await mount({ loader: 'velocity' })
  assert.equal(document.querySelector('.online-heads'), null)
  assert.equal(calls.length, 0)
})

test('stopped, empty, unavailable, loading and failed snapshots are explicit', async () => {
  const calls = requests(() => fixture(0))
  const { props } = await mount({ status: 'stopped' })
  assert.match(document.body.textContent, /Server offline/)
  assert.equal(calls.length, 0)
  props.status = 'running'
  await settle()
  assert.match(document.body.textContent, /No online players observed/)
  props.status = 'starting'
  await settle()
  assert.match(document.body.textContent, /Heads unavailable/)
  globalThis.fetch = async () => { throw new Error('Preview disconnected') }
  props.status = 'running'
  await settle()
  assert.match(document.querySelector('[role=alert]').textContent, /Preview disconnected/)
  assert.equal(document.querySelector('img'), null)
})

test('server and permission changes discard identities and in-flight responses', async () => {
  let resolve
  requests(() => new Promise(done => { resolve = done }))
  const { auth } = await mount()
  assert.match(document.querySelector('[role=status]').textContent, /Loading heads/)
  auth.user = null
  auth.revision++
  await settle()
  resolve(fixture())
  await settle()
  assert.equal(document.querySelector('.online-heads'), null)
  assert.doesNotMatch(document.body.textContent, /Player0/)
})

test('polling refreshes current sessions and stops when server scope changes', async () => {
  let poll
  globalThis.setTimeout = (callback, delay, ...args) => {
    if (delay === 5000) poll = callback
    return originalSetTimeout(callback, delay, ...args)
  }
  let current = fixture(2)
  const calls = requests(() => current)
  const { props } = await mount()
  assert.equal(document.querySelectorAll('.online-heads li').length, 2)
  current = fixture(0)
  await poll()
  await settle()
  assert.equal(document.querySelectorAll('.online-heads li').length, 0)
  assert.match(document.body.textContent, /No online players observed/)
  assert.equal(calls.length, 2)
  props.serverId = 'b' // No player-read grant on b.
  await settle()
  assert.equal(document.querySelector('.online-heads'), null)
  assert.equal(calls.length, 2)
})

test('overview keeps metrics, logs, mod management, connection and console navigation', async () => {
  requests(() => fixture(2))
  const { router } = await mount({ overview: true, loader: 'fabric' })
  assert.equal(document.querySelectorAll('.stat-card').length, 4)
  assert.match(document.body.textContent, /Existing overview log/)
  assert.match(document.body.textContent, /ExistingMod/)
  assert.match(document.body.textContent, /25565/)
  assert.equal(document.querySelectorAll('.online-heads li').length, 2)
  const playersCard = document.querySelector('.stat-card')
  assert.equal(playersCard.querySelector('.online-heads'), document.querySelector('.online-heads'))
  assert.equal(playersCard.querySelector('.stat-card__value > span').textContent, '13')
  assert.equal(document.querySelector('.online-heads__header'), null)
  assert.ok([...document.querySelectorAll('.stat-card')].slice(1).every(card => !card.querySelector('.online-heads')))
  const consoleLink = [...document.querySelectorAll('.overview-page__panel-link')].find(a => a.textContent.includes('Console'))
  assert.ok(consoleLink)
  consoleLink.click()
  await settle()
  assert.equal(router.currentRoute.value.name, 'ServerConsole')
})

test('head motion is bounded to hover/focus and respects reduced-motion preferences', async () => {
  const { descriptor } = parse(await readFile(new URL('./OnlinePlayerHeads.vue', import.meta.url), 'utf8'))
  const css = descriptor.styles.map(style => style.content).join('\n')
  assert.match(css, /transition:\s*transform 220ms/)
  assert.match(css, /li:focus-within a\s*\{[^}]*translateY\(-6px\)/)
  assert.match(css, /@media \(hover: hover\) and \(pointer: fine\)/)
  assert.match(css, /@media \(prefers-reduced-motion: reduce\)\s*\{[\s\S]*transition: none;[\s\S]*transform: none;/)
  assert.doesNotMatch(css, /transition:[^;]*(?:width|height|margin|padding)/)
})
