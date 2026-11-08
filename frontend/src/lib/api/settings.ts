// Instance settings API (backend `api_gateway/routes/settings_routes.py`), admin only.
import { getApiUrl } from '../utils'
import { ApiError, apiGet, apiPost, apiPut } from '../api-client'

export type FieldValue = boolean | number | string | null
export type FieldKind = 'boolean' | 'integer' | 'number' | 'url' | 'enum'
export type RestartState = 'idle' | 'requested' | 'restarting' | 'completed' | 'failed'

export interface SettingsField {
  /** `backend.<name>` or `frontend.<name>`. */
  key: string
  group: string
  level: 'basic' | 'advanced'
  apply: 'live' | 'restart'
  label: string
  description: string
  kind: FieldKind
  options: string[] | null
  nullable: boolean
  minimum: number | null
  maximum: number | null
  exclusive_minimum: number | null
  default: FieldValue
  value: FieldValue
  /** `config` when config.json sets the field. */
  source: 'config' | 'default'
  /** Saved, but the running services still use the old value. */
  restart_pending: boolean
}

export interface RestartStatus {
  /** False outside the image, where Hybro cannot restart itself. */
  available: boolean
  state: RestartState
  error?: string | null
  updated_at?: number | null
}

export interface SettingsNotice {
  code: 'removed_fields' | 'instances_ignored' | 'unlisted_fields' | 'capped_fields'
  fields: string[]
  message: string
}

export interface SystemInfo {
  version: string
  deployment: 'image' | 'checkout'
  port: number | null
  listen: 'loopback' | 'public' | null
  auth_mode: 'mock' | 'clerk'
  mcp_available: boolean | null
  config_location: string
}

export interface SettingsView {
  /** Token for the next save; null when config.json cannot be read. */
  revision: string | null
  config_error: string | null
  restart_pending: boolean
  restart: RestartStatus
  groups: { id: string; label: string }[]
  fields: SettingsField[]
  notices: SettingsNotice[]
  system: SystemInfo
}

export interface SettingsSaved {
  changed: string[]
  restart_required: boolean
  settings: SettingsView
}

export interface SettingsFieldError {
  field: string | null
  code: string
  message: string
}

/** A failed settings request: its HTTP status and the backend's safe error body. */
export interface SettingsFailure {
  status: number
  detail: string
  code: string
  errors: SettingsFieldError[]
}

const url = (path = '') => getApiUrl(path ? `settings/${path}` : 'settings')

export function getSettings(signal?: AbortSignal): Promise<SettingsView> {
  return apiGet<SettingsView>(url(), undefined, signal)
}

/** Saves `values` (`null` returns a field to its default) if config.json is still at `revision`. */
export function saveSettings(revision: string, values: Record<string, FieldValue>): Promise<SettingsSaved> {
  return apiPut<SettingsSaved>(url(), { revision, values })
}

/** Restarts the services in place; the JSON body is required by the backend. */
export function restartServices(): Promise<{ restart: RestartStatus }> {
  return apiPost<{ restart: RestartStatus }>(url('restart'), {})
}

export function settingsError(error: unknown): SettingsFailure | null {
  if (!(error instanceof ApiError)) return null
  const details = error.details
  if (typeof details !== 'object' || details === null) return null
  const { detail, code, errors } = details as Record<string, unknown>
  if (typeof detail !== 'string' || typeof code !== 'string') return null
  const fieldErrors = Array.isArray(errors)
    ? errors.filter((item): item is SettingsFieldError => (
      typeof item === 'object' && item !== null && typeof item.message === 'string' && typeof item.code === 'string'
    ))
    : []
  return { status: error.status, detail, code, errors: fieldErrors }
}

/** A message safe to show for a failed settings request. */
export function settingsErrorMessage(error: unknown): string {
  return settingsError(error)?.detail ?? 'The request failed. Check that Hybro is running, then try again.'
}
