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
globalThis.requestAnimationFrame = (fn) => setTimeout(fn, 0)
dom.window.HTMLDialogElement.prototype.showModal = function () { this.setAttribute('open', '') }
dom.window.HTMLDialogElement.prototype.close = function () { this.removeAttribute('open') }

// Compile actual client render functions, then use the same Vite import
// resolution as the app and the existing store tests (no test-only UI copies).
const vite = await createServer({
  configFile: false,
  root: fileURLToPath(new URL('../../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'client-components-for-dom-tests',
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
const { createApp, h, nextTick, reactive } = await import('vue')
const { createPinia } = await import('pinia')
const { routerKey, routeLocationKey } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Preview = (await vite.ssrLoadModule('/src/components/modals/ModProjectPreview.vue')).default
const Browser = (await vite.ssrLoadModule('/src/components/modals/ModBrowserModal.vue')).default
const Installed = (await vite.ssrLoadModule('/src/views/server/ServerModsPage.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach((app) => app.unmount())
  document.body.innerHTML = ''
  document.body.style.overflow = ''
  globalThis.fetch = originalFetch
})

async function settle() {
  for (let i = 0; i < 5; i++) {
    await new Promise((resolve) => setTimeout(resolve, 0))
    await nextTick()
  }
}
function button(name, root = document) {
  return [...root.querySelectorAll('button')].find((item) => item.textContent.trim() === name)
}
function mount(Component, props = {}, permissions = ['mods.read', 'server.view']) {
  const app = createApp({ render: () => h(Component, { ...props, ref: 'child' }) })
  app.use(createPinia())
  app.use(i18n)
  app.provide(routerKey, {})
  app.provide(routeLocationKey, { params: { id: 'preview' } })
  const stores = app.runWithContext(() => ({ store: useServerStore(), auth: useAuthStore() }))
  stores.auth.oidcMode = true
  stores.auth.user = { permissions: [], servers: { preview: { permissions } } }
  stores.store.currentServerId = 'preview'
  stores.store.server = { loader: 'fabric', version: '1.21.1' }
  const root = document.createElement('div')
  document.body.append(root)
  const wrapper = app.mount(root)
  apps.push(app)
  return { ...stores, root, vm: wrapper.$refs.child }
}

test('installed entry points handle manifest, disabled hash match and unknown files for read-only users', async () => {
  const calls = []
  globalThis.fetch = async (url) => {
    calls.push(url)
    return Response.json({ id: url.split('/').pop(), title: 'Identified project', body: '# Full description' })
  }
  const { store, root } = mount(Installed)
  store.installedMods = [
    { name: 'record.jar', filename: 'record.jar', path: 'record.jar', modrinth: { projectId: 'installed-record' } },
    { name: 'hash.jar.disabled', filename: 'hash.jar.disabled', path: 'hash.jar.disabled', disabled: true, modrinthGuess: { projectId: 'installed-hash' } },
    { name: 'unknown.jar', filename: 'unknown.jar', path: 'unknown.jar' },
  ]
  await settle()
  assert.equal(calls.length, 0)
  assert.ok(button('Browse mods', root))
  assert.equal(button('Remove', root), undefined)
  const details = [...root.querySelectorAll('button')].filter((item) => item.textContent.trim() === 'Details')
  details[0].click()
  await settle()
  assert.match(calls[0], /\/project\/installed-record$/)
  assert.match(root.querySelector('dialog').textContent, /Full description/)
  button('Close', root.querySelector('dialog')).click()
  await settle()
  details[1].click()
  await settle()
  assert.match(calls[1], /\/project\/installed-hash$/)
  button('Close', root.querySelector('dialog')).click()
  await settle()
  details[2].click()
  await settle()
  assert.match(root.querySelector('dialog').textContent, /No Modrinth project is identified/)
  assert.equal(calls.length, 2)
  assert.equal(store.installedMods.length, 3)
})

test('search previews preserve query, filters and results without installation rights or eager body requests', async () => {
  const calls = []
  globalThis.fetch = async (url) => {
    calls.push(url)
    if (url.includes('game-versions')) return Response.json([])
    return Response.json({ id: 'browse-project', title: 'Browse result', description: 'Short description', body: '# Body' })
  }
  const props = reactive({ show: true, loader: 'fabric', mcVersion: '1.21.1' })
  const { root, vm } = mount(Browser, props)
  vm.searchQuery = 'my query'
  vm.sortBy = 'downloads'
  vm.versionFilterExpanded = true
  vm.results = [{ project_id: 'browse-project', title: 'Browse result', description: 'Short', versions: ['1.21.1'], categories: ['fabric'] }]
  await settle()
  assert.equal(calls.some((url) => url.includes('/project/')), false)
  assert.equal(button('Install', root), undefined)
  button('Details', root).click()
  await settle()
  assert.match(root.querySelector('dialog').textContent, /Short description/)
  button('Close', root.querySelector('dialog')).click()
  await settle()
  assert.equal(vm.searchQuery, 'my query')
  assert.equal(vm.sortBy, 'downloads')
  assert.equal(vm.versionFilterExpanded, true)
  assert.equal(vm.results.length, 1)
  assert.equal(props.show, true)
  await vm.installMod(vm.results[0])
  assert.equal(vm.showCompatibilityModal, false)
})

test('preview guards stale responses, exposes loading/error/retry and empty body, aborts on close', async () => {
  const requests = []
  globalThis.fetch = (url, options) => new Promise((resolve) => requests.push({ url, options, resolve }))
  const props = reactive({ show: true, projectId: 'slow-first', title: 'First', onClose: () => { props.show = false } })
  const { root } = mount(Preview, props)
  await settle()
  assert.match(root.textContent, /Loading project details/)
  props.projectId = 'fast-second'
  await settle()
  assert.equal(requests[0].options.signal.aborted, true)
  requests[1].resolve(Response.json({ error: 'Rate limited' }, { status: 429 }))
  await settle()
  assert.match(root.textContent, /Could not load/)
  assert.match(root.textContent, /Rate limited/)
  button('Retry', root).click()
  await settle()
  requests[2].resolve(Response.json({ id: 'fast-second', title: 'Second', body: '' }))
  await settle()
  assert.match(root.textContent, /no full description/)
  requests[0].resolve(Response.json({ id: 'slow-first', title: 'Wrong stale title', body: '# Wrong' }))
  await settle()
  assert.equal(root.textContent.includes('Wrong stale title'), false)
  button('Close', root).click()
  await settle()
  assert.equal(root.querySelector('dialog').open, false)
  assert.equal(document.body.style.overflow, '')
})

test('search install action retains compatibility warning and installed guard', async () => {
  globalThis.fetch = async (url) => Response.json(url.includes('game-versions') ? [] : [])
  const { vm, store } = mount(Browser, { show: true, loader: 'fabric', mcVersion: '1.21.1' }, ['mods.read', 'server.view', 'mods.install'])
  const mod = { project_id: 'incompatible', title: 'Different version', versions: ['1.20.1'], categories: ['fabric'] }
  await vm.installMod(mod)
  assert.equal(vm.showCompatibilityModal, true)
  assert.deepEqual(vm.pendingMod, mod)
  vm.cancelCompatibilityInstall()
  store.installedMods = [{ modrinth: { projectId: mod.project_id } }]
  await vm.installMod(mod)
  assert.equal(vm.showCompatibilityModal, false)
})

test('compatible search install still emits the existing install payload', async () => {
  const installs = []
  globalThis.fetch = async (url) => Response.json(url.includes('game-versions') ? [] : { dependencies: [] })
  const { root, vm } = mount(Browser, {
    show: true, loader: 'fabric', mcVersion: '1.21.1', onInstall: (payload) => installs.push(payload),
  }, ['mods.read', 'server.view', 'mods.install'])
  const mod = { project_id: 'compatible', title: 'Compatible mod', versions: ['1.21.1'], categories: ['fabric'] }
  vm.results = [mod]
  await settle()
  assert.ok(button('Install', root))
  button('Install', root).click()
  await settle()
  assert.deepEqual(installs, [{ modId: 'compatible', modTitle: 'Compatible mod', mcVersion: '1.21.1', loader: 'fabric' }])
})
