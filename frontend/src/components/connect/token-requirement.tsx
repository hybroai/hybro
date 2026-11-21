'use client'

import Link from 'next/link'
import { useState, useSyncExternalStore } from 'react'
import { Loader2, ShieldAlert } from 'lucide-react'
import { NoticeBanner } from '@/components/setup/setup-banner'
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
import { Dialog, DialogContent, DialogDescription, DialogHeader, DialogTitle } from '@/components/ui/dialog'
import { Skeleton } from '@/components/ui/skeleton'
import { Switch } from '@/components/ui/switch'
import { useSetTokenRequirement, useTokenRequirement } from '@/hooks/useClients'
import { clientErrorMessage, type TokenRequirement } from '@/lib/api/clients'
import { createDismissalStore } from '@/lib/dismissal'
import { routes } from '@/lib/routes'

// The Connect banner's dismissal stores the switch's `updated_at`, so turning
// the requirement off again shows the banner again. The Overview notice is
// dismissed once for good.
const bannerDismissal = createDismissalStore('hybro.token-requirement.banner-dismissed')
const noticeDismissal = createDismissalStore('hybro.token-requirement.notice-dismissed')

const OPEN_RISK = 'Any program that can reach this Hybro can send work to your agents without a token.'

const CONFIRM = {
  on: {
    title: 'Require a client token?',
    body: 'From the next request on, programs and MCP clients that send no token are refused until they send one created on this tab. Their earlier sessions stay, recorded under "No token".',
    action: 'Require tokens',
  },
  off: {
    title: 'Stop requiring a client token?',
    body: `${OPEN_RISK} Their requests are recorded under "No token", and nothing tells them apart. A revoked or unknown token is still refused.`,
    action: 'Stop requiring',
  },
} as const

/** The switch, behind a confirmation that states the consequence. */
export function RequirementSwitch() {
  const requirement = useTokenRequirement()
  const change = useSetTokenRequirement()
  const [asking, setAsking] = useState<boolean | null>(null)

  if (requirement.isLoading) return <Skeleton className="h-16 w-full" />
  if (requirement.isError || !requirement.data) {
    return (
      <Alert variant="destructive">
        <AlertDescription className="flex flex-wrap items-center gap-3">
          The token requirement couldn&apos;t load.
          <Button variant="secondary" size="sm" onClick={() => { void requirement.refetch() }}>Retry</Button>
        </AlertDescription>
      </Alert>
    )
  }
  const required = requirement.data.required
  const copy = asking === null ? null : CONFIRM[asking ? 'on' : 'off']
  return (
    <div className="flex flex-col gap-3">
      <div className="flex items-start justify-between gap-4">
        <div className="flex min-w-0 flex-col gap-1">
          <label htmlFor="token-requirement" className="text-sm font-medium">Require a client token</label>
          <p id="token-requirement-state" className="text-sm text-muted-foreground">
            {required
              ? 'On: every Client API and MCP request needs a valid client token.'
              : 'Off: requests without a token are accepted, so any program that can reach Hybro can use your agents.'}
          </p>
        </div>
        <Switch
          id="token-requirement"
          aria-describedby="token-requirement-state"
          checked={required}
          disabled={change.isPending}
          onCheckedChange={checked => { change.reset(); setAsking(checked) }}
        />
      </div>
      {change.isError ? (
        <Alert variant="destructive"><AlertDescription>{clientErrorMessage(change.error)}</AlertDescription></Alert>
      ) : null}
      <AlertDialog open={copy !== null} onOpenChange={open => { if (!open) setAsking(null) }}>
        {copy ? (
          <AlertDialogContent>
            <AlertDialogHeader>
              <AlertDialogTitle>{copy.title}</AlertDialogTitle>
              <AlertDialogDescription>{copy.body}</AlertDialogDescription>
            </AlertDialogHeader>
            <AlertDialogFooter>
              <AlertDialogCancel>Cancel</AlertDialogCancel>
              <AlertDialogAction
                className={asking ? undefined : buttonVariants({ variant: 'destructive' })}
                onClick={() => { if (asking !== null) change.mutate(asking) }}
              >
                {change.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" aria-hidden="true" /> : null}
                {copy.action}
              </AlertDialogAction>
            </AlertDialogFooter>
          </AlertDialogContent>
        ) : null}
      </AlertDialog>
    </div>
  )
}

/** Explains what the requirement does, and holds the switch. */
export function RequirementDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        <DialogHeader>
          <DialogTitle>Requests without a client token</DialogTitle>
          <DialogDescription>
            A client token names the program that sends each step, and only you can create one.
          </DialogDescription>
        </DialogHeader>
        <div className="flex flex-col gap-3 text-sm text-muted-foreground">
          <p>
            While the requirement is off, Hybro accepts a request that carries no token and records it under the built-in client &quot;No token&quot;. {OPEN_RISK} On a Hybro that other machines can reach, that includes anyone on the network.
          </p>
          <p>
            A request with a revoked or unknown token is always refused, and a web page on another address can never send one without a token.
          </p>
          <p>Create a client for each program on the Clients tab, give it its token, then turn the requirement on.</p>
        </div>
        <RequirementSwitch />
      </DialogContent>
    </Dialog>
  )
}

function useDismissed(store: typeof bannerDismissal): string | null {
  return useSyncExternalStore(store.subscribe, store.read, () => null)
}

function isOff(requirement: TokenRequirement | undefined): requirement is TokenRequirement {
  return requirement !== undefined && !requirement.required
}

/** The Connect page's banner while the requirement is off; dismissable per browser. */
export function TokenRequirementBanner() {
  const requirement = useTokenRequirement()
  const dismissed = useDismissed(bannerDismissal)
  const [explaining, setExplaining] = useState(false)
  const data = requirement.data
  if (!isOff(data) || dismissed === data.updated_at) return explaining ? <RequirementDialog open onOpenChange={setExplaining} /> : null
  return (
    <>
      <NoticeBanner
        tone="warning"
        icon={ShieldAlert}
        testId="token-requirement-banner"
        title="Client tokens are not required"
        body={OPEN_RISK}
        action={<Button size="sm" onClick={() => setExplaining(true)}>Learn more</Button>}
        onDismiss={() => bannerDismissal.write(data.updated_at)}
      />
      <RequirementDialog open={explaining} onOpenChange={setExplaining} />
    </>
  )
}

/** Once, on the Overview, for an install that already had data and so started with the requirement off. */
export function TokenRequirementNotice() {
  const requirement = useTokenRequirement()
  const dismissed = useDismissed(noticeDismissal)
  const data = requirement.data
  if (!isOff(data) || !data.started_off || dismissed !== null) return null
  return (
    <NoticeBanner
      tone="warning"
      icon={ShieldAlert}
      testId="token-requirement-notice"
      title="New: client tokens"
      body="This Hybro was in use before client tokens, so it still accepts programs without one. Give each program a token on the Clients tab, then require tokens."
      action={(
        <Button size="sm" asChild>
          <Link href={routes.connectClients}>Open Clients</Link>
        </Button>
      )}
      onDismiss={() => noticeDismissal.write('1')}
    />
  )
}
