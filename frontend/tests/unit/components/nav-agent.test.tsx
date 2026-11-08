import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { SidebarProvider } from '@/components/ui/sidebar'
import { NavAgent } from '@/components/nav-agent'
import { CONSUMER_NAV } from '@/lib/consumer-nav'

vi.mock('next/navigation', () => ({ usePathname: () => '/network' }))
vi.mock('@/hooks/use-mobile', () => ({ useIsMobile: () => false }))

afterEach(cleanup)

describe('NavAgent', () => {
  it('renders the six destinations and marks the current one', () => {
    render(<SidebarProvider><NavAgent navAgents={CONSUMER_NAV} badges={{ '/chat': 2 }} /></SidebarProvider>)
    expect(CONSUMER_NAV.map(item => item.name)).toEqual(['Overview', 'Network', 'Chats', 'Inbox', 'Access', 'Settings'])
    for (const name of ['Overview', 'Network', 'Chats', 'Inbox', 'Access', 'Settings']) {
      expect(screen.getByRole('link', { name: new RegExp(`^${name}`) })).toBeInTheDocument()
    }
    expect(screen.getByRole('link', { name: /^Network/ })).toHaveAttribute('aria-current', 'page')
  })

  it('announces the rooms needing input', () => {
    render(<SidebarProvider><NavAgent navAgents={CONSUMER_NAV} badges={{ '/chat': 2 }} /></SidebarProvider>)
    expect(screen.getByRole('link', { name: 'Chats, 2 need input' })).toBeInTheDocument()
  })
  it('announces inbox sessions waiting on the owner with their own badge', () => {
    render(<SidebarProvider><NavAgent navAgents={CONSUMER_NAV} badges={{ '/sessions': 1 }} /></SidebarProvider>)
    expect(screen.getByRole('link', { name: 'Inbox, 1 need input' })).toHaveAttribute('href', '/sessions')
    expect(screen.getByRole('link', { name: 'Chats' })).toBeInTheDocument()
  })
})
