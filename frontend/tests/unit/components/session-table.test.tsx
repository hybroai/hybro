import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'

const nav = vi.hoisted(() => ({
  query: '',
  replace: vi.fn(),
  push: vi.fn(),
}))
const data = vi.hoisted(() => ({ sessions: [] as Record<string, unknown>[] }))

vi.mock('next/navigation', () => ({
  useSearchParams: () => new URLSearchParams(nav.query),
  usePathname: () => '/sessions',
  useRouter: () => ({ replace: nav.replace, push: nav.push }),
}))
vi.mock('next/link', () => ({
  default: ({ children, href, ...props }: React.ComponentProps<'a'>) => <a href={String(href)} {...props}>{children}</a>,
}))
vi.mock('@/lib/auth', () => ({ useUser: () => ({ user: { id: 'u' }, isLoaded: true, isSignedIn: true }) }))
vi.mock('@/hooks/useSessions', () => ({
  useSessions: () => ({ data: { sessions: data.sessions }, isLoading: false, isError: false, isSuccess: true, refetch: vi.fn() }),
}))

import { SessionsTablePage } from '@/components/sessions/session-table'
import { SESSION_LIST_QUERY_STORAGE_KEY } from '@/lib/session-list-state'

const summary = (id: string, title: string | null, label: string | null, state: string, updated: string) => ({
  session_id: id, client_label: label, source: { source_id: 'src_ci', name: 'CI bot', has_token: true }, title, agents: [{ agent_id: 'travel', name: 'Travel Planner Agent' }], state,
  paused_agent_id: null, conversation_id: `conv-${id}`, opened_at: updated, last_activity_at: updated, closed_at: null,
})

beforeEach(() => {
  nav.query = ''
  nav.replace.mockReset()
  nav.push.mockReset()
  data.sessions = [
    summary('a', 'Generate a travel plan', 'Northside buyer agent', 'waiting_on_owner', '2026-10-14T21:16:00Z'),
    summary('b', 'Weather in Kyoto', 'Blossom Lettings client', 'completed', '2026-10-15T09:47:00Z'),
    summary('c', null, 'Blossom Lettings client', 'closed', '2026-09-02T10:00:00Z'),
  ]
})
afterEach(cleanup)

function rows() {
  const [, ...body] = within(screen.getByRole('table', { name: 'Inbox' })).getAllByRole('row')
  return body.map(row => within(row).queryByRole('link')?.textContent)
}

describe('SessionsTablePage', () => {
  it('lists sessions as tickets with name, status, requester, and times', () => {
    render(<SessionsTablePage />)

    expect(screen.getByRole('heading', { level: 1, name: 'Inbox' })).toBeInTheDocument()
    for (const name of ['Name', 'Status', 'Requester', 'Created', 'Updated']) {
      expect(screen.getByRole('columnheader', { name: new RegExp(name) })).toBeInTheDocument()
    }
    expect(rows()).toEqual(['Weather in Kyoto', 'Generate a travel plan', 'Untitled session'])
    const first = screen.getAllByRole('row')[1]
    expect(within(first).getAllByText('Blossom Lettings client').length).toBeGreaterThan(0)
    expect(within(first).getByText('Completed')).toBeInTheDocument()
    expect(screen.getByText('3 sessions')).toBeInTheDocument()
    expect(document.body.textContent).not.toMatch(/[一-鿿]/)
  })

  it('applies filters and sort from the URL', () => {
    nav.query = 'status=waiting_on_owner,closed&sort=name&dir=asc'
    render(<SessionsTablePage />)
    expect(rows()).toEqual(['Generate a travel plan', 'Untitled session'])
    expect(screen.getByText('2 of 3 sessions')).toBeInTheDocument()
    expect(screen.getByRole('columnheader', { name: /Name/ })).toHaveAttribute('aria-sort', 'ascending')
  })

  it('writes search text to the URL', () => {
    render(<SessionsTablePage />)
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sessions' }), { target: { value: 'kyoto' } })
    expect(nav.replace).toHaveBeenCalledWith('/sessions?q=kyoto', { scroll: false })
  })

  it('sorts by a column header and flips direction on the second click', () => {
    nav.query = 'sort=created'
    render(<SessionsTablePage />)
    fireEvent.click(within(screen.getByRole('columnheader', { name: /Created/ })).getByRole('button', { name: /Created/ }))
    expect(nav.replace).toHaveBeenCalledWith('/sessions?sort=created&dir=asc', { scroll: false })
  })

  it('filters a column from its menu', async () => {
    render(<SessionsTablePage />)
    await userEvent.click(screen.getByRole('button', { name: 'Filter by status' }))
    await userEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Needs you' }))
    expect(nav.replace).toHaveBeenCalledWith('/sessions?status=waiting_on_owner', { scroll: false })
  })

  it('offers each requester once in the requester filter', async () => {
    render(<SessionsTablePage />)
    await userEvent.click(screen.getByRole('button', { name: 'Filter by requester' }))
    expect(screen.getAllByRole('menuitemcheckbox').map(item => item.textContent)).toEqual([
      'Blossom Lettings client',
      'Northside buyer agent',
    ])
  })

  it('names a session without a label by its source', async () => {
    data.sessions = [...data.sessions, summary('d', 'From CI', null, 'completed', '2026-10-15T10:00:00Z')]
    render(<SessionsTablePage />)
    const row = screen.getByRole('link', { name: 'From CI' }).closest('tr')!
    expect(within(row).getAllByText('CI bot').length).toBeGreaterThan(0)
    await userEvent.click(screen.getByRole('button', { name: 'Filter by requester' }))
    expect(screen.getAllByRole('menuitemcheckbox').map(item => item.textContent)).toContain('CI bot')
  })

  it('explains an empty result and clears the filters', () => {
    nav.query = 'q=nothing-matches&sort=name'
    render(<SessionsTablePage />)
    expect(screen.getByText('No sessions match these filters.')).toBeInTheDocument()
    fireEvent.click(screen.getAllByRole('button', { name: 'Clear filters' })[0])
    expect(nav.replace).toHaveBeenCalledWith('/sessions?sort=name', { scroll: false })
  })

  it('opens a ticket when its row is clicked and remembers the list view', () => {
    nav.query = 'status=completed'
    render(<SessionsTablePage />)
    fireEvent.click(screen.getAllByRole('row')[1])
    expect(nav.push).toHaveBeenCalledWith('/sessions/b')
    expect(window.sessionStorage.getItem(SESSION_LIST_QUERY_STORAGE_KEY)).toBe('status=completed')
  })

  it('explains how sessions start when there are none', () => {
    data.sessions = []
    render(<SessionsTablePage />)
    expect(screen.getByText(/No sessions yet/)).toBeInTheDocument()
  })
})

describe('SessionsTablePage URL updates', () => {
  it('keeps an earlier filter when the next change comes before the re-render', async () => {
    render(<SessionsTablePage />)
    await userEvent.click(screen.getByRole('button', { name: 'Filter by status' }))
    await userEvent.click(screen.getByRole('menuitemcheckbox', { name: 'Needs you' }))
    await userEvent.keyboard('{Escape}')
    // The mocked router never updates the URL, like a replace still in flight.
    fireEvent.change(screen.getByRole('searchbox', { name: 'Search sessions' }), { target: { value: 'kyoto' } })
    expect(nav.replace).toHaveBeenLastCalledWith('/sessions?q=kyoto&status=waiting_on_owner', { scroll: false })
  })
})
