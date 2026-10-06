export const MCP_URL = 'http://127.0.0.1:8001/mcp'
export const MCP_CONFIG = JSON.stringify({
  mcpServers: { hybro: { type: 'http', url: MCP_URL } }
}, null, 2)

export type McpStatus = 'ready' | 'unavailable' | 'unsupported_auth'

export function parseMcpStatus(value: unknown): McpStatus {
  if (typeof value !== 'object' || value === null || !('service' in value) || value.service !== 'hybro-mcp') {
    return 'unavailable'
  }
  if ('status' in value && (value.status === 'ready' || value.status === 'unsupported_auth')) {
    return value.status
  }
  return 'unavailable'
}
