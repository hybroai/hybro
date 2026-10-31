'use client'

import { useMemo, useState, type ReactNode } from 'react'
import Link from 'next/link'
import { Check, Copy, ExternalLink, Pencil, Plug, Trash2, X } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Card } from '@/components/ui/card'
import { StatusIndicator } from '@/components/status-indicator'
import { agentStatusView, inactiveReasonText } from '@/lib/status'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { routes } from '@/lib/routes'
import { cn } from '@/lib/utils'
import { AddToSubnetMenu } from './add-to-subnet-menu'
import type { NetworkDetailsPanelProps } from './types'

const SOURCE_LABEL = { cloud: 'Remote', local: 'Local', hub: 'Hub', bundled: 'Sample' } as const
const initials = (name: string) =>
  name.split(/[\s_-]+/).filter(Boolean).slice(0, 2).map(part => part[0]!.toUpperCase()).join('')

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="flex flex-col gap-2">
      <h3 className="text-xs font-semibold text-muted-foreground">{title}</h3>
      {children}
    </section>
  )
}

export function NetworkDetailsPanel({
  selection,
  networks,
  agents,
  pending,
  className,
  onClose,
  onManage,
  onDelete,
  onSetMembers,
  onCreateWithAgent,
}: NetworkDetailsPanelProps) {
  const agentsById = useMemo(() => new Map(agents.map(agent => [agent.id, agent])), [agents])
  const [copyResult, setCopyResult] = useState<{ id: string; copied: boolean } | null>(null)
  const isAgent = selection.type === 'agent'
  const title = isAgent ? selection.agent.name : selection.network.name
  const description = isAgent ? selection.agent.description : selection.network.description
  const memberOf = isAgent ? networks.filter(network => network.agents.includes(selection.agent.id)) : []
  const meta = isAgent
    ? [selection.agent.version ? `v${selection.agent.version}` : null, selection.agent.provider].filter(Boolean).join(' · ')
    : `${selection.network.agents.length} ${selection.network.agents.length === 1 ? 'agent' : 'agents'}`

  async function copyId(id: string) {
    try {
      await navigator.clipboard.writeText(id)
      setCopyResult({ id, copied: true })
    } catch {
      setCopyResult({ id, copied: false })
    }
  }

  return (
    <Card
      className={cn('network-details gap-0 py-0', className)}
      role="region"
      aria-label={isAgent ? 'Agent summary' : 'Subnet details'}
      onKeyDown={event => { if (event.key === 'Escape') { event.stopPropagation(); onClose() } }}
    >
      <div className="flex items-start gap-3 border-b px-4 py-4">
        <Avatar className="size-10 rounded-md">
          {isAgent ? <AvatarImage src={selection.agent.iconUrl || getAgentAvatarUri(selection.agent.id)} alt="" /> : null}
          <AvatarFallback className={cn('rounded-md text-xs font-medium', isAgent ? 'bg-muted' : 'bg-primary/10 text-primary')}>{initials(title)}</AvatarFallback>
        </Avatar>
        <div className="min-w-0 flex-1">
          <h2 className="truncate text-sm font-semibold" title={title}>{title}</h2>
          <p className="truncate text-xs text-muted-foreground">{meta}</p>
        </div>
        <Button variant="ghost" size="iconSm" onClick={onClose} aria-label="Close details"><X aria-hidden="true" /></Button>
      </div>

      <div className="network-details-body flex flex-col gap-4 px-4 py-4">
        <p className="text-sm text-muted-foreground whitespace-pre-wrap wrap-anywhere">{description || 'No description yet'}</p>

        {isAgent ? (
          <>
            <Section title="Status">
              <div className="flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-muted-foreground">
                <StatusIndicator view={agentStatusView(selection.agent.status, selection.agent.inactiveReason)} />
                {selection.agent.source ? <span>{SOURCE_LABEL[selection.agent.source]}</span> : null}
                <span>{selection.agent.roomCalls} room calls</span>
              </div>
              {selection.agent.status === 'inactive' && selection.agent.inactiveReason ? (
                <p className="mt-2 text-xs text-muted-foreground">{inactiveReasonText(selection.agent.inactiveReason)}</p>
              ) : null}
            </Section>
            {selection.agent.skills.length ? (
              <Section title="Skills">
                <div className="flex flex-wrap gap-1.5">
                  {selection.agent.skills.map(skill => <Badge key={skill} variant="badgeMuted" className="max-w-full shrink truncate" title={skill}>{skill}</Badge>)}
                </div>
              </Section>
            ) : null}
            <Section title="Subnets">
              {memberOf.length
                ? <div className="flex flex-wrap gap-1.5">{memberOf.map(network => <Badge key={network.group_id} variant="badgeMuted" className="max-w-full shrink truncate" title={network.name}>{network.name}</Badge>)}</div>
                : <p className="text-xs text-muted-foreground">Not in any subnet</p>}
            </Section>
          </>
        ) : (
          <>
            <Section title="Subnet ID">
              <div className="flex items-center gap-2">
                <code className="min-w-0 flex-1 select-text break-all font-mono text-xs">{selection.network.group_id}</code>
                <Button variant="ghost" size="iconSm" onClick={() => copyId(selection.network.group_id)} aria-label="Copy subnet ID">
                  {copyResult?.id === selection.network.group_id && copyResult.copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
                </Button>
              </div>
              {copyResult?.id === selection.network.group_id ? (
                copyResult.copied ? <p role="status" className="text-xs text-muted-foreground">Subnet ID copied</p> : (
                  <Alert variant="destructive">
                    <AlertDescription>Unable to copy. Select the ID above and copy it manually.</AlertDescription>
                  </Alert>
                )
              ) : null}
            </Section>
            <Section title="Members">
              {selection.network.agents.length === 0 ? (
                <p className="text-xs text-muted-foreground">No members yet. Add agents through Manage members.</p>
              ) : (
                <ul className="network-member-list -mx-1">
                  {selection.network.agents.map(id => {
                    const agent = agentsById.get(id)
                    return (
                      <li key={id} className="network-member-row">
                        <Avatar className="size-7 rounded-md">
                          <AvatarImage src={agent?.iconUrl || getAgentAvatarUri(id)} alt="" />
                          <AvatarFallback className="rounded-md bg-muted text-[11px]">{initials(agent?.name ?? id)}</AvatarFallback>
                        </Avatar>
                        <span className="min-w-0 flex-1 truncate text-sm font-medium" title={agent?.name ?? id}>{agent?.name ?? id}</span>
                        <StatusIndicator view={agentStatusView(agent?.status ?? 'unknown', agent?.inactiveReason)} />
                      </li>
                    )
                  })}
                </ul>
              )}
            </Section>
          </>
        )}
      </div>

      <div className="network-details-footer flex flex-wrap gap-2 border-t px-4 py-3">
        {isAgent ? (
          <>
            <Button variant="secondary" size="sm" asChild>
              <Link href={routes.networkAgent(selection.agent.id)}><ExternalLink data-icon="inline-start" aria-hidden="true" />Open details</Link>
            </Button>
            <AddToSubnetMenu
              agentId={selection.agent.id}
              networks={networks}
              pending={pending}
              onSetMembers={onSetMembers}
              onCreateWithAgent={() => onCreateWithAgent(selection.agent.id)}
            />
          </>
        ) : (
          <>
            <Button variant="secondary" size="sm" onClick={onManage} disabled={pending}>
              <Pencil data-icon="inline-start" aria-hidden="true" />Manage members
            </Button>
            <Button variant="secondary" size="sm" asChild>
              <Link href={routes.connectMcp}><Plug data-icon="inline-start" aria-hidden="true" />Connect</Link>
            </Button>
            <Button variant="danger" size="sm" onClick={onDelete} disabled={pending}>
              <Trash2 data-icon="inline-start" aria-hidden="true" />Delete subnet
            </Button>
          </>
        )}
      </div>
    </Card>
  )
}
