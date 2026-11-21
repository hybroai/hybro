'use client'

import { useState } from 'react'
import { Input } from '@/components/ui/input'
import { Card, CardContent, CardDescription, CardHeader, CardTitle } from '@/components/ui/card'
import { FormGroup } from '@/components/settings/form-group'
import { LoadingButton } from '@/components/settings/loading-button'
import { PasswordInput } from '@/components/settings/password-input'
import { PlainHttpWarning } from '@/components/auth/plain-http-warning'
import { accountErrorMessage, signIn } from '@/lib/api/account'
import { goTo, safeRedirect } from '@/lib/navigation'

/** The owner's sign-in. */
export function SignInForm({ redirectUrl }: { redirectUrl: string }) {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault()
    if (!email || !password) return
    setBusy(true)
    setError(null)
    try {
      await signIn(email.trim(), password)
      goTo(safeRedirect(redirectUrl))
    } catch (err: unknown) {
      setPassword('')
      setError(accountErrorMessage(err, 'Sign-in failed. Check that Hybro is running, then try again.'))
      setBusy(false)
    }
  }

  return (
    <Card className="w-full max-w-sm">
      <CardHeader>
        <CardTitle>Sign in to Hybro</CardTitle>
        <CardDescription>Sign in as the owner of this instance.</CardDescription>
      </CardHeader>
      <CardContent className="space-y-4">
        <PlainHttpWarning />
        <form onSubmit={handleSubmit} className="space-y-4">
          <FormGroup id="email" label="Email">
            <Input id="email" type="email" value={email} onChange={(e) => setEmail(e.target.value)} autoComplete="username" required />
          </FormGroup>
          <FormGroup id="password" label="Password">
            <PasswordInput id="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
          </FormGroup>
          {error && <p role="alert" className="text-sm text-destructive">{error}</p>}
          <LoadingButton type="submit" className="w-full" disabled={!email || !password} loading={busy}>
            Sign in
          </LoadingButton>
        </form>
        <p className="text-xs text-muted-foreground">
          Forgot the password? Run <code>hybro owner reset-password</code> on the machine that runs Hybro.
        </p>
      </CardContent>
    </Card>
  )
}
