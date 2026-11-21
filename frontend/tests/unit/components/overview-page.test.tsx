import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '../../utils/test-utils'
import { placeholderAgent } from '@/lib/network-agent'

vi.mock('@/lib/auth', () => ({ useUser: () => ({ user: { id: 'u' }, isLoaded: true, isSignedIn: true }) }))
const refreshGateway = vi.hoisted(() => vi.fn())
const refetchSetup = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/useMcpStatus', () => ({ useMcpStatus: () => ({ status: 'ready', refresh: refreshGateway }) }))
vi.mock('@/hooks/useSetupStatus', () => ({ useSetupStatus: () => ({ data: undefined, isSuccess: false, refetch: refetchSetup }) }))
const history = vi.hoisted(() => ({ isError: false, refetch: vi.fn() }))
const refreshNetworks = vi.hoisted(() => vi.fn())
vi.mock('@/hooks/useRoomHistory', () => ({ useRoomHistory: () => ({ refetch: history.refetch, data: { items: [{ conversation_id: 'r1', title: 'Trip plan', last_activity_at: '2026-10-10T00:00:00Z', is_pinned: false, pin_order: null, status: 'awaiting_input' }] }, dataUpdatedAt: Date.parse('2026-10-10T10:42:00Z'), isLoading: false, isError: history.isError }) }))
vi.mock('@/hooks/useSessions', () => ({ useSessions: () => ({ data: { sessions: [{ session_id: 's1', client_label: 'Buyer agent', client: { client_id: 'no_token', name: 'No token', has_token: false }, agents: [{ agent_id: 'a', name: 'writer' }], title: 'Generate a travel plan', state: 'waiting_on_owner', paused_agent_id: 'a', conversation_id: 'c1', opened_at: '2026-10-10T00:00:00Z', last_activity_at: '2026-10-10T00:00:00Z', closed_at: null }] }, isLoading: false, isError: false, refetch: vi.fn() }) }))
vi.mock('@/hooks/useNetworks', () => ({
  useNetworks: () => ({ networks: [], agents: [{ ...placeholderAgent('a', '', 'active'), name: 'writer', roomCalls: 2 }], loading: false, agentsLoading: false, error: null, agentsError: null, refresh: refreshNetworks }),
}))

import { OverviewPage } from '@/components/overview/overview-page'

afterEach(() => {
  cleanup()
  vi.clearAllMocks()
  history.isError = false
})

describe('OverviewPage', () => {
  it('has a visible title with the main actions', () => {
    render(<OverviewPage />)
    expect(screen.getByRole('heading', { level: 1, name: 'Overview' })).not.toHaveClass('sr-only')
    expect(screen.getByRole('link', { name: 'New chat' })).toHaveAttribute('href', '/chat')
    expect(screen.getByRole('link', { name: 'Add agent' })).toHaveAttribute('href', '/network/agents/new')
  })

  it('shows clickable tiles in the side-menu order, Chats before Inbox', () => {
    render(<OverviewPage />)
    const tiles = screen.getAllByRole('link', { name: /^(Chats|Inbox|Agents|MCP gateway),/ })
    expect(tiles.map(tile => tile.getAttribute('aria-label')?.split(',')[0])).toEqual(['Chats', 'Inbox', 'Agents', 'MCP gateway'])
    expect(tiles[0]).toHaveAttribute('href', '/chat')
    expect(tiles[0]).toHaveTextContent('1 need input')
    expect(tiles[1]).toHaveAttribute('href', '/sessions?status=waiting_on_owner')
    expect(tiles[1]).toHaveTextContent('1 need you')
    expect(tiles[2]).toHaveAttribute('href', '/network')
    expect(tiles[3]).toHaveAttribute('href', '/connect/mcp')
  })

  it('colors each tile number like its side-menu icon and keeps the gateway tile short', () => {
    render(<OverviewPage />)
    const number = (label: string) => screen.getByRole('link', { name: new RegExp(`^${label},`) }).querySelector('.text-2xl')
    expect(number('Chats')).toHaveClass('text-icon-create')
    expect(number('Inbox')).toHaveClass('text-icon-create')
    expect(number('Agents')).toHaveClass('text-icon-network')
    expect(number('MCP gateway')).toHaveClass('text-hybro-bro')
    expect(number('MCP gateway')).toHaveTextContent('Ready')
    expect(screen.queryByText('Clients can connect')).toBeNull()
  })

  it('stacks the sections line by line with Most-called agents last', () => {
    render(<OverviewPage />)
    expect(screen.getAllByRole('heading', { level: 2 }).map(heading => heading.textContent)).toEqual([
      'Recent chats',
      'Inbox',
      'Network',
      'Most-called agents',
    ])
    expect(screen.queryByText('Needs attention')).toBeNull()
    expect(screen.queryByText(/Updated /)).toBeNull()
  })

  it('lists sessions and chats with the same row design', () => {
    render(<OverviewPage />)
    const session = screen.getByRole('link', { name: /Generate a travel plan/ })
    expect(session).toHaveAttribute('href', '/sessions/s1')
    expect(session).toHaveTextContent('No token · writer')
    expect(session).toHaveTextContent('Needs you')
    const chat = screen.getByRole('link', { name: /Trip plan/ })
    expect(chat).toHaveAttribute('href', '/room/r1')
    expect(chat).toHaveTextContent('Needs input')
    expect(session.className).toBe(chat.className)
  })

  it('shows most-called agents with their avatar and call count', () => {
    render(<OverviewPage />)
    const agent = screen.getByRole('link', { name: /writer.*2 calls/ })
    expect(agent).toHaveAttribute('href', '/network/agents/a')
    expect(agent.querySelector('img, [data-slot="avatar"]')).not.toBeNull()
  })

  it('retries room history when rooms fail to load', () => {
    history.isError = true
    render(<OverviewPage />)
    expect(screen.getByText("Chats couldn't load.")).toBeInTheDocument()
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(history.refetch).toHaveBeenCalledTimes(1)
    expect(refreshNetworks).toHaveBeenCalledTimes(1)
  })

  it('retries the setup and MCP checks too, since they fail the same way while Hybro starts', () => {
    history.isError = true
    render(<OverviewPage />)
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }))
    expect(refetchSetup).toHaveBeenCalledTimes(1)
    expect(refreshGateway).toHaveBeenCalledTimes(1)
  })
})
