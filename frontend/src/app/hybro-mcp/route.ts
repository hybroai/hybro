import { parseMcpStatus, type McpStatus } from '@/lib/mcp'

export const dynamic = 'force-dynamic'

/** Public connection status only. Never forwards credentials or MCP tool calls. */
export async function GET(): Promise<Response> {
  let status: McpStatus = 'unavailable'
  // Fixed Compose transport metadata, not a user-controlled proxy destination.
  const origin = process.env.HYBRO_MCP_URL === 'http://mcp:8001'
    ? 'http://mcp:8001'
    : 'http://127.0.0.1:8001'
  try {
    const response = await fetch(`${origin}/health`, {
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
