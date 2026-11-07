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
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0)
const vite = await createServer({
  configFile: false, root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'world-export-dom-components',
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
const { routerKey, routeLocationKey } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Panel = (await vite.ssrLoadModule('/src/components/WorldExportsPanel.vue')).default
const SharePage = (await vite.ssrLoadModule('/src/views/WorldSharePage.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  document.body.style.overflow = ''
  globalThis.fetch = originalFetch
})
async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
const button = name => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === name)
function mount(permissions, status = 'stopped', component = Panel) {
  const props = ref({ serverId: 'source', server: { status } })
  const app = createApp({ render: () => h(component, props.value) })
  app.use(createPinia())
  app.use(i18n)
  app.provide(routerKey, {})
  app.provide(routeLocationKey, { params: { id: 'source' } })
  app.runWithContext(() => {
    const auth = useAuthStore()
    auth.oidcMode = true
    auth.user = { permissions: ['server.view', ...permissions] }
    useServerStore().currentServerId = 'source'
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  return props
}
const artifact = { id: 'a'.repeat(32), world: { name: 'island' }, created_at: 1700000000,
  size: 20000000, sha256: 'b'.repeat(64), active_shares: 0 }

test('download-only users never inspect live worlds or expose export/share actions', async () => {
  const calls = []
  globalThis.fetch = async url => {
    calls.push(url)
    return Response.json({ exports: [artifact] })
  }
  mount(['worlds.download'], 'running')
  button('Open world downloads').click()
  await settle()
  assert.deepEqual(calls, ['/api/servers/source/world-exports'])
  assert.equal(document.querySelector('a').getAttribute('href'), `/api/servers/source/world-exports/${artifact.id}/download`)
  assert.equal(button('Export world archive'), undefined)
  assert.equal(button('Review & share'), undefined)
  assert.match(document.body.textContent, /not anonymized or guaranteed secret-free/)
})

test('running-server export is disabled and does not discover live worlds', async () => {
  globalThis.fetch = () => { throw new Error('No live world reads') }
  mount(['worlds.read', 'worlds.export'], 'running')
  button('Open world downloads').click()
  await settle()
  assert(button('Export world archive').disabled)
  assert.match(document.body.textContent, /POSIX host and a stopped instance, including external file writers/)
})

test('sharing requires explicit privacy review and shows failures', async () => {
  let submitted
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push(url)
    if (url.endsWith('/shares')) {
      submitted = JSON.parse(options.body)
      return Response.json({ error: 'storage unavailable' }, { status: 500 })
    }
    if (url.endsWith('/world-export-shares')) return Response.json({ shares: [] })
    return Response.json({ exports: [artifact] })
  }
  mount(['worlds.download', 'worlds.share'])
  button('Open world downloads').click()
  await settle()
  button('Review & share').click()
  await settle()
  assert(button('Create anonymous download link').disabled)
  assert.match(document.body.textContent, /identities, inventories, coordinates, signs, books/)
  assert.match(document.body.textContent, /not anonymized or guaranteed secret-free/)
  const checkbox = document.querySelector('input[type=checkbox]')
  checkbox.click()
  await settle()
  assert(!button('Create anonymous download link').disabled)
  document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  await settle()
  assert.deepEqual(submitted, { expiry_seconds: 86400, acknowledge_privacy: true })
  assert.match(document.querySelector('[role=alert]').textContent, /storage unavailable/)
  assert(!calls.some(url => url.endsWith('/worlds')))
})

test('public world page strips fragments and reports expired/browser-limited links without auth calls', async () => {
  globalThis.fetch = () => { throw new Error('No auth fetch') }
  window.history.replaceState({}, '', '/world-share#expired')
  mount([], 'stopped', SharePage)
  await settle()
  assert.equal(window.location.hash, '')
  button('Download world archive').click()
  await settle()
  assert.match(document.querySelector('[role=alert]').textContent, /unavailable, expired or revoked/)
})

test('retained downloads remain visible if live world discovery refuses the current layout', async () => {
  globalThis.fetch = async url => url.endsWith('/worlds')
    ? Response.json({ error: 'unsupported dimensions require manual review' }, { status: 400 })
    : Response.json({ exports: [artifact] })
  mount(['worlds.read', 'worlds.export', 'worlds.download'])
  button('Open world downloads').click()
  await settle()
  assert.equal(document.querySelector('a').getAttribute('href'), `/api/servers/source/world-exports/${artifact.id}/download`)
  assert.match(document.querySelector('[role=alert]').textContent, /unsupported dimensions/)
  assert(button('Export world archive').disabled)
})

test('switching servers discards stale errors without unlocking a new request', async () => {
  let rejectSource
  let resolveDestination
  globalThis.fetch = url => new Promise((resolve, reject) => {
    if (url === '/api/servers/source/world-exports') rejectSource = reject
    else if (url === '/api/servers/destination/world-exports') resolveDestination = resolve
    else reject(new Error(`Unexpected request: ${url}`))
  })
  const props = mount(['worlds.download'])
  button('Open world downloads').click()
  await settle()
  props.value = { serverId: 'destination', server: { status: 'stopped' } }
  await settle()
  button('Open world downloads').click()
  await settle()
  rejectSource(new Error('stale source failure'))
  await settle()
  assert(button('Hide world downloads').disabled)
  assert.equal(document.querySelector('[role=alert]'), null)
  resolveDestination(Response.json({ exports: [artifact] }))
  await settle()
  assert(!button('Hide world downloads').disabled)
  assert.equal(document.querySelector('a').getAttribute('href'), `/api/servers/destination/world-exports/${artifact.id}/download`)
})
