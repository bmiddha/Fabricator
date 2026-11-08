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
  configFile: false, root: fileURLToPath(new URL('../../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
  optimizeDeps: { noDiscovery: true, include: [] },
  plugins: [{
    name: 'recovery-dom-components',
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
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')
const Modal = (await vite.ssrLoadModule('/src/components/modals/S3RecoveryModal.vue')).default
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
  for (let i = 0; i < 8; i++) {
    await new Promise(resolve => setTimeout(resolve, 0))
    await nextTick()
  }
}
const button = name => [...document.querySelectorAll('button')].find(b => b.textContent.trim() === name)
function input(label, value) {
  const element = [...document.querySelectorAll('label')].find(l => l.textContent.trim().startsWith(label))
  const field = document.getElementById(element.htmlFor)
  field.value = value
  field.dispatchEvent(new dom.window.Event('input', { bubbles: true }))
}
function mount(permissions = ['servers.import', 'servers.create', 'server.view']) {
  const app = createApp({ render: () => h(Modal, { show: true }) })
  app.use(createPinia())
  app.use(i18n)
  app.provide(routerKey, {})
  app.runWithContext(() => {
    const auth = useAuthStore()
    auth.oidcMode = true
    auth.user = { permissions }
  })
  const root = document.createElement('div')
  document.body.append(root)
  app.mount(root)
  apps.push(app)
}
const snapshot = { sourceServerId: 'source', snapshotId: 'snapshot', createdAt: '2026-10-01',
  objectKey: 'migration/fabricator/source/snapshot/server.tar', sizeBytes: 123, loader: 'vanilla', version: '1.21.1' }
const preview = {
  previewId: 'a'.repeat(32), name: 'Recovered', sourceServerId: 'source', snapshotId: 'snapshot',
  loader: 'vanilla', version: '1.21.1', archiveBytes: 123, expandedBytes: 456,
  java: { required_java: 21 }, worlds: ['island'], sha256: 'b'.repeat(64),
  removedAdministrativeFiles: 4, warnings: ['Review trusted code and listeners'],
  secrets: { suspectedCount: 1, suspectedFiles: ['config/plugin.yml'], scanTruncated: false },
}

test('recovery is hidden unless both global import and creation are allowed', async () => {
  globalThis.fetch = () => { throw new Error('No profile or bucket access') }
  mount(['servers.import'])
  await settle()
  assert.equal(button('Discover Fabricator backups'), undefined)
})

test('discovery, verified preview and explicit secret/name confirmation create only a stopped instance', async () => {
  const calls = []
  globalThis.fetch = async (url, options) => {
    const data = options.body ? JSON.parse(options.body) : null
    calls.push({ url, data })
    if (url.endsWith('/discover')) return Response.json({ snapshots: [snapshot] })
    if (url.endsWith('/previews')) return Response.json({ jobId: 'inspect' })
    if (url.endsWith('/jobs/inspect')) return Response.json({ phase: 'done', result: preview })
    if (url.endsWith('/imports')) return Response.json({ jobId: 'import' })
    if (url.endsWith('/jobs/import')) return Response.json({ phase: 'done', result: { serverId: 'fresh', name: 'Recovered', status: 'stopped' } })
    if (url === '/api/servers') return Response.json([])
    throw new Error(`Unexpected request ${url}`)
  }
  mount()
  input('Bucket', 'recovery-tests')
  input('Original backup prefix', 'migration')
  await settle()
  button('Discover Fabricator backups').click()
  await settle()
  const radio = document.querySelector('input[type=radio]')
  radio.checked = true
  radio.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  input('New instance name', 'Recovered')
  await settle()
  button('Download & inspect snapshot').click()
  await settle()
  assert.match(document.body.textContent, /Java major21|Java major\s+21/)
  assert(document.body.textContent.includes(preview.sha256))
  assert(document.body.textContent.includes('config/plugin.yml'))
  assert(button('Create stopped instance').disabled)
  input('Type', 'Recovered')
  const checks = [...document.querySelectorAll('input[type=checkbox]')]
  checks[0].checked = true
  checks[0].dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  assert(button('Create stopped instance').disabled)
  checks[1].checked = true
  checks[1].dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  await settle()
  assert(!button('Create stopped instance').disabled)
  button('Create stopped instance').click()
  await settle()
  assert.match(document.body.textContent, /Recovered recovered and left stopped/)
  const imported = calls.find(c => c.url.endsWith('/imports')).data
  assert.deepEqual(imported, { previewId: preview.previewId, confirmName: 'Recovered',
    acknowledgeRisks: true, retainEmbeddedSecrets: true })
  assert(!calls.some(c => c.url.endsWith('/start')))
  assert.equal(calls.find(c => c.url.endsWith('/discover')).data.destination.profile, 'default')
})

test('a saved ready preview resumes without rediscovering or downloading and requires fresh confirmation', async () => {
  const calls = []
  sessionStorage.setItem('fabricator-s3-recovery', JSON.stringify({ previewId: preview.previewId }))
  globalThis.fetch = async url => {
    calls.push(url)
    return Response.json(preview)
  }
  mount()
  await settle()
  assert.deepEqual(calls, [`/api/s3-recovery/previews/${preview.previewId}`])
  assert(button('Create stopped instance').disabled)
})

test('another owner or expired preview cannot be automatically retried', async () => {
  const calls = []
  sessionStorage.setItem('fabricator-s3-recovery', JSON.stringify({ previewId: preview.previewId }))
  globalThis.fetch = async url => {
    calls.push(url)
    return Response.json({ error: 'Recovery preview not found' }, { status: 404 })
  }
  mount()
  await settle()
  assert.match(document.body.textContent, /check the instance list before retrying/i)
  assert.equal(calls.length, 1)
  assert.equal(sessionStorage.getItem('fabricator-s3-recovery'), null)
})

test('lost creation submission disables blind retry and persists uncertain state', async () => {
  let submissions = 0
  sessionStorage.setItem('fabricator-s3-recovery', JSON.stringify({ previewId: preview.previewId }))
  globalThis.fetch = async url => {
    if (url.endsWith('/imports')) {
      submissions++
      throw new TypeError('fixture network connection lost')
    }
    return Response.json(preview)
  }
  mount()
  await settle()
  input('Type', 'Recovered')
  for (const check of document.querySelectorAll('input[type=checkbox]')) {
    check.checked = true
    check.dispatchEvent(new dom.window.Event('change', { bubbles: true }))
  }
  await settle()
  button('Create stopped instance').click()
  await settle()
  assert(button('Create stopped instance').disabled)
  assert.match(document.body.textContent, /check the instance list before retrying/i)
  assert.equal(JSON.parse(sessionStorage.getItem('fabricator-s3-recovery')).uncertain, true)
  assert.equal(submissions, 1)
})
