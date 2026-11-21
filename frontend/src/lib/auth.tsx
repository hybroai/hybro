'use client'

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react'
import { usePathname, useRouter } from 'next/navigation'
import { Loader2 } from 'lucide-react'
import { getAuthStatus, signOut as signOutRequest, type OwnerProfile } from '@/lib/api/account'
import { log } from '@/lib/log'
import { goTo } from '@/lib/navigation'
import { onSignedOut } from '@/lib/session-events'

/**
 * Headers for a client-side API request. The owner's sign-in cookie is the
 * credential, and the browser sends it itself, so no Authorization header is
 * added. `getToken` is accepted and ignored until its call sites go.
 */
export async function getClientAuthHeaders(
  _getToken?: () => Promise<string | null>
): Promise<Record<string, string>> {
  return { 'Content-Type': 'application/json' }
}

/** The signed-in owner, in the shape the portal components read. */
export interface AuthUser {
  id: string
  fullName: string
  firstName: string
  username: null
  primaryEmailAddress: { emailAddress: string }
  imageUrl: string
  /** The owner account cannot delete itself; the danger zone stays hidden. */
  deleteSelfEnabled: false
  delete: () => Promise<void>
}

function toUser(owner: OwnerProfile): AuthUser {
  return {
    id: owner.user_id,
    fullName: owner.name,
    firstName: owner.name.split(/\s+/)[0] ?? owner.name,
    username: null,
    primaryEmailAddress: { emailAddress: owner.email },
    imageUrl: '',
    deleteSelfEnabled: false,
    delete: async () => {
      throw new Error('The owner account cannot be deleted.')
    },
  }
}

type AuthState =
  | { phase: 'loading' }
  | { phase: 'failed' }
  | { phase: 'ready'; accountExists: boolean; user: AuthUser | null }

interface AuthContextValue {
  state: AuthState
  refresh: () => Promise<void>
}

// Outside the provider nothing is signed in yet; the portal layout provides it.
async function loadAuthState(): Promise<AuthState> {
  try {
    const status = await getAuthStatus()
    return {
      phase: 'ready',
      accountExists: status.account_exists,
      user: status.owner ? toUser(status.owner) : null,
    }
  } catch (error) {
    log.warn('auth_status_failed', { error_name: error instanceof Error ? error.name : typeof error })
    return { phase: 'failed' }
  }
}

const AuthContext = createContext<AuthContextValue>({
  state: { phase: 'loading' },
  refresh: async () => undefined,
})

/**
 * Loads `/auth/status` and shares the owner with the portal. It loads again
 * after a profile change and when an API call reports that the session ended.
 */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  const [state, setState] = useState<AuthState>({ phase: 'loading' })

  // A reload that fails keeps what is shown; only the first load can fail the page.
  const refresh = useCallback(async () => {
    const next = await loadAuthState()
    setState((current) => (next.phase === 'failed' && current.phase === 'ready' ? current : next))
  }, [])

  useEffect(() => {
    let active = true
    void loadAuthState().then((next) => {
      if (active) setState(next)
    })
    const stop = onSignedOut(() => {
      if (active) void refresh()
    })
    return () => {
      active = false
      stop()
    }
  }, [refresh])

  const value = useMemo(() => ({ state, refresh }), [state, refresh])
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>
}

function useAuthContext(): AuthContextValue {
  return useContext(AuthContext)
}

/** Reloads the owner after a profile change. */
export function useAuthRefresh(): () => Promise<void> {
  return useAuthContext().refresh
}

/**
 * Renders the portal only for the signed-in owner. Without an account it
 * opens setup; signed out, it opens sign-in and comes back to this page.
 */
export function AuthGate({ children }: { children: React.ReactNode }) {
  const { state } = useAuthContext()
  const router = useRouter()
  const pathname = usePathname()

  const signedOut = state.phase === 'ready' && !state.user
  const accountExists = state.phase === 'ready' && state.accountExists

  useEffect(() => {
    if (!signedOut) return
    // The query string too, so the page opens again as it was (filters, tabs).
    const back = `${pathname || '/'}${window.location.search}`
    router.replace(accountExists ? `/sign-in?${new URLSearchParams({ redirect_url: back })}` : '/setup')
  }, [router, signedOut, accountExists, pathname])

  if (state.phase === 'failed') {
    return (
      <div className="flex min-h-64 items-center justify-center p-6 text-sm text-muted-foreground" role="alert">
        Hybro is not reachable. Check that it is running, then reload the page.
      </div>
    )
  }
  if (state.phase !== 'ready' || !state.user) {
    return (
      <div className="flex h-full min-h-64 items-center justify-center">
        <Loader2 className="h-6 w-6 animate-spin text-muted-foreground" aria-label="Loading" />
      </div>
    )
  }
  return <>{children}</>
}

export function useUser() {
  const { state } = useAuthContext()
  const user = state.phase === 'ready' ? state.user : null
  return { isLoaded: state.phase !== 'loading', isSignedIn: user !== null, user }
}

/** The cookie is the credential, so there is no token to hand out. */
const noToken = async (): Promise<string | null> => null

export function useAuth() {
  const { user } = useUser()
  return { isLoaded: true, isSignedIn: user !== null, userId: user?.id, getToken: noToken }
}

export function useAuthClient() {
  return {
    signOut: async (_opts?: unknown) => {
      try {
        await signOutRequest()
      } catch (error) {
        log.warn('auth_sign_out_failed', { error_name: error instanceof Error ? error.name : typeof error })
      }
      goTo('/sign-in')
    },
  }
}
