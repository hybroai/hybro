'use client'

import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import { useIsMutating, useMutation, useQueryClient, type QueryClient } from '@tanstack/react-query'
import { AlertTriangle, ArrowDown, ArrowUp, Loader2, Plus, RefreshCw, Trash2 } from 'lucide-react'
import { PageHeader } from '@/components/page-header'
import { SELECT_CLASS } from '@/components/settings/model-provider-section'
import { Alert, AlertDescription } from '@/components/ui/alert'
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog'
import { Button, buttonVariants } from '@/components/ui/button'
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from '@/components/ui/dialog'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { useNetworks } from '@/hooks/useNetworks'
import { rulesQueryKey, useRules } from '@/hooks/useRules'
import {
  createRule,
  deleteRule,
  reorderRules,
  ruleErrorMessage,
  updateRule,
  type RuleAnswer,
  type RuleFields,
  type RuleList,
  type RuleView,
} from '@/lib/api/rules'
import { ApiError } from '@/lib/api-client'
import { cn } from '@/lib/utils'

const ANSWERS: { value: RuleAnswer; label: string }[] = [
  { value: 'allow', label: 'Allow' },
  { value: 'deny', label: 'Deny' },
  { value: 'ask', label: 'Ask' },
]
const ANSWER_LABEL: Record<RuleAnswer, string> = { allow: 'Allow', deny: 'Deny', ask: 'Ask' }
// The select's value for "any agent"; the API takes null.
const ANY = ''
// Every rule write runs one at a time, in the order sent, and is counted by
// `useIsMutating` so a move never starts from a list a write is changing.
const RULE_WRITE = { mutationKey: rulesQueryKey, scope: { id: 'rules' } } as const

/**
 * Reload the list once the last queued rule write settles. A reload between
 * two queued writes would show the list without the later, unsaved change.
 * The settling write still counts as pending here.
 */
function reloadAfterLastWrite(queryClient: QueryClient) {
  if (queryClient.isMutating({ mutationKey: rulesQueryKey }) > 1) return
  return queryClient.invalidateQueries({ queryKey: rulesQueryKey })
}

interface AgentChoice {
  id: string
  name: string
  inactive: boolean
}

/** The owner's agents to choose from, and whether that list can be trusted. */
function useAgentChoices() {
  const { directory, agentsLoading, agentsError } = useNetworks()
  const choices = useMemo<AgentChoice[]>(
    () => directory
      .map(agent => ({ id: agent.id, name: agent.name, inactive: agent.status === 'inactive' }))
      .sort((a, b) => a.name.localeCompare(b.name)),
    [directory],
  )
  return { choices, known: !agentsLoading && !agentsError, failed: Boolean(agentsError) }
}

type Agents = ReturnType<typeof useAgentChoices>

/** True only when the agents loaded and this rule's agent is not among them. */
function isMissing(agentId: string | null, agents: Agents): boolean {
  return agentId !== null && agents.known && !agents.choices.some(choice => choice.id === agentId)
}

function AgentOptions({ current, agents }: { current: string | null; agents: Agents }) {
  const listed = current === null || agents.choices.some(choice => choice.id === current)
  return (
    <>
      <option value={ANY}>Any agent</option>
      {listed ? null : <option value={current}>{agents.known ? `Missing agent (${current})` : current}</option>}
      {agents.choices.map(choice => (
        <option key={choice.id} value={choice.id}>{choice.inactive ? `${choice.name} (inactive)` : choice.name}</option>
      ))}
    </>
  )
}

function AnswerOptions() {
  return <>{ANSWERS.map(answer => <option key={answer.value} value={answer.value}>{answer.label}</option>)}</>
}

const fieldsOf = (rule: RuleView): RuleFields => ({ agent_id: rule.agent_id, answer: rule.answer, enabled: rule.enabled })

function RulesHelp() {
  return (
    <section aria-labelledby="rules-help-heading" className="flex flex-col gap-2 rounded-xl border bg-card p-5">
      <h2 id="rules-help-heading" className="text-[15px] font-semibold">How rules work</h2>
      <ul className="flex list-disc flex-col gap-1.5 pl-5 text-sm text-muted-foreground">
        <li>Rules are read from the top. The first rule that is on and matches the agent decides; a rule that is off is skipped.</li>
        <li>If no rule matches, the call is allowed.</li>
        <li>Rules are checked when a new agent call is about to be sent, in Room chat and through the Client API alike.</li>
        <li>A call that already passed the check, and a reply to an agent that asked a question, are not checked again.</li>
        <li>Allow sends the call. Deny refuses it: in Room chat the model is told, and through the Client API the step ends as denied.</li>
        <li>Ask holds the call until you approve or reject it in the Panel. If the work stops first, the call is not sent.</li>
      </ul>
    </section>
  )
}

