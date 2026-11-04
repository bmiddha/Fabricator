import assert from 'node:assert/strict'
import test from 'node:test'
import { EditorState } from '@codemirror/state'
import { ensureSyntaxTree } from '@codemirror/language'
import { languageForPath, parserLinterForPath } from './editorFiles.js'

function editor(path, content) {
  return {
    state: EditorState.create({ doc: content, extensions: languageForPath(path) }),
  }
}

const json5 = `{
  // Keep the mod author's comments.
  unquoted: 'hello',
  /* Nested arrays can have trailing commas too. */
  values: [0x10, +2, .5, Infinity, NaN,],
}`

test('JSON5 uses comment-aware highlighting and accepts JSON5 without changing it', () => {
  const view = editor('config/Mod.JSON5', json5)
  const tree = ensureSyntaxTree(view.state, view.state.doc.length, 500)
  assert.ok(tree.toString().includes('comment'))
  assert.deepEqual(parserLinterForPath('config/Mod.JSON5')(view), [])
  assert.equal(view.state.doc.toString(), json5)
})

test('JSON5 accepts strict JSON, scalar values, and multiline strings', () => {
  for (const content of ['{"enabled":true}', 'null', "'hello'", "'hello\\\nworld'"]) {
    const view = editor('mod.json5', content)
    assert.deepEqual(parserLinterForPath('mod.json5')(view), [], content)
  }
})

test('JSON5 diagnostics reject broken syntax and JavaScript-only constructs', () => {
  for (const content of [
    '', '{', '{value: }', '{value: 1,,}', '{value: undefined}',
    '{value: () => 1}', '{value: `template`}', '{value: 1n}',
    '// comment\n{\n value: @\n}', '{emoji: "😀", value: @}',
  ]) {
    const view = editor('mod.json5', content)
    const [diagnostic] = parserLinterForPath('mod.json5')(view)
    assert.equal(diagnostic.severity, 'error', content)
    assert.match(diagnostic.message, /JSON5/)
    assert.ok(diagnostic.from >= 0)
    assert.ok(diagnostic.to >= diagnostic.from)
    assert.ok(diagnostic.to <= view.state.doc.length)
    assert.equal(view.state.doc.toString(), content)
  }
})

test('JSON5 diagnostics locate errors on the correct line and refresh after edits', () => {
  const content = '{\n  value: @\n}'
  const view = editor('mod.json5', content)
  const lint = parserLinterForPath('mod.json5')
  assert.equal(lint(view)[0].from, content.indexOf('@'))
  view.state = view.state.update({ changes: { from: content.indexOf('@'), to: content.indexOf('@') + 1, insert: '1' } }).state
  assert.deepEqual(lint(view), [])
})

test('JSON validation remains strict when switching from JSON5', () => {
  const view = editor('mod.json5', json5)
  assert.deepEqual(parserLinterForPath('mod.json5')(view), [])
  assert.equal(parserLinterForPath('mod.JSON')(view)[0].severity, 'error')
  const strict = '{ "enabled": true, "values": [1, 2] }\n'
  assert.deepEqual(parserLinterForPath('mod.json')(editor('mod.json', strict)), [])
  for (const content of ['{"a":1,}', '{"a":1 // comment\n}', '{a:1}', "{'a':1}"]) {
    assert.equal(parserLinterForPath('mod.json')(editor('mod.json', content))[0].severity, 'error')
  }
})

test('other editor languages and plaintext keep their existing selection', () => {
  for (const path of ['server.properties', 'mod.cfg', 'mod.conf', 'mod.toml', 'mod.yaml', 'mod.yml']) {
    assert.ok(editor(path, 'value = 1').state.facet(EditorState.languageData).length, path)
    assert.equal(parserLinterForPath(path), null)
  }
  for (const path of ['notes.txt', 'latest.log', 'README.md', '', null]) {
    assert.deepEqual(languageForPath(path), [])
    assert.equal(parserLinterForPath(path), null)
  }
})
