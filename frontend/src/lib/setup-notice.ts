import type { SetupStatus } from '@/lib/api/setup'
import { createDismissalStore } from '@/lib/dismissal'

/** What a banner says about the model provider, keyed by the error code it reports. */
export interface SetupNotice {
  code: string
  /** `error` (red) for an expired ChatGPT sign-in, `warning` (amber) otherwise. */
  tone: 'warning' | 'error'
  title: string
  /** Body on the Overview. */
  message: string
  /** Body above the Room Chat composer. */
  chatMessage: string
  /** Label of the link to Settings, Model provider. */
  action: string
  /** True when model calls cannot work, so Room Chat disables its composer. */
  blocking: boolean
}

type NoticeCopy = Omit<SetupNotice, 'code' | 'blocking'>

const NOT_SET_UP: NoticeCopy = {
  tone: 'warning',
  title: 'Connect a model provider',
  message: 'Room Chat and 4 sample agents need one. Until then, MCP clients can use the Echo agent.',
  chatMessage: 'Room Chat needs a model to plan and answer. This stays until a provider works.',
  action: 'Set up provider',
}
const EXPIRED: NoticeCopy = {
  tone: 'error',
  title: 'ChatGPT sign-in expired',
  message: 'Room Chat and 4 sample agents are paused until you sign in again.',
  chatMessage: 'Room Chat is paused. Existing chats are kept; sign in again to continue.',
  action: 'Sign in again',
}

function attention(message: string): NoticeCopy {
  return { tone: 'warning', title: 'Model provider needs attention', message, chatMessage: message, action: 'Open settings' }
}

const OTHER: Record<string, NoticeCopy> = {
  credential_invalid: attention('The saved API key is missing or does not match the configuration. Save a new one to continue.'),
  config_invalid: attention('config.json or auth.json is invalid. Hybro keeps the last valid settings until the file is fixed.'),
  config_unreadable: attention('config.json or auth.json cannot be read. Hybro keeps the last valid settings until the file is fixed.'),
}
const FALLBACK = attention('The model provider reported a problem. Open Settings to check it.')

/** True when the saved ChatGPT sign-in can no longer be used or renewed. */
export function isSignInExpired(status: SetupStatus): boolean {
  return status.last_error === 'oauth_refresh_failed'
    || (status.state === 'credential_invalid' && status.auth === 'oauth')
}

export function setupNotice(status: SetupStatus | undefined): SetupNotice | null {
  if (!status) return null
  // A failed renewal means model calls fail too, so it blocks like a missing provider.
  if (isSignInExpired(status)) {
    return { code: status.last_error ?? status.state, blocking: true, ...EXPIRED }
  }
  if (status.state === 'unconfigured') return { code: status.state, blocking: true, ...NOT_SET_UP }
  if (status.state !== 'configured') return { code: status.state, blocking: true, ...OTHER[status.state] }
  if (!status.last_error) return null
  return { code: status.last_error, blocking: false, ...(OTHER[status.last_error] ?? FALLBACK) }
}

// Overview dismissal, per browser. Storing the dismissed code means a new error
// code shows the banner again.
const dismissal = createDismissalStore('hybro.setup-notice.dismissed')

export const subscribeDismissed = dismissal.subscribe
export const readDismissed = dismissal.read
export const writeDismissed = dismissal.write

export function isDismissed(notice: SetupNotice, dismissed: string | null): boolean {
  return dismissed === notice.code
}
