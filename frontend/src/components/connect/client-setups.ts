import { buildMcpConfig } from '@/lib/mcp'

/** Stands for the token in a snippet shown before one is created. */
export const TOKEN_PLACEHOLDER = '<source token>'

export interface ClientSetup {
  id: string
  label: string
  /** The text the user runs or pastes. */
  snippet: string
  kind: 'command' | 'configuration'
  /** Where the snippet goes. */
  hint: string
}

/** Each assistant's MCP setup, sending the source token as `Authorization: Bearer`. */
export function clientSetups(url: string, token: string = TOKEN_PLACEHOLDER): ClientSetup[] {
  const authorization = `Bearer ${token}`
  const headers = { Authorization: authorization }
  return [
    { id: 'claude-code', label: 'Claude Code', snippet: `claude mcp add --transport http hybro ${url} --header 'Authorization: ${authorization}'`, kind: 'command', hint: 'Run in a terminal' },
    { id: 'cursor', label: 'Cursor', snippet: JSON.stringify({ mcpServers: { hybro: { url, headers } } }, null, 2), kind: 'configuration', hint: 'Add to .cursor/mcp.json' },
    { id: 'codex', label: 'Codex', snippet: `[mcp_servers.hybro]\nurl = "${url}"\nhttp_headers = { Authorization = "${authorization}" }`, kind: 'configuration', hint: 'Add to ~/.codex/config.toml' },
    { id: 'vscode', label: 'VS Code', snippet: JSON.stringify({ servers: { hybro: { type: 'http', url, headers } } }, null, 2), kind: 'configuration', hint: 'Add to .vscode/mcp.json' },
    { id: 'other', label: 'Other', snippet: buildMcpConfig(url, token), kind: 'configuration', hint: 'Standard mcpServers configuration' },
  ]
}
