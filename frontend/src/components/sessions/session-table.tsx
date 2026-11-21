'use client'

import { useEffect, useMemo, type KeyboardEvent, type ReactNode } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { ArrowDown, ArrowUp, ArrowUpDown, ListFilter, RefreshCw, Search } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { StatusIndicator } from '@/components/status-indicator'
import { Button } from '@/components/ui/button'
import {
  DropdownMenu,
  DropdownMenuCheckboxItem,
  DropdownMenuContent,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { useSessions } from '@/hooks/useSessions'
import { useSessionListUrlState } from '@/hooks/useSessionListUrlState'
import type { SessionState, SessionSummary } from '@/lib/api/session'
import { useUser } from '@/lib/auth'
import { routes } from '@/lib/routes'
import { formatSessionTime, sessionClientLabel, sessionRequester } from '@/lib/session-format'
import { ClientLabel } from './client-label'
import {
  DEFAULT_SESSION_LIST_STATE,
  SESSION_LIST_QUERY_STORAGE_KEY,
  SESSION_STATES,
  TIME_RANGES,
  applySessionListState,
  hasActiveFilters,
  sessionName,
  type SessionListState,
  type SessionSortField,
  type SessionTimeRange,
} from '@/lib/session-list-state'
import { sessionStateView } from '@/lib/status'
import { cn } from '@/lib/utils'

type Update = (patch: Partial<SessionListState>) => void

// Typing in a menu's text field must not trigger the menu's type-to-select.
function keepTyping(event: KeyboardEvent<HTMLInputElement>) {
  if (event.key !== 'Escape') event.stopPropagation()
}

function ColumnHeader({ label, field, state, update, filter, filtered, className }: {
  label: string
  field: SessionSortField
  state: SessionListState
  update: Update
  filter: ReactNode
  filtered: boolean
  className?: string
}) {
  const sorted = state.sort === field
  const SortIcon = !sorted ? ArrowUpDown : state.dir === 'asc' ? ArrowUp : ArrowDown
  return (
    <th
      scope="col"
      aria-sort={sorted ? (state.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
      className={cn('px-3 py-2 text-left font-medium first:pl-4', className)}
    >
      <div className="flex items-center gap-0.5">
        <button
          type="button"
          onClick={() => update({ sort: field, dir: sorted && state.dir === 'desc' ? 'asc' : 'desc' })}
          className="inline-flex h-7 items-center gap-1.5 rounded-md px-1.5 -ml-1.5 text-muted-foreground outline-none hover:text-foreground focus-visible:ring-[3px] focus-visible:ring-ring/50"
        >
          {label}
          <SortIcon className={cn('size-3.5', !sorted && 'opacity-50')} aria-hidden="true" />
        </button>
        <DropdownMenu>
          <DropdownMenuTrigger asChild>
            <Button
              variant="ghost"
              size="iconSm"
              aria-label={`Filter by ${label.toLowerCase()}`}
              className={cn('size-7', filtered && 'text-primary hover:text-primary')}
            >
              <ListFilter className="size-3.5" aria-hidden="true" />
            </Button>
          </DropdownMenuTrigger>
          <DropdownMenuContent align="start" className="w-60">{filter}</DropdownMenuContent>
        </DropdownMenu>
      </div>
    </th>
  )
}

function RangeFilter({ value, onChange }: { value: SessionTimeRange; onChange: (value: SessionTimeRange) => void }) {
  return (
    <DropdownMenuRadioGroup value={value} onValueChange={next => onChange(next as SessionTimeRange)}>
      {TIME_RANGES.map(range => (
        <DropdownMenuRadioItem key={range.value} value={range.value}>{range.label}</DropdownMenuRadioItem>
      ))}
    </DropdownMenuRadioGroup>
  )
}

function toggle<T>(list: readonly T[], value: T): T[] {
  return list.includes(value) ? list.filter(item => item !== value) : [...list, value]
}

function SessionRow({ session }: { session: SessionSummary }) {
  const router = useRouter()
  const href = routes.session(session.session_id)
  const label = sessionClientLabel(session)
  return (
    <tr
      onClick={() => router.push(href)}
      className="cursor-pointer border-t transition-colors duration-150 hover:bg-muted/50"
    >
      <td className="max-w-0 py-3 pl-4 pr-3">
        <Link
          href={href}
          onClick={event => event.stopPropagation()}
          className="block truncate font-medium outline-none hover:underline focus-visible:underline"
        >
          {sessionName(session)}
        </Link>
        <span className="mt-0.5 block truncate text-xs text-muted-foreground md:hidden">
          {sessionRequester(session)}
          <span className="sm:hidden"> · {formatSessionTime(session.last_activity_at)}</span>
        </span>
      </td>
      <td className="whitespace-nowrap px-3 py-3"><StatusIndicator view={sessionStateView(session.state)} /></td>
      <td className="hidden max-w-0 px-3 py-3 md:table-cell">
        <span className="block truncate">{sessionRequester(session)}</span>
        {label ? <ClientLabel label={label} className="block" /> : null}
      </td>
      <td className="hidden whitespace-nowrap px-3 py-3 tabular-nums text-muted-foreground lg:table-cell">
        <time dateTime={session.opened_at}>{formatSessionTime(session.opened_at)}</time>
      </td>
      <td className="hidden whitespace-nowrap px-3 py-3 pr-4 tabular-nums text-muted-foreground sm:table-cell">
        <time dateTime={session.last_activity_at}>{formatSessionTime(session.last_activity_at)}</time>
      </td>
    </tr>
  )
}

export function SessionsTablePage() {
  const { user, isLoaded, isSignedIn } = useUser()
  const query = useSessions({ userId: user?.id ?? '', enabled: Boolean(isLoaded && isSignedIn && user?.id) })
  const [state, update, urlQuery] = useSessionListUrlState()
  const all = query.data?.sessions
  const sessions = useMemo(() => applySessionListState(all ?? [], state), [all, state])
  // Each source once, by id; two sources may share a name.
  const sources = useMemo(() => {
    const byId = new Map<string, string>()
    for (const session of all ?? []) byId.set(session.source.source_id, session.source.name)
    return [...byId].map(([id, name]) => ({ id, name })).sort((a, b) => a.name.localeCompare(b.name, 'en'))
  }, [all])
  const filtering = hasActiveFilters(state)

  // The ticket page's back link returns to this exact view.
  useEffect(() => {
    try {
      window.sessionStorage.setItem(SESSION_LIST_QUERY_STORAGE_KEY, urlQuery)
    } catch {
      // Storage can be unavailable; the back link then opens the plain list.
    }
  }, [urlQuery])

  const clearFilters = () => update({ ...DEFAULT_SESSION_LIST_STATE, sort: state.sort, dir: state.dir })

  return (
    <div className="flex flex-col gap-4 px-6 pb-6 pt-6 max-md:px-3">
      <PageHeader title="Inbox" />
      <div className="flex flex-wrap items-center gap-3">
        <div className="relative min-w-48 flex-1">
          <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
          <Input
            type="search"
            aria-label="Search sessions"
            placeholder="Find a session, requester, agent, or ID"
            value={state.q}
            onChange={event => update({ q: event.target.value })}
            className="h-9 rounded-lg pl-9"
          />
        </div>
        {filtering ? <Button variant="secondary" onClick={clearFilters}>Clear filters</Button> : null}
      </div>

      <div className="overflow-hidden rounded-lg border">
        <table className="w-full table-fixed text-sm">
          <caption className="sr-only">Inbox</caption>
          <thead className="bg-muted/30">
            <tr>
              <ColumnHeader
                label="Name"
                field="name"
                state={state}
                update={update}
                filtered={Boolean(state.name)}
                filter={(
                  <div className="flex flex-col gap-2 p-2">
                    <DropdownMenuLabel className="p-0 text-xs text-muted-foreground">Name contains</DropdownMenuLabel>
                    <Input
                      aria-label="Name contains"
                      placeholder="travel plan"
                      value={state.name}
                      onKeyDown={keepTyping}
                      onChange={event => update({ name: event.target.value })}
                      className="h-8"
                    />
                  </div>
                )}
              />
              <ColumnHeader
                label="Status"
                field="status"
                state={state}
                update={update}
                className="w-32 sm:w-36"
                filtered={state.status.length > 0}
                filter={SESSION_STATES.map(value => (
                  <DropdownMenuCheckboxItem
                    key={value}
                    checked={state.status.includes(value)}
                    onSelect={event => event.preventDefault()}
                    onCheckedChange={() => update({ status: SESSION_STATES.filter(item => toggle(state.status, value).includes(item)) as SessionState[] })}
                  >
                    {sessionStateView(value).label}
                  </DropdownMenuCheckboxItem>
                ))}
              />
              <ColumnHeader
                label="Requester"
                field="source"
                state={state}
                update={update}
                className="hidden w-[22%] md:table-cell"
                filtered={state.source.length > 0}
                filter={sources.length ? sources.map(source => (
                  <DropdownMenuCheckboxItem
                    key={source.id}
                    checked={state.source.includes(source.id)}
                    onSelect={event => event.preventDefault()}
                    onCheckedChange={() => update({ source: toggle(state.source, source.id) })}
                  >
                    <span className="truncate">{source.name}</span>
                  </DropdownMenuCheckboxItem>
                )) : <p className="px-2 py-1.5 text-sm text-muted-foreground">No requesters yet</p>}
              />
              <ColumnHeader
                label="Created"
                field="created"
                state={state}
                update={update}
                className="hidden w-40 lg:table-cell"
                filtered={state.created !== 'any'}
                filter={<RangeFilter value={state.created} onChange={created => update({ created })} />}
              />
              <ColumnHeader
                label="Updated"
                field="updated"
                state={state}
                update={update}
                className="hidden w-40 pr-4 sm:table-cell"
                filtered={state.updated !== 'any'}
                filter={<RangeFilter value={state.updated} onChange={updated => update({ updated })} />}
              />
            </tr>
          </thead>
          <tbody>
            {query.isError ? (
              <tr><td colSpan={5} className="p-4">
                <div className="flex flex-wrap items-center gap-3">
                  <span>Sessions couldn&apos;t load.</span>
                  <Button variant="secondary" size="sm" onClick={() => { void query.refetch() }}>
                    <RefreshCw data-icon="inline-start" aria-hidden="true" />Retry
                  </Button>
                </div>
              </td></tr>
            ) : query.isLoading ? (
              Array.from({ length: 5 }, (_, index) => (
                <tr key={index} className="border-t"><td colSpan={5} className="px-4 py-3"><Skeleton className="h-5" /></td></tr>
              ))
            ) : !all?.length ? (
              <tr><td colSpan={5} className="px-4 py-8 text-muted-foreground">
                No sessions yet. A client opens one with the MCP <code className="font-mono text-xs">send_message</code> tool
                or <code className="font-mono text-xs">POST /sessions/messages</code>.
              </td></tr>
            ) : sessions.length === 0 ? (
              <tr><td colSpan={5} className="px-4 py-8">
                <div className="flex flex-wrap items-center gap-3 text-muted-foreground">
                  No sessions match these filters.
                  <Button variant="secondary" size="sm" onClick={clearFilters}>Clear filters</Button>
                </div>
              </td></tr>
            ) : (
              sessions.map(session => <SessionRow key={session.session_id} session={session} />)
            )}
          </tbody>
        </table>
      </div>
      {all?.length ? (
        <p className="text-xs text-muted-foreground">
          {filtering ? `${sessions.length} of ${all.length} sessions` : `${all.length} ${all.length === 1 ? 'session' : 'sessions'}`}
        </p>
      ) : null}
    </div>
  )
}
