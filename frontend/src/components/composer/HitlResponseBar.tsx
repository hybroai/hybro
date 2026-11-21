'use client'

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AlertTriangle,
  ArrowRight,
  CalendarDays,
  Check,
  KeyRound,
  LoaderCircle,
  RefreshCw,
  ShieldAlert,
  X,
} from 'lucide-react'
import { ApiError } from '@/lib/api-client'
import { useHitlInteractionController } from '@/hooks/useHitlInteractionController'
import type { HitlDraftValue } from '@/lib/hitl/interaction-controller'
import type { HITLPromptType } from '@/lib/types/sse'
import type { HitlLifecycleState } from '@/lib/selectors/conversation-types'
import type { HitlQuestionPresentation } from '@/lib/hitl/presentation'
import { cn } from '@/lib/utils'
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
import { Card, CardContent } from '@/components/ui/card'
import { Input } from '@/components/ui/input'
import {
  Questionnaire,
  QuestionnaireChoice,
  QuestionnaireChoiceDescription,
  QuestionnaireChoices,
  QuestionnaireError,
  QuestionnaireInput,
  QuestionnaireItem,
  QuestionnaireNext,
  QuestionnairePrevious,
  QuestionnaireSkip,
  QuestionnaireSubmit,
  QuestionnaireTextarea,
  QuestionnaireTitle,
} from '@/components/ui/questionnaire'

export interface HitlPromptView {
  hitlId: string
  source: 'supervisor' | 'agent' | 'system'
  agentName?: string
  prompt: string
  promptType: HITLPromptType
  choices?: string[]
  interactionId: string
  lifecycleState: HitlLifecycleState
  errorMessage?: string
  clientRequestId?: string
  answer?: string
  groupIndex?: number
  presentation?: HitlQuestionPresentation
}

export interface HitlBatchAnswer {
  requestId: string
  answer: string
}

interface HitlResponseBarProps {
  hitls: HitlPromptView[]
  onSubmit: (
    interactionId: string,
    answers: HitlBatchAnswer[],
    clientRequestId?: string,
  ) => Promise<void>
  onCancel?: (requestId: string, interactionId?: string) => Promise<void>
  onRefresh?: () => Promise<void>
}

type DraftValue = HitlDraftValue

const GENERIC_PROMPT = /^the agent needs additional information\.?$/i
const APPLYING_REFRESH_MS = 1500
const APPROVAL_CHOICES = ['Approve', 'Reject']
const CONFIRMATION_CHOICES = ['Confirm', 'Decline']
const AUTHENTICATION_CHOICES = [
  'Authentication complete',
  'Unable to authenticate',
]

function choiceOptions(hitl: HitlPromptView): string[] {
  if (
    hitl.promptType === 'choice'
    || hitl.promptType === 'single_choice'
    || hitl.promptType === 'multi_choice'
  ) {
    return hitl.choices ?? []
  }
  if (hitl.promptType === 'approval') return APPROVAL_CHOICES
  if (hitl.promptType === 'confirmation') return CONFIRMATION_CHOICES
  if (hitl.promptType === 'authentication') return AUTHENTICATION_CHOICES
  return []
}

// A choice question that allows Other keeps the typed text in its own draft
// slot; the selection carries this marker in place of a listed value.
const OTHER_CHOICE = '__other__'

function otherDraftKey(requestId: string): string {
  return `${requestId}:other`
}

function isRequired(hitl: HitlPromptView): boolean {
  return hitl.presentation?.required !== false
}

function answerText(hitl: HitlPromptView, drafts: Record<string, DraftValue>): string {
  const value = drafts[hitl.hitlId]
  const selected = Array.isArray(value) ? value : value ? [value] : []
  if (!selected.includes(OTHER_CHOICE)) {
    return Array.isArray(value) ? value.join(', ') : value?.trim() ?? ''
  }
  const other = drafts[otherDraftKey(hitl.hitlId)]
  const otherText = typeof other === 'string' ? other.trim() : ''
  if (!otherText) return ''
  return [...selected.filter(choice => choice !== OTHER_CHOICE), otherText].join(', ')
}

