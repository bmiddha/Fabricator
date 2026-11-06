import assert from 'node:assert/strict'
import { after, test } from 'node:test'
import { fileURLToPath } from 'node:url'
import { createSSRApp } from 'vue'
import { createPinia } from 'pinia'
import { routerKey } from 'vue-router'
import { createServer } from 'vite'

// Use Vite's normal module resolution for the store's extensionless imports.
const vite = await createServer({
  root: fileURLToPath(new URL('../..', import.meta.url)),
  server: { middlewareMode: true, watch: null, hmr: false },
})
after(() => vite.close())
const { useServerStore } = await vite.ssrLoadModule('/src/stores/server.js')
const { useAuthStore } = await vite.ssrLoadModule('/src/stores/auth.js')

function stores() {
  const app = createSSRApp({})
  const pinia = createPinia()
  app.use(pinia)
  app.provide(routerKey, {})
  return app.runWithContext(() => ({
    store: useServerStore(pinia),
    auth: useAuthStore(pinia),
  }))
}

test('opening, editing and saving JSON5 and JSON preserves the exact text', async (t) => {
  const { store } = stores()
  store.currentServerId = 'srv1'
  for (const [path, content, edited] of [
    ['config/mod.json5', "{\n  // Keep me\n  unquoted: 'old',\n}\n", "{\n  // Keep me\n  unquoted: 'new',\n  values: [1, 2,],\n}\n"],
    ['config/Mod.JSON5', '{enabled: true,}', "{enabled: false, /* keep me */}\n"],
    ['config/mod.json', '{ "enabled": true }\n', '{ "enabled": false }\n'],
    ['server.properties', 'motd=old\n', 'motd=new\n'],
  ]) {
    const requests = []
    t.mock.method(globalThis, 'fetch', async (url, options) => {
      requests.push({ url, options })
      if (options.method === 'PUT') {
        assert.deepEqual(JSON.parse(options.body), { path, content: edited })
        return Response.json({ success: true })
      }
      assert.equal(new URL(url, 'http://localhost').searchParams.get('path'), path)
      return Response.json({ path, content })
    })
    await store.openFile(path)
    assert.equal(store.fileEditor.path, path)
    assert.equal(store.fileEditor.content, content)
    assert.equal(store.hasFileChanges, false)
    store.fileEditor.content = edited
    assert.equal(store.hasFileChanges, true)
    await store.saveFile()
    assert.equal(requests.length, 2)
    assert.equal(store.fileEditor.error, null)
    assert.equal(store.fileEditor.originalContent, edited)
    assert.equal(store.hasFileChanges, false)
    store.closeFile()
    t.mock.restoreAll()
  }
})

test('unsupported extensions and missing read permission still prevent opening', async (t) => {
  const { store, auth } = stores()
  store.currentServerId = 'srv1'
  const fetch = t.mock.method(globalThis, 'fetch')
  await store.openFile('mods/mod.jar')
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(store.fileEditor.path, null)
  auth.oidcMode = true
  auth.user = { permissions: [] }
  await store.openFile('config/mod.json5')
  assert.equal(fetch.mock.callCount(), 0)
  assert.equal(store.fileEditor.path, null)
})
