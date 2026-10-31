// Browser setup API (backend `api_gateway/routes/setup_routes.py`).
import { getApiUrl } from '../utils'
import { ApiError, apiDelete, apiGet, apiPost, apiPut } from '../api-client'

export type ProviderState = 'configured' | 'unconfigured' | 'credential_invalid'
export type ProviderId = 'openai' | 'deepseek' | 'anthropic'
export type AuthMethod = 'api_key' | 'oauth'
export type ImageSize = '1024x1024' | '1536x1024' | '1024x1536' | 'auto'
export type LoginState = 'pending' | 'authenticated' | 'failed' | 'expired'

export const IMAGE_SIZES: readonly ImageSize[] = ['1024x1024', '1536x1024', '1024x1536', 'auto']

export interface SetupStatus {
  state: ProviderState
  provider?: ProviderId | null
  auth?: AuthMethod | null
  text_model?: string | null
  image_model?: string | null
  image_size: ImageSize
  /** Safe code of the latest failure, such as `oauth_refresh_failed`. */
  last_error?: string | null
}

/** Eligible models; `text[0]` is the recommendation. */
export interface SetupModels {
  text: string[]
  image: string[]
}

export interface SetupSaved {
  changed: boolean
  status: SetupStatus
}

export interface OAuthLogin {
  login_id: string
  state: LoginState
  /** Present only while the login is pending. */
  authorization_url?: string | null
  /** False when the callback port could not be bound. */
  listener: boolean
  expires_at: number
  error?: string | null
}

export interface ImageSizeSaved {
  image_size: ImageSize
  restart_required: boolean
}

/** Error body of every setup route. */
export interface SetupErrorBody {
  detail: string
  code: string
  login_id?: string | null
}

const url = (path: string) => getApiUrl(`setup/${path}`)

export function getSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return apiGet<SetupStatus>(url('status'), undefined, signal)
}

export function getSetupModels(provider: ProviderId, auth: AuthMethod, signal?: AbortSignal): Promise<SetupModels> {
  const query = new URLSearchParams({ provider, auth })
  return apiGet<SetupModels>(`${url('models')}?${query}`, undefined, signal)
}

/** `api_key: null` reuses the saved key for this Provider to change models only. */
export function saveApiKey(body: { provider: ProviderId; api_key: string | null; text_model: string; image_model: string | null }): Promise<SetupSaved> {
  return apiPost<SetupSaved>(url('api-key'), body)
}

export function startOAuth(): Promise<OAuthLogin> {
  return apiPost<OAuthLogin>(url('oauth/start'))
}

export function getOAuthLogin(loginId: string): Promise<OAuthLogin> {
  return apiGet<OAuthLogin>(url(`oauth/${encodeURIComponent(loginId)}`))
}

export function completeOAuth(loginId: string, redirectUrl: string): Promise<OAuthLogin> {
  return apiPost<OAuthLogin>(url('oauth/complete'), { login_id: loginId, redirect_url: redirectUrl })
}

export function cancelOAuth(loginId: string): Promise<void> {
  return apiDelete<void>(url(`oauth/${encodeURIComponent(loginId)}`))
}

/** `login_id: null` reuses the saved sign-in to change models only. */
export function saveOAuthModels(body: { login_id: string | null; text_model: string; image_model: string | null }): Promise<SetupSaved> {
  return apiPost<SetupSaved>(url('oauth/models'), body)
}

export function saveImageSize(imageSize: ImageSize): Promise<ImageSizeSaved> {
  return apiPut<ImageSizeSaved>(url('image-size'), { image_size: imageSize })
}

/** The setup error body carried by a failed request, if it has one. */
export function setupError(error: unknown): SetupErrorBody | null {
  if (!(error instanceof ApiError)) return null
  const details = error.details
  if (typeof details !== 'object' || details === null) return null
  const { detail, code, login_id: loginId } = details as Record<string, unknown>
  if (typeof detail !== 'string' || typeof code !== 'string') return null
  return { detail, code, login_id: typeof loginId === 'string' ? loginId : null }
}

/** A message safe to show for a failed setup request. */
export function setupErrorMessage(error: unknown): string {
  const body = setupError(error)
  if (body) return body.detail
  return 'The request failed. Check that Hybro is running, then try again.'
}
