// The owner account API (backend `api_gateway/routes/auth_routes.py`).
// The sign-in cookie is HttpOnly: the browser sends it, no code reads it.
import { getApiUrl } from '../utils'
import { ApiError, apiClient } from '../api-client'

export interface OwnerProfile {
  user_id: string
  name: string
  email: string
}

export interface AuthStatus {
  account_exists: boolean
  /** Set only when this browser is signed in as the owner. */
  owner: OwnerProfile | null
}

export interface SignedInResponse {
  owner: OwnerProfile
}

export interface BrowserSession {
  id: string
  created_at: string
  last_used_at: string
  user_agent: string
  current: boolean
}

export interface SetupInput {
  code: string
  name: string
  email: string
  password: string
}

const url = (path: string) => getApiUrl(`auth/${path}`)

export function getAuthStatus(signal?: AbortSignal): Promise<AuthStatus> {
  return apiClient<AuthStatus>(url('status'), { signal })
}

export function setupOwner(input: SetupInput): Promise<SignedInResponse> {
  return apiClient<SignedInResponse>(url('setup'), { method: 'POST', body: input })
}

export function signIn(email: string, password: string): Promise<SignedInResponse> {
  return apiClient<SignedInResponse>(url('sign-in'), { method: 'POST', body: { email, password } })
}

export function signOut(): Promise<void> {
  return apiClient<void>(url('sign-out'), { method: 'POST', body: {} })
}

export function updateProfile(update: { name: string; email: string }): Promise<OwnerProfile> {
  return apiClient<OwnerProfile>(url('profile'), { method: 'PATCH', body: update })
}

/** Changes the password; the backend signs out every other browser. */
export function changePassword(change: { current_password: string; new_password: string }): Promise<void> {
  return apiClient<void>(url('password'), { method: 'POST', body: change })
}

export function listBrowsers(): Promise<{ items: BrowserSession[] }> {
  return apiClient<{ items: BrowserSession[] }>(url('browsers'))
}

export function revokeBrowser(browserId: string): Promise<void> {
  return apiClient<void>(url(`browsers/${encodeURIComponent(browserId)}`), { method: 'DELETE' })
}

/** The backend's message for a refused account request, else `fallback`. */
export function accountErrorMessage(error: unknown, fallback: string): string {
  if (!(error instanceof ApiError)) return fallback
  const details = error.details
  if (typeof details !== 'object' || details === null) return fallback
  const detail = (details as { detail?: unknown }).detail
  if (typeof detail === 'object' && detail !== null) {
    const message = (detail as { message?: unknown }).message
    if (typeof message === 'string' && message) return message
  }
  return fallback
}
