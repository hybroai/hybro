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
import {
  closeSession,
  getSessionRecord,
  getSessionSteps,
  type SessionRecord,
  type SessionStep,
  type SessionStepItem,
  type SessionStepQuestion,
} from '@/lib/api/session'
import { useAuth } from '@/lib/auth'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { parseHitlPresentation } from '@/lib/hitl/presentation'
import { routes } from '@/lib/routes'
import { hitlLifecycleState } from '@/lib/selectors/select-hitl'
import { formatSessionDuration, formatSessionTime, sessionAgentNames, sessionClientLabel, sessionClientName } from '@/lib/session-format'
import { ClientLabel } from './client-label'
import { SESSION_LIST_QUERY_STORAGE_KEY, sessionName } from '@/lib/session-list-state'
import { sessionStateView } from '@/lib/status'
import { cn } from '@/lib/utils'

const ACTIVE_POLL_MS = 4_000
const IDLE_POLL_MS = 20_000
const LONG_REPLY_CHARS = 600
const LONG_REPLY_LINES = 8

const recordKey = (sessionId: string) => ['session-record', sessionId] as const
const stepsKey = (sessionId: string) => ['session-steps', sessionId] as const
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

type Actor = 'client' | 'agent' | 'owner'

// A registered agent shows its own avatar; the external client, or an
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

