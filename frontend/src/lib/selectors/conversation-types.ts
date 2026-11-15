import type { HitlQuestionPresentation } from '@/lib/hitl/presentation'
import type { MessageEntity, ArtifactData, ArtifactPart } from '@/stores/message-store/types'
import type { HITLPromptType } from '@/lib/types/sse'
import { AGENT_PALETTE, agentColorIndex } from '@/lib/mention-color'

// ── Agent theme ─────────────────────────────────────────────

export interface AgentTheme {
  name: string
  accent: string
  border: string
  avatarBg: string
  avatarLightBg: string
  cardBg: string
}

export const AGENT_THEMES: AgentTheme[] = AGENT_PALETTE.map(({ name }, i) => ({
  name,
  accent: `hsl(var(--agent-color-${i}))`,
  border: `hsl(var(--agent-color-${i}) / 0.18)`,
  avatarBg: `hsl(var(--agent-color-${i}) / 0.06)`,
  avatarLightBg: `hsl(var(--agent-color-${i}) / 0.12)`,
  cardBg: `hsl(var(--agent-color-${i}) / 0.08)`,
}))

export const UNRESOLVED_THEME: AgentTheme = {
  name: 'muted', accent: 'var(--conversation-text-muted)', border: 'var(--conversation-border)', avatarBg: '#27272a', avatarLightBg: 'rgba(113, 113, 122, 0.15)', cardBg: 'hsl(var(--color-card))',
}

export function getAgentTheme(agentId: string | undefined, agentName: string): AgentTheme {
  const key = agentId ?? agentName
  return AGENT_THEMES[agentColorIndex(key)]
}

// ── Agent display props ─────────────────────────────────────

export interface AgentDisplayProps {
  label: string
  tone: 'accent' | 'muted' | 'danger' | 'warning'
  isAnimated: boolean
  ariaLabel: string
}

// ── HITL ────────────────────────────────────────────────────

export type HitlLifecycleState =
  | 'open'
  | 'applying'
  | 'expired'
  | 'agent_timeout'
  | 'delivery_uncertain'
  | 'routing_failed'
  | 'canceled'

export interface PendingHitl {
  hitlId: string
  source: 'agent' | 'supervisor'
  agentName: string
  question: string
  promptType: HITLPromptType
  choices?: string[]
  presentation?: HitlQuestionPresentation
  messageId: string
  interactionId: string
  interactionVersion?: number
  interactionStatus?: string
  applicationStatus?: string
  lifecycleState: HitlLifecycleState
  errorMessage?: string
  expiresAt?: string
  clientRequestId?: string
  groupId?: string
  groupTotal?: number
  groupIndex?: number
  isAnswered: boolean
  answer?: string
}

export interface HitlState {
  hitlId: string
  resolved: boolean
  question: string
  answer: string | null
}

// ── Composer ────────────────────────────────────────────────

export interface ComposerState {
  mode: 'normal' | 'hitl_responding'
  isProcessing: boolean
  pendingHitls: PendingHitl[]
}

export interface AgentResponseDetail {
  messageId: string
  agentId?: string
  agentName: string
  display: AgentDisplayProps
  taskDescription: string
  theme: AgentTheme
  content: string
  /** Ordered A2A response parts. Undefined means the legacy flattened contract. */
  parts?: ArtifactPart[]
  isStreaming: boolean
  artifacts?: ArtifactData[]
  taskStatus?: MessageEntity['taskStatus']
  taskStatusMessage?: string | null
  taskError?: string | null
  requestMessage: MessageEntity | null
  agentSource?: 'cloud' | 'local' | 'hub'
}
