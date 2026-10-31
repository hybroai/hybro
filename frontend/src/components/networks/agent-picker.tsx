'use client'

import { useId, useMemo, useState } from 'react'
import { RotateCw, Search, X } from 'lucide-react'
import { Alert, AlertDescription } from '@/components/ui/alert'
import { Avatar, AvatarFallback, AvatarImage } from '@/components/ui/avatar'
import { Badge } from '@/components/ui/badge'
import { Button } from '@/components/ui/button'
import { Checkbox } from '@/components/ui/checkbox'
import { Input } from '@/components/ui/input'
import { Skeleton } from '@/components/ui/skeleton'
import { StatusIndicator } from '@/components/status-indicator'
import { agentStatusView } from '@/lib/status'
import { getAgentAvatarUri } from '@/lib/agent-avatar'
import { placeholderAgent } from '@/lib/network-agent'
import { cn } from '@/lib/utils'
import type { AgentPickerProps, NetworkAgent } from './types'

export function AgentPicker({
  agents,
  value,
  onChange,
  existingIds,
  loading,
  error,
  disabled = false,
  onRetry,
}: AgentPickerProps) {
  const searchId = useId()
  const [query, setQuery] = useState('')
  const selected = useMemo(() => new Set(value), [value])
  const existing = useMemo(() => new Set(existingIds), [existingIds])
  const choices = useMemo(() => {
    const byId = new Map(agents.map(agent => [agent.id, agent]))
    for (const id of [...existing, ...selected]) {
      if (!byId.has(id)) {
        byId.set(id, placeholderAgent(id, '', 'unknown'))
      }
    }
    return Array.from(byId.values()).filter(agent =>
      agent.status === 'active' || agent.status === 'inactive' || existing.has(agent.id) || selected.has(agent.id)
    )
  }, [agents, existing, selected])
  const normalizedQuery = query.trim().toLocaleLowerCase()
  const visible = choices.filter(agent =>
    `${agent.name} ${agent.id} ${agent.description} ${agent.skills.join(' ')}`.toLocaleLowerCase().includes(normalizedQuery)
  )
  const chosen = choices.filter(agent => selected.has(agent.id) && !existing.has(agent.id))
  const hasNewChoices = choices.some(agent => !existing.has(agent.id) && (agent.status === 'active' || agent.status === 'inactive'))
  const busy = disabled || loading || Boolean(error)

  function toggle(agent: NetworkAgent, checked: boolean) {
    if (busy || existing.has(agent.id)) return
    if (checked && agent.status !== 'active' && agent.status !== 'inactive') return
    const draft = value.filter(id => !existing.has(id))
    onChange(checked ? [...draft, agent.id] : draft.filter(id => id !== agent.id))
  }

  return (
    <fieldset className="flex min-w-0 flex-col gap-3" disabled={disabled}>
      <legend className="sr-only">Select agents</legend>
      <div className="relative">
        <Search className="pointer-events-none absolute left-3 top-1/2 size-4 -translate-y-1/2 text-muted-foreground" aria-hidden="true" />
        <Input
          id={searchId}
          type="search"
          aria-label="Search agents"
          placeholder="Search agents by name or skill"
          value={query}
          onChange={event => setQuery(event.target.value)}
          disabled={disabled || loading}
          className="h-9 rounded-lg pl-9"
        />
      </div>
      <div className="flex min-h-6 flex-wrap items-center gap-1.5" aria-live="polite">
        <span className="text-xs text-muted-foreground">{chosen.length === 1 ? '1 selected' : `${chosen.length} selected`}</span>
        {chosen.map(agent => (
          <Badge key={agent.id} variant="badgeMuted" className="max-w-48 gap-1 pr-1">
            <span className="truncate">{agent.name}</span>
            <button
              type="button"
              aria-label={`Remove ${agent.name}`}
              disabled={busy}
              onClick={() => toggle(agent, false)}
              className="rounded-sm p-0.5 hover:bg-muted hover:text-foreground"
            >
              <X className="size-3" aria-hidden="true" />
            </button>
          </Badge>
        ))}
      </div>
      {loading ? (
        <div className="flex flex-col gap-2" role="status" aria-label="Loading agents">
          <Skeleton className="h-12 w-full" />
          <Skeleton className="h-12 w-full" />
          <span className="sr-only">Loading agents…</span>
        </div>
      ) : null}
      {error ? (
        <Alert variant="destructive">
          <AlertDescription className="gap-2 wrap-anywhere">
            <p>{error}</p>
            <Button type="button" variant="secondary" size="sm" onClick={onRetry} disabled={disabled || loading}>
              <RotateCw data-icon="inline-start" aria-hidden="true" />
              {loading ? 'Retrying…' : 'Reload agents'}
            </Button>
          </AlertDescription>
        </Alert>
      ) : null}
      <div className="max-h-[40dvh] min-h-32 overflow-y-auto rounded-lg border" aria-busy={loading}>
        {visible.map((agent, index) => {
          const added = existing.has(agent.id)
          const unavailable = agent.status !== 'active' && agent.status !== 'inactive'
          const checkboxId = `${searchId}-agent-${index}`
          const checked = added || selected.has(agent.id)
          const choiceDisabled = busy || added || (unavailable && !selected.has(agent.id))
          const note = unavailable
            ? agent.status === 'unknown' ? 'Details could not be verified; membership is preserved.' : 'Currently unavailable; membership is preserved.'
            : null
          return (
            <label
              key={agent.id}
              htmlFor={checkboxId}
              className={cn(
                'flex min-h-12 cursor-pointer items-center gap-3 border-b px-3 py-2 last:border-b-0',
                checked && !added && 'bg-primary/5',
                choiceDisabled ? 'cursor-not-allowed opacity-60' : 'hover:bg-muted/60',
              )}
            >
              <Avatar className={cn('size-8 rounded-md', unavailable && 'grayscale')}>
                <AvatarImage src={agent.iconUrl || getAgentAvatarUri(agent.id)} alt="" />
                <AvatarFallback className="rounded-md bg-muted text-[11px]">{agent.name.slice(0, 2).toUpperCase()}</AvatarFallback>
              </Avatar>
              <span className="flex min-w-0 flex-1 flex-col">
                <span className="flex min-w-0 items-center gap-2">
                  <span className="truncate text-sm font-medium">{agent.name}</span>
                  <StatusIndicator view={agentStatusView(agent.status, agent.inactiveReason)} />
                  {added ? <Badge variant="badgeMuted">Added</Badge> : null}
                </span>
                {agent.description || note ? (
                  <span id={`${checkboxId}-description`} className="truncate text-xs text-muted-foreground" title={agent.description || undefined}>
                    {note ?? agent.description}
                  </span>
                ) : null}
              </span>
              <Checkbox
                id={checkboxId}
                checked={checked}
                disabled={choiceDisabled}
                onCheckedChange={next => toggle(agent, next === true)}
                aria-label={`${added ? 'Already added:' : 'Select'} ${agent.name}`}
                aria-describedby={agent.description || note ? `${checkboxId}-description` : undefined}
              />
            </label>
          )
        })}
        {!loading && !error && visible.length === 0 ? (
          <p className="px-3 py-6 text-center text-sm text-muted-foreground">{normalizedQuery ? 'No matching agents.' : 'No agents available to add.'}</p>
        ) : null}
      </div>
      {!loading && !error && !normalizedQuery && choices.length > 0 && !hasNewChoices ? (
        <p className="text-xs text-muted-foreground">No more agents available to add.</p>
      ) : null}
    </fieldset>
  )
}
