'use client'

import Link from 'next/link'
import { useRef, useState, type FormEvent } from 'react'
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
import { Switch } from '@/components/ui/switch'
import { clientsQueryKey, useClients } from '@/hooks/useClients'
import { usePageOrigin } from '@/hooks/usePageOrigin'
import { ApiError } from '@/lib/api-client'
import {
  BUILT_IN_CLIENT_IDS,
  createClient,
  renameClient,
  setAnswerGrant,
  revokeClient,
  rotateClient,
  clientErrorMessage,
  type ClientList,
  type ClientView,
  type ClientWithToken,
} from '@/lib/api/clients'
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
  const claude = clientSetups(mcpUrl(origin), token)[0]
  return [
    { id: 'mcp', label: 'Claude Code command', text: claude.snippet },
    { id: 'api', label: 'API request', text: api.configuration },
  ]
}

const VIA_LABEL = { api: 'API', mcp: 'MCP' } as const

function TokenShown({ client }: { client: ClientWithToken }) {
  return <ShowOnceToken token={client.token} snippets={useTokenSnippets(client.token)} />
}

function NameField({ value, onChange, autoFocus }: { value: string; onChange: (value: string) => void; autoFocus?: boolean }) {
  return (
    <div className="flex flex-col gap-2">
      <Label htmlFor="client-name">Name</Label>
      <Input
        id="client-name"
        value={value}
        maxLength={NAME_MAX}
        autoFocus={autoFocus}
        autoComplete="off"
        placeholder="CI bot"
        onChange={event => onChange(event.target.value)}
      />
      <p className="text-xs text-muted-foreground">The app, assistant, or script this token is for, as you want to see it on its sessions.</p>
    </div>
  )
}

