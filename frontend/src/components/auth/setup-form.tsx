'use client'

import { useState, useSyncExternalStore } from 'react'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormGroup } from '@/components/settings/form-group'
import { LoadingButton } from '@/components/settings/loading-button'
import { PasswordInput } from '@/components/settings/password-input'
import { PlainHttpWarning } from '@/components/auth/plain-http-warning'
import { accountErrorMessage, setupOwner } from '@/lib/api/account'
import { ApiError } from '@/lib/api-client'
import { goTo } from '@/lib/navigation'

const MIN_PASSWORD_LENGTH = 8

/** The code after the `#` of the setup link; the fragment never reaches a server. */
function readSetupCode(): string {
  try {
    return decodeURIComponent(window.location.hash.replace(/^#/, '')).trim()
  } catch {
    return ''
  }
}

function subscribeToHash(onChange: () => void): () => void {
  window.addEventListener('hashchange', onChange)
  return () => window.removeEventListener('hashchange', onChange)
}

/** Creates the owner account through the one-time setup link. */
/**
 * The refusal to show. With several administrators listed, the browser cannot
 * choose one; `hybro owner create --owner-id` on the machine can.
 */
function setupErrorMessage(err: unknown): string {
  const message = accountErrorMessage(err, 'Setup failed. Check that Hybro is running, then try again.')
  const detail = err instanceof ApiError
    ? (err.details as { detail?: { code?: unknown; choices?: unknown } } | undefined)?.detail
    : undefined
  if (detail?.code !== 'owner_id_required') return message
  const choices = Array.isArray(detail.choices) ? detail.choices.map(String).join(', ') : ''
  return `${message} Run on Hybro's machine: hybro owner create --name <name> --email <email> --owner-id <id>`
    + (choices ? `, with one of: ${choices}.` : '.')
}

export function SetupForm() {
  // Null while rendering on the server, which never sees the fragment.
  const code = useSyncExternalStore<string | null>(subscribeToHash, readSetupCode, () => null)
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  const passwordsMatch = password === confirm
  const longEnough = password.length >= MIN_PASSWORD_LENGTH
  const canSubmit = Boolean(code) && name.trim() !== '' && email.trim() !== '' && longEnough && passwordsMatch

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!canSubmit || !code) return
    setBusy(true)
    setError(null)
    try {
      await setupOwner({ code, name: name.trim(), email: email.trim(), password })
      // The code is used up; keep it out of the history.
      window.history.replaceState(null, '', window.location.pathname)
      goTo('/')
    } catch (err: unknown) {
      setError(setupErrorMessage(err))
      setBusy(false)
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>Create the owner account</CardTitle>
        <CardDescription>The account that signs in to this Hybro. There is one.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <PlainHttpWarning />
        {code === null ? null : code === '' ? (
          <p className="text-sm text-muted-foreground">
            Open the setup link that <code>hybro</code> prints. To see it again, run{' '}
            <code>hybro status</code> on the machine that runs Hybro. Without a browser, create the
            account with <code>hybro owner create</code>.
          </p>
        ) : (
          <form onSubmit={handleSubmit} className="space-y-4">
            <FormGroup id="name" label="Name">
              <Input id="name" value={name} onChange={(e) => setName(e.target.value)} autoComplete="name" required />
            </FormGroup>
            <FormGroup id="email" label="Email" hint="You sign in with it.">
              <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
            </FormGroup>
            <FormGroup
              id="password"
              label="Password"
              error={password.length > 0 && !longEnough ? `At least ${MIN_PASSWORD_LENGTH} characters` : undefined}
            >
              <PasswordInput id="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="new-password" required />
            </FormGroup>
            <FormGroup
              id="confirm"
              label="Confirm password"
              error={confirm.length > 0 && !passwordsMatch ? 'Passwords do not match' : undefined}
            >
              <PasswordInput id="confirm" value={confirm} onChange={(e) => setConfirm(e.target.value)} autoComplete="new-password" required />
            </FormGroup>
            {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
            <LoadingButton type="submit" className="w-full" disabled={!canSubmit} loading={busy}>
              Create account
            </LoadingButton>
          </form>
        )}
      </CardContent>
    </Card>
  )
}
