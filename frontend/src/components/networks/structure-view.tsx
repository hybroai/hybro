'use client'

import { useMemo, useState, type ReactNode } from 'react'
import { ArrowDown, ArrowUp, Info } from 'lucide-react'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { SegmentedControl } from '@/components/ui/segmented-control'
import { Tooltip, TooltipContent, TooltipTrigger } from '@/components/ui/tooltip'
import { StatusIndicator } from '@/components/status-indicator'
import { agentStatusView } from '@/lib/status'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { scopeKey, type NetworkScope, type NetworkUrlState, type StructureLayout } from '@/lib/network-url-state'
import { buildStructureGroups, DEFAULT_STRUCTURE_SORT, scopeCounts, type StructureSort } from '@/lib/structure-groups'
import { cn } from '@/lib/utils'
import type { AgentGroup } from '@/lib/types/agent-group'
import type { NetworkAgent } from './types'

const SOURCE_LABEL = { cloud: 'Remote', local: 'Local', hub: 'Hub', bundled: 'Sample' } as const
const COLUMNS = 'grid grid-cols-[minmax(0,2fr)_6.5rem_minmax(0,1.4fr)_4.5rem_6rem] items-center gap-3'

export function StructureView({ agents, networks, state, onChange, details }: {
  agents: NetworkAgent[]
  networks: AgentGroup[]
  state: NetworkUrlState
  onChange: (patch: Partial<NetworkUrlState>) => void
  details: ReactNode
}) {
  const [sort, setSort] = useState<StructureSort>(DEFAULT_STRUCTURE_SORT)
  const groups = useMemo(
    () => buildStructureGroups({ agents, networks, scope: state.scope, layout: state.layout, q: state.q, sort }),
    [agents, networks, state.scope, state.layout, state.q, sort],
  )
  const counts = useMemo(() => scopeCounts({ agents, networks, q: state.q }), [agents, networks, state.q])
  const scope = state.scope
  const scopeTitle = scope.kind === 'all' ? 'All agents'
    : scope.kind === 'unassigned' ? 'Unassigned'
      : networks.find(network => network.group_id === scope.id)?.name ?? 'All agents'
  const shown = [...new Map(groups.flatMap(group => group.rows).map(row => [row.agent.id, row.agent])).values()]
  const online = shown.filter(agent => agent.status === 'active').length
  const selectedAgent = state.select?.type === 'agent' ? state.select.id : null

  const scopeItem = (item: NetworkScope, label: string, count: number) => {
    const active = scopeKey(item) === scopeKey(state.scope)
    return (
      <button
        type="button"
        aria-current={active ? 'true' : undefined}
        onClick={() => onChange({ scope: item })}
        className={cn('flex min-h-9 w-full items-center justify-between rounded-lg px-2.5 text-left text-sm hover:bg-muted', active && 'bg-muted font-medium')}
      >
        <span className="truncate">{label}</span>
        <span className="text-xs tabular-nums text-muted-foreground">{count}</span>
      </button>
    )
  }

  const sortButton = (by: StructureSort['by'], label: string, tooltip?: string) => {
    const active = sort.by === by
    const button = (
      <button
        type="button"
        onClick={() => setSort(active ? { by, direction: sort.direction === 'asc' ? 'desc' : 'asc' } : { by, direction: by === 'calls' ? 'desc' : 'asc' })}
        className="inline-flex items-center gap-1 text-xs font-medium text-muted-foreground hover:text-foreground"
      >
        {label}
        {active ? (
          <>
            {sort.direction === 'asc' ? <ArrowUp className="size-3" aria-hidden="true" /> : <ArrowDown className="size-3" aria-hidden="true" />}
            <span className="sr-only">{sort.direction === 'asc' ? ', sorted ascending' : ', sorted descending'}</span>
          </>
        ) : null}
      </button>
    )
    return tooltip ? <Tooltip><TooltipTrigger asChild>{button}</TooltipTrigger><TooltipContent>{tooltip}</TooltipContent></Tooltip> : button
  }

  return (
    <div className="structure-view">
      <nav aria-label="Scope" className="structure-scope">
        {scopeItem({ kind: 'all' }, 'All agents', counts.all)}
        {scopeItem({ kind: 'unassigned' }, 'Unassigned', counts.unassigned)}
        <h2 className="px-2.5 pt-4 pb-1 text-xs font-semibold text-muted-foreground">Subnets</h2>
        {networks.map(network => (
          <div key={network.group_id} className="flex items-center gap-1">
            <div className="min-w-0 flex-1">{scopeItem({ kind: 'subnet', id: network.group_id }, network.name, counts.subnets.get(network.group_id) ?? 0)}</div>
            <Button variant="ghost" size="iconSm" aria-label={`${network.name} details`} onClick={() => onChange({ select: { type: 'subnet', id: network.group_id } })}>
              <Info aria-hidden="true" />
            </Button>
          </div>
        ))}
        <label className="structure-scope-select">
          <span className="sr-only">Scope</span>
          <select
            value={scopeKey(state.scope)}
            onChange={event => {
              const value = event.target.value
              onChange({ scope: value === 'all' || value === 'unassigned' ? { kind: value } : { kind: 'subnet', id: value.slice('subnet:'.length) } })
            }}
          >
            <option value="all">All agents ({counts.all})</option>
            <option value="unassigned">Unassigned ({counts.unassigned})</option>
            {networks.map(network => <option key={network.group_id} value={`subnet:${network.group_id}`}>{network.name}</option>)}
          </select>
        </label>
      </nav>
      <section aria-label={scopeTitle} className="structure-main">
        <div className="flex flex-wrap items-center justify-between gap-3 pb-3">
          <div>
            <h2 className="text-[15px] font-semibold">{scopeTitle}</h2>
            <p className="text-xs text-muted-foreground">{`${shown.length} agents · ${online} online`}</p>
          </div>
          <SegmentedControl<StructureLayout>
            label="Layout"
            value={state.layout}
            options={[{ value: 'list', label: 'List' }, { value: 'subnet', label: 'By subnet' }]}
            onValueChange={layout => onChange({ layout })}
          />
        </div>
        <div className={cn(COLUMNS, 'structure-head border-b px-2 pb-2')}>
          {sortButton('name', 'Agent')}
          {sortButton('status', 'Status')}
          <span className="text-xs font-medium text-muted-foreground">Subnets</span>
          <span className="text-xs font-medium text-muted-foreground">Source</span>
          {sortButton('calls', 'Room calls', 'Direct MCP and API sends are not counted')}
        </div>
        {groups.map(group => (
          <div key={group.key} role="group" aria-label={group.title ?? scopeTitle}>
            {group.title ? <h3 className="px-2 pt-4 pb-1 text-xs font-semibold text-muted-foreground">{`${group.title} · ${group.count}`}</h3> : null}
            {group.rows.length === 0 ? <p className="px-2 py-3 text-sm text-muted-foreground">{state.q ? 'No agents match your search.' : 'No agents here yet.'}</p> : null}
            {group.rows.map(row => {
              const selected = row.agent.id === selectedAgent
              return (
                <button
                  key={row.key}
                  type="button"
                  tabIndex={row.tabbable ? 0 : -1}
                  aria-current={selected ? 'true' : undefined}
                  onClick={() => onChange({ select: { type: 'agent', id: row.agent.id } })}
                  className={cn(COLUMNS, 'structure-row w-full rounded-lg px-2 py-2 text-left hover:bg-muted', selected && 'bg-primary/10')}
                >
                  <span className="flex min-w-0 items-center gap-2.5">
                    <Avatar className="size-7 rounded-md">
                      <AvatarImage src={row.agent.iconUrl || getAgentAvatarUri(row.agent.id)} alt="" />
                      <AvatarFallback className="rounded-md bg-muted text-[11px]">{row.agent.name.slice(0, 2).toUpperCase()}</AvatarFallback>
                    </Avatar>
                    <span className="min-w-0">
                      <span className="block truncate text-sm font-medium">{row.agent.name}</span>
                      <span className="block truncate text-xs text-muted-foreground">{row.agent.description || 'No description yet'}</span>
                    </span>
                  </span>
                  <StatusIndicator view={agentStatusView(row.agent.status, row.agent.inactiveReason)} />
                  <span className="flex min-w-0 flex-wrap gap-1">
                    {row.subnets.length ? row.subnets.map(network => <Badge key={network.group_id} variant="badgeMuted" className="max-w-full shrink truncate" title={network.name}>{network.name}</Badge>) : <span className="text-muted-foreground">—</span>}
                  </span>
                  <span className="text-xs text-muted-foreground">{row.agent.source ? SOURCE_LABEL[row.agent.source] : '—'}</span>
                  <span className="text-right text-sm tabular-nums">{row.agent.roomCalls}</span>
                </button>
              )
            })}
          </div>
        ))}
      </section>
      {state.select ? details : null}
    </div>
  )
}
