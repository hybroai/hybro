import React from 'react'
import { describe, expect, it, vi } from 'vitest'
import userEvent from '@testing-library/user-event'
import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import { render as renderWithoutProviders, waitFor } from '@testing-library/react'
import { render, screen } from '../../../utils/test-utils'
import { AgentCard } from '@/components/conversation/AgentCard'
import { AGENT_THEMES } from '@/lib/selectors/conversation-types'
import type { Agent } from '@/lib/types/agent'

vi.mock('next/link', () => ({
  default: ({ children, href, onClick, ...rest }: { children: React.ReactNode; href: string; onClick?: React.MouseEventHandler; [k: string]: unknown }) => (
    <a href={href} onClick={onClick} {...rest}>{children}</a>
  ),
}))

describe('AgentCard', () => {
  it('uses conversation density classes for card and status sizing', () => {
    const { container } = render(
      <AgentCard
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{
          label: 'Completed',
          tone: 'muted',
          isAnimated: false,
          ariaLabel: 'Completed',
        }}
      />
    )

    expect(container.querySelector('.conversation-agent-card')).toBeTruthy()
    expect(screen.getByRole('status').className).toContain('conversation-agent-status')
  })

  it('opens the matching agent message when interactive', async () => {
    const onOpen = vi.fn()
    render(
      <AgentCard
        messageId="agent-message-1"
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{
          label: 'Completed',
          tone: 'muted',
          isAnimated: false,
          ariaLabel: 'Completed',
        }}
        onOpen={onOpen}
      />
    )

    const cardButton = screen.getByRole('button', { name: /open planner response/i })
    expect(cardButton).toHaveClass('cursor-pointer')

    await userEvent.click(cardButton)

    expect(onOpen).toHaveBeenCalledWith('agent-message-1')
  })

  it('links the avatar to the agent profile without opening response detail', async () => {
    const onOpen = vi.fn()
    const { container } = render(
      <AgentCard
        messageId="agent-message-1"
        agentId="agent-abc"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{ label: 'Completed', tone: 'muted', isAnimated: false, ariaLabel: 'Completed' }}
        onOpen={onOpen}
      />
    )

    const avatarLink = container.querySelector('a[aria-label="View Planner profile"]')
    expect(avatarLink).not.toBeNull()
    expect(avatarLink).toHaveAttribute('href', '/agents/agent-abc')
    avatarLink!.addEventListener('click', event => event.preventDefault())

    await userEvent.click(avatarLink!)

    expect(onOpen).not.toHaveBeenCalled()
  })

  it('adds profile links when the agent catalog arrives after render', async () => {
    const queryClient = new QueryClient()
    const { container } = renderWithoutProviders(
      <QueryClientProvider client={queryClient}>
        <AgentCard
          agentName="Planner"
          taskDescription="Plan the trip"
          theme={AGENT_THEMES[0]}
          display={{ label: 'Completed', tone: 'muted', isAnimated: false, ariaLabel: 'Completed' }}
        />
      </QueryClientProvider>
    )

    expect(container.querySelector('a[aria-label="View Planner profile"]')).toBeNull()

    queryClient.setQueryData<Agent[]>(['agents', 'all'], [{
      agent_id: 'agent-late',
      agent_card: { name: 'Planner' },
    } as Agent])

    await waitFor(() => {
      expect(container.querySelector('a[aria-label="View Planner profile"]'))
        .toHaveAttribute('href', '/agents/agent-late')
    })
  })

  it('marks selected cards without changing the card element', () => {
    const { container } = render(
      <AgentCard
        messageId="agent-message-1"
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        selected
        display={{
          label: 'Completed',
          tone: 'muted',
          isAnimated: false,
          ariaLabel: 'Completed',
        }}
      />
    )

    expect(container.querySelector('.conversation-agent-card')?.getAttribute('data-selected')).toBe('true')
  })

  it('does not render helper copy for input-required cards', () => {
    render(
      <AgentCard
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{
          label: 'Needs Input',
          tone: 'warning',
          isAnimated: true,
          ariaLabel: 'Planner needs input',
        }}
      />
    )

    expect(screen.getByRole('status', { name: 'Planner needs input' })).toHaveTextContent('Needs Input')
    expect(screen.queryByText('Agent is waiting for your response in the input panel below.')).not.toBeInTheDocument()
  })

  it('renders a link to the agent profile page', () => {
    const { container } = render(
      <AgentCard
        agentId="agent-abc"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{ label: 'Completed', tone: 'muted', isAnimated: false, ariaLabel: 'Completed' }}
      />
    )

    const links = Array.from(container.querySelectorAll('a[href="/agents/agent-abc"]'))
    expect(links).toHaveLength(2)
    expect(links.some(link => link.textContent === 'Planner')).toBe(true)
  })

  it('renders AgentSourceBadge when agentSource is provided', () => {
    const { container } = render(
      <AgentCard
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        agentSource="cloud"
        display={{ label: 'Completed', tone: 'muted', isAnimated: false, ariaLabel: 'Completed' }}
      />
    )

    // AgentSourceBadge renders a Cloud SVG icon for 'cloud' source
    expect(container.querySelector('svg')).not.toBeNull()
  })

  it('does not render AgentSourceBadge when agentSource is absent', () => {
    const { container } = render(
      <AgentCard
        agentId="agent-1"
        agentName="Planner"
        taskDescription="Plan the trip"
        theme={AGENT_THEMES[0]}
        display={{ label: 'Completed', tone: 'muted', isAnimated: false, ariaLabel: 'Completed' }}
      />
    )

    // Without agentSource, no source badge icon SVG should appear
    expect(container.querySelector('svg')).toBeNull()
  })
})
