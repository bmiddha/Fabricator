import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node', 'sessionStorage']) globalThis[key] = dom.window[key]
globalThis.requestAnimationFrame = fn => setTimeout(fn, 0)
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
const Tasks = (await vite.ssrLoadModule('/src/components/TaskStatus.vue')).default
const Snapshots = (await vite.ssrLoadModule('/src/components/backups/SnapshotsTable.vue')).default
const { useBackupsStore } = await vite.ssrLoadModule('/src/stores/backups.js')
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
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
function mount(component, props = {}, permissions = ['server.view', 'activity.read']) {
  const app = createApp({ render: () => h(component, props) })
  app.use(createPinia())
  app.use(i18n)
  app.component('router-link', { props: ['to'], template: '<a><slot /></a>' })
  const auth = app.runWithContext(() => useAuthStore())
  auth.oidcMode = true
  auth.user = { permissions }
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  return auth
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
    if (url.endsWith('/tasks')) return Response.json({ items: [] })
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

const task = {
  id: 'durable-generic', job_id: 'bjb_generic', action: 'backups.create', kind: 'backup',
  server_id: 'a', server_name: 'Generic instance', phase: 'uploading', active: true,
  elapsed_seconds: 42, total_bytes: 100, transferred_bytes: 25, stale: false,
}

test('task status resumes server records and renders only measured progress', async () => {
  globalThis.fetch = async () => Response.json({ items: [
    task, { ...task, id: 'restore', action: 'worlds.restore', phase: 'safety_snapshot', total_bytes: null },
    { ...task, id: 'interrupted', phase: 'interrupted', active: false },
  ] })
  mount(Tasks, { serverId: 'a' })
  await settle()
  assert.match(document.body.textContent, /Generic instance/)
  assert.match(document.body.textContent, /Uploading archive/)
  assert.match(document.body.textContent, /42s elapsed/)
  assert.match(document.body.textContent, /Interrupted by backend restart/)
  assert.equal(document.querySelectorAll('progress').length, 1)
  assert.equal(document.querySelector('progress').value, 25)
  assert.equal(document.querySelector('progress').max, 100)
})

test('stale progress is explicit and auth revocation clears task data and polling', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    return Response.json({ items: [{ ...task, stale: true }] })
  }
  const auth = mount(Tasks, { serverId: 'a' })
  await settle()
  assert.match(document.body.textContent, /No recent update/)
  assert.equal(document.querySelectorAll('progress').length, 0)
  auth.user = null
  auth.revision++
  await settle()
  assert.doesNotMatch(document.body.textContent, /Generic instance/)
  assert.equal(calls, 1)
})

test('lost contact retains last known work and retries without claiming completion', async () => {
  let calls = 0
  globalThis.fetch = async () => {
    calls++
    if (calls === 2) return Response.json({ error: 'unavailable' }, { status: 503 })
    return Response.json({ items: [{ ...task, phase: calls > 2 ? 'done' : 'uploading', active: calls < 3 }] })
  }
  mount(Tasks, { serverId: 'a' })
  await settle()
  await new Promise(resolve => setTimeout(resolve, 3100))
  await settle()
  assert.match(document.body.textContent, /Task status unavailable/)
  assert.match(document.body.textContent, /Uploading archive/)
  assert.equal(document.querySelectorAll('progress').length, 0)
  await new Promise(resolve => setTimeout(resolve, 3100))
  await settle()
  assert.match(document.body.textContent, /Completed/)
  assert.doesNotMatch(document.body.textContent, /Task status unavailable/)
})

test('archive icons distinguish missing from unknown; missing entries cannot restore or download', async () => {
  globalThis.fetch = async () => Response.json({})
  const snapshots = ['available', 'missing', 'unavailable', 'unchecked'].map(availability => ({
    id: availability, fileName: `${availability}.tar`, filePath: `/isolated/${availability}.tar`,
    availability, status: 'success', type: 'backup',
  }))
  mount(Snapshots, { serverId: 'a', snapshots },
    ['server.view', 'backups.restore', 'backups.download'])
  await settle()
  const rows = [...document.querySelectorAll('tbody tr')]
  const restore = row => [...row.querySelectorAll('button')].find(b => b.textContent.trim() === 'Restore')
  assert.equal(restore(rows[0]).disabled, false)
  assert.equal(restore(rows[1]).disabled, true)
  assert.equal(restore(rows[2]).disabled, true)
  assert.equal(restore(rows[3]).disabled, false)
  assert.equal(rows[1].querySelectorAll('a').length, 0)
  assert.equal(rows[1].querySelector('[role=img]').getAttribute('aria-label'), 'Archive missing')
  assert.equal(rows[2].querySelector('[role=img]').getAttribute('aria-label'), 'Cannot verify archive')
  assert.equal(rows[3].querySelector('[role=img]').getAttribute('aria-label'), 'Not checked')
})