/** Name a new client; then its token, once. */
function CreateClientDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState('')
  const create = useMutation({
    mutationFn: (value: string) => createClient(value),
    onSuccess: () => { void queryClient.invalidateQueries({ queryKey: clientsQueryKey }) },
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
          <DialogTitle>{created ? `Token for ${created.name}` : 'Create a client'}</DialogTitle>
          <DialogDescription>
            {created
              ? 'Give this token to your app, assistant, or script. It sends it as Authorization: Bearer on every request.'
              : 'A client is an app, assistant, or script that sends work to Hybro. It gets its own token, and its sessions carry its name.'}
          </DialogDescription>
        </DialogHeader>
        {created ? (
          <>
            <TokenShown client={created} />
            <DialogFooter><Button onClick={() => close(false)}>Done</Button></DialogFooter>
          </>
        ) : (
          <form onSubmit={submit} className="flex flex-col gap-4">
            <NameField value={name} onChange={setName} autoFocus />
            {create.isError ? <Alert variant="destructive"><AlertDescription>{clientErrorMessage(create.error)}</AlertDescription></Alert> : null}
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

function RenameDialog({ client, onClose }: { client: ClientView; onClose: () => void }) {
  const queryClient = useQueryClient()
  const [name, setName] = useState(client.name)
  const rename = useMutation({
    mutationFn: (value: string) => renameClient(client.client_id, value),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: clientsQueryKey })
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
          <DialogTitle>Rename client</DialogTitle>
          <DialogDescription>Its sessions show the new name. The token stays the same.</DialogDescription>
        </DialogHeader>
        <form onSubmit={submit} className="flex flex-col gap-4">
          <NameField value={name} onChange={setName} autoFocus />
          {rename.isError ? <Alert variant="destructive"><AlertDescription>{clientErrorMessage(rename.error)}</AlertDescription></Alert> : null}
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
function TokenActionDialog({ client, action, onClose }: { client: ClientView; action: 'rotate' | 'revoke'; onClose: () => void }) {
  const queryClient = useQueryClient()
  const run = useMutation({
    mutationFn: async (): Promise<ClientView | ClientWithToken> => (action === 'rotate' ? rotateClient(client.client_id) : revokeClient(client.client_id)),
    onSuccess: () => {
      void queryClient.invalidateQueries({ queryKey: clientsQueryKey })
      if (action === 'revoke') onClose()
    },
  })
  const close = () => {
    run.reset()
    onClose()
  }
  const rotated = action === 'rotate' && run.data && 'token' in run.data ? (run.data as ClientWithToken) : null
  if (rotated) {
    return (
      <Dialog open onOpenChange={open => { if (!open) close() }}>
        <DialogContent className="sm:max-w-xl">
          <DialogHeader>
            <DialogTitle>{`New token for ${rotated.name}`}</DialogTitle>
            <DialogDescription>The old token stopped working. Give the client this new token; its sessions stay.</DialogDescription>
          </DialogHeader>
          <TokenShown client={rotated} />
          <DialogFooter><Button onClick={close}>Done</Button></DialogFooter>
        </DialogContent>
      </Dialog>
    )
  }
  const copy = action === 'rotate'
    ? { title: `Rotate the token of ${client.name}?`, body: 'The current token stops working at once, and a new one is shown once. The client keeps its sessions.', label: 'Rotate token' }
    : { title: `Revoke ${client.name}?`, body: 'Its token stops working for good, and its requests are refused. The client and its sessions stay listed. This cannot be undone.', label: 'Revoke' }
  // Closing mid-request would drop a rotated token nobody has seen.
  return (
    <AlertDialog open onOpenChange={open => { if (!open && !run.isPending) close() }}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{copy.title}</AlertDialogTitle>
          <AlertDialogDescription>{copy.body}</AlertDialogDescription>
        </AlertDialogHeader>
        {run.isError ? <Alert variant="destructive"><AlertDescription>{clientErrorMessage(run.error)}</AlertDescription></Alert> : null}
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

type Pending = { client: ClientView; action: 'rename' | 'rotate' | 'revoke' }

const GRANT_HELP_ID = 'answer-grant-help'
const GRANT_HELP = 'This client can approve or reject permission questions (a check rule\'s ask and an agent\'s own approval question) in its own sessions. Other questions stay with you.'

/** Fixed text for a refusal; the list is reloaded, since it was out of date. */
function grantErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 404) return 'This client no longer exists. The list is up to date again.'
  if (error instanceof ApiError && error.status === 409) return 'This client is revoked or built in, so it cannot answer questions. The list is up to date again.'
  return clientErrorMessage(error)
}

function canHoldGrant(client: ClientView): boolean {
  return client.has_token && !BUILT_IN_CLIENT_IDS.has(client.client_id)
}

/** Turning the grant on asks first; turning it off applies at once. */
function useAnswerGrant(onError: (message: string | null) => void) {
  const queryClient = useQueryClient()
  // Per client: one shared mutation only knows its latest call.
  const [inFlight, setInFlight] = useState<ReadonlySet<string>>(() => new Set())
  const mark = (clientId: string, busy: boolean) => setInFlight(current => {
    const next = new Set(current)
    if (busy) next.add(clientId)
    else next.delete(clientId)
    return next
  })
  const mutation = useMutation({
    mutationFn: ({ client, granted }: { client: ClientView; granted: boolean }) => setAnswerGrant(client.client_id, granted),
    onMutate: ({ client }) => { onError(null); mark(client.client_id, true) },
    onSettled: (_data, _error, { client }) => mark(client.client_id, false),
    onSuccess: view => {
      queryClient.setQueryData<ClientList>(clientsQueryKey, list => list && { clients: list.clients.map(item => item.client_id === view.client_id ? view : item) })
    },
    onError: error => {
      onError(grantErrorMessage(error))
      void queryClient.invalidateQueries({ queryKey: clientsQueryKey })
    },
  })
  return { mutate: mutation.mutate, inFlight }
}

function GrantSwitch({ client, pending, onChange }: { client: ClientView; pending: boolean; onChange: (granted: boolean, opener: HTMLElement) => void }) {
  const ref = useRef<HTMLButtonElement>(null)
  if (!canHoldGrant(client)) return <><span aria-hidden="true">—</span><span className="sr-only">Not available</span></>
  // Not disabled while the change is in flight: a disabled switch loses focus.
  return (
    <Switch
      ref={ref}
      checked={client.answer_grant}
      aria-busy={pending || undefined}
      aria-label={`${client.name} can answer permission questions`}
      aria-describedby={GRANT_HELP_ID}
      onCheckedChange={granted => { if (!pending && ref.current) onChange(granted, ref.current) }}
    />
  )
}

function GrantConfirmDialog({ client, opener, onConfirm, onClose }: { client: ClientView; opener: HTMLElement; onConfirm: () => void; onClose: () => void }) {
  return (
    <AlertDialog open onOpenChange={open => { if (!open) onClose() }}>
      {/* Opened without a trigger, so focus is handed back to the switch. */}
      <AlertDialogContent onCloseAutoFocus={event => { event.preventDefault(); opener.focus() }}>
        <AlertDialogHeader>
          <AlertDialogTitle>{`Let ${client.name} answer permission questions?`}</AlertDialogTitle>
          <AlertDialogDescription>
            It can approve or reject a check rule&apos;s ask and an agent&apos;s own approval question in its own sessions, as you can in Hybro. Other questions stay with you. You can turn this off at any time.
          </AlertDialogDescription>
        </AlertDialogHeader>
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction onClick={onConfirm}>Allow</AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  )
}

function ClientRow({ client, onAction, grant }: { client: ClientView; onAction: (pending: Pending) => void; grant: { pending: boolean; onChange: (granted: boolean, opener: HTMLElement) => void } }) {
  const revoked = !client.has_token
  return (
    <tr className="border-t">
      <td className="max-w-0 py-3 pr-3 pl-4">
        <span className="block truncate font-medium">{client.name}</span>
        <span className="mt-0.5 block text-xs text-muted-foreground sm:hidden">
          {revoked ? 'Revoked' : `…${client.token_last4 ?? ''}`}
        </span>
      </td>
      <td className="hidden px-3 py-3 font-mono text-xs whitespace-nowrap sm:table-cell">
        {revoked ? <span className="font-sans text-muted-foreground">Revoked</span> : <span aria-label={`Token ending in ${client.token_last4}`}>{`…${client.token_last4}`}</span>}
      </td>
      <td className="hidden px-3 py-3 whitespace-nowrap tabular-nums text-muted-foreground md:table-cell">
        {client.last_used_at ? <time dateTime={client.last_used_at}>{formatSessionTime(client.last_used_at)}</time> : 'Never'}
      </td>
      <td data-testid="client-via" className="hidden px-3 py-3 whitespace-nowrap text-muted-foreground md:table-cell" aria-label={client.last_used_via ? undefined : 'Not known'}>
        {client.last_used_via ? VIA_LABEL[client.last_used_via] : <span aria-hidden="true">—</span>}
      </td>
      <td className="hidden px-3 py-3 whitespace-nowrap tabular-nums text-muted-foreground lg:table-cell">
        <time dateTime={client.created_at}>{formatSessionTime(client.created_at)}</time>
      </td>
      <td className="px-3 py-3 whitespace-nowrap text-muted-foreground">
        <GrantSwitch client={client} {...grant} />
      </td>
      <td className="py-3 pr-4 pl-3 text-right whitespace-nowrap">
        <div className="flex items-center justify-end gap-1">
          <Button variant="ghost" size="sm" asChild>
            <Link href={routes.sessionsFromClient(client.client_id)}>Sessions</Link>
          </Button>
          <DropdownMenu>
            <DropdownMenuTrigger asChild>
              <Button variant="ghost" size="iconSm" className="size-8" aria-label={`Actions for ${client.name}`}>
                <MoreHorizontal aria-hidden="true" />
              </Button>
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end">
              <DropdownMenuItem onSelect={() => onAction({ client, action: 'rename' })}>Rename</DropdownMenuItem>
              {revoked ? null : (
                <>
                  <DropdownMenuItem onSelect={() => onAction({ client, action: 'rotate' })}>Rotate token</DropdownMenuItem>
                  <DropdownMenuItem variant="destructive" onSelect={() => onAction({ client, action: 'revoke' })}>Revoke</DropdownMenuItem>
                </>
              )}
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </td>
    </tr>
  )
}

function ClientsTable({ onAction }: { onAction: (pending: Pending) => void }) {
  const clients = useClients()
  const list = clients.data?.clients
  const [grantError, setGrantError] = useState<string | null>(null)
  const [confirming, setConfirming] = useState<{ client: ClientView; opener: HTMLElement } | null>(null)
  const grant = useAnswerGrant(setGrantError)
  const change = (client: ClientView) => (granted: boolean, opener: HTMLElement) => {
    if (granted) setConfirming({ client, opener })
    else grant.mutate({ client, granted: false })
  }
  return (
    <>
    {grantError ? <Alert variant="destructive"><AlertDescription>{grantError}</AlertDescription></Alert> : null}
    <div className="overflow-hidden rounded-lg border">
      <table className="w-full table-fixed text-sm">
        <caption className="sr-only">Clients</caption>
        <thead className="bg-muted/30">
          <tr className="text-left text-muted-foreground">
            <th scope="col" className="py-2 pr-3 pl-4 font-medium">Name</th>
            <th scope="col" className="hidden w-28 px-3 py-2 font-medium sm:table-cell">Token</th>
            <th scope="col" className="hidden w-36 px-3 py-2 font-medium md:table-cell">Last used</th>
            <th scope="col" className="hidden w-36 px-3 py-2 font-medium md:table-cell">Connects through</th>
            <th scope="col" className="hidden w-36 px-3 py-2 font-medium lg:table-cell">Created</th>
            <th scope="col" className="w-[4.5rem] px-3 py-2 font-medium sm:w-28 sm:whitespace-nowrap">Can answer</th>
            <th scope="col" className="w-36 py-2 pr-4 pl-3"><span className="sr-only">Actions</span></th>
          </tr>
        </thead>
        <tbody>
          {clients.isError ? (
            <tr><td colSpan={7} className="p-4">
              <div className="flex flex-wrap items-center gap-3">
                <span>Clients couldn&apos;t load.</span>
                <Button variant="secondary" size="sm" onClick={() => { void clients.refetch() }}>
                  <RefreshCw data-icon="inline-start" aria-hidden="true" />Retry
                </Button>
              </div>
            </td></tr>
          ) : clients.isLoading ? (
            Array.from({ length: 3 }, (_, index) => (
              <tr key={index} className="border-t"><td colSpan={7} className="px-4 py-3"><Skeleton className="h-5" /></td></tr>
            ))
          ) : !list?.length ? (
            <tr><td colSpan={7} className="px-4 py-8 text-muted-foreground">
              No clients yet. Create one for each app, assistant, or script that sends work to Hybro, over the API or MCP.
            </td></tr>
          ) : (
            list.map(client => (
              <ClientRow
                key={client.client_id}
                client={client}
                onAction={onAction}
                grant={{ pending: grant.inFlight.has(client.client_id), onChange: change(client) }}
              />
            ))
          )}
        </tbody>
      </table>
    </div>
    {confirming ? (
      <GrantConfirmDialog
        key={confirming.client.client_id}
        client={confirming.client}
        opener={confirming.opener}
        onConfirm={() => grant.mutate({ client: confirming.client, granted: true })}
        onClose={() => setConfirming(null)}
      />
    ) : null}
    </>
  )
}

export function ClientsPanel() {
  const [creating, setCreating] = useState(false)
  const [pending, setPending] = useState<Pending | null>(null)
  const close = () => setPending(null)
  return (
    <div className="flex flex-col gap-8">
      <section aria-labelledby="requirement-heading" className="flex flex-col gap-3 rounded-xl border bg-card p-5">
        <h2 id="requirement-heading" className="sr-only">Token requirement</h2>
        <RequirementSwitch />
      </section>
      <section aria-labelledby="clients-heading" className="flex flex-col gap-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div className="flex flex-col gap-1">
            <h2 id="clients-heading" className="text-[15px] font-semibold">Clients</h2>
            <p className="text-sm text-muted-foreground">Each connected app, assistant, or script has its own token, and every step it sends is recorded under its name.</p>
            <p className="text-sm text-muted-foreground">
              <span className="font-medium text-foreground">Can answer: </span><span id={GRANT_HELP_ID}>{GRANT_HELP}</span>
            </p>
          </div>
          <Button onClick={() => setCreating(true)}>
            <Plus data-icon="inline-start" aria-hidden="true" />Create client
          </Button>
        </div>
        <ClientsTable onAction={setPending} />
        <p className="flex items-center gap-1.5 text-xs text-muted-foreground">
          <KeyRound className="size-3.5" aria-hidden="true" />
          On the machine that runs Hybro, <code className="font-mono">hybro client create --name &lt;name&gt;</code> creates one too.
        </p>
      </section>
      <CreateClientDialog open={creating} onOpenChange={setCreating} />
      {pending?.action === 'rename' ? <RenameDialog key={pending.client.client_id} client={pending.client} onClose={close} /> : null}
      {pending && pending.action !== 'rename' ? (
        <TokenActionDialog key={`${pending.action}-${pending.client.client_id}`} client={pending.client} action={pending.action} onClose={close} />
      ) : null}
    </div>
  )
}
