'use client'

import { useEffect, useMemo, useRef, useState, useSyncExternalStore, type ReactNode } from 'react'
import Link from 'next/link'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, Bot, Check, Copy, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
  AlertDialogTrigger,
} from '@/components/ui/alert-dialog'
import { Button, buttonVariants } from '@/components/ui/button'
import { Skeleton } from '@/components/ui/skeleton'
import { HitlResponseBar, type HitlBatchAnswer, type HitlPromptView } from '@/components/composer/HitlResponseBar'
import { MarkdownContent } from '@/components/markdown-content'
import { StatusIndicator } from '@/components/status-indicator'
import {
  cancelHitl,
  fetchPendingHitlRequests,
  respondToHitlBatch,
  type HitlPendingRequest,
} from '@/lib/api/hitl'
import { closeSession, fetchSessionEvents, getSessionRecord, type SessionRecord } from '@/lib/api/session'
import { useAuth } from '@/lib/auth'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { parseHitlPresentation } from '@/lib/hitl/presentation'
import { routes } from '@/lib/routes'
import { hitlLifecycleState } from '@/lib/selectors/select-hitl'
import { formatSessionDuration, formatSessionTime, sessionAgentNames, sessionClientLabel, sessionRequester } from '@/lib/session-format'
import { ClientLabel } from './client-label'
import { SESSION_LIST_QUERY_STORAGE_KEY, sessionName } from '@/lib/session-list-state'
import { foldSessionEvents, type ExchangeItem, type SessionExchange } from '@/lib/session-record'
import { sessionStateView } from '@/lib/status'
import { cn } from '@/lib/utils'

const ACTIVE_POLL_MS = 4_000
const IDLE_POLL_MS = 20_000
const LONG_REPLY_CHARS = 600
const LONG_REPLY_LINES = 8

const recordKey = (sessionId: string) => ['session-record', sessionId] as const
const eventsKey = (conversationId: string) => ['session-events', conversationId] as const
const pendingKey = (conversationId: string) => ['session-pending', conversationId] as const

function newRequestId(): string {
  return typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID()
    : `owner-${Date.now()}-${Math.random().toString(16).slice(2)}`
}

// The pending API returns one row per question; the chat question card takes
// the same shape the chat store produces.
function toPromptView(request: HitlPendingRequest): HitlPromptView | null {
  if (!request.interaction_id) return null
  return {
    hitlId: request.request_id,
    source: request.source,
    agentName: request.agent_name,
    prompt: request.prompt,
    promptType: request.prompt_type,
    choices: request.choices ?? undefined,
    presentation: parseHitlPresentation(request),
    interactionId: request.interaction_id,
    lifecycleState: hitlLifecycleState({
      interactionStatus: request.interaction_status,
      applicationStatus: request.application_status,
      question: request.prompt,
    }),
    errorMessage: request.application_error ?? undefined,
    clientRequestId: request.client_request_id ?? undefined,
    groupIndex: request.question_index,
  }
}

// The remembered list view only changes on the list page, so there is
// nothing to subscribe to here.
const noSubscription = () => () => {}

function listHref(): string {
  try {
    const query = window.sessionStorage.getItem(SESSION_LIST_QUERY_STORAGE_KEY)
    return query ? `${routes.sessions}?${query}` : routes.sessions
  } catch {
    return routes.sessions
  }
}

// ── Record ──────────────────────────────────────────────────────────────

type Actor = 'requester' | 'agent' | 'owner'

// A registered agent shows its own avatar; the external requester, or an
// agent the session no longer lists, shows a generic agent avatar.
function Avatar({ actor, agentId }: { actor: Actor; agentId?: string }) {
  if (actor === 'agent' && agentId) {
    return (
      // eslint-disable-next-line @next/next/no-img-element -- generated data URI
      <img
        data-avatar={agentId}
        src={getAgentAvatarUri(agentId)}
        alt=""
        className="size-8 shrink-0 rounded-lg"
      />
    )
  }
  if (actor === 'owner') {
    return (
      <span
        data-avatar="owner"
        aria-hidden="true"
        className="flex size-8 shrink-0 items-center justify-center rounded-full bg-status-online/15 text-xs font-semibold text-status-online"
      >
        You
      </span>
    )
  }
  return (
    <span
      data-avatar="external-agent"
      aria-hidden="true"
      className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-muted text-muted-foreground"
    >
      <Bot className="size-4" />
    </span>
  )
}