function Items({ items }: { items: SessionStepItem[] }) {
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

// What the record says when a Question ended without an answer.
const QUESTION_END: Partial<Record<SessionStepQuestion['outcome'], string>> = {
  canceled: 'The question was canceled.',
  expired: 'The question expired before anyone answered.',
  error: 'The question could not be completed.',
  unanswered: 'The request ended before anyone answered.',
}

// A run's failure code, as the owner reads it. A reason that is not a code is
// the run's own summary and is shown as it is; an unknown code is not shown.
const FAILURE_TEXT: Record<string, string> = {
  budget_exhausted: 'The run used up its execution budget.',
  provider_error: 'The model provider returned an error.',
  assembly_error: 'The request could not be prepared.',
  tool_failure: 'An agent call failed.',
  hitl_error: 'A question could not be completed.',
  rejected: 'The request was rejected.',
  internal_error: 'The run could not be completed.',
}

function failureText(reason: string | null): string | null {
  if (!reason) return null
  if (FAILURE_TEXT[reason]) return FAILURE_TEXT[reason]
  return /^[a-z0-9_:]+$/.test(reason) ? null : reason
}

type Answerer = { actor: Actor, name: string, recorded: boolean }

// Who answered a Question: you, the session's client, another client, or
// not recorded (an answer written before the record said who answered).
function answerer(question: SessionStepQuestion, step: SessionStep, client: string): Answerer {
  if (question.answered_by === 'owner') return { actor: 'owner', name: 'You', recorded: true }
  if (question.answered_by === 'client') {
    const named = step.client_id != null && question.answered_by_client_id === step.client_id
    return { actor: 'client', name: named ? client : 'A client', recorded: true }
  }
  return { actor: 'owner', name: 'Someone', recorded: false }
}

function QuestionEntries({ question, step, agent, agentId, client, open }: {
  question: SessionStepQuestion
  step: SessionStep
  agent: string
  agentId?: string
  client: string
  open: boolean
}) {
  const askedBy = question.asked_by === 'system' ? 'Your check rules' : agent
  const entries: ReactNode[] = [
    <Entry
      key="asked"
      actor={question.asked_by === 'system' ? 'owner' : 'agent'}
      agentId={agentId}
      name={askedBy}
      action={open ? 'needs your answer below' : 'asked you'}
      at={question.asked_at}
    >
      {!open && question.items.length ? <Items items={question.items} /> : null}
    </Entry>,
  ]
  if (question.outcome === 'answered') {
    const by = answerer(question, step, client)
    entries.push(
      <Entry
        key="answered"
        actor={by.actor}
        name={by.name}
        action={by.actor === 'client' ? 'answered for you' : 'answered'}
        at={question.resolved_at}
      >
        {by.recorded ? null : <p className="text-xs text-muted-foreground">Who answered was not recorded.</p>}
        {question.answers.length ? <Items items={question.answers} /> : null}
      </Entry>,
    )
  } else if (question.outcome === 'canceled' && step.state === 'rejected') {
    entries.push(<Entry key="rejected" actor="owner" name="You" action="rejected the request" at={question.resolved_at} />)
  } else if (QUESTION_END[question.outcome]) {
    entries.push(
      <Entry key="ended" actor="agent" agentId={agentId} name={agent} action="got no answer" at={question.resolved_at}>
        <p className="text-muted-foreground">{QUESTION_END[question.outcome]}</p>
      </Entry>,
    )
  }
  return <>{entries}</>
}

function StepEntries({ step, client, waitingBelow }: {
  step: SessionStep
  client: string
  waitingBelow: boolean
}) {
  const agent = step.agent_name ?? 'The agent'
  const agentId = step.agent_id ?? undefined
  const rejectedByQuestion = step.questions.some(question => question.outcome === 'canceled')
  // A refusal at a Question is the last answer's; credit whoever gave it.
  const lastAnswer = step.questions.filter(question => question.outcome === 'answered').at(-1)
  const rejecter = lastAnswer ? answerer(lastAnswer, step, client) : { actor: 'owner' as const, name: 'You', recorded: true }
  const after = step.duration_ms != null ? ` after ${formatSessionDuration(step.duration_ms)}` : ''
  const canceled = step.state === 'failed' && (step.reason === 'canceled' || step.reason?.startsWith('canceled:'))
  return (
    <>
      <Entry actor="client" name={client} action={step.agent_name ? `asked ${step.agent_name}` : 'sent a request'} at={step.sent_at}>
        <p className="whitespace-pre-wrap">{step.prompt || 'Request text unavailable'}</p>
        {step.skill ? <p className="text-xs text-muted-foreground">Skill: {step.skill}</p> : null}
      </Entry>
      {step.questions.map(question => (
        <QuestionEntries
          key={question.question_id}
          question={question}
          step={step}
          agent={agent}
          agentId={agentId}
          client={client}
          open={waitingBelow && question.outcome === 'pending'}
        />
      ))}
      {step.state === 'completed' ? (
        <Entry actor="agent" agentId={agentId} name={agent} action={`replied${after}`} at={step.finished_at}>
          {step.result ? <Reply text={step.result} /> : <p className="text-muted-foreground">The reply is not available.</p>}
        </Entry>
      ) : step.state === 'denied' ? (
        <Entry actor="owner" name="Your check rules" action="denied this agent call" at={step.finished_at}>
          {step.reason ? <p className="text-muted-foreground">{step.reason}</p> : null}
        </Entry>
      ) : step.state === 'rejected' && !rejectedByQuestion ? (
        <Entry actor={rejecter.actor} name={rejecter.name} action="rejected the request" at={step.finished_at} />
      ) : canceled ? (
        <Entry actor="agent" agentId={agentId} name={agent} action={`stopped${after}; the request was canceled`} at={step.finished_at} />
      ) : step.state === 'failed' && step.reason === 'run_not_started' ? (
        <Entry actor="agent" agentId={agentId} name={agent} action="never started" at={null}>
          <p className="text-destructive">The request never started.</p>
        </Entry>
      ) : step.state === 'failed' ? (
        <Entry actor="agent" agentId={agentId} name={agent} action={`couldn't finish${after}`} at={step.finished_at}>
          <p className="text-destructive">{failureText(step.reason) ?? 'The request failed.'}</p>
        </Entry>
      ) : step.state === 'closed' ? (
        <Entry actor="agent" agentId={agentId} name={agent} action="stopped; the session was closed" at={step.finished_at} />
      ) : step.state === 'working' ? (
        <Entry actor="agent" agentId={agentId} name={agent} action={<span className="inline-flex items-center gap-1.5">is working on it<Loader2 className="size-3.5 animate-spin motion-reduce:animate-none" aria-hidden="true" /></span>} at={null} />
      ) : null}
    </>
  )
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
        <Detail label="Client">{sessionClientName(record)}</Detail>
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
                Any pending question or running request is canceled, and the client can no longer send messages here. The record stays available.
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

  const steps = useQuery({
    queryKey: stepsKey(sessionId),
    queryFn: ({ signal }) => getSessionSteps(sessionId, getToken, signal),
    enabled: Boolean(conversationId),
    refetchInterval: pollMs,
  })
  const pending = useQuery({
    queryKey: pendingKey(conversationId ?? ''),
    queryFn: () => fetchPendingHitlRequests(conversationId as string, getToken),
    enabled: Boolean(conversationId) && record.data?.status === 'open',
    refetchInterval: pollMs,
  })

  const sessionSteps = steps.data?.steps ?? []
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
      queryClient.invalidateQueries({ queryKey: stepsKey(sessionId) }),
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
          {steps.isLoading ? (
            <Skeleton className="h-24 w-full" />
          ) : steps.isError ? (
            <p className="text-sm text-destructive">The record couldn&apos;t load. It refreshes automatically.</p>
          ) : sessionSteps.length === 0 ? (
            <p className="text-sm text-muted-foreground">No requests yet.</p>
          ) : (
            <ol aria-label="Session record" className="flex flex-col gap-6">
              {sessionSteps.map((step, index) => (
                <li key={step.step_id} className="flex flex-col gap-6">
                  {index > 0 ? <hr className="border-border" /> : null}
                  <ol className="flex flex-col gap-6">
                    <StepEntries
                      step={step}
                      client={sessionClientName(data)}
                      waitingBelow={deciding && step.state === 'waiting_on_owner'}
                    />
                  </ol>
                </li>
              ))}
            </ol>
          )}
          {deciding && conversationId ? (
            <div ref={cardRef}>
              <HitlResponseBar hitls={prompts} onSubmit={answer} onCancel={reject} onRefresh={refresh} />
            </div>
          ) : null}
        </section>
        <DetailsPanel record={data} requests={sessionSteps.length} closing={close.isPending} onClose={() => close.mutate()} />
      </div>
    </div>
  )
}
