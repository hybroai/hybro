/** This instance's MCP address: the image serves MCP at `/mcp` on the page's origin. */
export function mcpUrl(pageOrigin: string): string {
  return `${pageOrigin}/mcp`
}

export function buildMcpConfig(url: string): string {
  return JSON.stringify({
    mcpServers: { hybro: { type: 'http', url } }
  }, null, 2)
}

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
