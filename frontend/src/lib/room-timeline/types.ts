import type { ArtifactData } from '@/stores/message-store/types'
import type { AttachmentData } from '@/lib/types/attachments'
import type { SummaryOrigin } from '@/lib/conversation/transcript'

// ── Turn-level status ──────────────────────────────────────────

export type TurnStatus =
  | 'active'
  | 'awaiting_input'
  | 'completed'
  | 'failed'
  | 'partial'

export type TurnDisplayMode =
  | 'single_agent'
  | 'summary_with_sources'
  | 'parallel_results'
  | 'awaiting_input'
  | 'working'

export type TurnPhase =
  | 'collecting'
  | 'synthesizing'
  | 'answering'
  | 'completed'

// ── Turn view model ────────────────────────────────────────────

export interface TurnViewModel {
  id: string
  conversationId: string
  userMessageId: string | null
  userContent: string
  userAttachments: AttachmentData[]
  timestamp: string
  status: TurnStatus
  agentResults: AgentResultViewModel[]
  activeAgentIds: string[]
  /** Whether this turn was dispatched via Supervisor orchestration.
   *  Derived from presence of system:clarifier / system:hybro entities. */
  isSupervisorTurn: boolean
  /** Resolved display mode (legacy; derived from finalAnswer for incremental rebuild). */
  displayMode: TurnDisplayMode
  /** Layout phase within a live turn. */
  phase?: TurnPhase
  /** Room-level terminal signal from user entity (processing_status SSE). */
  turnTerminalStatus?: 'completed' | 'failed' | 'canceled'
  /** V3: unified final-answer slot (§17). */
  finalAnswer: FinalAnswerViewModel
}

// ── V3 final answer ──────────────────────────────────────────

export type FinalAnswerKind =
  | 'pending'
  | 'hitl'
  | 'llm_synthesis'
  | 'deterministic_done'
  | 'canceled'
  | 'failed'
  | 'single'

export type FinalAnswerLabel =
  | 'Synthesized'
  | 'Synthesizing'
  | 'Combined agent responses'
  | 'Working'
  | 'Needs input'
  | 'Canceled'
  | 'Failed'

export interface FinalAnswerSection {
  messageId: string
  agentId?: string
  agentName: string
  content: string
  artifacts: ArtifactData[]
  status: 'working' | 'completed' | 'failed' | 'awaiting_input'
}

export interface FinalAnswerHitlPrompt {
  messageId: string
  agentName: string
  prompt: string
  resolved?: { prompt: string; answer: string }
}

export interface FinalAnswerHitlViewModel {
  source: 'supervisor' | 'agent'
  prompts: FinalAnswerHitlPrompt[]
}

export interface FinalAnswerViewModel {
  kind: FinalAnswerKind
  label: FinalAnswerLabel
  primaryMessageId?: string
  /** Short HYBRO presenter copy for deterministic_done (virtual or backend entity body). */
  deterministicIntro?: string
  /** HYBRO presenter copy when the turn was canceled. */
  canceledIntro?: string
  /** HYBRO presenter copy when the turn failed (all agents failed / room failed). */
  failedIntro?: string
  summaryOrigin?: SummaryOrigin
  sections?: FinalAnswerSection[]
  hitl?: FinalAnswerHitlViewModel
}

// ── Agent result ───────────────────────────────────────────────

export interface AgentResultViewModel {
  agentId?: string
  agentName: string
  agentSource?: 'cloud' | 'local' | 'hub'
  messageId: string
  clientRequestId?: string
  status: 'completed' | 'failed' | 'canceled' | 'awaiting_input' | 'working'
  /** Canonical Tool lifecycle status shared verbatim by Trace and Agent Card. */
  executionStatus?: 'running' | 'awaiting_input' | 'completed' | 'failed' | 'canceled'
  content: string
  artifacts: ArtifactData[]
  /** Task-level status hint (for terminal/interactive states). */
  taskStatusMessage?: string | null
  /** The failing Agent's own reason, shown on its card. */
  failureDetail?: string
  hitlHistory?: { prompt: string; answer: string }[]
  /** Whether this agent is a summary-family system agent. */
  isSummaryAgent: boolean
  /** Resolved HITL: prompt and user answer. */
  hitlResolved?: { prompt: string; answer: string }
  /** Active (unanswered) HITL prompt. */
  hitlPending?: { prompt: string; source: 'agent' | 'supervisor' }
  /** Event count for inline chips. */
  eventCount?: number
  /** Duration in ms for inline chips. */
  durationMs?: number
  /** True when this result came from an ephemeral placeholder entity. */
  isEphemeral?: boolean
  /** When isSummaryAgent — distinguishes LLM synthesis from deterministic DONE intro. */
  summaryOrigin?: SummaryOrigin
}
