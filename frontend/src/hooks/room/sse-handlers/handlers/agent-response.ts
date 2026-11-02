import type { RoomSSEFrameMap } from '@/lib/types/sse'
import { isTerminalState, TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import type { ArtifactData } from '@/stores/message-store/types'
import { normalizeTimestampOrNow } from '@/lib/time'
import { isSummarySystemAgent } from '@/lib/system-agents'
import { isDeterministicDigestContent, type SummaryOrigin } from '@/lib/conversation/transcript'
import { partsToReplacementArtifacts } from '../artifacts'
import type { SSEHandlerDeps } from '../types'
import { resolveUserMessageId } from '../client-request'

const PARTIAL_STREAM_ARTIFACT_SUFFIX = '-partial-stream'

function partialStreamArtifactId(messageId: string): string {
  return `${messageId}${PARTIAL_STREAM_ARTIFACT_SUFFIX}`
}

function textPartialToArtifact(messageId: string, content: string): ArtifactData {
  return {
    artifactId: partialStreamArtifactId(messageId),
    name: 'response',
    parts: [{ kind: 'text', text: content }],
    isStreaming: true,
  }
}

function inferSummaryOriginFromAgentResponse(
  agentId: string | undefined,
  messageId: string,
  content: string,
): SummaryOrigin | undefined {
  if (!agentId || !isSummarySystemAgent(agentId)) return undefined
  if (!messageId.startsWith('summary-')) return undefined
  const text = content.trim()
  if (!text) return undefined
  if (isDeterministicDigestContent(text)) return 'deterministic'
  return 'llm'
}

function artifactsEqual(a: ArtifactData[] | undefined, b: ArtifactData[] | undefined): boolean {
  const left = a ?? []
  const right = b ?? []
  if (left.length !== right.length) return false
  return JSON.stringify(left) === JSON.stringify(right)
}

export function handleAgentResponsePartial(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['agent_response_partial'],
  clientReqId: string | null,
): void {
  const { message_id, content_delta } = sseMessage.data
  if (!message_id || typeof content_delta !== 'string') return

  const store = useMessageStore.getState()
  const streaming = useStreamingStore.getState()
  if (!store.entities[message_id]) {
    const agentId = sseMessage.data.agent_id
    const userMessageId = resolveUserMessageId(ctx.conversationId, clientReqId)
    store.upsertMessage({
      id: message_id,
      conversationId: ctx.conversationId,
      messageType: 'agent',
      content: '',
      senderName: agentId || 'Agent',
      agentId,
      agentSource: agentId ? ctx.getAgentSource?.(agentId) : undefined,
      clientRequestId: clientReqId ?? undefined,
      relatedMessageId: userMessageId ?? undefined,
      timestamp: normalizeTimestampOrNow(sseMessage.timestamp),
      taskStatus: TASK_STATE.WORKING,
      taskContent: '',
      isEphemeral: false,
    }, 'sse')
  }
  const hasPartialArtifact = (streaming.buffers[message_id]?.artifacts ?? [])
    .some(a => a.artifactId === partialStreamArtifactId(message_id))

  streaming.append(
    message_id,
    ctx.conversationId,
    textPartialToArtifact(message_id, content_delta),
    hasPartialArtifact,
    {
      clientRequestId: clientReqId ?? undefined,
      userMessageId: resolveUserMessageId(ctx.conversationId, clientReqId),
    },
  )
}

export async function handleAgentResponse(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['agent_response'],
): Promise<void> {
  if (!sseMessage.data.message_id) return

  const store = useMessageStore.getState()
  const streaming = useStreamingStore.getState()
  const messageId = sseMessage.data.message_id
  const { conversationId } = ctx
  const existing = store.entities[messageId]
  const incomingArtifacts = partsToReplacementArtifacts(
    sseMessage.data.parts as Record<string, unknown>[] | undefined,
    messageId,
    existing?.artifacts,
  )

  if (existing) {
    const incomingContent = (sseMessage.data.content ?? '').trim()
    const existingContent = (existing.content ?? '').trim()
    const hasExistingRenderable = existingContent.length > 0
      || (existing.artifacts?.length ?? 0) > 0
    const looksDuplicateContent = incomingContent.length > 0
      && incomingContent === existingContent
      && artifactsEqual(existing.artifacts, incomingArtifacts)
    if (hasExistingRenderable && looksDuplicateContent) {
      const canFinalizeExisting = !existing.taskStatus || !isTerminalState(existing.taskStatus)
      const needsStatusUpdate = existing.taskStatus !== TASK_STATE.COMPLETED
      if (canFinalizeExisting && needsStatusUpdate) {
        store.upsertMessage({
          id: messageId,
          conversationId,
          messageType: 'agent',
          content: existing.content,
          senderName: existing.senderName,
          agentId: existing.agentId,
          agentSource: existing.agentSource,
          clientRequestId: existing.clientRequestId || sseMessage.data.client_request_id,
          timestamp: existing.timestamp,
          taskStatus: TASK_STATE.COMPLETED,
          taskContent: '',
          taskUpdatedAt: normalizeTimestampOrNow(sseMessage.timestamp),
          isEphemeral: false,
          ...(existing.artifacts ? { artifacts: existing.artifacts } : {}),
        }, 'sse')
      }
      streaming.clear(messageId)
      return
    }
  }

  if (sseMessage.data.content === undefined && !sseMessage.data.parts) return

  const agentId = sseMessage.data.agent_id
  const agentName = agentId
    ? await ctx.getAgentName(agentId)
    : 'Agent'
  const content = sseMessage.data.content ?? ''
  const msgTimestamp = normalizeTimestampOrNow(sseMessage.timestamp)
  const entity = store.entities[messageId]
  const responseTaskStatus = entity?.taskStatus && isTerminalState(entity.taskStatus)
    ? entity.taskStatus
    : TASK_STATE.COMPLETED
  const summaryOrigin = inferSummaryOriginFromAgentResponse(agentId, messageId, content)
    ?? entity?.summaryOrigin

  store.upsertMessage({
    id: messageId,
    conversationId,
    messageType: 'agent',
    content,
    senderName: agentName,
    agentId,
    agentSource: agentId ? ctx.getAgentSource(agentId) : undefined,
    clientRequestId: entity?.clientRequestId || sseMessage.data.client_request_id,
    relatedMessageId: entity?.relatedMessageId ?? sseMessage.data.related_message_id ?? undefined,
    timestamp: msgTimestamp,
    taskStatus: responseTaskStatus,
    taskContent: '',
    taskUpdatedAt: msgTimestamp,
    isEphemeral: false,
    artifacts: incomingArtifacts,
    ...(summaryOrigin ? { summaryOrigin } : {}),
  }, 'sse')
  streaming.clear(messageId)
}
