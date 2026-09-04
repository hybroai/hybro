import { banner } from '@/components/ui/banner'
import type { RoomSSEFrameMap, TaskState } from '@/lib/types/sse'
import { isTerminalState, TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { normalizeTimestampOrNow } from '@/lib/time'
import { patchedPublicAgentName } from '@/lib/agent-display-name'
import { appendEvent } from '@/lib/room-timeline/event-log'
import { partsToArtifacts } from '../artifacts'
import { applyRoomCommands } from '../apply-commands'
import { stampLiveTurnTerminalIfInferable } from '@/lib/room-timeline/stamp-live-turn-terminal'
import {
  buildTurnForRecoveryHint,
  scheduleTurnTerminalBackendTruthCheck,
  shouldScheduleTurnTerminalRecovery,
} from '@/lib/room-timeline/turn-terminal-stamp'
import type { SSEHandlerDeps } from '../types'

function safeTaskStatusMessage(
  status: TaskState,
  rawStatusMessage: string | null | undefined,
): string | null | undefined {
  if (rawStatusMessage === undefined) return undefined
  const text = rawStatusMessage?.trim()
  if (!text) return null
  if (isTerminalState(status)) return undefined
  return text
}

function maybeScheduleTurnTerminalRecovery(
  ctx: SSEHandlerDeps,
  hint: {
    clientRequestId?: string | null
    relatedMessageId?: string | null
  },
  taskStatus: TaskState,
): void {
  const turn = buildTurnForRecoveryHint(ctx.roomId, hint)
  if (!shouldScheduleTurnTerminalRecovery(turn, taskStatus)) return

  scheduleTurnTerminalBackendTruthCheck(
    ctx.roomId,
    ctx.lifecycle,
    hint,
    ctx.getToken,
  )
}

export async function handleTaskUpdate(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['task_update'],
  _clientReqId: string | null,
  canonical = false,
): Promise<void> {
  if (!sseMessage.data.message_id) return

  const messageId = sseMessage.data.message_id
  const status = sseMessage.data.status as TaskState
  let resolvedAgentName = sseMessage.data.agent_name
  if (!resolvedAgentName && sseMessage.data.agent_id) {
    resolvedAgentName = await ctx.getAgentName(sseMessage.data.agent_id)
  }
  const taskTimestamp = sseMessage.timestamp
  const content = sseMessage.data.content || ''

  const taskFields = {
    taskStatus: status,
    taskError: sseMessage.data.error !== undefined ? (sseMessage.data.error || null) : undefined,
    taskStatusMessage: safeTaskStatusMessage(status, sseMessage.data.status_message),
    taskRequiresInput: sseMessage.data.requires_input,
    taskRequiresAuth: sseMessage.data.requires_auth,
    stepNumber: sseMessage.data.step_number ?? undefined,
    totalSteps: sseMessage.data.total_steps ?? undefined,
    relatedMessageId: sseMessage.data.related_message_id ?? undefined,
    timestamp: normalizeTimestampOrNow(taskTimestamp),
    taskCreatedAt: normalizeTimestampOrNow(taskTimestamp),
    taskUpdatedAt: normalizeTimestampOrNow(taskTimestamp),
  }

  const store = useMessageStore.getState()
  const existing = store.entities[messageId]
  const senderName = patchedPublicAgentName(
    existing?.messageType === 'agent' ? existing.senderName : undefined,
    resolvedAgentName,
  ) ?? 'Unknown agent'

  const baseMsg = {
    id: messageId,
    roomId: ctx.roomId,
    messageType: 'agent' as const,
    senderName,
    agentId: sseMessage.data.agent_id ?? undefined,
    agentSource: ctx.getAgentSource(sseMessage.data.agent_id ?? undefined),
    clientRequestId: sseMessage.data.client_request_id,
    timestamp: existing?.timestamp ?? normalizeTimestampOrNow(taskTimestamp),
  }

  // INVARIANT: buffer read + stream_clear in same sync turn (see applyRoomCommands).
  const streamingBuffers = useStreamingStore.getState().buffers
  const bufferText = streamingBuffers[messageId]?.text
  const resolvedContent = (content ?? '').trim().length > 0
    ? content
    : (bufferText && bufferText.length > 0 ? bufferText : (existing?.content ?? ''))
  const artifacts = partsToArtifacts(
    sseMessage.data.parts as Record<string, unknown>[] | undefined,
    messageId,
    existing,
  )

  if (isTerminalState(status)) {
    const rootCancellationPending = useRoomUiStore
      .getState()
      .getRoomFlags(ctx.roomId)
      .cancelling
    applyRoomCommands([
      ...(!rootCancellationPending
        ? [{
          type: 'remove_message' as const,
          id: ctx.lifecycle.placeholderId(ctx.roomId),
        }]
        : []),
      {
        type: 'upsert_message',
        source: 'sse',
        message: {
          ...baseMsg,
          content: resolvedContent,
          isEphemeral: false,
          ...taskFields,
          ...(artifacts ? { artifacts } : {}),
        },
      },
      { type: 'stream_clear', messageId },
    ])
    if (!rootCancellationPending) {
      ctx.lifecycle.dismissPlaceholder()
    }

    if (!canonical) {
      if (status === TASK_STATE.COMPLETED) {
        appendEvent(ctx.roomId, {
          kind: 'agent_completed',
          timestamp: sseMessage.timestamp,
          agentId: sseMessage.data.agent_id ?? undefined,
          agentName: senderName,
          label: `${senderName} completed`,
        })
      } else if (
        status === TASK_STATE.FAILED ||
        status === TASK_STATE.REJECTED ||
        status === TASK_STATE.CANCELED
      ) {
        appendEvent(ctx.roomId, {
          kind: 'agent_failed',
          timestamp: sseMessage.timestamp,
          agentId: sseMessage.data.agent_id ?? undefined,
          agentName: senderName,
          label: `${senderName} failed`,
          body: sseMessage.data.error ?? undefined,
        })
      }

      if (!ctx.lifecycle.hasCancelTimedOut()) {
        if (status === TASK_STATE.FAILED) {
          banner.error(sseMessage.data.error || 'Task failed')
        } else if (status === TASK_STATE.REJECTED) {
          banner.error(sseMessage.data.error || 'Task was rejected')
        }
      }

      const stamped = stampLiveTurnTerminalIfInferable(ctx.roomId, ctx.lifecycle, {
        clientRequestId: sseMessage.data.client_request_id,
        relatedMessageId: sseMessage.data.related_message_id,
      })
      if (!stamped) {
        maybeScheduleTurnTerminalRecovery(ctx, {
          clientRequestId: sseMessage.data.client_request_id,
          relatedMessageId: sseMessage.data.related_message_id,
        }, status)
      }
    }
  } else {
    store.upsertMessage({
      ...baseMsg,
      content: resolvedContent,
      ...taskFields,
      ...(artifacts ? { artifacts } : {}),
    }, 'sse')
  }
}
