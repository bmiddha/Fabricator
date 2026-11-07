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
    name: 'diagnostics-dom-components',
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
const { i18n } = await vite.ssrLoadModule('/src/i18n.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Diagnostics = (await vite.ssrLoadModule('/src/components/PerformanceDiagnostics.vue')).default
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
const button = name => [...document.querySelectorAll('button')].find(value => value.textContent.trim() === name)
function fixture() {
  return {
    samples: [{ ts: Date.now() / 1000 - 30, running: true, tps: 19.5, rssBytes: 1024 ** 3 }],
    captures: [], capability: { supported: true, docs: 'https://docs.papermc.io/paper/profiling/' },
    samplingEnabled: true, jvmSampling: false, captureReady: true,
  }
}
function mount(manage = false) {
  const app = createApp({ render: () => h(Diagnostics, { serverId: 'a' }) })
  app.use(createPinia())
  app.use(i18n)
  const auth = app.runWithContext(() => useAuthStore())
  auth.oidcMode = true
  auth.user = { permissions: ['server.view', 'server.metrics.read', ...(manage ? ['diagnostics.manage'] : [])] }
  app.mount(document.body.appendChild(document.createElement('div')))
  apps.push(app)
}
function mockRequests(data = fixture()) {
  const requests = []
  globalThis.fetch = async (url, options) => {
    requests.push({ url, options })
    return new Response(JSON.stringify(options.method === 'GET' ? data : { status: 'requested' }), {
      headers: { 'Content-Type': 'application/json' },
    })
  }
  return requests
}

test('viewer sees scoped history, unavailable gaps and no mutation controls', async () => {
  const requests = mockRequests()
  mount()
  await settle()
  assert.equal(document.querySelectorAll('input[type=checkbox]').length, 0)
  assert.equal(button('Start bounded spark capture'), undefined)
  assert.match(document.body.textContent, /Read-only/)
  assert.match(document.body.textContent, /No available measurement/)
  assert.equal(document.querySelectorAll('svg[role=img]').length, 2)
  assert.ok(requests.every(entry => entry.options.method === 'GET'))
})
test('capture requires informed consent and posts only a bounded duration', async () => {
  const requests = mockRequests()
  mount(true)
  await settle()
  const start = button('Start bounded spark capture')
  assert.equal(start.disabled, true)
  const consent = [...document.querySelectorAll('input[type=checkbox]')].at(-1)
  consent.click()
  await settle()
  assert.equal(start.disabled, false)
  start.click()
  await settle()
  const submitted = requests.find(entry => entry.options.method === 'POST')
  assert.equal(submitted.url, '/api/servers/a/diagnostics/captures')
  assert.deepEqual(JSON.parse(submitted.options.body), { durationSeconds: 60, acceptUpload: true })
  assert.equal(consent.checked, false)
  assert.equal(start.disabled, true)
})
test('unsupported runtime offers guidance, never an install or capture', async () => {
  const data = fixture()
  data.capability.supported = false
  mockRequests(data)
  mount(true)
  await settle()
  assert.equal(button('Start bounded spark capture'), undefined)
  assert.match(document.body.textContent, /Other loaders/)
  assert.equal([...document.querySelectorAll('input[type=checkbox]')].length, 1)
})
test('unsafe report URLs never become links and statuses do not imply success', async () => {
  const data = fixture()
  data.captures = [{ id: 'one', created: Date.now() / 1000, durationSeconds: 60, status: 'requested', message: 'Waiting', reportUrl: 'javascript:alert(1)' }]
  mockRequests(data)
  mount(true)
  await settle()
  assert.match(document.body.textContent, /Waiting for acknowledgement/)
  assert.equal([...document.querySelectorAll('a')].some(link => link.href.startsWith('javascript:')), false)
  assert.equal(button('Start bounded spark capture').disabled, true)
})
test('failed reads surface an actionable error instead of empty success', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({ error: 'History storage unavailable' }), {
    status: 503, headers: { 'Content-Type': 'application/json' },
  })
  mount()
  await settle()
  assert.match(document.querySelector('[role=alert]').textContent, /History storage unavailable/)
})
