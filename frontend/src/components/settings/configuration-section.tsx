'use client'

import type React from 'react'
import { useId, useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Info, Loader2 } from 'lucide-react'
import { toast } from 'sonner'
import { Button } from '@/components/ui/button'
import { Dialog, DialogContent, DialogDescription, DialogFooter, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Input } from '@/components/ui/input'
import { Switch } from '@/components/ui/switch'
import { SELECT_CLASS } from '@/components/settings/model-provider-section'
import { SettingsNotice } from '@/components/settings/settings-notice'
import { instanceSettingsQueryKey, isRestarting, useInstanceSettings, useRestartServices, useRestartStarting } from '@/hooks/useInstanceSettings'
import { saveSettings, settingsError, settingsErrorMessage, type FieldValue, type SettingsField, type SettingsView } from '@/lib/api/settings'
import { formatValue, pendingChanges, toDraft, type DraftValue } from '@/lib/instance-settings'
import { cn } from '@/lib/utils'

const plural = (count: number, one: string, many: string) => `${count} ${count === 1 ? one : many}`

/** Loading, refusal and failure states shared by the Configuration and System pages. */
export function SettingsQueryState({ query }: { query: ReturnType<typeof useInstanceSettings> }) {
  if (query.isPending) {
    return (
      <p className="flex items-center gap-2 text-sm text-muted-foreground">
        <Loader2 className="size-4 animate-spin" aria-hidden="true" />
        Loading settings…
      </p>
    )
  }
  const failure = settingsError(query.error)
  if (failure?.status === 403) {
    return <SettingsNotice tone="neutral" title="Only an admin can view and change these settings." />
  }
  return (
    <SettingsNotice
      tone="error"
      role="alert"
      title="Settings could not be loaded"
      action={<Button variant="secondary" onClick={() => void query.refetch()}>Try again</Button>}
    >
      {settingsErrorMessage(query.error)}
    </SettingsNotice>
  )
}

function FieldTag({ field }: { field: SettingsField }) {
  const [label, tone] = field.restart_pending
    ? ['Restart pending', 'bg-status-warning/15 text-status-warning']
    : field.apply === 'live'
      ? ['Live', 'bg-status-online/15 text-status-online']
      : ['Restart', 'bg-muted text-muted-foreground']
  return <span className={cn('inline-flex h-5 items-center rounded-full px-2 text-[11px] font-medium', tone)}>{label}</span>
}

function FieldRow({ field, draft, dirty, error, disabled, onChange, onReset }: {
  field: SettingsField
  draft: DraftValue | undefined
  dirty: boolean
  error: string | undefined
  disabled: boolean
  onChange: (value: DraftValue) => void
  onReset: () => void
}) {
  const id = useId()
  const errorId = `${id}-error`
  const shown = draft === undefined ? toDraft(field) : draft === null ? toDraft(field, field.default) : draft
  const meta = dirty
    ? `Unsaved · saved value ${formatValue(field, field.value)}`
    : field.source === 'config' ? `Set in config.json · default ${formatValue(field, field.default)}` : null
  const described = error ? { 'aria-invalid': true, 'aria-describedby': errorId } : {}

  let control: React.ReactNode
  if (field.kind === 'boolean') {
    control = <Switch id={id} checked={shown === true} disabled={disabled} onCheckedChange={checked => onChange(checked)} {...described} />
  } else if (field.kind === 'enum') {
    control = (
      <select id={id} className={SELECT_CLASS} value={String(shown)} disabled={disabled} onChange={event => onChange(event.target.value)} {...described}>
        {(field.options ?? []).map(option => <option key={option} value={option}>{option}</option>)}
      </select>
    )
  } else {
    control = (
      <Input
        id={id}
        type="text"
        inputMode={field.kind === 'url' ? 'url' : field.kind === 'integer' ? 'numeric' : 'decimal'}
        placeholder={field.kind === 'url' ? 'https://hybro.example.com' : field.nullable ? 'None' : undefined}
        value={String(shown)}
        disabled={disabled}
        onChange={event => onChange(event.target.value)}
        className={cn(field.kind !== 'url' && 'text-right tabular-nums', dirty && !error && 'border-icon-action/60')}
        {...described}
      />
    )
  }

  return (
    <div data-testid={`field-${field.key}`} className="flex flex-wrap items-start justify-between gap-x-6 gap-y-2 border-t py-3.5 first:border-t-0">
      <div className="flex min-w-0 flex-[1_1_320px] flex-col gap-1">
        <div className="flex flex-wrap items-center gap-2">
          <label htmlFor={id} className="text-sm font-medium">{field.label}</label>
          <FieldTag field={field} />
          {dirty ? <span aria-hidden="true" className="size-1.5 rounded-full bg-icon-action" /> : null}
        </div>
        <p className="text-[13px] leading-[18px] text-muted-foreground">{field.description}</p>
        {meta ? (
          <div className="flex flex-wrap items-center gap-x-2.5 gap-y-1 text-xs text-muted-foreground">
            <span>{meta}</span>
            {!dirty && field.source === 'config' ? (
              <button
                type="button"
                className="cursor-pointer text-icon-action hover:underline disabled:cursor-not-allowed disabled:opacity-50"
                aria-label={`Reset ${field.label} to default`}
                disabled={disabled}
                onClick={onReset}
              >
                Reset to default
              </button>
            ) : null}
          </div>
        ) : null}
        {error ? <p id={errorId} className="text-[13px] leading-[18px] text-status-error">{error}</p> : null}
      </div>
      <div className="flex min-w-40 flex-[0_1_240px] justify-end">{control}</div>
    </div>
  )
}