function hasAnswer(hitl: HitlPromptView, drafts: Record<string, DraftValue>): boolean {
  return answerText(hitl, drafts).length > 0
}

function choiceLabel(hitl: HitlPromptView, value: string): { label: string; description?: string } {
  const option = hitl.presentation?.choiceOptions?.find(item => item.value === value)
  return { label: option?.label ?? value, description: option?.description }
}

function sortHitls(hitls: HitlPromptView[]): HitlPromptView[] {
  return [...hitls].sort((a, b) => (
    (a.groupIndex ?? 0) - (b.groupIndex ?? 0)
    || a.hitlId.localeCompare(b.hitlId)
  ))
}

function RecoveryState({
  state,
  message,
  onCancel,
  onRefresh,
}: {
  state: Exclude<HitlLifecycleState, 'open'>
  message?: string
  onCancel?: () => Promise<void>
  onRefresh?: () => Promise<void>
}) {
  const [working, setWorking] = useState(false)
  const [actionError, setActionError] = useState<string | null>(null)
  const copy = {
    applying: {
      title: 'Applying your answers',
      body: 'Your responses are saved. Hybro is continuing the run.',
      icon: LoaderCircle,
    },
    expired: {
      title: 'Input request expired',
      body: 'The response deadline passed, so this run can no longer continue from this request.',
      icon: CalendarDays,
    },
    agent_timeout: {
      title: 'The agent stopped responding',
      body: 'The agent did not acknowledge the task in time. No result was produced from this step.',
      icon: AlertTriangle,
    },
    delivery_uncertain: {
      title: 'Checking whether your answers were received',
      body: 'The connection ended before the agent confirmed receipt. Do not submit the answers again yet.',
      icon: RefreshCw,
    },
    routing_failed: {
      title: 'This input request cannot be answered',
      body: 'Hybro did not receive a valid question or could not route the response. Your run is paused.',
      icon: ShieldAlert,
    },
    canceled: {
      title: 'Input request canceled',
      body: 'This request is no longer actionable.',
      icon: X,
    },
  }[state]
  const Icon = copy.icon

  const run = async (action: (() => Promise<void>) | undefined) => {
    if (!action) return
    setWorking(true)
    setActionError(null)
    try {
      await action()
    } catch (error) {
      setActionError(
        error instanceof Error ? error.message : 'Unable to complete this request.',
      )
    } finally {
      setWorking(false)
    }
  }

  return (
    <Card role="status" aria-live="polite" className="gap-0 overflow-hidden rounded-xl py-0 shadow-none">
      <CardContent className="flex flex-col gap-4 py-5 sm:flex-row sm:items-start">
        <Icon
          className={state === 'applying' ? 'size-5 shrink-0 animate-spin text-primary motion-reduce:animate-none' : 'size-5 shrink-0 text-primary'}
          aria-hidden="true"
        />
        <div className="min-w-0 flex-1">
          <h3 className="text-sm font-medium">{copy.title}</h3>
          <p className="mt-1 max-w-[68ch] text-sm text-muted-foreground">
            {message || copy.body}
          </p>
          {actionError ? (
            <p className="mt-2 text-sm text-destructive" role="alert">
              {actionError}
            </p>
          ) : null}
        </div>
        <div className="flex shrink-0 flex-wrap gap-2">
          {state === 'delivery_uncertain' && onRefresh ? (
            <Button type="button" variant="outline" disabled={working} onClick={() => run(onRefresh)}>
              <RefreshCw data-icon="inline-start" aria-hidden="true" />
              Check status
            </Button>
          ) : null}
          {(state === 'agent_timeout' || state === 'routing_failed') && onCancel ? (
            <Button type="button" variant="outline" disabled={working} onClick={() => run(onCancel)}>
              Cancel request
            </Button>
          ) : null}
        </div>
      </CardContent>
    </Card>
  )
}

