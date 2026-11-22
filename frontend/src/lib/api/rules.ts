// The owner's check rules (backend `api_gateway/routes/rule_routes.py`).
// The list is ordered; the first rule that is on and matches decides.
import { getApiUrl } from '../utils'
import { ApiError, apiClient } from '../api-client'

/** What a check rule answers. `ask` holds the call for the owner's answer. */
export type RuleAnswer = 'allow' | 'deny' | 'ask'

export interface RuleView {
  rule_id: string
  kind: 'check'
  /** The one agent the rule matches; null for any agent. */
  agent_id: string | null
  answer: RuleAnswer
  /** A rule that is off never matches. */
  enabled: boolean
  created_at: string
  updated_at: string
}

export interface RuleList {
  rules: RuleView[]
}

/** The whole of a rule, as create and replace take it. */
export type RuleFields = Pick<RuleView, 'agent_id' | 'answer' | 'enabled'>

const ruleUrl = (ruleId: string) => getApiUrl(`rules/${encodeURIComponent(ruleId)}`)

export function listRules(signal?: AbortSignal): Promise<RuleList> {
  return apiClient<RuleList>(getApiUrl('rules'), { signal })
}

export function createRule(fields: RuleFields): Promise<RuleView> {
  return apiClient<RuleView>(getApiUrl('rules'), { method: 'POST', body: fields })
}

export function updateRule(ruleId: string, fields: RuleFields): Promise<RuleView> {
  return apiClient<RuleView>(ruleUrl(ruleId), { method: 'PUT', body: fields })
}

export function deleteRule(ruleId: string): Promise<void> {
  return apiClient<void>(ruleUrl(ruleId), { method: 'DELETE' })
}

/** Every rule's id, once each, in the new order. */
export function reorderRules(ruleIds: string[]): Promise<RuleList> {
  return apiClient<RuleList>(getApiUrl('rules/order'), { method: 'PUT', body: { rule_ids: ruleIds } })
}

// The names a 422 may carry (backend `_invalid_fields`); anything else is dropped.
const FIELD_NAMES = new Set(['agent_id', 'answer', 'enabled', 'rule_ids', 'unknown field', 'body'])

function refusal(error: unknown): { status: number; code?: string; fields: string[] } | null {
  if (!(error instanceof ApiError)) return null
  const details = error.details
  const detail = typeof details === 'object' && details !== null ? (details as { detail?: unknown }).detail : undefined
  if (typeof detail !== 'object' || detail === null) return { status: error.status, fields: [] }
  const { code, fields } = detail as { code?: unknown; fields?: unknown }
  return {
    status: error.status,
    code: typeof code === 'string' ? code : undefined,
    fields: Array.isArray(fields) ? fields.filter((name): name is string => typeof name === 'string' && FIELD_NAMES.has(name)) : [],
  }
}

/** Fixed text for a refused rule request; the backend's text is not shown. */
export function ruleErrorMessage(error: unknown): string {
  const refused = refusal(error)
  if (refused?.status === 404) return 'This rule no longer exists. The list is being reloaded.'
  if (refused?.status === 409 && refused.code === 'rule_limit_reached') return 'The list holds the most rules it can. Delete a rule to add another.'
  if (refused?.status === 409) return 'The rules changed since this page loaded. The list is being reloaded; try again once it is.'
  if (refused?.status === 422) {
    return refused.fields.length
      ? `The rule was not saved. Check: ${refused.fields.join(', ')}.`
      : 'The rule was not saved. Check the rule and try again.'
  }
  if (refused?.status === 503) return 'Rules are unavailable right now. Try again.'
  return 'Something went wrong. Try again.'
}
