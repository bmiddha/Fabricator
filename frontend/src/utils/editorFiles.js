import { StreamLanguage } from '@codemirror/language'
import { json, jsonParseLinter } from '@codemirror/lang-json'
import { yaml } from '@codemirror/lang-yaml'
import { javascript } from '@codemirror/legacy-modes/mode/javascript'
import { properties } from '@codemirror/legacy-modes/mode/properties'
import { toml } from '@codemirror/legacy-modes/mode/toml'
import JSON5 from 'json5'

function extensionForPath(path) {
  return typeof path === 'string' ? path.toLowerCase().split('.').pop() : ''
}

export function languageForPath(path) {
  switch (extensionForPath(path)) {
    case 'json':
      return json()
    case 'json5':
      // Reuse JavaScript tokenization for comments, unquoted keys and strings;
      // validation below still enforces JSON5 rather than JavaScript syntax.
      return StreamLanguage.define(javascript)
    case 'yml':
    case 'yaml':
      return yaml()
    case 'properties':
    case 'cfg':
    case 'conf':
      return StreamLanguage.define(properties)
    case 'toml':
      return StreamLanguage.define(toml)
    default:
      return []
  }
}

function json5Linter(view) {
  try {
    JSON5.parse(view.state.doc.toString())
    return []
  } catch (error) {
    if (!(error instanceof SyntaxError)) throw error
    const line = view.state.doc.line(error.lineNumber)
    const from = Math.min(line.to, line.from + Math.max(0, error.columnNumber - 1))
    return [{
      from,
      to: Math.min(from + 1, view.state.doc.length),
      severity: 'error',
      message: error.message,
    }]
  }
}

const jsonLinter = jsonParseLinter()

export function parserLinterForPath(path) {
  switch (extensionForPath(path)) {
    case 'json':
      return jsonLinter
    case 'json5':
      return json5Linter
    default:
      return null
  }
}