interface AddProps {
  open: boolean
  onOpenChange: (open: boolean) => void
  agents: Agents
  /** The dialog has no Radix trigger, so it says where the focus goes back to. */
  returnFocus: () => void
}

function AddRuleDialog({ open, onOpenChange, agents, returnFocus }: AddProps) {
  const queryClient = useQueryClient()
  const [agentId, setAgentId] = useState(ANY)
  const [answer, setAnswer] = useState<RuleAnswer>('deny')
  const create = useMutation({
    ...RULE_WRITE,
    mutationFn: (fields: RuleFields) => createRule(fields),
    // Close here, not through `close`: this render still sees the request pending.
    onSuccess: () => finish(),
    onSettled: () => reloadAfterLastWrite(queryClient),
  })
  function finish() {
    onOpenChange(false)
    create.reset()
    setAgentId(ANY)
    setAnswer('deny')
  }
  function close(next: boolean) {
    if (next) onOpenChange(true)
    // Closing mid-request would hide whether the rule was added.
    else if (!create.isPending) finish()
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (!create.isPending) create.mutate({ agent_id: agentId === ANY ? null : agentId, answer, enabled: true })
  }
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent onCloseAutoFocus={event => { event.preventDefault(); returnFocus() }}>
        <DialogHeader>
          <DialogTitle>Add a rule</DialogTitle>
          <DialogDescription>The rule goes to the bottom of the list and is on. Move it up to check it earlier.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-rule-agent">Agent</Label>
            <select id="new-rule-agent" className={SELECT_CLASS} value={agentId} onChange={event => setAgentId(event.target.value)}>
              <AgentOptions current={null} agents={agents} />
            </select>
            {agents.failed ? <p className="text-xs text-muted-foreground">Agents couldn&apos;t load, so only Any agent can be chosen.</p> : null}
          </div>
          <div className="flex flex-col gap-2">
            <Label htmlFor="new-rule-answer">Answer</Label>
            <select id="new-rule-answer" className={SELECT_CLASS} value={answer} onChange={event => setAnswer(event.target.value as RuleAnswer)}>
              <AnswerOptions />
            </select>
          </div>
          {create.isError ? <Alert variant="destructive"><AlertDescription>{ruleErrorMessage(create.error)}</AlertDescription></Alert> : null}
          <DialogFooter>
            <Button type="button" variant="secondary" disabled={create.isPending} onClick={() => close(false)}>Cancel</Button>
            {/* aria-disabled keeps the focus here while the request runs. */}
            <Button type="submit" aria-disabled={create.isPending}>
              {create.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
              Add
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

function agentName(agentId: string | null, agents: Agents): string {
  if (agentId === null) return 'any agent'
  return agents.choices.find(choice => choice.id === agentId)?.name ?? agentId
}

interface DeleteProps {
  rule: RuleView
  position: number
  agents: Agents
  onClose: () => void
  /** Where the keyboard focus goes when the dialog closes; it has no Radix trigger. */
  returnFocus: (deleted: boolean) => void
}

function DeleteRuleDialog({ rule, position, agents, onClose, returnFocus }: DeleteProps) {
  const queryClient = useQueryClient()
  const removed = useRef(false)
  const done = () => {
    removed.current = true
    // Drop the row at once, so it cannot be edited before the reload lands.
    queryClient.setQueryData<RuleList>(rulesQueryKey, current => current && {
      rules: current.rules.filter(item => item.rule_id !== rule.rule_id),
    })
    onClose()
  }
  const remove = useMutation({
    ...RULE_WRITE,
    mutationFn: () => deleteRule(rule.rule_id),
    onSuccess: done,
    // Already gone, as asked: the reload shows the list without it.
    onError: error => { if (error instanceof ApiError && error.status === 404) done() },
    onSettled: () => reloadAfterLastWrite(queryClient),
  })
  return (
    <AlertDialog open onOpenChange={open => { if (!open && !remove.isPending) onClose() }}>
      <AlertDialogContent
        onCloseAutoFocus={event => {
          event.preventDefault()
          returnFocus(removed.current)
        }}
      >
        <AlertDialogHeader>
          <AlertDialogTitle>{`Delete rule ${position}?`}</AlertDialogTitle>
          <AlertDialogDescription>
            {rule.enabled
              ? `${ANSWER_LABEL[rule.answer]} for ${agentName(rule.agent_id, agents)}. Calls it matched are then decided by the rules below it, or allowed if none matches.`
              : `${ANSWER_LABEL[rule.answer]} for ${agentName(rule.agent_id, agents)}. The rule is off, so deleting it changes no answer.`}
          </AlertDialogDescription>
        </AlertDialogHeader>
        {remove.isError ? <Alert variant="destructive"><AlertDescription>{ruleErrorMessage(remove.error)}</AlertDescription></Alert> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={remove.isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            aria-disabled={remove.isPending}
            className={buttonVariants({ variant: 'destructive' })}
            // Keep the dialog open until the backend answers; aria-disabled keeps the focus.
            onClick={event => { event.preventDefault(); if (!remove.isPending) remove.mutate() }}
          >
            {remove.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
            Delete
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

type Direction = 'up' | 'down'

interface RowProps {
  rule: RuleView
  position: number
  count: number
  agents: Agents
  editBlocked: boolean
  moveBlocked: boolean
  onChange: (rule: RuleView, fields: RuleFields) => void
  onMove: (rule: RuleView, direction: Direction) => void
  onDelete: (rule: RuleView, position: number) => void
  buttonRef: (key: string, element: HTMLButtonElement | null) => void
}

function RuleRow({ rule, position, count, agents, editBlocked, moveBlocked, onChange, onMove, onDelete, buttonRef }: RowProps) {
  const fields = fieldsOf(rule)
  const missing = isMissing(rule.agent_id, agents)
  const id = `rule-${rule.rule_id}`
  // Controls stay focusable while a request runs; a disabled button would drop
  // the keyboard focus. Input made while blocked is ignored.
  const change = (next: Partial<RuleFields>) => { if (!editBlocked) onChange(rule, { ...fields, ...next }) }
  const moveButton = (direction: Direction) => {
    const blocked = direction === 'up' ? position === 1 : position === count
    const Icon = direction === 'up' ? ArrowUp : ArrowDown
    return (
      <Button
        ref={element => buttonRef(`${rule.rule_id}:${direction}`, element)}
        variant="ghost"
        size="iconSm"
        className="size-8 aria-disabled:cursor-not-allowed aria-disabled:opacity-40"
        aria-label={`Move rule ${position} ${direction}`}
        aria-disabled={blocked || moveBlocked}
        onClick={() => { if (!blocked && !moveBlocked) onMove(rule, direction) }}
      >
        <Icon aria-hidden="true" />
      </Button>
    )
  }
  return (
    <li className={cn('flex flex-col gap-3 border-t px-4 py-3 first:border-t-0', !rule.enabled && 'bg-muted/30')}>
      <div className="flex flex-wrap items-center gap-x-3 gap-y-2">
        <span className="w-6 shrink-0 text-sm font-medium tabular-nums text-muted-foreground" aria-hidden="true">{position}</span>
        <div className="flex min-w-0 flex-1 basis-56 flex-col gap-1">
          <Label htmlFor={`${id}-agent`} className="sr-only">{`Agent for rule ${position}`}</Label>
          <select
            id={`${id}-agent`}
            className={cn(SELECT_CLASS, missing && 'border-destructive')}
            value={rule.agent_id ?? ANY}
            aria-describedby={missing ? `${id}-missing` : undefined}
            aria-invalid={missing || undefined}
            onChange={event => change({ agent_id: event.target.value === ANY ? null : event.target.value })}
          >
            <AgentOptions current={rule.agent_id} agents={agents} />
          </select>
        </div>
        <div className="flex w-24 shrink-0 flex-col gap-1 sm:w-28">
          <Label htmlFor={`${id}-answer`} className="sr-only">{`Answer for rule ${position}`}</Label>
          <select
            id={`${id}-answer`}
            className={SELECT_CLASS}
            value={rule.answer}
            onChange={event => change({ answer: event.target.value as RuleAnswer })}
          >
            <AnswerOptions />
          </select>
        </div>
        <div className="flex shrink-0 items-center gap-2">
          <Switch
            id={`${id}-on`}
            checked={rule.enabled}
            aria-label={`Rule ${position} on`}
            onCheckedChange={enabled => change({ enabled })}
          />
          <span className="hidden w-7 text-sm text-muted-foreground sm:inline" aria-hidden="true">{rule.enabled ? 'On' : 'Off'}</span>
        </div>
        <div className="ml-auto flex shrink-0 items-center gap-1">
          {moveButton('up')}
          {moveButton('down')}
          <Button
            variant="ghost"
            size="iconSm"
            className="size-8 text-destructive hover:text-destructive"
            ref={element => buttonRef(`${rule.rule_id}:delete`, element)}
            aria-label={`Delete rule ${position}`}
            aria-disabled={editBlocked}
            onClick={() => { if (!editBlocked) onDelete(rule, position) }}
          >
            <Trash2 aria-hidden="true" />
          </Button>
        </div>
      </div>
      {missing ? (
        <p id={`${id}-missing`} className="flex items-start gap-1.5 pl-9 text-xs text-destructive">
          <AlertTriangle className="mt-px size-3.5 shrink-0" aria-hidden="true" />
          <span>{`This agent is no longer in your agents (id ${rule.agent_id}). The rule matches only calls to that id; choose another agent or delete the rule.`}</span>
        </p>
      ) : null}
    </li>
  )
}

function moved(ids: string[], ruleId: string, direction: Direction): string[] {
  const from = ids.indexOf(ruleId)
  const to = direction === 'up' ? from - 1 : from + 1
  const next = [...ids]
  ;[next[from], next[to]] = [next[to], next[from]]
  return next
}

export function RulesPage() {
  const queryClient = useQueryClient()
  const writing = useIsMutating({ mutationKey: rulesQueryKey }) > 0
  const rules = useRules({ paused: writing })
  const agents = useAgentChoices()
  const list = rules.data?.rules
  const [adding, setAdding] = useState(false)
  // The position is kept from when the dialog opened, so a reload cannot change its title.
  const [deleting, setDeleting] = useState<{ rule: RuleView; position: number } | null>(null)
  const [actionError, setActionError] = useState<string | null>(null)
  const [announcement, setAnnouncement] = useState('')
  // The move button to focus once the moved list renders.
  const focusAfterMove = useRef<string | null>(null)
  const buttons = useRef(new Map<string, HTMLButtonElement>())
  const addButton = useRef<HTMLButtonElement>(null)
  const heading = useRef<HTMLHeadingElement>(null)

  const reload = () => reloadAfterLastWrite(queryClient)

  const failed = (error: unknown) => {
    setActionError(ruleErrorMessage(error))
  }

  // Each edit is shown at once, from the newest list, so edits queued behind
  // one another each send the whole rule as it now stands.
  const edit = (rule: RuleView, fields: RuleFields) => {
    setActionError(null)
    void queryClient.cancelQueries({ queryKey: rulesQueryKey })
    queryClient.setQueryData<RuleList>(rulesQueryKey, current => current && {
      rules: current.rules.map(item => item.rule_id === rule.rule_id ? { ...item, ...fields } : item),
    })
    update.mutate({ ruleId: rule.rule_id, fields })
  }

  const update = useMutation({
    ...RULE_WRITE,
    mutationFn: ({ ruleId, fields }: { ruleId: string; fields: RuleFields }) => updateRule(ruleId, fields),
    // A refusal shows its text; the reload on settle puts the stored rule back.
    onError: failed,
    onSettled: reload,
  })

  const reorder = useMutation({
    ...RULE_WRITE,
    mutationFn: ({ ids }: { ids: string[]; ruleId: string; direction: Direction }) => reorderRules(ids),
    onMutate: async () => {
      setActionError(null)
      // An older read landing after this write would bring back the old order.
      await queryClient.cancelQueries({ queryKey: rulesQueryKey })
    },
    onSuccess: async (saved, { ruleId, direction }) => {
      await queryClient.cancelQueries({ queryKey: rulesQueryKey })
      // The moved rule keeps the focus; at an end, the other arrow takes it.
      const index = saved.rules.findIndex(item => item.rule_id === ruleId)
      if (index >= 0) {
        const atEnd = direction === 'up' ? index === 0 : index === saved.rules.length - 1
        focusAfterMove.current = `${ruleId}:${atEnd ? (direction === 'up' ? 'down' : 'up') : direction}`
        setAnnouncement(`Rule moved to position ${index + 1} of ${saved.rules.length}.`)
      }
      queryClient.setQueryData(rulesQueryKey, saved)
    },
    onError: failed,
    onSettled: reload,
  })

  // A move is computed from the list on screen, so it waits until every
  // write has settled; edits wait only for a move.
  // A list that failed to reload, or is reloading after a refused write, may be
  // stale: nothing in it can be edited or moved until it is read again.
  const stale = rules.isError || (actionError !== null && writing)
  const editBlocked = reorder.isPending || stale

  const move = (rule: RuleView, direction: Direction) => {
    if (!list || writing) return
    const ids = moved(list.map(item => item.rule_id), rule.rule_id, direction)
    reorder.mutate({ ids, ruleId: rule.rule_id, direction })
  }

  // The control last focused in the list. A reload that removes its row would
  // leave the focus nowhere; it then goes to the list heading.
  const lastFocused = useRef<HTMLElement | null>(null)
  useEffect(() => {
    if (focusAfterMove.current) {
      buttons.current.get(focusAfterMove.current)?.focus()
      focusAfterMove.current = null
      return
    }
    const last = lastFocused.current
    if (last && !last.isConnected && (!document.activeElement || document.activeElement === document.body)) {
      heading.current?.focus()
    }
  }, [list])

  const buttonRef = (key: string, element: HTMLButtonElement | null) => {
    if (element) buttons.current.set(key, element)
    else buttons.current.delete(key)
  }


  return (
    <div className="page-container">
      <div className="page-content flex min-w-0 flex-col gap-6">
        <PageHeader title="Rules" description="Check rules decide whether Hybro sends a new agent call." />
        <RulesHelp />
        <section aria-labelledby="rules-heading" className="flex flex-col gap-3">
          <div className="flex flex-wrap items-center justify-between gap-3">
            <div className="flex flex-col gap-1">
              <h2 id="rules-heading" ref={heading} tabIndex={-1} className="text-[15px] font-semibold outline-none">Check rules</h2>
              <p className="text-sm text-muted-foreground">Each rule matches one agent or any agent, and answers Allow, Deny or Ask.</p>
            </div>
            <Button ref={addButton} onClick={() => setAdding(true)} disabled={!list}>
              <Plus data-icon="inline-start" aria-hidden="true" />Add rule
            </Button>
          </div>
          {actionError ? <Alert variant="destructive"><AlertDescription>{actionError}</AlertDescription></Alert> : null}
          {agents.failed && list?.length ? (
            <p className="text-xs text-muted-foreground">Agents couldn&apos;t load, so rules show agent ids.</p>
          ) : null}
          {rules.isError && list ? (
            <Alert variant="destructive">
              <AlertDescription className="flex flex-wrap items-center gap-3">
                <span>Rules couldn&apos;t reload, so the list may be out of date. Reload it before changing a rule.</span>
                <Button variant="secondary" size="sm" onClick={() => { void rules.refetch() }}>
                  <RefreshCw data-icon="inline-start" aria-hidden="true" />Reload
                </Button>
              </AlertDescription>
            </Alert>
          ) : null}
          {rules.isError && !list ? (
            <div className="flex flex-wrap items-center gap-3 rounded-lg border p-4 text-sm">
              <span>Rules couldn&apos;t load.</span>
              <Button variant="secondary" size="sm" onClick={() => { void rules.refetch() }}>
                <RefreshCw data-icon="inline-start" aria-hidden="true" />Retry
              </Button>
            </div>
          ) : rules.isLoading ? (
            <div className="flex flex-col gap-2 rounded-lg border p-4" aria-busy="true" aria-label="Loading rules">
              {Array.from({ length: 3 }, (_, index) => <Skeleton key={index} className="h-9" />)}
            </div>
          ) : !list?.length ? (
            <div className="flex flex-col gap-1 rounded-lg border border-dashed px-4 py-8 text-sm">
              <p className="font-medium">No rules yet, so every agent call is allowed.</p>
              <p className="text-muted-foreground">Add a rule to deny calls to an agent, or to have Hybro ask you first.</p>
            </div>
          ) : (
            <ol
              aria-label="Check rules"
              className="overflow-hidden rounded-lg border"
              onFocus={event => { if (event.target instanceof HTMLElement) lastFocused.current = event.target }}
            >
              {list.map((rule, index) => (
                <RuleRow
                  key={rule.rule_id}
                  rule={rule}
                  position={index + 1}
                  count={list.length}
                  agents={agents}
                  editBlocked={editBlocked}
                  moveBlocked={writing || rules.isError}
                  onChange={edit}
                  onMove={move}
                  onDelete={(target, position) => { setActionError(null); setDeleting({ rule: target, position }) }}
                  buttonRef={buttonRef}
                />
              ))}
            </ol>
          )}
          <p role="status" className="sr-only">{announcement}</p>
        </section>
      </div>
      <AddRuleDialog open={adding} onOpenChange={setAdding} agents={agents} returnFocus={() => addButton.current?.focus()} />
      {deleting ? (
        <DeleteRuleDialog
          key={deleting.rule.rule_id}
          rule={deleting.rule}
          position={deleting.position}
          agents={agents}
          onClose={() => setDeleting(null)}
          // Back to the row's Delete button, or to the list heading once the row is gone.
          returnFocus={deleted => {
            const button = deleted ? undefined : buttons.current.get(`${deleting.rule.rule_id}:delete`)
            if (button) button.focus()
            else heading.current?.focus()
          }}
        />
      ) : null}
    </div>
  )
}