test('restored task records block duplicate submissions; terminal records release a hung poll', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method })
    if (url.includes('/backup-jobs/')) return new Promise(() => {})
    return Response.json([])
  }
  mount({ render: () => h('div') }, {}, ['server.view', 'backups.create'])
  const server = useServerStore()
  server.currentServerId = 'a'
  const backups = useBackupsStore()
  backups.syncTasks([task])
  assert.equal(backups.activeJob.id, task.job_id)
  assert.equal(backups.taskBusy, true)
  await backups.runQuickBackup({})
  assert.equal(calls.filter(call => call.method === 'POST').length, 0)
  backups.syncTasks([{ ...task, active: false, phase: 'done' }])
  assert.equal(backups.taskBusy, false)
  assert.equal(backups.activeJob.phase, 'done')
  backups.resetState()
})

test('pending acceptance blocks duplicates and late responses cannot cross server/auth scope', async () => {
  let resolve
  let posts = 0
  globalThis.fetch = async (url, options) => {
    if (options.method === 'POST') {
      posts++
      return new Promise(done => { resolve = done })
    }
    return Response.json([])
  }
  const auth = mount({ render: () => h('div') }, {}, ['server.view', 'backups.create'])
  const server = useServerStore()
  server.currentServerId = 'a'
  const backups = useBackupsStore()
  backups.syncTasks([])
  const request = backups.runQuickBackup({})
  await backups.runQuickBackup({})
  assert.equal(posts, 1)
  server.currentServerId = 'b'
  auth.revision++
  backups.resetState()
  backups.syncTasks([task], 'a')
  assert.equal(backups.activeJob, null)
  resolve(Response.json({ job_id: 'bjb_old_scope' }))
  await request
  assert.equal(backups.activeJob, null)
  assert.equal(backups.submittingJob, false)
  backups.resetState()
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

test('refreshing delivery status does not hide a failed notification action', async () => {
  globalThis.fetch = async (url, options) => {
    if (options.method === 'POST') return Response.json({ error: 'Wait 60 seconds between test notifications' }, { status: 400 })
    return Response.json({
      destinations: [{ id: 'd1', name: 'Operations', kind: 'webhook', enabled: true, alerts: ['job.failed'] }],
      deliveries: [], alerts: [],
    })
  }
  mount(Notifications)
  await settle()
  button('Send test').click()
  await settle()
  button('Refresh').click()
  await settle()
  assert.match(document.querySelector('[role=alert]').textContent, /Wait 60 seconds/)
})

test('removing the edited destination clears its secret form', async () => {
  let removed = false
  globalThis.fetch = async (url, options) => {
    if (options.method === 'DELETE') { removed = true; return Response.json({ success: true }) }
    return Response.json({
      destinations: removed ? [] : [{ id: 'd1', name: 'Operations', kind: 'webhook', enabled: true, alerts: ['job.failed'] }],
      deliveries: [], alerts: [],
    })
  }
  mount(Notifications)
  await settle()
  button('Edit destination').click()
  await settle()
  await input('notification-url', 'https://hooks.example/secret-canary')
  button('Remove destination').click()
  await settle()
  button('Confirm').click()
  await settle()
  assert(removed)
  assert.equal(document.querySelector('form'), null)
  assert.equal(button('Save destination'), undefined)
})

test('links never bypass view permissions', () => {
  const crash = { ...event, alert: 'server.crashed' }
  assert.equal(eventTarget(crash, { can: () => false }), null)
  assert.equal(eventTarget(crash, { can: p => p === 'server.view' }), null)
  assert.equal(eventTarget(crash, { can: p => ['server.view', 'server.logs.read'].includes(p) }).name, 'ServerConsole')
})
