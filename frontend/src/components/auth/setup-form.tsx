'use client'

import { useState, useSyncExternalStore } from 'react'
import { Loader2 } from 'lucide-react'
import { Input } from '@/components/ui/input'
import { Button } from '@/components/ui/button'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormGroup } from '@/components/settings/form-group'
import { LoadingButton } from '@/components/settings/loading-button'
import { PasswordInput } from '@/components/settings/password-input'
import { PlainHttpWarning } from '@/components/auth/plain-http-warning'
import { accountErrorMessage, setupOwner } from '@/lib/api/account'
import { ApiError } from '@/lib/api-client'
import { goTo } from '@/lib/navigation'

const MIN_PASSWORD_LENGTH = 8
const MISSING_CODE = 'That has no setup code. Paste the full link, including the part after #, or paste the code itself.'

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

/**
 * The code from a pasted setup URL or a bare code. A URL with nothing after
 * `#`, or other text, yields an empty string.
 */
function codeFromPaste(value: string): string {
  const lines = value.split(/\r?\n/).map((line) => line.trim()).filter(Boolean)
  const source = lines.find((line) => line.includes('#')) ?? lines[0] ?? ''
  const hashAt = source.lastIndexOf('#')
  if (hashAt >= 0) {
    try {
      return decodeURIComponent(source.slice(hashAt + 1)).trim().replace(/[.,);]+$/, '')
    } catch {
      return ''
    }
  }
  if (source.includes('/') || source.includes(':') || /\s/.test(source)) return ''
  return source
}

/** Keeps the code in the fragment without adding a history entry. */
function writeSetupCode(code: string) {
  const next = `${window.location.pathname}${window.location.search}#${encodeURIComponent(code)}`
  window.history.replaceState(null, '', next)
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
  const fromHash = useSyncExternalStore<string | null>(subscribeToHash, readSetupCode, () => null)
  const [applied, setApplied] = useState<string | null>(null)
  const code = applied ?? fromHash
  const [name, setName] = useState('')
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [paste, setPaste] = useState('')
  const [pasteError, setPasteError] = useState<string | null>(null)

  const passwordsMatch = password === confirm
  const longEnough = password.length >= MIN_PASSWORD_LENGTH
  const canSubmit = Boolean(code) && name.trim() !== '' && email.trim() !== '' && longEnough && passwordsMatch

  function applyPaste(event: React.FormEvent) {
    event.preventDefault()
    const next = codeFromPaste(paste)
    if (!next) {
      setPasteError(MISSING_CODE)
      return
    }
    setPasteError(null)
    setApplied(next)
    writeSetupCode(next)
  }

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
    <Card className="w-full max-w-md">
      {code === null ? (
        <CardContent className="flex min-h-32 items-center justify-center">
          <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
        </CardContent>
      ) : code === '' ? (
        <>
          <CardHeader>
            <CardTitle>Open your setup link</CardTitle>
            <CardDescription>
              This browser does not have the setup link, so the account cannot be created here.
            </CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <PlainHttpWarning />
            <p className="text-sm text-muted-foreground">
              Copy the setup link from the terminal where Hybro is running and paste it here. It ends
              with <code>/setup#…</code>.
            </p>
            <form onSubmit={applyPaste} className="space-y-4">
              <FormGroup id="setup-link" label="Setup link" error={pasteError ?? undefined}>
                <Input
                  id="setup-link"
                  value={paste}
                  onChange={(e) => {
                    setPaste(e.target.value)
                    if (pasteError) setPasteError(null)
                  }}
                  placeholder="http://localhost:3000/setup#…"
                  autoComplete="off"
                  spellCheck={false}
                  required
                />
              </FormGroup>
              <Button type="submit" className="w-full" disabled={paste.trim() === ''}>
                Continue
              </Button>
            </form>
            <p className="text-sm text-muted-foreground">
              If you cannot find it, run <code>hybro status</code> on the machine that runs Hybro. That
              prints a new link and replaces the earlier one.
            </p>
            <details className="text-sm text-muted-foreground">
              <summary className="cursor-pointer">No browser on that machine?</summary>
              <p className="mt-2">
                Run <code>hybro owner create --name {'<name>'} --email {'<email>'}</code> there. It asks
                for a password.
              </p>
            </details>
          </CardContent>
        </>
      ) : (
        <>
          <CardHeader>
            <CardTitle>Create your owner account</CardTitle>
            <CardDescription>You will use this account to sign in to Hybro.</CardDescription>
          </CardHeader>
          <CardContent className="space-y-4">
            <PlainHttpWarning />
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
            <p className="text-xs text-muted-foreground">This link works once and expires in 24 hours.</p>
          </CardContent>
        </>
      )}
    </Card>
  )
}
