import type { A2ATaskStatus } from '@/lib/a2a-task-projection'
import { extractTaskContent, extractTaskError } from '@/lib/a2a-task-projection'
import type { RoomMessage } from '@/lib/types/response'
import type { TaskState } from '@/lib/types/sse'
import { isInteractiveState, isTerminalState, TASK_STATE } from '@/lib/types/sse'
import { isSupervisorClarifyAgent } from '@/lib/system-agents'
import type { AttachmentData } from '@/lib/types/attachments'
import { normalizeTimestampOrNow } from '@/lib/time'
import { parseSummaryOrigin } from '@/lib/room-timeline/derive-final-answer'
import { deduplicateArtifactsByPart } from '@/lib/artifacts/artifact-identity'
import { specificPublicAgentName } from '@/lib/agent-display-name'
import type { ArtifactData, ArtifactPart, IncomingMessage } from './types'

function parseTurnCompletionKind(raw: unknown): 'synthesis' | 'deterministic' | undefined {
  if (raw === 'synthesis' || raw === 'deterministic') return raw
  return undefined
}

function publicTaskError(status: TaskState | undefined): string | undefined {
  switch (status as string | undefined) {
    case TASK_STATE.FAILED:
      return 'Task failed'
    case TASK_STATE.REJECTED:
      return 'Task was rejected by the agent'
    case TASK_STATE.CANCELED:
      return 'Task was canceled'
    case 'expired':
      return 'Task expired'
    default:
      return undefined
  }
}

function parseTurnTerminalStatus(raw: unknown): 'completed' | 'failed' | 'canceled' | undefined {
  if (raw === 'completed' || raw === 'failed' || raw === 'canceled') return raw
  if (raw === 'budget_exhausted') return 'failed'
  return undefined
}

/**
 * Parameters for converting API messages to IncomingMessage shape.
 */
export interface ConvertApiMessageOptions {
  userId?: string
  userName?: string
  getAgentName: (agentId: string) => Promise<string>
  getAgentSource?: (agentId: string) => 'cloud' | 'local' | 'hub' | undefined
}

/**
 * Convert a RoomMessage (DB API format) to an IncomingMessage (normalized store format).
 *
 * Extracts content, task state, sender info, and timestamps from the API response.
 * Display-type resolution happens downstream in the store's upsert path via
 * resolveDisplayType — this function does not perform any type-conversion logic.
 */
