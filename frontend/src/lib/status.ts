import type { McpStatus } from '@/lib/mcp'
import type { RoomHistoryStatus } from '@/lib/api/room'
import type { SessionState } from '@/lib/api/session'
import type { NetworkAgent } from '@/components/networks/types'
import type { AgentInactiveReason } from '@/lib/types/agent'

export type StatusTone = 'online' | 'offline' | 'unverified' | 'warning' | 'error'
export type StatusDot = 'filled' | 'hollow' | 'spinner'

export interface StatusView {
  label: string
  tone: StatusTone
  dot: StatusDot
  colorLabel: boolean
}

const view = (label: string, tone: StatusTone, dot: StatusDot, colorLabel = false): StatusView =>
  ({ label, tone, dot, colorLabel })

const INACTIVE_REASONS: Record<AgentInactiveReason, { label: string; text: string }> = {
  provider_not_configured: {
    label: 'Needs model provider',
    text: 'Needs a model provider. Set one up in Settings, Model provider.',
  },
  image_model_not_configured: {
    label: 'Needs image model',
    text: 'Needs an image model. Choose one in Settings, Model provider.',
  },
}

/** Why an agent waits for setup, in words; null when it does not. */
export function inactiveReasonText(reason: AgentInactiveReason | null | undefined): string | null {
  return reason ? INACTIVE_REASONS[reason].text : null
}

export function agentStatusView(status: NetworkAgent['status'], inactiveReason?: AgentInactiveReason | null): StatusView {
  if (status === 'inactive' && inactiveReason) return view(INACTIVE_REASONS[inactiveReason].label, 'warning', 'hollow')
  switch (status) {
    case 'active': return view('Online', 'online', 'filled')
    case 'inactive':
    case 'unavailable': return view('Offline', 'offline', 'hollow')
    case 'deleted': return view('Removed', 'error', 'hollow')
    default: return view('Unverified', 'unverified', 'hollow')
  }
}

export function agentStatusRank(status: NetworkAgent['status']): number {
  return { active: 0, unknown: 1, inactive: 2, unavailable: 2, deleted: 3 }[status]
}

export function gatewayStatusView(status: McpStatus | 'checking' | 'error'): StatusView {
  switch (status) {
    case 'ready': return view('Ready', 'online', 'filled')
    case 'checking': return view('Checking', 'offline', 'spinner')
    case 'error': return view("Can't check", 'error', 'filled')
    default: return view('Unavailable', 'offline', 'hollow')
  }
}

export function roomStatusView(status: RoomHistoryStatus): StatusView | null {
  switch (status) {
    case 'queued': return view('Queued', 'offline', 'filled')
    case 'processing': return view('Running', 'offline', 'spinner')
    case 'awaiting_input': return view('Needs input', 'warning', 'filled', true)
    default: return null
  }
}

export function sessionStateView(state: SessionState): StatusView {
  switch (state) {
    case 'waiting_on_owner': return view('Needs you', 'warning', 'filled', true)
    case 'working': return view('Working', 'offline', 'spinner')
    case 'completed': return view('Completed', 'online', 'filled')
    case 'rejected': return view('Rejected', 'offline', 'hollow')
    case 'failed': return view('Failed', 'error', 'filled')
    case 'closed': return view('Closed', 'offline', 'hollow')
  }
}

export const LOAD_ERROR_STATUS: StatusView = view("Couldn't load", 'error', 'filled')
