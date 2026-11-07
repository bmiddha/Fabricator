import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'sessionStorage']) globalThis[key] = dom.window[key]
const vite = await createServer({
  configFile: false, root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'activity-dom-components',
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
const Timeline = (await vite.ssrLoadModule('/src/components/ActivityTimeline.vue')).default
const Notifications = (await vite.ssrLoadModule('/src/components/settings/NotificationsPanel.vue')).default
const { eventTarget } = await vite.ssrLoadModule('/src/utils/events.js')
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
const button = name => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === name)
function mount(component, props = {}) {
  const app = createApp({ render: () => h(component, props) })
  app.use(createPinia())
  app.use(i18n)
  app.component('router-link', { props: ['to'], template: '<a><slot /></a>' })
  const auth = app.runWithContext(() => useAuthStore())
  auth.oidcMode = true
  auth.user = { permissions: ['server.view', 'activity.read'] }
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
}
async function input(id, value) {
  const field = document.getElementById(id)
  field.value = value
  field.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
  await settle()
}
const event = { id: 2, at: 1794000000, actor: { kind: 'oidc', id: 'alice' }, action: 'configs.update', outcome: 'succeeded', server_id: 'a', ref: null }

test('timeline shows actor, operation, outcome and pagination; alerts use the scoped API', async () => {
  const requests = []
  globalThis.fetch = async url => {
    requests.push(url)
    return Response.json(url.includes('before=') ? { items: [{ ...event, id: 1 }], next: null } : { items: [event], next: 2 })
  }
  mount(Timeline, { serverId: 'a' })
  await settle()
  assert.match(document.body.textContent, /alice/)
  assert.match(document.body.textContent, /Changed server configuration/)
  assert.match(document.body.textContent, /Succeeded/)
  button('Load older activity').click()
  await settle()
  assert.match(requests[1], /before=2/)
  assert.equal(document.querySelectorAll('.activity__event').length, 2)
  const filter = document.querySelector('input[type=checkbox]')
  filter.checked = true
  filter.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  assert.match(requests.at(-1), /\/servers\/a\/alerts/)
})

test('delivery failures remain visible; editing never fills a saved secret and tests say queued', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, options })
    if (options.method === 'POST') return Response.json({ status: 'queued' }, { status: 202 })
    return Response.json({
      destinations: [{ id: 'd1', name: 'Operations', kind: 'webhook', enabled: true, has_url: true, alerts: ['job.failed'] }],
      deliveries: [{ id: 1, destination_id: 'd1', name: 'Operations', created: 1794000000, state: 'failed', attempts: 5, error: 'http_503' }],
      alerts: [],
    })
  }
  mount(Notifications)
  await settle()
  assert.match(document.body.textContent, /http_503/)
  assert.match(document.body.textContent, /5 \/ 5/)
  button('Edit destination').click()
  await settle()
  assert.equal(document.getElementById('notification-url').value, '')
  assert.match(document.body.textContent, /Leave blank to keep/)
  button('Cancel').click()
  await settle()
  button('Send test').click()
  await settle()
  assert.match(document.body.textContent, /Test queued, not yet delivered/)
  assert(calls.some(c => c.url.endsWith('/d1/test') && c.options.method === 'POST'))
})

test('saving a destination sends the secret once and clears the form', async () => {
  let saved
  globalThis.fetch = async (url, options) => {
    if (options.method === 'POST') { saved = JSON.parse(options.body); return Response.json({ id: 'd1' }, { status: 201 }) }
    return Response.json({ destinations: [], deliveries: [], alerts: [] })
  }
  mount(Notifications)
  await settle()
  button('Add destination').click()
  await settle()
  await input('notification-name', 'Operations')
  await input('notification-url', 'https://hooks.example/secret-canary')
  document.querySelector('form').dispatchEvent(new dom.window.Event('submit', { bubbles: true, cancelable: true }))
  await settle()
  assert.equal(saved.url, 'https://hooks.example/secret-canary')
  assert.equal(document.querySelector('form'), null)
  assert(!document.body.textContent.includes('secret-canary'))
})

test('links never bypass view permissions', () => {
  const crash = { ...event, alert: 'server.crashed' }
  assert.equal(eventTarget(crash, { can: () => false }), null)
  assert.equal(eventTarget(crash, { can: p => p === 'server.view' }), null)
  assert.equal(eventTarget(crash, { can: p => ['server.view', 'server.logs.read'].includes(p) }).name, 'ServerConsole')
})
