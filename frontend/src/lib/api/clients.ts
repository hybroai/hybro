// The owner's clients and the token requirement (backend
// `api_gateway/routes/client_routes.py`). A token is in the create and rotate
// responses only: callers keep it in memory to show it once, never in storage,
// a URL or a log.
import { getApiUrl } from '../utils'
import { apiClient } from '../api-client'
import { accountErrorMessage } from './account'

export interface ClientView {
  client_id: string
  name: string
  /** False once revoked. */
  has_token: boolean
  /** May answer permission questions through the Client API; false once revoked. */
  answer_grant: boolean
  token_last4: string | null
  created_at: string
  last_used_at: string | null
  /** How the last request connected: through the MCP server, or the API directly. */
  last_used_via: 'api' | 'mcp' | null
  revoked_at: string | null
}

/** A client with its new token; shown once, then dropped. */
export interface ClientWithToken extends ClientView {
  token: string
}

export interface ClientList {
  clients: ClientView[]
}

/** Whether a Client API request needs a client token. */
export interface TokenRequirement {
  required: boolean
  /** True when this install already had data when the switch was created, so it began off. */
  started_off: boolean
  updated_at: string
}

const clientUrl = (clientId: string, suffix = '') => getApiUrl(`clients/${encodeURIComponent(clientId)}${suffix}`)
const REQUIREMENT_URL = () => getApiUrl('clients/token-requirement')

export function listClients(signal?: AbortSignal): Promise<ClientList> {
  return apiClient<ClientList>(getApiUrl('clients'), { signal })
}

export function createClient(name: string): Promise<ClientWithToken> {
  return apiClient<ClientWithToken>(getApiUrl('clients'), { method: 'POST', body: { name } })
}

export function renameClient(clientId: string, name: string): Promise<ClientView> {
  return apiClient<ClientView>(clientUrl(clientId), { method: 'PATCH', body: { name } })
}

export function rotateClient(clientId: string): Promise<ClientWithToken> {
  return apiClient<ClientWithToken>(clientUrl(clientId, '/rotate'), { method: 'POST' })
}

export function revokeClient(clientId: string): Promise<ClientView> {
  return apiClient<ClientView>(clientUrl(clientId, '/revoke'), { method: 'POST' })
}

export function getTokenRequirement(signal?: AbortSignal): Promise<TokenRequirement> {
  return apiClient<TokenRequirement>(REQUIREMENT_URL(), { signal })
}

export function setTokenRequirement(required: boolean): Promise<TokenRequirement> {
  return apiClient<TokenRequirement>(REQUIREMENT_URL(), { method: 'PUT', body: { required } })
}

/** The backend's coded message (`{"detail": {"code", "message"}}`), else fixed text. */
export function clientErrorMessage(error: unknown): string {
  return accountErrorMessage(error, 'Something went wrong. Try again.')
}
