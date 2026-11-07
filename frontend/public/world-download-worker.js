// A one-use local URL bridges cookie-free POST redemption to native streaming
// downloads. Neither a bearer token nor the archive is persisted or cached.
const pending = new Map()
self.addEventListener('install', () => self.skipWaiting())
self.addEventListener('activate', event => event.waitUntil(self.clients.claim()))
self.addEventListener('message', event => {
  const source = event.source
  const data = event.data
  const port = event.ports[0]
  if (!source || new URL(source.url).origin !== self.location.origin
      || new URL(source.url).pathname !== '/world-share' || !port
      || !data || !/^[a-f0-9]{64}\.[A-Za-z0-9_-]{43}$/.test(data.token)
      || !/^\/world-download-stream\/[a-f0-9-]{36}$/.test(data.path)) return
  if (pending.size >= 4) { port.postMessage({ error: 'busy' }); return }
  pending.set(data.path, { token: data.token, port })
  setTimeout(() => pending.delete(data.path), 60000)
  port.postMessage({ ready: true })
})
self.addEventListener('fetch', event => {
  const url = new URL(event.request.url)
  if (url.origin !== self.location.origin || !url.pathname.startsWith('/world-download-stream/')) return
  const item = pending.get(url.pathname)
  pending.delete(url.pathname)
  if (!item || event.request.method !== 'GET') {
    event.respondWith(new Response('Download expired; reopen the share link.', { status: 404 }))
    return
  }
  event.respondWith((async () => {
    try {
      const response = await fetch('/api/world-shares/download', {
        method: 'POST', credentials: 'omit', cache: 'no-store', referrerPolicy: 'no-referrer',
        headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: item.token }),
      })
      item.token = ''
      if (!response.ok) {
        item.port.postMessage({ error: response.status, retry: response.headers.get('Retry-After') })
        return new Response('Download unavailable; return to the share page to retry.', { status: response.status })
      }
      item.port.postMessage({ started: true })
      const headers = new Headers(response.headers)
      headers.set('Cache-Control', 'no-store')
      return new Response(response.body, { status: 200, headers })
    } catch (_) {
      item.token = ''
      item.port.postMessage({ error: 'network' })
      return new Response('Download failed; return to the share page to retry.', { status: 503 })
    }
  })())
})
