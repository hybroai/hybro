import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, render, screen, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { StructureView } from '@/components/networks/structure-view'
import { DEFAULT_NETWORK_URL_STATE } from '@/lib/network-url-state'
import { placeholderAgent } from '@/lib/network-agent'
import type { AgentGroup } from '@/lib/types/agent-group'

afterEach(cleanup)

const agents = [{ ...placeholderAgent('a', '', 'active'), name: 'alpha' }, { ...placeholderAgent('b', '', 'active'), name: 'beta' }]
const networks = [{ group_id: 'R', name: 'Research', agents: ['a', 'b'], type: 'user', owner_id: 'u' }, { group_id: 'T', name: 'Travel', agents: ['b'], type: 'user', owner_id: 'u' }] as AgentGroup[]

describe('StructureView', () => {
  it('sets scope from the scope list and selection from a row, independently', async () => {
    const onChange = vi.fn()
    render(<StructureView agents={agents} networks={networks} state={{ ...DEFAULT_NETWORK_URL_STATE, view: 'structure' }} onChange={onChange} details={null} />)
    await userEvent.click(screen.getByRole('button', { name: /^Research\s*\d+$/ }))
    expect(onChange).toHaveBeenLastCalledWith({ scope: { kind: 'subnet', id: 'R' } })
    await userEvent.click(screen.getByRole('button', { name: 'Research details' }))
    expect(onChange).toHaveBeenLastCalledWith({ select: { type: 'subnet', id: 'R' } })
    await userEvent.click(screen.getByRole('button', { name: /alpha/ }))
    expect(onChange).toHaveBeenLastCalledWith({ select: { type: 'agent', id: 'a' } })
  })

  it('tints every instance of the selected agent in the by-subnet layout', () => {
    render(<StructureView agents={agents} networks={networks} state={{ ...DEFAULT_NETWORK_URL_STATE, view: 'structure', layout: 'subnet', select: { type: 'agent', id: 'b' } }} onChange={vi.fn()} details={<aside>panel</aside>} />)
    const rows = screen.getAllByRole('button', { name: /beta/ })
    expect(rows).toHaveLength(2)
    expect(rows.every(row => row.getAttribute('aria-current') === 'true')).toBe(true)
    expect(rows.map(row => row.tabIndex)).toEqual([0, -1])
    expect(screen.getByText('panel')).toBeInTheDocument()
  })

  it('shows the scope summary and labels the calls column honestly', () => {
    render(<StructureView agents={agents} networks={networks} state={{ ...DEFAULT_NETWORK_URL_STATE, view: 'structure' }} onChange={vi.fn()} details={null} />)
    const main = screen.getByRole('region', { name: 'All agents' })
    expect(within(main).getByText('2 agents · 2 online')).toBeInTheDocument()
    expect(within(main).getByRole('button', { name: /Room calls/ })).toBeInTheDocument()
  })

  it('labels the column for where an agent comes from as Origin', () => {
    render(<StructureView agents={agents} networks={networks} state={{ ...DEFAULT_NETWORK_URL_STATE, view: 'structure' }} onChange={vi.fn()} details={null} />)
    const main = screen.getByRole('region', { name: 'All agents' })
    expect(within(main).getByText('Origin')).toBeInTheDocument()
    expect(within(main).queryByText('Source')).not.toBeInTheDocument()
  })
})
