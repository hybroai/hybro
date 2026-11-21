import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '../../utils/test-utils'
import userEvent from '@testing-library/user-event'
import { ConnectPage } from '@/components/connect/connect-page'
import { buildMcpConfig } from '@/lib/mcp'

// A dev instance publishes MCP on its own port; the page must show that one.
const MCP_URL = `${window.location.origin}/mcp`
const MCP_CONFIG = buildMcpConfig(MCP_URL, '<source token>')
vi.mock('@/lib/config', () => ({ publicConfig: () => ({ api_base_url: 'https://hybro.example', api_prefix: '/api/v1' }) }))
const push = vi.fn()
vi.mock('next/navigation', () => ({ useRouter: () => ({ push }) }))

beforeEach(() => {
  push.mockClear()
  vi.stubGlobal('fetch', vi.fn())
  Object.defineProperty(navigator, 'clipboard', { configurable: true, value: { writeText: vi.fn().mockResolvedValue(undefined) } })
})

afterEach(() => {
  cleanup()
  vi.unstubAllGlobals()
})

// The page reads only the token requirement, for its banner.
const fetchedUrls = () => vi.mocked(fetch).mock.calls.map(([url]) => String(url))
const onlyReadsTheRequirement = () => {
  for (const url of fetchedUrls()) expect(url).toMatch(/\/api\/v1\/sources\/token-requirement$/)
}

const lastCopied = () => String(vi.mocked(navigator.clipboard.writeText).mock.lastCall?.[0] ?? '')

describe('Access page', () => {
  it('shows the MCP address, assistant snippets, and features without steps or scope', () => {
    render(<ConnectPage kind="mcp" />)
    expect(screen.getByRole('heading', { level: 1, name: 'Access' })).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'MCP' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText(MCP_URL)).toBeInTheDocument()
    expect(screen.getByRole('tab', { name: 'Claude Code' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByLabelText('Claude Code command')).toHaveTextContent(`claude mcp add --transport http hybro ${MCP_URL} --header 'Authorization: Bearer <source token>'`)
    expect(screen.getByRole('heading', { name: 'Features' })).toBeInTheDocument()
    for (const tool of ['find_agents', 'list_subnets', 'get_agent', 'send_message', 'check_session']) {
      expect(screen.getByText(tool)).toBeInTheDocument()
    }
    expect(screen.queryByText(/send_agent_message/)).toBeNull()
    expect(screen.queryByText(/Open a terminal|Try asking|group_id|scope/i)).toBeNull()
    onlyReadsTheRequirement()
  })

  it('switches to the API route from the top tab', async () => {
    render(<ConnectPage kind="mcp" />)
    await userEvent.click(screen.getByRole('tab', { name: 'API' }))
    expect(push).toHaveBeenCalledWith('/connect/api', { scroll: false })
  })

  it('copies the address and an assistant configuration', async () => {
    render(<ConnectPage kind="mcp" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy address' }))
    await screen.findByText('Address copied')
    expect(lastCopied()).toBe(MCP_URL)
    await userEvent.click(screen.getByRole('tab', { name: 'Other' }))
    expect(screen.getByLabelText('Other configuration').textContent).toBe(MCP_CONFIG)
    fireEvent.click(screen.getByRole('button', { name: 'Copy configuration' }))
    await screen.findByText('Configuration copied')
    expect(lastCopied()).toBe(MCP_CONFIG)
  })

  it('offers manual selection without claiming clipboard success', async () => {
    vi.mocked(navigator.clipboard.writeText).mockRejectedValue(new Error('Denied'))
    render(<ConnectPage kind="mcp" />)
    fireEvent.click(screen.getByRole('button', { name: 'Copy command' }))
    expect(await screen.findByRole('alert')).toHaveTextContent('Select and copy the text manually')
    expect(screen.getByLabelText('Claude Code command')).toHaveFocus()
    expect(screen.queryByText('Command copied')).not.toBeInTheDocument()
  })

  it('shows the API tab with a token placeholder and no scope', async () => {
    render(<ConnectPage kind="api" />)
    expect(screen.getByRole('tab', { name: 'API' })).toHaveAttribute('aria-selected', 'true')
    expect(screen.getByText('https://hybro.example')).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Copy discovery request' }))
    await screen.findByText('Discovery request copied')
    expect(lastCopied()).toContain('https://hybro.example/api/v1/agents/discovery')
    expect(lastCopied()).toContain("--header 'Authorization: Bearer <source token>'")
    expect(lastCopied()).not.toMatch(/hybro_src_|access_token|group_id/i)
    fireEvent.click(screen.getByRole('button', { name: 'Copy search request' }))
    await screen.findByText('Search request copied')
    expect(lastCopied()).toContain('https://hybro.example/api/v1/agents/search')
    expect(lastCopied()).toContain("--header 'Authorization: Bearer <source token>'")
    expect(lastCopied()).not.toMatch(/hybro_src_|access_token|group_id/i)
    expect(screen.getByText('GET /api/v1/agents/groups')).toBeInTheDocument()
    expect(screen.getByLabelText('Message request')).toHaveTextContent('/api/v1/sessions/messages')
    expect(screen.getByLabelText('Message request')).not.toHaveTextContent('/api/v1/agents/messages')
    expect(screen.getByText('POST /api/v1/sessions/messages')).toBeInTheDocument()
    expect(screen.getByText('GET /api/v1/sessions/{session_id}')).toBeInTheDocument()
    expect(screen.queryByText('POST /api/v1/agents/messages')).not.toBeInTheDocument()
    expect(screen.getByRole('heading', { name: 'Features' })).toBeInTheDocument()
    onlyReadsTheRequirement()
  })
})
