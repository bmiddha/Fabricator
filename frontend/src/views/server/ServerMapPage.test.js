import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node']) {
  globalThis[key] = dom.window[key]
}
const vite = await createServer({
  configFile: false,
  root: fileURLToPath(new URL('../../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'client-map-components',
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
const { routeLocationKey } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const MapPage = (await vite.ssrLoadModule('/src/views/server/ServerMapPage.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  globalThis.fetch = originalFetch
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
function button(name) {
  return [...document.querySelectorAll('button')].find(item => item.textContent.trim() === name)
}
function mount(permissions) {
  const app = createApp({ render: () => h(MapPage) })
  app.use(createPinia())
  app.use(i18n)
  app.provide(routeLocationKey, { params: { id: 'preview' } })
  app.runWithContext(() => {
    const auth = useAuthStore()
    auth.oidcMode = true
    auth.user = { permissions: [], servers: { preview: { permissions } } }
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  return root
}
function state(overrides = {}) {
  return {
    enabled: false, ready: false,
    preview: {
      compatible: true, ready_to_configure: true, target: 'COBBLEVERSE test fixture',
      dependencies: [{ id: 'bluemap', required: '5.7', installed: '5.7', compatible: true }],
    },
    ...overrides,
  }
}

test('read-only map embeds opaque viewer without management controls', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push([url, options.method])
    return Response.json(url.endsWith('/session')
      ? { url: '/api/servers/preview/map/view/fixture/', expires_in: 300 }
      : state({ enabled: true, ready: true }))
  }
  const root = mount(['server.view', 'map.read'])
  await settle()
  const iframe = root.querySelector('iframe')
  assert.equal(iframe.getAttribute('sandbox'), 'allow-scripts')
  assert.equal(iframe.getAttribute('referrerpolicy'), 'no-referrer')
  assert.equal(iframe.title, 'Interactive server map with live players and global waystones')
  assert.equal(root.querySelector('details'), null)
  assert.deepEqual(calls, [
    ['/api/servers/preview/map', 'GET'],
    ['/api/servers/preview/map/session', 'POST'],
  ])
})

test('setup requires explicit resource consent and submits it without restarting', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    if (options.method === 'POST') calls.push([url, JSON.parse(options.body)])
    return Response.json(options.method === 'POST' ? { enabled: true, restart_required: true } : state())
  }
  const root = mount(['server.view', 'map.read', 'map.manage'])
  await settle()
  assert.equal(button('Configure private map').disabled, true)
  const checkbox = root.querySelector('input[type="checkbox"]')
  checkbox.checked = true
  checkbox.dispatchEvent(new window.Event('change', { bubbles: true }))
  await settle()
  button('Configure private map').click()
  await settle()
  assert.deepEqual(calls, [
    ['/api/servers/preview/map/setup', { accept_resource_download: true }],
  ])
  assert.match(root.textContent, /no automatic restart was performed/)
})

test('transport errors remove stale viewer rather than leave player positions visible', async () => {
  let failed = false
  globalThis.fetch = async url => {
    if (failed) return Response.json({ error: 'BlueMap offline' }, { status: 409 })
    return Response.json(url.endsWith('/session')
      ? { url: '/api/servers/preview/map/view/fixture/', expires_in: 300 }
      : state({ enabled: true, ready: true }))
  }
  const root = mount(['server.view', 'map.read'])
  await settle()
  assert.ok(root.querySelector('iframe'))
  failed = true
  button('Refresh map').click()
  await settle()
  assert.equal(root.querySelector('iframe'), null)
  assert.match(root.querySelector('[role="alert"]').textContent, /BlueMap offline/)
})
