import assert from 'node:assert/strict'
import { after, afterEach, test } from 'node:test'
import { readFile } from 'node:fs/promises'
import { fileURLToPath } from 'node:url'
import { JSDOM } from 'jsdom'
import { compileScript, compileTemplate, parse } from '@vue/compiler-sfc'
import { createServer } from 'vite'

const dom = new JSDOM('<body></body>', { url: 'http://localhost/' })
for (const key of ['window', 'document', 'Element', 'HTMLElement', 'SVGElement', 'Node']) globalThis[key] = dom.window[key]
const vite = await createServer({
  configFile: false, root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'update-dom-components',
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
const Updates = (await vite.ssrLoadModule('/src/components/ModpackUpdates.vue')).default
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
const checkbox = text => [...document.querySelectorAll('label')].find(label => label.textContent.includes(text)).querySelector('input')
async function input(element, value) {
  if (element.type === 'checkbox') element.checked = value
  else element.value = value
  element.dispatchEvent(new dom.window.Event(element.type === 'checkbox' ? 'change' : 'input', { bubbles: true }))
  await settle()
}
const basePlan = {
  id: 'a'.repeat(32), reviewToken: 'immutable-review', status: 'review', warnings: ['Review trusted configs.'],
  summary: { name: 'Fixture pack', version: '2' }, loaderChanges: [], changes: [
    { path: 'mods/new.jar', category: 'mod', action: 'add', conflict: false },
    { path: 'config/edited.json', category: 'config', action: 'preserve', conflict: true, diff: '-old\n+new\n' },
  ],
  preservedFiles: ['config/local.json'], dependencyReview: ['Dependency version ranges require startup review.'],
  clientImpact: { previousFiles: 1, proposedFiles: 2 },
}
async function mount(plan = basePlan, permissions = null) {
  const calls = []
  globalThis.fetch = async (url, options) => {
    calls.push({ url, method: options.method, data: options.body ? JSON.parse(options.body) : null })
    if (options.method === 'GET') return Response.json({ plans: [plan] })
    return Response.json({ ...plan, status: 'preparing' })
  }
  const app = createApp({ render: () => h(Updates, {
    serverId: 'production', server: { name: 'Production', loader: 'fabric', modpack: { projectId: 'fixture' } },
  }) })
  app.use(createPinia())
  app.use(i18n)
  app.runWithContext(() => {
    const auth = useAuthStore()
    auth.oidcMode = permissions !== null
    auth.user = permissions === null ? null : { permissions }
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
  await settle()
  button('Review staged updates').click()
  await settle()
  return calls
}

test('review does not start anything; conflicts require rebuilding before staging', async () => {
  const calls = await mount()
  assert.equal(calls.length, 1)
  assert.equal(calls[0].method, 'GET')
  assert.match(document.body.textContent, /Preserve local edit/)
  assert.match(document.body.textContent, /Client files: 1 → 2/)
  assert(button('Fork & apply to staging').disabled)
  await input(document.querySelector('#update-staging-name'), 'pack-test')
  await input(checkbox('I reviewed the changes'), true)
  assert(!button('Fork & apply to staging').disabled)
  await input(checkbox('Replace this local file'), true)
  assert(button('Fork & apply to staging').disabled)
  button('Review selected replacements').click()
  await settle()
  assert.deepEqual(calls.at(-1).data.replaceFiles, ['config/edited.json'])
  assert.equal(calls.at(-1).url, '/api/servers/production/modpack-updates')
})

test('stage sends the reviewed token and explicit copy acknowledgement', async () => {
  const calls = await mount()
  await input(document.querySelector('#update-staging-name'), 'pack-test')
  await input(checkbox('I reviewed the changes'), true)
  button('Fork & apply to staging').click()
  await settle()
  assert(calls.at(-1).url.endsWith('/stage'))
  assert.equal(calls.at(-1).data.reviewToken, 'immutable-review')
  assert.equal(calls.at(-1).data.acknowledgeRisks, true)
})

test('validation requires separate opt-in to code execution', async () => {
  const calls = await mount({ ...basePlan, status: 'staged', stagingId: 'isolated-test' })
  assert(button('Run staging startup check').disabled)
  await input(checkbox('I trust this pack'), true)
  assert(!button('Run staging startup check').disabled)
  button('Run staging startup check').click()
  await settle()
  assert(calls.at(-1).url.endsWith('/validate'))
  assert.equal(calls.at(-1).data.executePackCode, true)
  assert(!calls.some(call => call.url.endsWith('/start')))
})

test('promotion requires exact production name and uses backup-gated promotion endpoint', async () => {
  const calls = await mount({ ...basePlan, status: 'validated', stagingId: 'isolated-test' })
  const promote = button('Back up & promote reviewed files')
  assert(promote.disabled)
  await input(document.querySelector('#update-approve'), 'production')
  assert(promote.disabled)
  await input(document.querySelector('#update-approve'), 'Production')
  assert(!promote.disabled)
  promote.click()
  await settle()
  assert(calls.at(-1).url.endsWith('/promote'))
  assert.equal(calls.at(-1).data.confirmName, 'Production')
  assert(!calls.some(call => call.url.endsWith('/start')))
})

test('missing promotion capability keeps approval disabled', async () => {
  await mount({ ...basePlan, status: 'validated', stagingId: 'isolated-test' }, [
    'server.view', 'server.settings.read', 'files.read', 'server.start', 'server.stop', 'modpacks.install', 'backups.create',
  ])
  await input(document.querySelector('#update-approve'), 'Production')
  assert(button('Back up & promote reviewed files').disabled)
})

test('runtime migrations display the mismatch without a stage action', async () => {
  await mount({ ...basePlan, loaderChanges: [{ field: 'version', current: '1.21.1', proposed: '1.22' }] })
  assert.match(document.body.textContent, /Runtime migration required/)
  assert.equal(button('Fork & apply to staging'), undefined)
})

test('refresh requests cannot overlap other operations', async () => {
  await mount()
  let requests = 0
  let finish
  globalThis.fetch = () => {
    requests += 1
    return new Promise(resolve => { finish = resolve })
  }
  const refresh = button('Refresh status')
  refresh.click()
  refresh.click()
  await settle()
  assert.equal(requests, 1)
  assert(button('Prepare review').disabled)
  finish(Response.json({ plans: [basePlan] }))
  await settle()
  assert(!button('Prepare review').disabled)
})