function ChoiceRows({
  hitl,
  value,
  other,
  multiple,
  onChange,
  onOtherChange,
}: {
  hitl: HitlPromptView
  value: DraftValue | undefined
  other: string
  multiple: boolean
  onChange: (value: DraftValue) => void
  onOtherChange: (value: string) => void
}) {
  const selected = new Set(Array.isArray(value) ? value : value ? [value] : [])
  const allowOther = hitl.presentation?.allowOther === true
  const values = [...choiceOptions(hitl), ...(allowOther ? [OTHER_CHOICE] : [])]
  const toggle = (choice: string) => {
    if (!multiple) return onChange(choice)
    const next = new Set(selected)
    if (next.has(choice)) next.delete(choice)
    else next.add(choice)
    onChange(values.filter(item => next.has(item)))
  }

  return (
    <div className="flex flex-col gap-2">
      <QuestionnaireChoices>
        {values.map(choice => {
          const { label, description } = choice === OTHER_CHOICE
            ? { label: 'Other', description: 'Type your own answer' }
            : choiceLabel(hitl, choice)
          return (
            <QuestionnaireChoice
              key={choice}
              value={choice}
              checked={selected.has(choice)}
              onChange={() => toggle(choice)}
              className="gap-3"
            >
              <span className="flex flex-col gap-1">
                <span className="font-medium leading-5">{label}</span>
                {description ? (
                  <QuestionnaireChoiceDescription className="text-xs leading-5">{description}</QuestionnaireChoiceDescription>
                ) : null}
              </span>
            </QuestionnaireChoice>
          )
        })}
      </QuestionnaireChoices>
      {allowOther && selected.has(OTHER_CHOICE) ? (
        <Input
          aria-label="Other answer"
          placeholder="Your answer"
          value={other}
          onChange={event => onOtherChange(event.target.value)}
        />
      ) : null}
    </div>
  )
}

function PromptQuestion({
  hitl,
  value,
  other,
  active,
  onChange,
  onOtherChange,
}: {
  hitl: HitlPromptView
  value: DraftValue | undefined
  other: string
  active: boolean
  onChange: (value: DraftValue) => void
  onOtherChange: (value: string) => void
}) {
  const selected = new Set(Array.isArray(value) ? value : value ? [value] : [])
  const textValue = Array.isArray(value) ? value.join(', ') : value ?? ''

  if (
    hitl.promptType === 'choice'
    || hitl.promptType === 'single_choice'
    || hitl.promptType === 'multi_choice'
    || hitl.promptType === 'confirmation'
    || hitl.promptType === 'approval'
  ) {
    return (
      <ChoiceRows
        hitl={hitl}
        value={value}
        other={other}
        multiple={hitl.promptType === 'multi_choice'}
        onChange={onChange}
        onOtherChange={onOtherChange}
      />
    )
  }
  if (hitl.promptType === 'authentication') {
    return (
      <div className="grid gap-3 rounded-lg border bg-muted/40 p-3 sm:grid-cols-[auto_minmax(0,1fr)]">
        <KeyRound className="size-5 text-muted-foreground" aria-hidden="true" />
        <div className="min-w-0">
          <strong className="text-sm font-medium">Use the provider&apos;s secure sign-in page</strong>
          <p className="mt-1 text-sm text-muted-foreground">Never paste passwords, API keys, one-time codes, or recovery secrets into this chat.</p>
        </div>
        <QuestionnaireChoices className="sm:col-span-2">
          {choiceOptions(hitl).map(choice => (
            <QuestionnaireChoice
              key={choice}
              value={choice}
              checked={selected.has(choice)}
              onChange={() => onChange(choice)}
            >
              <span className="font-medium">{choice}</span>
            </QuestionnaireChoice>
          ))}
        </QuestionnaireChoices>
      </div>
    )
  }
  if (hitl.promptType === 'date') {
    return (
      <QuestionnaireInput
        type="date"
        value={textValue}
        onChange={event => onChange(event.target.value)}
      />
    )
  }
  if (hitl.promptType === 'textarea') {
    return (
      <QuestionnaireTextarea
        value={textValue}
        onChange={event => onChange(event.target.value)}
        placeholder={active ? 'Add the details needed to continue…' : undefined}
        rows={4}
      />
    )
  }
  return (
    <QuestionnaireInput
      value={textValue}
      onChange={event => onChange(event.target.value)}
      placeholder={active ? 'Type your answer…' : undefined}
    />
  )
}