function Entry({ actor, agentId, name, action, at, children }: {
  actor: Actor
  agentId?: string
  name: string
  action: ReactNode
  at: string | null
  children?: ReactNode
}) {
  return (
    <li className="flex gap-3">
      <Avatar actor={actor} agentId={agentId} />
      <div className="flex min-w-0 flex-1 flex-col gap-1.5 pt-1.5">
        <p className="text-sm leading-5">
          <span className="font-medium">{name}</span>{' '}
          <span className="text-muted-foreground">{action}</span>
          {at ? (
            <>
              <span aria-hidden="true" className="text-muted-foreground"> · </span>
              <time dateTime={at} className="text-xs text-muted-foreground">{formatSessionTime(at)}</time>
            </>
          ) : null}
        </p>
        {children ? <div className="min-w-0 text-sm leading-relaxed">{children}</div> : null}
      </div>
    </li>
  )
}

function Items({ items }: { items: ExchangeItem[] }) {
  if (items.length === 1 && !items[0].label) {
    return <p className="whitespace-pre-wrap">{items[0].text}</p>
  }
  return (
    <dl className="flex flex-col gap-2">
      {items.map((item, index) => (
        <div key={index} className="flex flex-col gap-0.5">
          {item.label ? <dt className="text-xs text-muted-foreground">{item.label}</dt> : null}
          <dd className="whitespace-pre-wrap">{item.text}</dd>
        </div>
      ))}
    </dl>
  )
}

function Reply({ text }: { text: string }) {
  const [expanded, setExpanded] = useState(false)
  const long = text.length > LONG_REPLY_CHARS || text.split('\n').length > LONG_REPLY_LINES
  return (
    <div className="flex flex-col gap-1.5">
      <div className={cn(long && !expanded && 'max-h-40 overflow-hidden')}>
        <MarkdownContent content={text} className="text-sm" />
      </div>
      {long ? (
        <Button variant="link" size="sm" className="h-auto w-fit p-0" onClick={() => setExpanded(value => !value)}>
          {expanded ? 'Show less' : 'Show full reply'}
        </Button>
      ) : null}
    </div>
  )
}

function ExchangeEntries({ exchange, requester, agents, waitingBelow }: {
  exchange: SessionExchange
  requester: string
  agents: SessionRecord['agents']
  waitingBelow: boolean
}) {
  const agent = exchange.agentName ?? 'The agent'
  const agentId = agents.find(item => item.name === exchange.agentName)?.agent_id
  const entries: ReactNode[] = [
    <Entry key="sent" actor="requester" name={requester} action={exchange.agentName ? `asked ${exchange.agentName}` : 'sent a request'} at={exchange.startedAt}>
      <p className="whitespace-pre-wrap">{exchange.task ?? 'Request text unavailable'}</p>
    </Entry>,
  ]
  exchange.steps.forEach((step, index) => {
    if (step.kind === 'asked') {
      const open = waitingBelow && !exchange.steps.slice(index + 1).some(next => next.kind === 'answered' || next.kind === 'rejected')
      entries.push(
        <Entry key={index} actor="agent" agentId={agentId} name={agent} action={open ? 'needs your answer below' : 'asked you'} at={step.at}>
          {!open && step.items?.length ? <Items items={step.items} /> : null}
        </Entry>,
      )
    } else if (step.kind === 'answered') {
      entries.push(
        <Entry key={index} actor="owner" name="You" action="answered" at={step.at}>
          {step.items?.length ? <Items items={step.items} /> : null}
        </Entry>,
      )
    } else if (step.kind === 'rejected') {
      entries.push(<Entry key={index} actor="owner" name="You" action="rejected the request" at={step.at} />)
    } else if (step.kind === 'replied') {
      const after = exchange.durationMs != null ? ` after ${formatSessionDuration(exchange.durationMs)}` : ''
      entries.push(
        <Entry key={index} actor="agent" agentId={agentId} name={agent} action={`replied${after}`} at={step.at}>
          {exchange.reply ? <Reply text={exchange.reply} /> : <p className="text-muted-foreground">The reply is not available.</p>}
        </Entry>,
      )
    } else if (step.kind === 'failed') {
      entries.push(
        <Entry key={index} actor="agent" agentId={agentId} name={agent} action="couldn't finish" at={step.at}>
          <p className="text-destructive">{exchange.failure ?? 'The request failed.'}</p>
        </Entry>,
      )
    } else if (step.kind === 'canceled') {
      entries.push(<Entry key={index} actor="agent" agentId={agentId} name={agent} action="stopped; the request was canceled" at={step.at} />)
    }
  })
  if (exchange.status === 'working') {
    entries.push(
      <Entry key="working" actor="agent" agentId={agentId} name={agent} action={<span className="inline-flex items-center gap-1.5">is working on it<Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /></span>} at={null} />,
    )
  }
  return <>{entries}</>
}

