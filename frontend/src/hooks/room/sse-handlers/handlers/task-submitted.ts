import type { RoomSSEFrameMap, TaskState } from '@/lib/types/sse'
import { TASK_STATE } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import { normalizeTimestampOrNow } from '@/lib/time'
import { specificPublicAgentName } from '@/lib/agent-display-name'
import type { SSEHandlerDeps } from '../types'

export async function handleTaskSubmitted(
  ctx: SSEHandlerDeps,
  sseMessage: RoomSSEFrameMap['task_submitted'],
  _clientReqId: string | null,
): Promise<void> {
  if (!sseMessage.data.message_id) return

  const messageId = sseMessage.data.message_id
  let resolvedAgentName = sseMessage.data.agent_name
  if (!resolvedAgentName && sseMessage.data.agent_id) {
    resolvedAgentName = await ctx.getAgentName(sseMessage.data.agent_id)
  }
  const taskTimestamp = sseMessage.timestamp
  const senderName = specificPublicAgentName(resolvedAgentName) ?? 'Unknown agent'

  useMessageStore.getState().upsertMessage({
    id: messageId,
    conversationId: ctx.conversationId,
    messageType: 'agent',
    content: '',
    senderName,
    agentId: sseMessage.data.agent_id ?? undefined,
    agentSource: ctx.getAgentSource(sseMessage.data.agent_id ?? undefined),
    taskStatus: (sseMessage.data.status as TaskState) || TASK_STATE.WORKING,
    stepNumber: sseMessage.data.step_number ?? undefined,
    totalSteps: sseMessage.data.total_steps ?? undefined,
    relatedMessageId: sseMessage.data.related_message_id ?? undefined,
    clientRequestId: sseMessage.data.client_request_id,
    timestamp: normalizeTimestampOrNow(taskTimestamp),
    taskCreatedAt: normalizeTimestampOrNow(taskTimestamp),
  }, 'sse')
}
