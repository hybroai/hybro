'use client'

import Link from 'next/link'
import type { ReactNode } from 'react'
import { ChevronRight, Plus, RefreshCw } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { Button } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { StatusIndicator } from '@/components/status-indicator'
import { MiniTopology } from './mini-topology'
import { OverviewSetupBanner } from '@/components/setup/setup-banner'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { cn } from '@/lib/utils'
import { useNetworks } from '@/hooks/useNetworks'
import { useRoomHistory } from '@/hooks/useRoomHistory'
import { useSessions } from '@/hooks/useSessions'
import { useMcpStatus } from '@/hooks/useMcpStatus'
import { useUser } from '@/lib/auth'
import { routes } from '@/lib/routes'
import { formatSessionTime, sessionAgentNames } from '@/lib/session-format'
import { sessionName } from '@/lib/session-list-state'
import { gatewayStatusView, roomStatusView, sessionStateView, type StatusView } from '@/lib/status'
import { overviewMetrics, recentRooms, recentSessions, topAgentsByCalls } from '@/lib/overview'

const RECENT = 5

// Each tile's number takes the color of its side-menu icon.
function Tile({ label, value, detail, attention, href, tone }: {
  label: string
  value: ReactNode
  detail?: string
  attention: boolean
  href: string
  tone: string
}) {
  return (
    <Link
      href={href}
      aria-label={[label, value, detail].filter(part => typeof part === 'string' || typeof part === 'number').join(', ')}
      className="flex flex-col gap-1 rounded-xl border bg-card p-4 outline-none transition-colors duration-150 hover:bg-muted/50 focus-visible:ring-[3px] focus-visible:ring-ring/50"
    >
      <span className="text-xs text-muted-foreground">{label}</span>
      <span className={cn('text-2xl font-semibold tabular-nums', tone)}>{value}</span>
      {detail ? <span className={cn('text-xs', attention ? 'font-medium text-status-warning' : 'text-muted-foreground')}>{detail}</span> : null}
    </Link>
  )
}

function Section({ id, title, action, children }: { id: string; title: string; action?: ReactNode; children: ReactNode }) {
  return (
    <section aria-labelledby={id} className="flex flex-col gap-2">
      <div className="flex items-center justify-between gap-3">
        <h2 id={id} className="text-[15px] font-semibold">{title}</h2>
        {action}
      </div>
      {children}
    </section>
  )
}

function ViewAll({ href, children }: { href: string; children: ReactNode }) {
  return (
    <Button variant="ghost" size="sm" asChild>
      <Link href={href}>{children}<ChevronRight aria-hidden="true" /></Link>
    </Button>
  )
}

const ROW = 'flex min-h-14 items-center justify-between gap-3 border-t px-4 py-3 outline-none transition-colors duration-150 first:border-t-0 hover:bg-muted/50 focus-visible:bg-muted/50'

// One row design for sessions and chats: title, a grey line of who and when,
// and the status on the right.
function ActivityRow({ href, title, meta, status }: { href: string; title: string; meta: string; status: StatusView | null }) {
  return (
    <li>
      <Link href={href} className={ROW}>
        <span className="flex min-w-0 flex-col gap-0.5">
          <span className="truncate text-sm font-medium">{title}</span>
          <span className="truncate text-xs text-muted-foreground">{meta}</span>
        </span>
        <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground">
          {status ? <StatusIndicator view={status} /> : null}
          <ChevronRight className="size-4" aria-hidden="true" />
        </span>
      </Link>
    </li>
  )
}

function ListCard({ loading, empty, children }: { loading: boolean; empty: string; children: ReactNode[] }) {
  if (loading) return <Skeleton className="h-40 rounded-xl" />
  if (children.length === 0) return <p className="rounded-xl border bg-card px-4 py-6 text-sm text-muted-foreground">{empty}</p>
  return <ul className="overflow-hidden rounded-xl border bg-card">{children}</ul>
}

