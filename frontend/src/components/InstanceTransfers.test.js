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
    name: 'transfer-dom-components',
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
const { routerKey } = await import('vue-router')
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Transfers = (await vite.ssrLoadModule('/src/components/InstanceTransfers.vue')).default
const apps = []
const originalFetch = globalThis.fetch
after(async () => { await vite.close(); dom.window.close() })
afterEach(() => {
  apps.splice(0).forEach(app => app.unmount())
  document.body.innerHTML = ''
  document.body.style.overflow = ''
  sessionStorage.clear()
  globalThis.fetch = originalFetch
})
async function settle() {
  for (let i = 0; i < 6; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
const button = name => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === name)
async function change(id, value) {
  const field = document.getElementById(id)
  field.value = value
  field.dispatchEvent(new dom.window.Event(field.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }))
  await settle()
}
function mount(readOnly = false) {
  const app = createApp({ render: () => h(Transfers) })
  app.use(createPinia())
  app.use(i18n)
  app.provide(routerKey, {})
  const stores = app.runWithContext(() => ({ store: useServerStore(), auth: useAuthStore() }))
  stores.auth.oidcMode = readOnly
  stores.auth.user = { permissions: ['server.view'] }
  stores.store.currentServerId = 'source'
  stores.store.server = { id: 'source', name: 'Source', loader: 'vanilla' }
  stores.store.serversList = [
    { id: 'source', name: 'Source', loader: 'vanilla' },
    { id: 'destination', name: 'Destination', loader: 'vanilla' },
  ]
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  return stores
}
const world = { id: 'island', name: 'island', version: '1.21.1', dataVersion: 3955,
  layout: 'java-nested', directories: ['island'], dimensions: ['DIM-1', 'dimensions/test/moon'] }
const plan = { bytes: 42, worlds: [world], warnings: ['Review trusted mod configs for credentials.'],
  destinationWorld: 'island', replaces: true, previewToken: 'reviewed' }

test('read-only view exposes no transfer actions and reads no world metadata', async () => {
  globalThis.fetch = () => { throw new Error('must not fetch') }
  mount(true)
  await settle()
  assert.match(document.body.textContent, /Read-only/)
  assert.equal(document.querySelectorAll('button').length, 0)
})

test('fork requires explicit inclusion choices and invalidates preview when name changes', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, data: JSON.parse(options.body) })
    return Response.json({ ...plan, replaces: false })
  }
  mount()
  button('Fork / create from existing').click()
  await settle()
  assert(button('Preview copy scope').disabled)
  await change('transfer-name', 'survival-test')
  await change('transfer-worlds', 'no')
  assert(button('Preview copy scope').disabled)
  await change('transfer-players', 'no')
  assert(!button('Preview copy scope').disabled)
  button('Preview copy scope').click()
  await settle()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].data.includeWorlds, false)
  assert.equal(calls[0].data.includePlayerData, false)
  assert.match(document.body.textContent, /Review trusted mod configs/)
  assert.match(document.body.textContent, /DataVersion 3955/)
  assert.match(document.body.textContent, /dimensions\/test\/moon/)
  await change('transfer-name', 'another-name')
  assert(!document.getElementById('transfer-replacement'))
  assert(button('Preview copy scope'))
  assert.equal(document.querySelector('input[type="checkbox"]'), null)
})

test('world replacement needs distinct typed confirmation, reports progress and preserves restart choice', async () => {
  let submitted
  globalThis.fetch = async (url, options) => {
    if (url.endsWith('/worlds')) return Response.json({ worlds: [world] })
    if (url.endsWith('/preview')) return Response.json(plan)
    if (url.includes('/transfer-jobs/')) return Response.json({
      phase: 'done', result: { safetySnapshotId: 'snapshot-fixture' },
    })
    if (url === '/api/servers') return Response.json([])
    submitted = JSON.parse(options.body)
    return Response.json({ jobId: 'fixture-job' })
  }
  mount()
  button('Copy world to instance').click()
  await settle()
  await change('transfer-world', 'island')
  await change('transfer-destination', 'destination')
  await change('transfer-restart', 'no')
  await change('transfer-players', 'yes')
  button('Preview copy scope').click()
  await settle()
  assert(button('Back up & replace world').disabled)
  const acknowledge = document.querySelector('input[type="checkbox"]')
  acknowledge.click()
  await settle()
  assert(button('Back up & replace world').disabled)
  await change('transfer-replacement', 'wrong')
  assert(button('Back up & replace world').disabled)
  await change('transfer-replacement', 'island')
  assert(!button('Back up & replace world').disabled)
  button('Back up & replace world').click()
  await settle()
  assert.equal(submitted.confirmReplace, 'island')
  assert.equal(submitted.restart, false)
  assert.equal(submitted.acknowledgeRisks, true)
  assert.match(document.body.textContent, /Copy complete/)
  assert.match(document.body.textContent, /snapshot-fixture/)
  assert.equal(sessionStorage.length, 0)
})

test('incompatible preview surfaces the backend error without a confirmation action', async () => {
  globalThis.fetch = async (url) => url.endsWith('/worlds')
    ? Response.json({ worlds: [world] })
    : Response.json({ error: 'Installed mod/plugin jar hashes differ' }, { status: 400 })
  mount()
  button('Copy world to instance').click()
  await settle()
  await change('transfer-world', 'island')
  await change('transfer-destination', 'destination')
  await change('transfer-restart', 'no')
  await change('transfer-players', 'no')
  button('Preview copy scope').click()
  await settle()
  assert.match(document.querySelector('[role="alert"]').textContent, /hashes differ/)
  assert(!button('Back up & replace world'))
})

test('lost submission contact never enables blind duplicate submission', async () => {
  let submissions = 0
  globalThis.fetch = async (url) => {
    if (url.endsWith('/preview')) return Response.json({ ...plan, replaces: false })
    submissions++
    throw new Error('fixture connection lost')
  }
  mount()
  button('Fork / create from existing').click()
  await settle()
  await change('transfer-name', 'survival-test')
  await change('transfer-worlds', 'yes')
  await change('transfer-players', 'no')
  button('Preview copy scope').click()
  await settle()
  document.querySelector('input[type="checkbox"]').click()
  await settle()
  const submit = [...document.querySelectorAll('button')].filter(b => b.textContent.trim() === 'Fork / create from existing').at(-1)
  submit.click()
  await settle()
  assert.equal(submissions, 1)
  assert.match(document.body.textContent, /outcome unknown/)
  assert.equal(document.querySelectorAll('fieldset').length, 0)
  assert(!button('Preview copy scope'))
})
