import assert from 'node:assert/strict'
import test from 'node:test'
import { canVisit, ROUTE_PERMISSIONS, safeReturn } from './permissions.js'

test('route entry follows capabilities rather than role names', () => {
  const auth = { can: (permission, id) => id === 'a' && ['server.view', 'server.logs.read'].includes(permission) }
  assert.equal(canVisit(auth, 'ServerOverview', 'a'), true)
  assert.equal(canVisit(auth, 'ServerConsole', 'a'), true)
  assert.equal(canVisit(auth, 'ServerFiles', 'a'), false)
  assert.equal(canVisit(auth, 'ServerConsole', 'b'), false)
})

test('partial custom grants expose their action surface', () => {
  for (const [route, permissions] of Object.entries(ROUTE_PERMISSIONS)) {
    for (const grant of permissions) {
      assert.equal(canVisit({ can: permission => permission === grant }, route, 'a'), true, `${route}: ${grant}`)
    }
    assert.equal(canVisit({ can: () => false }, route, 'a'), false, route)
  }
  assert.equal(canVisit({ can: () => false }, 'GlobalSettings'), true)
})

test('safe return paths reject external, encoded and login-loop destinations', () => {
  for (const path of [
    null, undefined, {}, '', 'https://other.example', '//other.example',
    '/\\other', '/%5cother', '/%2fother', '/%0aother', '/a b', '/a\tb',
    '/api/auth/oidc/login', '/%61pi/auth/status', '/login', '/setup', '/bad%ff',
  ]) {
    assert.equal(safeReturn(path), '/', String(path))
  }
  for (const path of ['/server/a/overview', '/settings/mcp', '/server/a/files?path=server.properties']) {
    assert.equal(safeReturn(path), path)
  }
})
