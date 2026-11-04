/**
 * The generated MCP client configuration — the SINGLE place its shape lives.
 *
 * The fork installs directly from main, independently of upstream's package
 * registry identity. Keep the same launch command in mcp/README.md.
 *
 * The token always rides in `env`, never in `args`: a command line is readable
 * by every process on the machine and lands in shell history. The panel URL
 * comes from the editable field in the UI.
 */
export const MCP_CLIENT_LAUNCH = {
  command: 'uvx',
  args: [
    '--from',
    'git+https://github.com/bmiddha/Fabricator@main#subdirectory=mcp',
    'fabricator-panel-mcp'
  ]
}

export const MCP_TOKEN_PLACEHOLDER = 'YOUR_TOKEN_HERE'

export function buildMcpClientConfig({ url, token } = {}) {
  return {
    mcpServers: {
      fabricator: {
        command: MCP_CLIENT_LAUNCH.command,
        args: [...MCP_CLIENT_LAUNCH.args],
        env: {
          FABRICATOR_URL: url || '',
          FABRICATOR_TOKEN: token || MCP_TOKEN_PLACEHOLDER
        }
      }
    }
  }
}
