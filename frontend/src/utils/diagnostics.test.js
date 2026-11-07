import assert from 'node:assert/strict'
import { test } from 'node:test'
import { metricValue, reportUrl, trend } from './diagnostics.js'

test('only canonical official report references are navigable', () => {
  assert.equal(reportUrl('https://spark.lucko.me/abc123'), 'https://spark.lucko.me/abc123')
  for (const value of ['javascript:alert(1)', 'https://spark.lucko.me.evil/abc123', 'https://spark.lucko.me/abc123?x=y', null]) {
    assert.equal(reportUrl(value), null)
  }
})
test('missing samples, restarts and sampling gaps never draw fabricated continuity', () => {
  const data = trend([
    { ts: 0, run: 'a', tps: 20 }, { ts: 30, run: 'a', tps: 18 },
    { ts: 60, run: 'a' }, { ts: 90, run: 'a', tps: 15 },
    { ts: 120, run: 'b', tps: 20 }, { ts: 240, run: 'b', tps: 10 },
  ], 'tps', 0, 240)
  assert.deepEqual(data.paths.map(path => path.length), [2, 1, 1, 1])
  assert.equal(data.min, 10)
  assert.equal(data.max, 20)
  assert.equal(data.paths.at(-1)[0].x, 688)
})
test('zero is real, stale latest values are unavailable, and units are binary GiB', () => {
  assert.equal(trend([{ ts: 0, gcTimeMs: 0 }, { ts: 30 }], 'gcTimeMs', 0, 30).latest, null)
  assert.equal(trend([{ ts: 0, gcTimeMs: 0 }], 'gcTimeMs', 0, 30).latest, 0)
  assert.equal(trend([{ ts: 0, tps: 20 }], 'tps', 0, 150).latest, null)
  assert.equal(metricValue(1024 ** 3, 'GiB'), '1.00')
  assert.equal(metricValue(undefined, 'GiB'), null)
})