function ConfirmSave({ view, fields, values, restartAvailable, saving, onCancel, onSave }: {
  view: SettingsView
  fields: SettingsField[]
  values: Record<string, FieldValue>
  restartAvailable: boolean
  saving: boolean
  onCancel: () => void
  onSave: (restart: boolean) => void
}) {
  const changed = fields.filter(field => field.key in values)
  const restartCount = changed.filter(field => field.apply === 'restart').length
  const liveCount = changed.length - restartCount
  return (
    <Dialog open onOpenChange={open => { if (!open) onCancel() }}>
      <DialogContent className="sm:max-w-[480px]">
        <DialogHeader>
          <DialogTitle>Save {plural(changed.length, 'change', 'changes')}?</DialogTitle>
          <DialogDescription>
            {liveCount
              ? `${plural(liveCount, 'change applies', 'changes apply')} right away. ${plural(restartCount, 'change takes', 'changes take')} effect after Hybro restarts.`
              : 'These changes take effect after Hybro restarts.'}
          </DialogDescription>
        </DialogHeader>
        <ul className="rounded-[10px] border bg-card">
          {changed.map(field => (
            <li key={field.key} className="flex items-center justify-between gap-3 border-t px-3.5 py-2.5 first:border-t-0">
              <span className="flex min-w-0 flex-wrap gap-x-2 text-sm">
                <span>{field.label}</span>
                <span className="text-muted-foreground">{formatValue(field, field.value)} → {formatValue(field, values[field.key] ?? field.default)}</span>
              </span>
              <FieldTag field={{ ...field, restart_pending: false }} />
            </li>
          ))}
        </ul>
        <div className="flex gap-2.5 rounded-[10px] border border-status-warning/30 bg-status-warning/[0.06] px-3.5 py-3">
          <Info className="mt-0.5 size-4 shrink-0 text-status-warning" aria-hidden="true" />
          <p className="text-[13px] leading-[18px] text-muted-foreground">
            A restart is needed for these changes to work.{' '}
            {restartAvailable
              ? 'Restarting stops the backend, web app, MCP and agents for a few seconds. You can also save now and restart later.'
              : 'Hybro cannot restart itself here; restart it from the terminal after saving.'}
          </p>
        </div>
        <DialogFooter>
          <Button variant="ghost" onClick={onCancel} disabled={saving}>Cancel</Button>
          <Button variant={restartAvailable ? 'secondary' : 'default'} onClick={() => onSave(false)} disabled={saving || view.revision === null}>Save</Button>
          {restartAvailable ? (
            <Button onClick={() => onSave(true)} disabled={saving || view.revision === null}>Save &amp; restart</Button>
          ) : null}
        </DialogFooter>
      </DialogContent>
    </Dialog>
  )
}

