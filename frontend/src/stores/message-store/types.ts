import type { HitlQuestionPresentation } from '@/lib/hitl/presentation'
import type { TaskState } from '@/lib/types/sse'
import type { HITLPromptType } from '@/lib/types/sse'
import type { AttachmentData } from '@/lib/types/attachments'

/** A single part within an A2A artifact. */
export interface ArtifactPart {
  kind: 'text' | 'file' | 'data'
  text?: string
  file?: {
    uri?: string
    fileId?: string
    mime_type?: string
    name?: string
    sizeBytes?: number
    sha256?: string
  }
  data?: Record<string, unknown> | unknown[]
}

/** An artifact emitted by an agent, stored alongside the message entity. */
export interface ArtifactData {
  artifactId: string
  name?: string
  parts: ArtifactPart[]
  isStreaming?: boolean
  /** Client-only marker for task/response parts lacking a canonical artifact ID. */
  isSynthetic?: boolean
}

/** Which pipeline last wrote this entity. */
export type MessageSource = 'db' | 'sse' | 'optimistic'

/**
 * Display type — resolved once at write time.
 * Determines which React component renders this message.
 */
export type DisplayType = 'user-bubble' | 'agent-bubble'

/**
 * The normalized entity stored in the message store.
 * Carries provenance metadata for conflict resolution.
 */
export interface MessageEntity {
  // ── Identity ──────────────────────────────────────────────
  id: string
  conversationId: string

  // ── Core content ──────────────────────────────────────────
  messageType: 'user' | 'agent'
  content: string
  senderName: string
  agentId?: string
  agentSource?: 'cloud' | 'local' | 'hub'
  userId?: string
  clientRequestId?: string

  // ── Task state (agent messages backed by A2A tasks) ───────
  taskStatus?: TaskState
  taskError?: string | null
  taskStatusMessage?: string | null
  taskRequiresInput?: boolean
  taskRequiresAuth?: boolean
  taskContent?: string
  dispatchText?: string
  taskCreatedAt?: string
  taskUpdatedAt?: string

  // ── Ordering ──────────────────────────────────────────────
  timestamp: string
  stepNumber?: number
  totalSteps?: number
  relatedMessageId?: string

  // ── HITL (Human-in-the-Loop) ─────────────────────────────
  hitlRequestId?: string
  /** Wire Agent message/card identity when this is a request-scoped HITL projection. */
  hitlMessageId?: string
  hitlSource?: 'agent' | 'supervisor'
  hitlPrompt?: string
  hitlPromptType?: HITLPromptType
  hitlChoices?: string[] | null
  hitlPresentation?: HitlQuestionPresentation
  hitlExpiresAt?: string
  hitlResolved?: boolean
  hitlInteractionId?: string
  hitlInteractionStatus?: string
  hitlInteractionVersion?: number
  hitlApplicationStatus?: string
  hitlGroupId?: string | null
  hitlGroupTotal?: number | null
  hitlGroupIndex?: number | null
  hitlUserAnswer?: string

  // ── Provenance & conflict resolution ──────────────────────
  source: MessageSource
  sourceVersion: number
  displayType: DisplayType
  isEphemeral: boolean
  createdAt: number
  updatedAt: number

  // ── Multimodal artifacts ──────────────────────────────────
  artifacts?: ArtifactData[]

  // ── User attachments ──────────────────────────────────────
  attachments?: AttachmentData[]

  // ── Quoted context (user messages only) ────────────────────
  quotedText?: string
  quotedSenderName?: string
  /** Persisted quote snapshot id (QUOTE_REPLY). */
  quoteId?: string

  // ── Turn terminal signal ──────────────────────────────────
  // Written by the processing_status SSE handler onto the user entity so
  // useMessageStoreSync can derive turn_completed/failed/canceled without
  // the turn store receiving direct writes (derived-only constraint).
  turnTerminalStatus?: 'completed' | 'failed' | 'canceled'

  /** Parsed from backend extend_info.summary_origin for summary-family agents. */
  summaryOrigin?: 'llm' | 'deterministic'
}

/**
 * Input shape for the write gateway.
 * Data sources build IncomingMessage and pass it to upsertMessage,
 * which fills in provenance fields.
 */
export interface IncomingMessage {
  id: string
  conversationId: string
  messageType: 'user' | 'agent'
  content: string
  senderName: string
  timestamp: string

  // All optional — omitted fields preserve existing values on update
  agentId?: string
  agentSource?: 'cloud' | 'local' | 'hub'
  userId?: string
  clientRequestId?: string
  taskStatus?: TaskState | null
  taskError?: string | null
  taskStatusMessage?: string | null
  taskRequiresInput?: boolean
  taskRequiresAuth?: boolean
  taskContent?: string
  dispatchText?: string
  taskCreatedAt?: string
  taskUpdatedAt?: string
  stepNumber?: number
  totalSteps?: number
  relatedMessageId?: string
  hitlRequestId?: string
  /** Wire Agent message/card identity retained separately from the store entity ID. */
  hitlMessageId?: string
  hitlSource?: 'agent' | 'supervisor'
  hitlPrompt?: string
  hitlPromptType?: HITLPromptType
  hitlChoices?: string[] | null
  hitlPresentation?: HitlQuestionPresentation
  hitlExpiresAt?: string
  hitlResolved?: boolean
  hitlInteractionId?: string
  hitlInteractionStatus?: string
  hitlInteractionVersion?: number
  hitlApplicationStatus?: string
  hitlGroupId?: string | null
  hitlGroupTotal?: number | null
  hitlGroupIndex?: number | null
  hitlUserAnswer?: string
  isEphemeral?: boolean
  artifacts?: ArtifactData[]
  attachments?: AttachmentData[]
  quotedText?: string
  quotedSenderName?: string
  quoteId?: string
  turnTerminalStatus?: 'completed' | 'failed' | 'canceled'
  summaryOrigin?: 'llm' | 'deterministic'
}
