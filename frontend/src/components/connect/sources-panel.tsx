'use client'

import Link from 'next/link'
import { useState, type FormEvent } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { KeyRound, Loader2, MoreHorizontal, Plus, RefreshCw } from 'lucide-react'
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
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu'
import { Input } from '@/components/ui/input'
import { Label } from '@/components/ui/label'
import { Skeleton } from '@/components/ui/skeleton'
import { sourcesQueryKey, useSources } from '@/hooks/useSources'
import { usePageOrigin } from '@/hooks/usePageOrigin'
import {
  createSource,
  renameSource,
  revokeSource,
  rotateSource,
  sourceErrorMessage,
  type SourceView,
  type SourceWithToken,
} from '@/lib/api/source'
import { publicConfig } from '@/lib/config'
import { mcpUrl } from '@/lib/mcp'
import { routes } from '@/lib/routes'
import { formatSessionTime } from '@/lib/session-format'
import { buildAccessMaterial } from './access-material'
import { clientSetups } from './client-setups'
import { ShowOnceToken, type TokenSnippet } from './show-once-token'
import { RequirementSwitch } from './token-requirement'

const NAME_MAX = 120

/** Ready-to-paste snippets that already carry the token. */
function useTokenSnippets(token: string): TokenSnippet[] {
  const origin = usePageOrigin()
  const config = publicConfig()
  const api = buildAccessMaterial('api', config.api_base_url || origin, config.api_prefix, undefined, token)
  const snippets: TokenSnippet[] = [{ id: 'api', label: 'API request', text: api.configuration }]
  // MCP is not served on a public listener yet, so no MCP snippet there.
  if (!config.mcp_disabled) {
    const claude = clientSetups(mcpUrl(origin), token)[0]
    snippets.unshift({ id: 'mcp', label: 'Claude Code command', text: claude.snippet })
  }
  return snippets
}

function TokenShown({ source }: { source: SourceWithToken }) {
  return <ShowOnceToken token={source.token} snippets={useTokenSnippets(source.token)} />
}

function NameField({ value, onChange, autoFocus }: { value: string; onChange: (value: string) => void; autoFocus?: boolean }) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor="source-name">Name</Label>
      <Input
        id="source-name"
        value={value}
        maxLength={NAME_MAX}
        autoFocus={autoFocus}
        autoComplete="off"
        placeholder="CI bot"
        onChange={event => onChange(event.target.value)}
      />
      <p className="text-xs text-muted-foreground">The program this token is for, as you want to see it on its sessions.</p>
    </div>
  )
}

/** Name a new source; then its token, once. */
function CreateSourceDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: (value: string) => createSource(value),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: sourcesQueryKey }) },
  })
  const close = (next: boolean) => {
    // Closing mid-request would drop a token nobody has seen.
    if (!next && create.isPending) return
    onOpenChange(next)
    if (!next) {
      // Drop the token with the dialog.
      create.reset()
      setName('')
    }
  }
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (name.trim() && !create.isPending) create.mutate(name.trim())
  }
  const created = create.data
  return (
    <Dialog open={open} onOpenChange={close}>
      <DialogContent className="sm:max-w-xl">
        <DialogHeader>
          <DialogTitle>{created ? `Token for ${created.name}` : 'Create a source'}</DialogTitle>
          <DialogDescription>
            {created
              ? 'Give this token to the program. It sends it as Authorization: Bearer on every request.'
              : 'A source is one program that sends work to Hybro. It gets its own token, and its sessions carry its name.'}
          </DialogDescription>
        </DialogHeader>
        {created ? (
          <>
            <TokenShown source={created} />
            <DialogFooter><Button onClick={() => close(false)}>Done</Button></DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <NameField value={name} onChange={setName} autoFocus />
            {create.isError ? <Alert variant="destructive"><AlertDescription>{sourceErrorMessage(create.error)}</AlertDescription></Alert> : null}
            <DialogFooter>
              <Button type="button" variant="secondary" disabled={create.isPending} onClick={() => close(false)}>Cancel</Button>
              <Button type="submit" disabled={!name.trim() || create.isPending}>
                {create.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
                Create
              </Button>
            </DialogFooter>
          </form>
        )}
      </DialogContent>
    </Dialog>
  )
}