// ── Details panel ───────────────────────────────────────────────────────

function Detail({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <dt className="text-xs text-muted-foreground">{label}</dt>
      <dd className="min-w-0 text-sm">{children}</dd>
    </div>
  )
}

function CopySessionId({ value }: { value: string }) {
  const [copied, setCopied] = useState(false)
  return (
    <span className="flex min-w-0 items-center gap-1">
      <span className="min-w-0 truncate font-mono text-xs">{value}</span>
      <Button
        variant="ghost"
        size="iconSm"
        aria-label="Copy session ID"
        className="size-7 shrink-0"
        onClick={() => {
          void navigator.clipboard?.writeText(value).then(() => {
            setCopied(true)
            window.setTimeout(() => setCopied(false), 1500)
          })
        }}
      >
        {copied ? <Check aria-hidden="true" /> : <Copy aria-hidden="true" />}
      </Button>
    </span>
  )
}

function DetailsPanel({ record, requests, closing, onClose }: {
  record: SessionRecord
  requests: number
  closing: boolean
  onClose: () => void
}) {
  return (
    <aside aria-labelledby="session-details" className="flex h-fit flex-col gap-4 rounded-lg border p-4 lg:sticky lg:top-6">
      <h2 id="session-details" className="text-sm font-medium">Details</h2>
      <dl className="flex flex-col gap-3.5">
        <Detail label="Status"><StatusIndicator view={sessionStateView(record.state)} /></Detail>
        <Detail label="Requester">{sessionRequester(record)}</Detail>
        {sessionClientLabel(record) ? (
          <Detail label="Client label"><ClientLabel label={sessionClientLabel(record)!} className="block text-sm" /></Detail>
        ) : null}
        <Detail label="Agents">{sessionAgentNames(record.agents) || 'None yet'}</Detail>
        <Detail label="Created"><time dateTime={record.opened_at}>{formatSessionTime(record.opened_at)}</time></Detail>
        <Detail label="Updated"><time dateTime={record.last_activity_at}>{formatSessionTime(record.last_activity_at)}</time></Detail>
        {record.closed_at ? (
          <Detail label="Closed"><time dateTime={record.closed_at}>{formatSessionTime(record.closed_at)}</time></Detail>
        ) : null}
        <Detail label="Requests">{requests}</Detail>
        <Detail label="Session ID"><CopySessionId value={record.session_id} /></Detail>
      </dl>
      {record.status === 'open' ? (
        <AlertDialog>
          <AlertDialogTrigger asChild>
            <Button variant="secondary" disabled={closing} className="w-full">
              {closing ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
              Close session
            </Button>
          </AlertDialogTrigger>
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>Close this session?</AlertDialogTitle>
              <AlertDialogDescription>
                Any pending question or running request is canceled, and the requester can no longer send messages here. The record stays available.
              </AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Keep open</AlertDialogCancel>
              <AlertDialogAction onClick={onClose} className={buttonVariants({ variant: 'destructive' })}>Close</AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        </AlertDialog>
      ) : null}
    </aside>
  )
}

// ── Page ────────────────────────────────────────────────────────────────

export function SessionRecordView({ sessionId }: { sessionId: string }) {
  const { getToken } = useAuth()
  const queryClient = useQueryClient()
  const backHref = useSyncExternalStore(noSubscription, listHref, () => routes.sessions)
  const cardRef = useRef<HTMLDivElement>(null)
  const scrolledToCard = useRef(false)

  const record = useQuery({
    queryKey: recordKey(sessionId),
    queryFn: ({ signal }) => getSessionRecord(sessionId, getToken, signal),
    refetchInterval: query => {
      const state = query.state.data?.state
      return state === 'working' || state === 'waiting_on_owner' ? ACTIVE_POLL_MS : IDLE_POLL_MS
    },
    retry: false,
  })
  const conversationId = record.data?.conversation_id ?? null
  const active = record.data?.state === 'working' || record.data?.state === 'waiting_on_owner'
  const pollMs = active ? ACTIVE_POLL_MS : IDLE_POLL_MS

  const events = useQuery({
    queryKey: eventsKey(conversationId ?? ''),
    queryFn: ({ signal }) => fetchSessionEvents(conversationId as string, getToken, signal),
    enabled: Boolean(conversationId),
    refetchInterval: pollMs,
  })
  const pending = useQuery({
    queryKey: pendingKey(conversationId ?? ''),
    queryFn: () => fetchPendingHitlRequests(conversationId as string, getToken),
    enabled: Boolean(conversationId) && record.data?.status === 'open',
    refetchInterval: pollMs,
  })

  const exchanges = useMemo(() => foldSessionEvents(events.data?.events ?? []), [events.data])
  const prompts = useMemo(() => {
    const views = (pending.data?.requests ?? []).map(toPromptView).filter(view => view !== null)
    const first = views[0]?.interactionId
    return views.filter(view => view.interactionId === first)
  }, [pending.data])
  const deciding = record.data?.status === 'open' && prompts.length > 0

  // Open on the question when the session is waiting for the owner.
  useEffect(() => {
    if (!deciding || scrolledToCard.current) return
    scrolledToCard.current = true
    cardRef.current?.scrollIntoView?.({ block: 'center' })
  }, [deciding])

  const refresh = async () => {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: recordKey(sessionId) }),
      conversationId ? queryClient.invalidateQueries({ queryKey: eventsKey(conversationId) }) : null,
      conversationId ? queryClient.invalidateQueries({ queryKey: pendingKey(conversationId) }) : null,
      queryClient.invalidateQueries({ queryKey: ['sessions'] }),
    ])
  }

  const answer = async (interactionId: string, answers: HitlBatchAnswer[], clientRequestId?: string) => {
    await respondToHitlBatch(conversationId as string, interactionId, answers, clientRequestId ?? newRequestId(), getToken)
    void refresh()
  }
  const reject = async (_requestId: string, interactionId?: string) => {
    const version = pending.data?.requests.find(request => request.interaction_id === interactionId)?.interaction_version ?? 1
    try {
      await cancelHitl(conversationId as string, interactionId as string, version, newRequestId(), getToken)
    } catch {
      toast.error("The request couldn't be rejected. Try again.")
    }
    await refresh()
  }

  const close = useMutation({
    mutationFn: () => closeSession(sessionId, getToken),
    onSuccess: closed => {
      queryClient.setQueryData<SessionRecord>(recordKey(sessionId), current => current && {
        ...current,
        status: closed.status,
        state: closed.status === 'closed' ? 'closed' : current.state,
        closed_at: current.closed_at ?? new Date().toISOString(),
      })
      void refresh()
    },
    onError: () => toast.error("The session couldn't be closed. Try again."),
  })

  const back = (
    <Link href={backHref} className="inline-flex w-fit items-center gap-1.5 text-sm text-muted-foreground hover:text-foreground">
      <ArrowLeft className="size-4" aria-hidden="true" />Inbox
    </Link>
  )

  if (record.isLoading) {
    return (
      <div className="flex flex-col gap-4 px-6 py-6 max-md:px-3" aria-label="Loading session">
        {back}
        <Skeleton className="h-8 w-80" />
        <Skeleton className="h-24 w-full max-w-3xl" />
      </div>
    )
  }
  if (record.isError || !record.data) {
    return (
      <div className="flex flex-col gap-4 px-6 py-6 max-md:px-3">
        {back}
        <p className="text-sm text-muted-foreground">This session was not found.</p>
      </div>
    )
  }

  const data = record.data
  return (
    <div className="flex flex-col gap-4 px-6 py-6 max-md:px-3">
      {back}
      <div className="grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_18rem]">
        <section aria-labelledby="session-title" className="flex min-w-0 max-w-3xl flex-col gap-6">
          <h1 id="session-title" className="text-2xl font-semibold tracking-tight">{sessionName(data)}</h1>
          {events.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : events.isError ? (
            <p className="text-sm text-destructive">The record couldn&apos;t load. It refreshes automatically.</p>
          ) : exchanges.length === 0 ? (
            <p className="text-sm text-muted-foreground">No requests yet.</p>
          ) : (
            <ol aria-label="Session record" className="flex flex-col gap-6">
              {exchanges.map((exchange, index) => (
                <li key={exchange.runId} className="flex flex-col gap-6">
                  {index > 0 ? <hr className="border-border" /> : null}
                  <ol className="flex flex-col gap-6">
                    <ExchangeEntries
                      exchange={exchange}
                      requester={sessionRequester(data)}
                      agents={data.agents}
                      waitingBelow={deciding && exchange.status === 'waiting'}
                    />
                  </ol>
                </li>
              ))}
            </ol>
          )}
          {data.state === 'failed' && data.reason === 'run_not_started' ? (
            <p className="text-sm text-destructive">The last request never started.</p>
          ) : null}
          {deciding && conversationId ? (
            <div ref={cardRef}>
              <HitlResponseBar hitls={prompts} onSubmit={answer} onCancel={reject} onRefresh={refresh} />
            </div>
          ) : null}
        </section>
        <DetailsPanel record={data} requests={exchanges.length} closing={close.isPending} onClose={() => close.mutate()} />
      </div>
    </div>
  )
}