export function OverviewPage() {
  const { user, isLoaded, isSignedIn } = useUser()
  const signedIn = Boolean(isLoaded && isSignedIn && user?.id)
  const data = useNetworks()
  const history = useRoomHistory({ userId: user?.id ?? '', enabled: signedIn })
  const sessionList = useSessions({ userId: user?.id ?? '', enabled: signedIn })
  const gateway = useMcpStatus()
  const rooms = history.data?.items ?? []
  const sessions = sessionList.data?.sessions ?? []
  const loading = data.loading || data.agentsLoading || history.isLoading
  const metrics = overviewMetrics({ agents: data.agents, networks: data.networks, rooms, sessions })
  const failed = data.error || data.agentsError || (history.isError ? "Chats couldn't load." : null)
  const topAgents = topAgentsByCalls(data.agents, RECENT)

  return (
    <div className="page-container">
      <div className="page-content flex flex-col gap-8">
        <header className="flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-2xl font-semibold tracking-tight">Overview</h1>
          <div className="flex gap-2">
            <Button variant="secondary" asChild>
              <Link href={routes.registerAgent}><Plus data-icon="inline-start" aria-hidden="true" />Add agent</Link>
            </Button>
            <Button asChild><Link href={routes.chat}>New chat</Link></Button>
          </div>
        </header>

        <OverviewSetupBanner />

        {failed ? (
          <Alert variant="destructive">
            <AlertDescription className="flex flex-wrap items-center gap-2">
              <span>{failed}</span>
              <Button variant="secondary" size="sm" onClick={() => { void data.refresh(); void history.refetch() }}><RefreshCw data-icon="inline-start" aria-hidden="true" />Retry</Button>
            </AlertDescription>
          </Alert>
        ) : null}

        <div className="grid grid-cols-2 gap-3 lg:grid-cols-4">
          {loading ? Array.from({ length: 4 }, (_, index) => <Skeleton key={index} className="h-[100px] rounded-xl" />) : (
            <>
              <Tile
                label="Sessions"
                value={metrics.sessions.open}
                detail={metrics.sessions.waiting ? `${metrics.sessions.waiting} need you` : 'Open'}
                attention={metrics.sessions.waiting > 0}
                tone="text-icon-create"
                href={metrics.sessions.waiting ? `${routes.sessions}?status=waiting_on_owner` : routes.sessions}
              />
              <Tile
                label="Chats"
                value={metrics.rooms.total}
                detail={metrics.rooms.needsInput ? `${metrics.rooms.needsInput} need input` : `${metrics.rooms.running} running`}
                attention={metrics.rooms.needsInput > 0}
                tone="text-icon-create"
                href={routes.chat}
              />
              <Tile
                label="Agents"
                value={metrics.agents.total}
                detail={metrics.agents.offline ? `${metrics.agents.offline} offline` : `${metrics.agents.online} online`}
                attention={metrics.agents.offline > 0}
                tone="text-icon-network"
                href={routes.network}
              />
              <Tile
                label="MCP gateway"
                value={gatewayStatusView(gateway.status).label}
                detail={gateway.status === 'ready' || gateway.status === 'checking' ? undefined : 'Check the connection'}
                attention={gateway.status !== 'ready' && gateway.status !== 'checking'}
                tone="text-hybro-bro"
                href={routes.connectMcp}
              />
            </>
          )}
        </div>

        <Section id="sessions-heading" title="Recent sessions" action={<ViewAll href={routes.sessions}>All sessions</ViewAll>}>
          <ListCard loading={sessionList.isLoading} empty="No sessions yet. A client opens one through MCP or the Sessions API.">
            {recentSessions(sessions, RECENT).map(session => (
              <ActivityRow
                key={session.session_id}
                href={routes.session(session.session_id)}
                title={sessionName(session)}
                meta={[session.client_label, sessionAgentNames(session.agents), formatSessionTime(session.last_activity_at)].filter(Boolean).join(' · ')}
                status={sessionStateView(session.state)}
              />
            ))}
          </ListCard>
        </Section>

        <Section id="chats-heading" title="Recent chats" action={<ViewAll href={routes.chat}>All chats</ViewAll>}>
          <ListCard loading={history.isLoading} empty="Start a chat to try several agents at once.">
            {recentRooms(rooms, RECENT).map(room => (
              <ActivityRow
                key={room.conversation_id}
                href={routes.room(room.conversation_id)}
                title={room.title}
                meta={formatSessionTime(room.last_activity_at)}
                status={roomStatusView(room.status)}
              />
            ))}
          </ListCard>
        </Section>

        <Section id="network-heading" title="Network" action={<ViewAll href={routes.network}>Open network</ViewAll>}>
          <Link href={routes.network} aria-label="Open the network map" className="rounded-xl border bg-card p-3 transition-colors duration-150 hover:bg-muted/50">
            <MiniTopology networks={data.networks} agents={data.agents} />
          </Link>
        </Section>

        <Section id="agents-heading" title="Most-called agents">
          <ListCard loading={loading} empty="No agent calls yet.">
            {topAgents.map(agent => (
              <li key={agent.id}>
                <Link href={routes.networkAgent(agent.id)} className={ROW}>
                  <span className="flex min-w-0 items-center gap-3">
                    <Avatar className="size-8 rounded-lg">
                      <AvatarImage src={agent.iconUrl || getAgentAvatarUri(agent.id)} alt="" />
                      <AvatarFallback className="rounded-lg bg-muted text-[11px]">{agent.name.slice(0, 2).toUpperCase()}</AvatarFallback>
                    </Avatar>
                    <span className="truncate text-sm font-medium">{agent.name}</span>
                  </span>
                  <span className="flex shrink-0 items-center gap-2 text-xs text-muted-foreground tabular-nums">
                    {agent.roomCalls === 1 ? '1 call' : `${agent.roomCalls} calls`}
                    <ChevronRight className="size-4" aria-hidden="true" />
                  </span>
                </Link>
              </li>
            ))}
          </ListCard>
        </Section>
      </div>
    </div>
  )
}
