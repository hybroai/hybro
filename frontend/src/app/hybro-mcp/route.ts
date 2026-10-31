import { parseMcpStatus, type McpStatus } from '@/lib/mcp'

export const dynamic = 'force-dynamic'

/** Public connection status only. Never forwards credentials or MCP tool calls. */
export async function GET(): Promise<Response> {
  let status: McpStatus = 'unavailable'
  try {
    // Fixed loopback destination: MCP shares the container (or the host) with this server.
    const response = await fetch('http://127.0.0.1:8001/health', {
      cache: 'no-store',
      redirect: 'error',
      signal: AbortSignal.timeout(4000)
    })
    if (response.ok) status = parseMcpStatus(await response.json())
  } catch {
    // Keep upstream errors, addresses and responses out of the public endpoint.
  }
  return Response.json({ service: 'hybro-mcp', status }, { headers: { 'Cache-Control': 'no-store' } })
}
