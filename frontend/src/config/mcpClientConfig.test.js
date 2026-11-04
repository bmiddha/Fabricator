import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import { buildMcpClientConfig } from './mcpClientConfig.js'

test('MCP installs fork main using a declared entry point and keeps credentials out of arguments', () => {
  const config = buildMcpClientConfig({ url: 'https://panel.example', token: 'test-token' })
    .mcpServers.fabricator
  assert.equal(config.command, 'uvx')
  assert.deepEqual(config.args, [
    '--from',
    'git+https://github.com/bmiddha/Fabricator@main#subdirectory=mcp',
    'fabricator-panel-mcp'
  ])
  const manifest = readFileSync(new URL('../../../mcp/pyproject.toml', import.meta.url), 'utf8')
  assert.match(manifest, /^fabricator-panel-mcp = /m)
  assert.equal(config.env.FABRICATOR_TOKEN, 'test-token')
  assert.equal(config.env.FABRICATOR_URL, 'https://panel.example')
  assert.ok(!config.args.includes('test-token'))
})