export function HitlResponseBar({ hitls, onSubmit, onCancel, onRefresh }: HitlResponseBarProps) {
  const ordered = useMemo(() => sortHitls(hitls), [hitls])
  const interactionId = ordered[0]?.interactionId
  const interactionKey = `${interactionId ?? 'none'}:${ordered.map(hitl => hitl.hitlId).join(',')}`
  const draftKey = JSON.stringify([
    interactionId,
    ordered.map(hitl => [hitl.hitlId, hitl.prompt, hitl.promptType]),
  ])
  const seed = useMemo(() => ({
    interactionKey,
    draftKey,
    firstRequestId: ordered[0]?.hitlId ?? null,
    answers: ordered.map(hitl => ({
      requestId: hitl.hitlId,
      answer: hitl.answer,
    })),
  }), [draftKey, interactionKey, ordered])
  const { state: controller, dispatch } = useHitlInteractionController(seed)
  const {
    currentId,
    drafts,
    errorState,
    errorMessage: submissionError,
  } = controller
  const submitting = controller.submission === 'submitting'
  const submitted = controller.submission === 'submitted'
  const headingRef = useRef<HTMLHeadingElement>(null)
  const questionnaireRef = useRef<HTMLFormElement>(null)

  useEffect(() => {
    if (currentId && ordered.some(hitl => hitl.hitlId === currentId)) return
    const first = ordered[0]
    if (first) dispatch({ type: 'navigate', requestId: first.hitlId })
  }, [currentId, dispatch, ordered])

  const currentIndex = Math.max(0, ordered.findIndex(hitl => hitl.hitlId === currentId))
  const current = ordered[currentIndex] ?? ordered[0]
  // Optional questions may be left blank; they are left out of the answers.
  const allAnswered = ordered.every(hitl => !isRequired(hitl) || hasAnswer(hitl, drafts))

  // Authoritative lifecycle reconciliation: local submit/recovery state must
  // never override a fresher store lifecycle (e.g. SSE/refresh reporting
  // `failed`, `expired`, or `delivery_uncertain` after a successful submit).
  const authoritativeLifecycle = current?.lifecycleState
  const lastLifecycleRef = useRef<HitlLifecycleState | undefined>(undefined)
  useEffect(() => {
    if (!authoritativeLifecycle) return
    const previous = lastLifecycleRef.current
    lastLifecycleRef.current = authoritativeLifecycle
    if (previous !== undefined && previous !== authoritativeLifecycle) {
      dispatch({ type: 'server_reconciled', lifecycle: authoritativeLifecycle })
    }
  }, [authoritativeLifecycle, dispatch])

  const setDraft = useCallback((requestId: string, value: DraftValue) => {
    dispatch({ type: 'answer', requestId, value })
  }, [dispatch])

  const goTo = useCallback((index: number) => {
    const target = ordered[index]
    if (!target) return
    dispatch({ type: 'navigate', requestId: target.hitlId })
  }, [dispatch, ordered])

  // `decided` answers a one-question decision card directly from its button.
  const handleSubmit = useCallback(async (decided?: HitlBatchAnswer[]) => {
    if (!interactionId || submitting || (!decided && !allAnswered)) return
    dispatch({ type: 'submit_started' })
    try {
      await onSubmit(
        interactionId,
        decided ?? ordered
          .filter(hitl => hasAnswer(hitl, drafts))
          .map(hitl => ({ requestId: hitl.hitlId, answer: answerText(hitl, drafts) })),
        ordered.find(hitl => hitl.clientRequestId)?.clientRequestId,
      )
      dispatch({ type: 'submit_succeeded' })
    } catch (error) {
      const apiDetail = error instanceof ApiError && error.details && typeof error.details === 'object'
        ? (error.details as { detail?: { lifecycle_state?: string } }).detail
        : undefined
      if (
        error instanceof ApiError
        && (error.status === 503 || apiDetail?.lifecycle_state === 'delivery_uncertain')
      ) {
        dispatch({
          type: 'submit_failed',
          lifecycle: 'delivery_uncertain',
          message: 'The connection ended before receipt was confirmed.',
        })
      } else if (error instanceof ApiError && error.status === 410) {
        dispatch({
          type: 'submit_failed',
          lifecycle: 'expired',
          message: 'The response deadline passed before these answers were accepted.',
        })
      } else if (error instanceof ApiError && error.status === 409) {
        dispatch({
          type: 'submit_failed',
          message: 'This request changed before submission. Check its latest status before trying again.',
        })
      } else if (error instanceof ApiError && error.status === 502) {
        dispatch({
          type: 'submit_failed',
          lifecycle: 'routing_failed',
          message: 'Your answers were saved, but Hybro could not continue the run.',
        })
      } else if (error instanceof Error && error.name === 'AbortError') {
        dispatch({
          type: 'submit_failed',
          lifecycle: 'delivery_uncertain',
          message: 'The connection ended before receipt was confirmed.',
        })
      } else {
        dispatch({
          type: 'submit_failed',
          message: error instanceof Error ? error.message : 'Unable to submit these answers.',
        })
      }
    }
  }, [allAnswered, dispatch, drafts, interactionId, onSubmit, ordered, submitting])

  const lifecycleState = errorState ?? current?.lifecycleState
  const invalidPrompt = !current?.prompt.trim()
    || GENERIC_PROMPT.test(current?.prompt.trim() ?? '')
  const showApplying = Boolean(
    current && (submitted || lifecycleState === 'applying'),
  )
  const answerSurfaceReady = Boolean(
    current
    && !showApplying
    && lifecycleState === 'open'
    && !invalidPrompt,
  )
  const currentPromptType = current?.promptType

  useEffect(() => {
    if (!answerSurfaceReady) return
    const frame = window.requestAnimationFrame(() => {
      const acceptsTextInput = currentPromptType === 'text'
        || currentPromptType === 'textarea'
        || currentPromptType === 'date'
      const activeControl = acceptsTextInput
        ? questionnaireRef.current?.querySelector<HTMLElement>(
          '[data-slot="questionnaire-item"]:not([hidden]) input:not([disabled]), [data-slot="questionnaire-item"]:not([hidden]) textarea:not([disabled])',
        )
        : null
      const target = activeControl ?? headingRef.current
      target?.focus()
    })
    return () => window.cancelAnimationFrame(frame)
  }, [answerSurfaceReady, currentId, currentPromptType, submissionError, interactionKey])

  useEffect(() => {
    if (!showApplying || !onRefresh) return
    let cancelled = false
    const refresh = async () => {
      try {
        await onRefresh()
      } catch {
        // Transient refresh failures must not leave follow-up input hidden.
      }
    }
    void refresh()
    const timer = window.setInterval(() => {
      if (!cancelled) void refresh()
    }, APPLYING_REFRESH_MS)
    return () => {
      cancelled = true
      window.clearInterval(timer)
    }
  }, [onRefresh, showApplying])

  if (!current) return null

  if (showApplying) {
    return (
      <section data-testid="hitl-response-bar" aria-label="Human input status">
        <RecoveryState state="applying" />
      </section>
    )
  }
  if (lifecycleState !== 'open' || invalidPrompt) {
    const state = invalidPrompt ? 'routing_failed' : lifecycleState
    return (
      <section data-testid="hitl-response-bar" aria-label="Human input status">
        <RecoveryState
          state={state as Exclude<HitlLifecycleState, 'open'>}
          message={submissionError ?? current.errorMessage}
          onRefresh={onRefresh}
          onCancel={onCancel
            ? () => onCancel(current.hitlId, current.interactionId)
            : undefined}
        />
      </section>
    )
  }

  // A system question is asked by one of the owner's own check rules.
  const sourceLabel = current.source === 'agent'
    ? (current.agentName ?? 'Agent')
    : current.source === 'system' ? 'Your check rule' : 'HYBRO AI'
  const heading = (text: string) => (
    <h2
      id="hitl-heading"
      ref={headingRef}
      tabIndex={-1}
      className="flex items-center gap-2 text-sm text-muted-foreground outline-none"
    >
      <span aria-hidden="true" className="size-1.5 shrink-0 rounded-full bg-status-warning" />
      {text}
    </h2>
  )

  // One approval or yes/no question is answered by its own two buttons.
  if (
    ordered.length === 1
    && (current.promptType === 'approval' || current.promptType === 'confirmation')
  ) {
    const [accept, decline] = choiceOptions(current)
    const decide = (answer: string) => {
      setDraft(current.hitlId, answer)
      void handleSubmit([{ requestId: current.hitlId, answer }])
    }
    return (
      <section data-testid="hitl-response-bar" aria-labelledby="hitl-heading">
        <div className="flex flex-col gap-4 rounded-xl border bg-card p-4 text-card-foreground">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
            {heading(`${sourceLabel} needs your ${current.promptType === 'approval' ? 'approval' : 'answer'}`)}
            {current.presentation?.header ? (
              <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">{current.presentation.header}</span>
            ) : null}
          </div>
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{current.prompt}</p>
          {submissionError ? (
            <p className="text-sm text-destructive" role="alert">{submissionError}</p>
          ) : null}
          <div className="flex items-center justify-end gap-2">
            <Button type="button" variant="destructive" className="font-semibold" disabled={submitting} onClick={() => decide(decline)}>
              {decline}
            </Button>
            <Button type="button" disabled={submitting} onClick={() => decide(accept)}>
              {submitting ? (
                <LoaderCircle data-icon="inline-start" className="animate-spin motion-reduce:animate-none" aria-hidden="true" />
              ) : null}
              {accept}
            </Button>
          </div>
        </div>
      </section>
    )
  }

  const items = ordered.map(hitl => ({
    name: hitl.hitlId,
    required: isRequired(hitl),
    choices: [
      ...choiceOptions(hitl),
      ...(hitl.presentation?.allowOther ? [OTHER_CHOICE] : []),
    ].map(value => ({ value })),
  }))
  const isLast = currentIndex === ordered.length - 1
  const currentAnswered = hasAnswer(current, drafts)
  const currentOptional = !isRequired(current)
  const singleHeader = ordered.length === 1 ? current.presentation?.header : undefined

  const handleFormSubmit = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault()
    if (isLast) {
      if (allAnswered) void handleSubmit()
      return
    }
    if (currentAnswered || currentOptional) goTo(currentIndex + 1)
  }

  return (
    <section data-testid="hitl-response-bar" aria-labelledby="hitl-heading">
      <Questionnaire
        ref={questionnaireRef}
        items={items}
        item={currentId ?? undefined}
        onItemChange={requestId => {
          if (requestId && ordered.some(hitl => hitl.hitlId === requestId)) {
            dispatch({ type: 'navigate', requestId })
          }
        }}
        shortcuts="numbers"
        onSubmit={handleFormSubmit}
      >
        <div className="flex flex-col gap-4 rounded-xl border bg-card p-4 text-card-foreground">
          <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-2">
            {heading(`${sourceLabel} needs your input`)}
            {singleHeader ? (
              <span className="rounded-md bg-muted px-2 py-0.5 text-xs font-medium text-muted-foreground">{singleHeader}</span>
            ) : null}
          </div>

          {ordered.length > 1 ? (
            <div role="tablist" aria-label="Questions" className="flex flex-wrap gap-1">
              {ordered.map((hitl, index) => {
                const selected = index === currentIndex
                const answered = hasAnswer(hitl, drafts)
                return (
                  <button
                    key={hitl.hitlId}
                    type="button"
                    role="tab"
                    aria-selected={selected}
                    onClick={() => goTo(index)}
                    className={cn(
                      'inline-flex h-7 items-center gap-1.5 rounded-md px-2.5 text-xs outline-none transition-colors focus-visible:ring-[3px] focus-visible:ring-ring/50',
                      selected ? 'bg-muted font-medium text-foreground' : 'text-muted-foreground hover:text-foreground',
                    )}
                  >
                    {answered ? <Check className="size-3 text-status-online" aria-hidden="true" /> : null}
                    {hitl.presentation?.header ?? `Question ${index + 1}`}
                  </button>
                )
              })}
            </div>
          ) : null}

          {ordered.map(hitl => (
            <QuestionnaireItem
              key={hitl.hitlId}
              name={hitl.hitlId}
              required={isRequired(hitl)}
              multiple={hitl.promptType === 'multi_choice'}
              className="gap-3"
            >
              <QuestionnaireTitle className="mb-3 whitespace-pre-wrap text-sm leading-relaxed">{hitl.prompt}</QuestionnaireTitle>
              <PromptQuestion
                hitl={hitl}
                value={drafts[hitl.hitlId]}
                other={(drafts[otherDraftKey(hitl.hitlId)] as string | undefined) ?? ''}
                active={hitl.hitlId === current.hitlId}
                onChange={value => setDraft(hitl.hitlId, value)}
                onOtherChange={value => setDraft(otherDraftKey(hitl.hitlId), value)}
              />
              <QuestionnaireError />
            </QuestionnaireItem>
          ))}
          {submissionError ? (
            <p className="text-sm text-destructive" role="alert">{submissionError}</p>
          ) : null}

          <div className="flex flex-wrap items-center gap-2">
            {onCancel ? (
              <AlertDialog>
                <AlertDialogTrigger asChild>
                  <Button type="button" variant="destructive" className="font-semibold" disabled={submitting}>
                    Reject
                  </Button>
                </AlertDialogTrigger>
                <AlertDialogContent>
                  <AlertDialogHeader>
                    <AlertDialogTitle>Reject this request?</AlertDialogTitle>
                    <AlertDialogDescription>
                      {sourceLabel} stops waiting for your answer and the request ends.
                    </AlertDialogDescription>
                  </AlertDialogHeader>
                  <AlertDialogFooter>
                    <AlertDialogCancel>Go back</AlertDialogCancel>
                    <AlertDialogAction
                      onClick={() => { void onCancel(current.hitlId, current.interactionId) }}
                      className={buttonVariants({ variant: 'destructive' })}
                    >
                      Reject request
                    </AlertDialogAction>
                  </AlertDialogFooter>
                </AlertDialogContent>
              </AlertDialog>
            ) : null}
            <div className="ml-auto flex items-center gap-2">
              {ordered.length > 1 ? (
                <QuestionnairePrevious variant="ghost" disabled={submitting || currentIndex === 0}>
                  Back
                </QuestionnairePrevious>
              ) : null}
              {/* The primitive only counts an optional question as done once skipped. */}
              {currentOptional && !currentAnswered ? (
                <QuestionnaireSkip
                  variant="ghost"
                  disabled={submitting}
                  onClick={() => setDraft(current.hitlId, '')}
                >
                  {isLast ? 'Skip and send' : 'Skip'}
                </QuestionnaireSkip>
              ) : null}
              {isLast ? (
                <QuestionnaireSubmit disabled={submitting || !allAnswered || (currentOptional && !currentAnswered)}>
                  {submitting ? (
                    <LoaderCircle
                      data-icon="inline-start"
                      className="animate-spin motion-reduce:animate-none"
                      aria-hidden="true"
                    />
                  ) : null}
                  {submitting ? 'Sending…' : 'Send'}
                </QuestionnaireSubmit>
              ) : (
                <QuestionnaireNext disabled={submitting || !(currentAnswered || currentOptional)}>
                  Next
                  <ArrowRight data-icon="inline-end" aria-hidden="true" />
                </QuestionnaireNext>
              )}
            </div>
          </div>
        </div>
      </Questionnaire>
    </section>
  )
}