export async function convertApiMessageToIncoming(
  apiMessage: RoomMessage,
  options: ConvertApiMessageOptions,
): Promise<IncomingMessage> {
  const { userId, userName, getAgentName, getAgentSource } = options
  const extendInfo = apiMessage.extend_info as Record<string, unknown> | null | undefined

  // ── Extract task status (before content, so we can gate error fallback) ──
  const messageTask = apiMessage.message_content?.message_task
  let taskStatus: TaskState | undefined
  if (messageTask) {
    const maybeStatus = (messageTask as A2ATaskStatus['task']).status?.state
    if (typeof maybeStatus === 'string') {
      taskStatus = maybeStatus as TaskState
    }
  }

  // ── Extract content ──────────────────────────────────────────
  let content = ''
  let taskError: string | undefined
  let taskContent: string | undefined
  let taskStatusMessage: string | undefined

  if (
    apiMessage.message_content?.message_text
    && (
      apiMessage.message_type !== 'agent'
      || !messageTask
      || taskStatus === TASK_STATE.COMPLETED
    )
  ) {
    content = apiMessage.message_content.message_text
  }

  if (messageTask) {
    const messageTaskTyped = messageTask as A2ATaskStatus['task']
    const safeError = publicTaskError(taskStatus)
    const extractedError = taskStatus
      && taskStatus !== TASK_STATE.COMPLETED
      && isTerminalState(taskStatus)
      ? extractTaskError(messageTaskTyped)
      : undefined
    taskError = safeError ?? extractedError
    if (!content) {
      const extractedContent = taskStatus === TASK_STATE.COMPLETED
        ? extractTaskContent(messageTaskTyped)
        : undefined
      if (extractedContent) {
        content = extractedContent
      } else if (taskError && (!taskStatus || isTerminalState(taskStatus))) {
        content = taskError
      }
    }
  }

  // Only backend-labeled public text may become a visible task description.
  const publicTaskLabel = extendInfo?.public_task_label
  if (typeof publicTaskLabel === 'string' && publicTaskLabel.trim()) {
    const publicLabel = publicTaskLabel.trim()
    taskContent = publicLabel
    taskStatusMessage = publicLabel
  }
  const publicDispatchText = extendInfo?.public_dispatch_text
  const dispatchText = typeof publicDispatchText === 'string' && publicDispatchText.trim()
    ? publicDispatchText.trim()
    : undefined

  // ── Resolve sender name ──────────────────────────────────────
  let senderName: string
  let agentId: string | undefined

  if (apiMessage.message_type === 'user') {
    senderName = userName ?? userId ?? 'User'
  } else if (apiMessage.message_type === 'agent') {
    if (apiMessage.agent_id) {
      agentId = apiMessage.agent_id
    } else if (apiMessage.message_content?.message_task?.metadata?.agent_id) {
      agentId = apiMessage.message_content.message_task.metadata.agent_id as string
    }

    const publicAgentName = specificPublicAgentName(extendInfo?.public_agent_name)
    if (publicAgentName) {
      senderName = publicAgentName
    } else if (agentId) {
      try {
        senderName = specificPublicAgentName(await getAgentName(agentId)) ?? 'Unknown agent'
      } catch {
        senderName = 'Unknown agent'
      }
    } else {
      senderName = 'Unknown agent'
    }
  } else {
    senderName = 'Unknown'
  }

  // ── Extract persisted HITL user answer ───────────────────
  let hitlUserAnswer: string | undefined

  // ── Extract persisted HITL request metadata ─────────────
  let hitlRequestId: string | undefined
  let hitlPrompt: string | undefined
  let hitlPromptType: 'text' | 'choice' | 'confirmation' | undefined
  let hitlChoices: string[] | null | undefined

  // ── Extract persisted HITL group metadata ───────────────
  let hitlGroupId: string | undefined
  let hitlGroupTotal: number | undefined
  let hitlGroupIndex: number | undefined
  const meta = messageTask?.metadata
  const trustedHitlRequestId = extendInfo?.hitl_request_id
  const hasTrustedHitlMetadata = typeof trustedHitlRequestId === 'string'
    && trustedHitlRequestId.length > 0
    && meta?.hitl_request_id === trustedHitlRequestId

  if (meta && hasTrustedHitlMetadata) {
    const rid = meta.hitl_request_id ?? meta.request_id
    if (typeof rid === 'string') hitlRequestId = rid
    const hp = meta.hitl_prompt ?? meta.prompt
    if (typeof hp === 'string') hitlPrompt = hp
    const hpt = meta.hitl_prompt_type ?? meta.prompt_type
    if (typeof hpt === 'string') hitlPromptType = hpt as 'text' | 'choice' | 'confirmation'
    if (Array.isArray(meta.hitl_choices)) hitlChoices = meta.hitl_choices as string[]
    else if (Array.isArray(meta.choices)) hitlChoices = meta.choices as string[]
    const maybeUserAnswer = meta.user_answer
    if (typeof maybeUserAnswer === 'string') hitlUserAnswer = maybeUserAnswer
    if (typeof meta.hitl_interaction_id === 'string') hitlGroupId = meta.hitl_interaction_id
    if (typeof meta.hitl_question_count === 'number') hitlGroupTotal = meta.hitl_question_count
    if (typeof meta.hitl_question_index === 'number') hitlGroupIndex = meta.hitl_question_index
  }

  // ── Extract user attachments ────────────────────────────
  let attachments: AttachmentData[] | undefined
  const rawAttachments = apiMessage.message_content?.attachments
  if (Array.isArray(rawAttachments) && rawAttachments.length > 0) {
    attachments = rawAttachments
      .filter((att: Record<string, unknown>) => typeof att.file_id === 'string' && typeof att.mime_type === 'string')
      .map((att: Record<string, unknown>) => ({
        fileId: att.file_id as string,
        fileUrl: (att.file_url as string) || undefined,
        mimeType: att.mime_type as string,
        fileName: (att.file_name as string) || 'unknown',
        sizeBytes: (att.size_bytes as number) || 0,
        sha256: att.sha256 as string | undefined,
      }))
    if (attachments.length === 0) attachments = undefined
  }

  // ── Extract multimodal artifacts from task ───────────────
  let artifacts: ArtifactData[] | undefined
  const outputFailureCode = (messageTask?.metadata as Record<string, unknown> | undefined)
    ?.output_failure_code
  const exposesArtifacts = taskStatus === TASK_STATE.COMPLETED
    || (
      taskStatus === TASK_STATE.FAILED
      && outputFailureCode === 'artifact_delivery_failed'
    )
  const rawArtifacts = exposesArtifacts
    ? messageTask?.artifacts as Record<string, unknown>[] | undefined
    : undefined
  if (Array.isArray(rawArtifacts) && rawArtifacts.length > 0) {
    const mapped = rawArtifacts
      .map((a) => {
        const rawParts = a.parts as Record<string, unknown>[] | undefined
        if (!Array.isArray(rawParts) || rawParts.length === 0) return null

        const parts = rawParts
          .map((p) => {
            const root = (p.root ?? p) as Record<string, unknown>
            const kind = (root.kind as string) || 'text'
            if (kind === 'text') return null
            const fileData = root.file as Record<string, unknown> | undefined
            const fileMetadata = root.metadata as Record<string, unknown> | undefined
            const safeFile = fileData || typeof fileMetadata?.file_id === 'string' ? {
              uri: fileData?.uri as string | undefined,
              fileId: fileMetadata?.file_id as string | undefined,
              mime_type: (
                fileMetadata?.mime_type
                || fileData?.mime_type
                || fileData?.mimeType
              ) as string | undefined,
              name: (fileMetadata?.file_name || fileData?.name) as string | undefined,
              sizeBytes: fileMetadata?.size_bytes as number | undefined,
              sha256: fileMetadata?.sha256 as string | undefined,
            } : undefined
            return {
              kind: kind as ArtifactPart['kind'],
              text: root.text as string | undefined,
              file: safeFile,
              data: root.data as Record<string, unknown> | undefined,
            }
          })
          .filter((p): p is NonNullable<typeof p> => p !== null) as ArtifactPart[]

        if (parts.length === 0) return null
        return {
          artifactId: (a.artifactId || a.artifact_id || `db-${apiMessage.message_id}`) as string,
          name: a.name as string | undefined,
          parts,
        }
      })
      .filter((a): a is NonNullable<typeof a> => a !== null) as ArtifactData[]
    artifacts = deduplicateArtifactsByPart(mapped)
  }

  // Answered HITL from DB: supervisor clarify is done; real agents may still be working.
  let hitlResolved: boolean | undefined
  if (hitlUserAnswer !== undefined) {
    hitlResolved = true
    if (
      apiMessage.message_type === 'agent'
      && taskStatus
      && isInteractiveState(taskStatus)
      && isSupervisorClarifyAgent(agentId)
    ) {
      taskStatus = TASK_STATE.COMPLETED
    }
  }

  // ── Build IncomingMessage ────────────────────────────────────
  const summaryOrigin = parseSummaryOrigin(extendInfo?.summary_origin)
  const turnCompletionKind = parseTurnCompletionKind(extendInfo?.turn_completion_kind)
  const turnTerminalStatus = apiMessage.message_type === 'user'
    ? parseTurnTerminalStatus(extendInfo?.orchestration_status)
    : undefined
  const quotedText = typeof extendInfo?.quoted_text === 'string' ? extendInfo.quoted_text : undefined
  const quotedSenderName = typeof extendInfo?.quoted_sender_name === 'string' ? extendInfo.quoted_sender_name : undefined
  const extQuoteId = typeof extendInfo?.quote_id === 'string' ? extendInfo.quote_id : undefined
  const topQuoteId = typeof (apiMessage as { quote_id?: unknown }).quote_id === 'string'
    ? (apiMessage as { quote_id: string }).quote_id
    : undefined
  const quoteId = topQuoteId || extQuoteId

  return {
    id: apiMessage.message_id,
    clientRequestId: apiMessage.client_request_id ?? undefined,
    roomId: apiMessage.room_id,
    messageType: apiMessage.message_type as 'user' | 'agent',
    content,
    senderName,
    timestamp: normalizeTimestampOrNow(apiMessage.message_created_at),

    agentId: apiMessage.message_type === 'agent' ? (agentId || undefined) : undefined,
    agentSource: apiMessage.message_type === 'agent' && agentId && getAgentSource
      ? getAgentSource(agentId)
      : undefined,
    userId: apiMessage.message_type === 'user' ? userId : undefined,

    taskStatus,
    taskError: messageTask ? (taskError || null) : undefined,
    taskStatusMessage,
    taskContent,
    dispatchText,

    stepNumber: apiMessage.step_number ?? undefined,
    totalSteps: apiMessage.total_steps ?? undefined,
    relatedMessageId: apiMessage.related_message_id ?? undefined,

    taskUpdatedAt: apiMessage.task_updated_at
      ? normalizeTimestampOrNow(apiMessage.task_updated_at)
      : undefined,
    taskCreatedAt: apiMessage.message_created_at
      ? normalizeTimestampOrNow(apiMessage.message_created_at)
      : undefined,

    hitlRequestId,
    hitlPrompt,
    hitlPromptType,
    hitlChoices,
    hitlResolved,
    hitlUserAnswer,
    hitlGroupId,
    hitlGroupTotal,
    hitlGroupIndex,
    attachments,
    artifacts,
    summaryOrigin,
    turnTerminalStatus,
    turnCompletionKind,
    quotedText,
    quotedSenderName,
    quoteId,
  }
}