function RenameDialog({ source, onClose }: { source: SourceView; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(source.name)
  const rename = useMutation({
    mutationFn: (value: string) => renameSource(source.source_id, value),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: sourcesQueryKey })
      onClose()
    },
  })
  const submit = (event: FormEvent) => {
    event.preventDefault()
    if (name.trim() && !rename.isPending) rename.mutate(name.trim())
  }
  return (
    <Dialog open onOpenChange={open => { if (!open) onClose() }}>
      <DialogContent>
        <DialogHeader>
          <DialogTitle>Rename source</DialogTitle>
          <DialogDescription>Its sessions show the new name. The token stays the same.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <NameField value={name} onChange={setName} autoFocus />
          {rename.isError ? <Alert variant="destructive"><AlertDescription>{sourceErrorMessage(rename.error)}</AlertDescription></Alert> : null}
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={onClose}>Cancel</Button>
            <Button type="submit" disabled={!name.trim() || rename.isPending}>Save</Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  )
}

/** Confirm rotating or revoking; a rotation then shows the new token once. */
function TokenActionDialog({ source, action, onClose }: { source: SourceView; action: 'rotate' | 'revoke'; onClose: () => void }) {
  const queryClient = useQueryClient()
  const run = useMutation({
    mutationFn: async (): Promise<SourceView | SourceWithToken> => (action === 'rotate' ? rotateSource(source.source_id) : revokeSource(source.source_id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: sourcesQueryKey })
      if (action === 'revoke') onClose()
    },
  })
  const close = () => {
    run.reset()
    onClose()
  }
  const rotated = action === 'rotate' && run.data && 'token' in run.data ? (run.data as SourceWithToken) : null
  if (rotated) {
    return (
      <Dialog open onOpenChange={open => { if (!open) close() }}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{`New token for ${rotated.name}`}</DialogTitle>
            <DialogDescription>The old token stopped working. Give the program this one; its sessions stay.</DialogDescription>
          </DialogHeader>
          <TokenShown source={rotated} />
          <DialogFooter><Button onClick={close}>Done</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }
  const copy = action === 'rotate'
    ? { title: `Rotate the token of ${source.name}?`, body: 'The current token stops working at once, and a new one is shown once. The source keeps its sessions.', label: 'Rotate token' }
    : { title: `Revoke ${source.name}?`, body: 'Its token stops working for good, and its requests are refused. The source and its sessions stay listed. This cannot be undone.', label: 'Revoke' }
  // Closing mid-request would drop a rotated token nobody has seen.
  return (
    <AlertDialog open onOpenChange={open => { if (!open && !run.isPending) close() }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{copy.title}</AlertDialogTitle>
          <AlertDialogDescription>{copy.body}</AlertDialogDescription>
        </AlertDialogHeader>
        {run.isError ? <Alert variant="destructive"><AlertDescription>{sourceErrorMessage(run.error)}</AlertDescription></Alert> : null}
        <AlertDialogFooter>
          <AlertDialogCancel disabled={run.isPending}>Cancel</AlertDialogCancel>
          <AlertDialogAction
            disabled={run.isPending}
            className={buttonVariants({ variant: 'destructive' })}
            // Keep the dialog open until the backend answers.
            onClick={event => { event.preventDefault(); run.mutate() }}
          >
            {run.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
            {copy.label}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

type Pending = { source: SourceView; action: 'rename' | 'rotate' | 'revoke' }

function SourceRow({ source, onAction }: { source: SourceView; onAction: (pending: Pending) => void }) {
  const revoked = !source.has_token
  return (
    <tr className="border-t">
      <td className="max-w-0 py-3 pr-3 pl-4">
        <span className="block truncate font-medium">{source.name}</span>
        <span className="mt-0.5 block text-xs text-muted-foreground sm:hidden">
          {revoked ? 'Revoked' : `…${source.token_last4 ?? ''}`}
        </span>
      </td>
      <td className="hidden px-3 py-3 font-mono text-xs whitespace-nowrap sm:table-cell">
        {revoked ? <span className="font-sans text-muted-foreground">Revoked</span> : <span aria-label={`Token ending in ${source.token_last4}`}>{`…${source.token_last4}`}</span>}
      </td>
      <td className="hidden px-3 py-3 whitespace-nowrap tabular-nums text-muted-foreground md:table-cell">
        {source.last_used_at ? <time dateTime={source.last_used_at}>{formatSessionTime(source.last_used_at)}</time> : 'Never'}
      </td>
      <td className="hidden px-3 py-3 whitespace-nowrap tabular-nums text-muted-foreground lg:table-cell">
        <time dateTime={source.created_at}>{formatSessionTime(source.created_at)}</time>
      </td>
      <td className="py-3 pr-4 pl-3 text-right whitespace-nowrap">
        <div className="flex items-center justify-end gap-1">
          <Button variant="ghost" size="sm" asChild>
            <Link href={routes.sessionsFromSource(source.source_id)}>Sessions</Link>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="iconSm" className="size-8" aria-label={`Actions for ${source.name}`}>
                <MoreHorizontal aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onAction({ source, action: 'rename' })}>Rename</DropdownMenuItem>
              {revoked ? null : (
                <>
                  <DropdownMenuItem onSelect={() => onAction({ source, action: 'rotate' })}>Rotate token</DropdownMenuItem>
                  <DropdownMenuItem variant="destructive" onSelect={() => onAction({ source, action: 'revoke' })}>Revoke</DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </td>
    </tr>
  )
}

function SourcesTable({ onAction }: { onAction: (pending: Pending) => void }) {
  const sources = useSources()
  const list = sources.data?.sources
  return (
    <div className="overflow-hidden rounded-lg border">
      <table className="w-full table-fixed text-sm">
        <caption className="sr-only">Sources</caption>
        <thead className="bg-muted/30">
          <tr className="text-left text-muted-foreground">
            <th scope="col" className="py-2 pr-3 pl-4 font-medium">Name</th>
            <th scope="col" className="hidden w-28 px-3 py-2 font-medium sm:table-cell">Token</th>
            <th scope="col" className="hidden w-36 px-3 py-2 font-medium md:table-cell">Last used</th>
            <th scope="col" className="hidden w-36 px-3 py-2 font-medium lg:table-cell">Created</th>
            <th scope="col" className="w-36 py-2 pr-4 pl-3"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {sources.isError ? (
            <tr><td colSpan={5} className="p-4">
              <div className="flex flex-wrap items-center gap-3">
                <span>Sources couldn&apos;t load.</span>
                <Button variant="secondary" size="sm" onClick={() => { void sources.refetch() }}>
                  <RefreshCw data-icon="inline-start" aria-hidden="true" />Retry
                </Button>
              </div>
            </td></tr>
          ) : sources.isLoading ? (
            Array.from({ length: 3 }, (_, index) => (
              <tr key={index} className="border-t"><td colSpan={5} className="px-4 py-3"><Skeleton className="h-5" /></td></tr>
            ))
          ) : !list?.length ? (
            <tr><td colSpan={5} className="px-4 py-8 text-muted-foreground">
              No sources yet. Create one for each program that sends work to Hybro, over the API or MCP.
            </td></tr>
          ) : (
            list.map(source => <SourceRow key={source.source_id} source={source} onAction={onAction} />)
          )}
        </tbody>
      </table>
    </div>
  )
}

export function SourcesPanel() {
  const [creating, setCreating] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const close = () => setPending(null)
  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="requirement-heading" className="flex flex-col gap-3 rounded-xl border bg-card p-5">
        <h2 id="requirement-heading" className="sr-only">Token requirement</h2>
        <RequirementSwitch />
      </section>
      <section aria-labelledby="sources-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h2 id="sources-heading" className="text-[15px] font-semibold">Sources</h2>
            <p className="text-sm text-muted-foreground">Each program that sends work has its own token, and every step it sends is recorded under its name.</p>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus data-icon="inline-start" aria-hidden="true" />Create source
          </Button>
        </div>
        <SourcesTable onAction={setPending} />
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <KeyRound className="size-3.5" aria-hidden="true" />
          On the machine that runs Hybro, <code className="font-mono">hybro source create --name &lt;name&gt;</code> creates one too.
        </p>
      </section>
      <CreateSourceDialog open={creating} onOpenChange={setCreating} />
      {pending?.action === 'rename' ? <RenameDialog key={pending.source.source_id} source={pending.source} onClose={close} /> : null}
      {pending && pending.action !== 'rename' ? (
        <TokenActionDialog key={`${pending.action}-${pending.source.source_id}`} source={pending.source} action={pending.action} onClose={close} />
      ) : null}
    </div>
  )
}
