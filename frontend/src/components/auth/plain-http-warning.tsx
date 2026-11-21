'use client'

import { useSyncExternalStore } from 'react'
import { TriangleAlert } from 'lucide-react'
import { Alert, AlertDescription, AlertTitle } from '@/components/ui/alert'
import { isPlainHttpFromAnotherMachine } from '@/lib/plain-http'

// The page's address does not change while it is open.
const subscribeNever = () => () => undefined

const PASSWORD_WARNING = 'It is served over plain HTTP from another machine, so your password and sign-in travel the network readable. Use HTTPS, through a reverse proxy in front of Hybro, before signing in from another machine.'

/** Warns that what is typed or shown here crosses the network unencrypted; the sign-in text by default. */
export function PlainHttpWarning({ children = PASSWORD_WARNING }: { children?: string }) {
  const show = useSyncExternalStore(
    subscribeNever,
    () => isPlainHttpFromAnotherMachine(window.location),
    () => false,
  )
  if (!show) return null
  return (
    <Alert variant="destructive">
      <TriangleAlert aria-hidden="true" />
      <AlertTitle>This page is not encrypted</AlertTitle>
      <AlertDescription>{children}</AlertDescription>
    </Alert>
  )
}
