// The owner's sources and the token requirement (backend
// `api_gateway/routes/source_routes.py`). A token is in the create and rotate
// responses only: callers keep it in memory to show it once, never in storage,
// a URL or a log.
import { getApiUrl } from '../utils'
import { apiClient } from '../api-client'
import { accountErrorMessage } from './account'

export interface SourceView {
  source_id: string
  name: string
  /** False once revoked. */
  has_token: boolean
  token_last4: string | null
  created_at: string
  last_used_at: string | null
  revoked_at: string | null
}

/** A source with its new token; shown once, then dropped. */
export interface SourceWithToken extends SourceView {
  token: string
}

export interface SourceList {
  sources: SourceView[]
}

/** Whether a Source API request needs a source token. */
export interface TokenRequirement {
  required: boolean
  /** True when this install already had data when the switch was created, so it began off. */
  started_off: boolean
  updated_at: string
}

const sourceUrl = (sourceId: string, suffix = '') => getApiUrl(`sources/${encodeURIComponent(sourceId)}${suffix}`)
const REQUIREMENT_URL = () => getApiUrl('sources/token-requirement')

export function listSources(signal?: AbortSignal): Promise<SourceList> {
  return apiClient<SourceList>(getApiUrl('sources'), { signal })
}

export function createSource(name: string): Promise<SourceWithToken> {
  return apiClient<SourceWithToken>(getApiUrl('sources'), { method: 'POST', body: { name } })
}

export function renameSource(sourceId: string, name: string): Promise<SourceView> {
  return apiClient<SourceView>(sourceUrl(sourceId), { method: 'PATCH', body: { name } })
}

export function rotateSource(sourceId: string): Promise<SourceWithToken> {
  return apiClient<SourceWithToken>(sourceUrl(sourceId, '/rotate'), { method: 'POST' })
}

export function revokeSource(sourceId: string): Promise<SourceView> {
  return apiClient<SourceView>(sourceUrl(sourceId, '/revoke'), { method: 'POST' })
}

export function getTokenRequirement(signal?: AbortSignal): Promise<TokenRequirement> {
  return apiClient<TokenRequirement>(REQUIREMENT_URL(), { signal })
}

export function setTokenRequirement(required: boolean): Promise<TokenRequirement> {
  return apiClient<TokenRequirement>(REQUIREMENT_URL(), { method: 'PUT', body: { required } })
}

/** The backend's coded message (`{"detail": {"code", "message"}}`), else fixed text. */
export function sourceErrorMessage(error: unknown): string {
  return accountErrorMessage(error, 'Something went wrong. Try again.')
}
