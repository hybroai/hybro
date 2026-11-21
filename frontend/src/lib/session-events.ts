/**
 * Tells the auth provider that the owner's session ended while a page was
 * open: revoked from another browser, signed out by a password change, or
 * expired. The provider then reloads the status, and the gate opens sign-in.
 */
type Listener = () => void

const listeners = new Set<Listener>()

export function onSignedOut(listener: Listener): () => void {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

export function reportSignedOut(): void {
  for (const listener of listeners) listener()
}

/** Reports a 401 `not_signed_in` answer; any other refusal is not a sign-out. */
export function reportIfSignedOut(status: number, details: unknown): void {
  if (status !== 401) return
  const detail = (details as { detail?: { code?: unknown } } | undefined)?.detail
  if (detail?.code === 'not_signed_in') reportSignedOut()
}