export function ConfigurationSection() {
  const queryClient = useQueryClient()
  const settings = useInstanceSettings()
  const restart = useRestartServices()
  const restartStarting = useRestartStarting()
  const [drafts, setDrafts] = useState<Record<string, DraftValue>>({})
  // The revision the drafts were made against; a save sends it, so a newer file is a conflict, not an overwrite.
  const [baseRevision, setBaseRevision] = useState<string | null>(null)
  const [serverErrors, setServerErrors] = useState<Record<string, string>>({})
  const [conflict, setConflict] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [advancedOpen, setAdvancedOpen] = useState(false)

  const clearDrafts = () => {
    setDrafts({})
    setBaseRevision(null)
    setServerErrors({})
  }

  const save = useMutation({
    mutationFn: ({ revision, values }: { revision: string; values: Record<string, FieldValue>; restart: boolean }) => saveSettings(revision, values),
    // A view fetched before the save must not overwrite the saved one.
    onMutate: () => queryClient.cancelQueries({ queryKey: instanceSettingsQueryKey }),
    onSuccess: async (saved, variables) => {
      await queryClient.cancelQueries({ queryKey: instanceSettingsQueryKey })
      queryClient.setQueryData(instanceSettingsQueryKey, saved.settings)
      clearDrafts()
      setConflict(false)
      setConfirming(false)
      if (variables.restart) restart.mutate()
      else toast.success(saved.restart_required ? 'Saved. Restart Hybro to apply the changes.' : 'Saved.')
    },
    onError: error => {
      setConfirming(false)
      const failure = settingsError(error)
      if (failure?.code === 'revision_conflict') {
        setConflict(true)
        return
      }
      if (failure?.code === 'config_invalid') {
        // The view then reports the file error and turns the form read-only.
        void queryClient.invalidateQueries({ queryKey: instanceSettingsQueryKey })
      }
      const known = new Set(settings.data?.fields.map(field => field.key))
      const items = failure?.errors ?? []
      const onRows = items.filter(item => item.field !== null && known.has(item.field))
      const elsewhere = items.filter(item => !onRows.includes(item)).map(item => item.message)
      setServerErrors(Object.fromEntries(onRows.map(item => [item.field, item.message])))
      if (elsewhere.length) toast.error(elsewhere.join(' '))
      else if (!onRows.length) toast.error(settingsErrorMessage(error))
    },
  })

  const view = settings.data
  if (!view) return <SettingsQueryState query={settings} />

  const changes = pendingChanges(view.fields, drafts)
  const errors = { ...serverErrors, ...changes.errors }
  const readOnly = view.config_error !== null || view.revision === null || isRestarting(view) || restartStarting || save.isPending

  // Drafts that no longer differ from the saved values are dropped, so a new edit starts from the current revision.
  const editing = changes.keys.length > 0 || Object.keys(changes.errors).length > 0
  const edit = (key: string, value: DraftValue) => {
    setBaseRevision(editing ? baseRevision ?? view.revision : view.revision)
    setDrafts(current => ({ ...(editing ? current : {}), [key]: value }))
    setServerErrors(current => {
      const { [key]: _dropped, ...rest } = current
      return rest
    })
  }
  const submit = (withRestart: boolean) => {
    const revision = baseRevision ?? view.revision
    if (revision === null) return
    save.mutate({ revision, values: changes.values, restart: withRestart })
  }
  const loadNewValues = async () => {
    const result = await settings.refetch()
    if (result.isError) {
      toast.error(settingsErrorMessage(result.error))
      return
    }
    clearDrafts()
    setConflict(false)
  }

  const row = (field: SettingsField) => (
    <FieldRow
      key={field.key}
      field={field}
      draft={drafts[field.key]}
      dirty={changes.keys.includes(field.key)}
      error={errors[field.key]}
      disabled={readOnly}
      onChange={value => edit(field.key, value)}
      onReset={() => edit(field.key, null)}
    />
  )
  const basicGroups = view.groups
    .map(group => ({ ...group, fields: view.fields.filter(field => field.group === group.id && field.level === 'basic') }))
    .filter(group => group.fields.length)
  const advanced = view.fields.filter(field => field.level === 'advanced')
  // A changed or invalid advanced field keeps the group open, so the toggle is disabled until that is resolved.
  const advancedForced = advanced.some(field => changes.keys.includes(field.key) || errors[field.key])
  const advancedShown = advancedOpen || advancedForced
  const changeCount = changes.keys.length
  const errorCount = Object.keys(changes.errors).length

  return (
    <div className="flex flex-col gap-4">
      {view.config_error ? (
        <SettingsNotice tone="error" role="alert" title="config.json has an error">
          {view.config_error} Fix the file, then reload this page.
        </SettingsNotice>
      ) : null}
      {conflict ? (
        <SettingsNotice
          tone="error"
          role="alert"
          title="config.json changed since you opened this page"
          action={<Button variant="secondary" onClick={() => void loadNewValues()}>Load new values</Button>}
        >
          The file was edited, saved from another tab, or changed by a Model provider image size save. Nothing was saved. Load the new values, then make your changes again.
        </SettingsNotice>
      ) : null}

      {basicGroups.map(group => (
        <section key={group.id} aria-labelledby={`group-${group.id}`} className="rounded-xl border bg-card px-5 pt-1">
          <h3 id={`group-${group.id}`} className="mt-4 mb-1 text-[15px] font-semibold">{group.label}</h3>
          {group.fields.map(row)}
        </section>
      ))}

      {advanced.length ? (
        <section aria-label="Advanced" className="rounded-xl border bg-card px-5">
          <button
            type="button"
            aria-expanded={advancedShown}
            disabled={advancedForced}
            onClick={() => setAdvancedOpen(!advancedShown)}
            className="flex w-full cursor-pointer items-center disabled:cursor-default justify-between gap-3 py-3.5 text-left"
          >
            <span className="flex flex-col gap-0.5">
              <span className="text-[15px] font-semibold">Advanced</span>
              <span className="text-[13px] text-muted-foreground">Run limits, model gateway timeouts and other tuning. {plural(advanced.length, 'setting', 'settings')}.</span>
            </span>
            <ChevronDown className={cn('size-4 text-muted-foreground transition-transform', advancedShown && 'rotate-180')} aria-hidden="true" />
          </button>
          {advancedShown ? <div className="border-t">{advanced.map(row)}</div> : null}
        </section>
      ) : null}

      {changeCount ? (
        <section aria-label="Unsaved changes" className="sticky bottom-0 z-10 -mx-4 border-t bg-background/95 px-4 py-3 backdrop-blur-sm md:-mx-6 md:px-6">
          <div className="flex flex-wrap items-center justify-between gap-3" aria-live="polite">
            <span className="flex flex-col gap-0.5">
              <span className="text-sm font-semibold">{plural(changeCount, 'unsaved change', 'unsaved changes')}</span>
              <span className={cn('text-[13px]', errorCount ? 'text-status-error' : 'text-muted-foreground')}>
                {errorCount
                  ? `${plural(errorCount, 'setting needs', 'settings need')} fixing before saving.`
                  : changes.restartKeys.length
                    ? changeCount === 1 ? 'It needs a restart to take effect.' : `${changes.restartKeys.length} of them need a restart to take effect.`
                    : changeCount === 1 ? 'It applies right away.' : 'They apply right away.'}
              </span>
            </span>
            <span className="flex gap-2">
              <Button variant="secondary" onClick={clearDrafts} disabled={save.isPending}>Discard</Button>
              <Button
                onClick={() => (changes.restartKeys.length ? setConfirming(true) : submit(false))}
                disabled={readOnly || errorCount > 0}
              >
                Save changes
              </Button>
            </span>
          </div>
        </section>
      ) : null}

      {confirming ? (
        <ConfirmSave
          view={view}
          fields={view.fields}
          values={changes.values}
          restartAvailable={view.restart.available}
          saving={save.isPending}
          onCancel={() => setConfirming(false)}
          onSave={submit}
        />
      ) : null}
    </div>
  )
}
