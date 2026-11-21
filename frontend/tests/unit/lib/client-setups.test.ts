import { describe, expect, it } from 'vitest'
import { clientSetups, TOKEN_PLACEHOLDER } from '@/components/connect/client-setups'
import { buildMcpConfig } from '@/lib/mcp'

const url = 'http://127.0.0.1:8001/mcp'
const TOKEN = 'hybro_src_fake-token-for-tests'

describe('clientSetups', () => {
  it('gives every supported assistant a snippet with the address and the token header', () => {
    const setups = clientSetups(url)
    expect(setups.map(setup => setup.label)).toEqual(['Claude Code', 'Cursor', 'Codex', 'VS Code', 'Other'])
    for (const setup of setups) {
      expect(setup.snippet).toContain(url)
      expect(setup.snippet).toContain(`Bearer ${TOKEN_PLACEHOLDER}`)
    }
  })

  it('puts a given token in every snippet', () => {
    for (const setup of clientSetups(url, TOKEN)) {
      expect(setup.snippet).toContain(`Bearer ${TOKEN}`)
      expect(setup.snippet).not.toContain(TOKEN_PLACEHOLDER)
    }
  })

  it('uses each client\'s own header syntax', () => {
    const setups = Object.fromEntries(clientSetups(url, TOKEN).map(setup => [setup.id, setup]))
    expect(setups['claude-code']).toMatchObject({
      kind: 'command',
      snippet: `claude mcp add --transport http hybro ${url} --header 'Authorization: Bearer ${TOKEN}'`,
    })
    expect(JSON.parse(setups.cursor.snippet)).toEqual({ mcpServers: { hybro: { url, headers: { Authorization: `Bearer ${TOKEN}` } } } })
    expect(setups.codex.snippet).toBe(`[mcp_servers.hybro]\nurl = "${url}"\nhttp_headers = { Authorization = "Bearer ${TOKEN}" }`)
    expect(JSON.parse(setups.vscode.snippet)).toEqual({ servers: { hybro: { type: 'http', url, headers: { Authorization: `Bearer ${TOKEN}` } } } })
    expect(setups.other.snippet).toBe(buildMcpConfig(url, TOKEN))
    expect(JSON.parse(setups.other.snippet)).toEqual({ mcpServers: { hybro: { type: 'http', url, headers: { Authorization: `Bearer ${TOKEN}` } } } })
  })
})
