import type { RoomSSEFrameMap } from '@/lib/types/sse'
import { isTerminalState } from '@/lib/types/sse'
import { useMessageStore } from '@/stores/message-store'
import { useStreamingStore } from '@/stores/streaming-store'
import type { ProcessingLifecycle } from '../../processing-lifecycle'
import { sseArtifactDataFromPayload } from '../artifacts'
import { resolveUserMessageId } from '../client-request'

export interface ArtifactUpdateContext {
  conversationId: string
  lifecycle: ProcessingLifecycle
}

export function handleArtifactUpdate(
  ctx: ArtifactUpdateContext,
  sseMessage: RoomSSEFrameMap['artifact_update'],
  clientReqId: string | null,
): void {
  if (sseMessage.data.last_chunk !== true) return

  const store = useMessageStore.getState()
  const streaming = useStreamingStore.getState()

  if (!ctx.lifecycle.isPlaceholderDismissed()) {
    store.removeMessage(ctx.lifecycle.placeholderId(ctx.conversationId))
    ctx.lifecycle.dismissPlaceholder()
  }

  if (!sseMessage.data.message_id || !sseMessage.data.artifact) return

  const { message_id, artifact, append: isAppend, last_chunk } = sseMessage.data

  const entity = store.entities[message_id]
  if (entity?.taskStatus && isTerminalState(entity.taskStatus)) {
    streaming.clear(message_id)
    return
  }
  const artifactData = sseArtifactDataFromPayload(
    artifact as Record<string, unknown>,
    isAppend,
    last_chunk,
  )
  const artifactId = artifactData.artifactId
  const hasExistingArtifact = (streaming.buffers[message_id]?.artifacts ?? [])
    .some(a => a.artifactId === artifactId)
  const resolvedAppend = isAppend ?? hasExistingArtifact

  streaming.append(message_id, ctx.conversationId, artifactData, resolvedAppend, {
    clientRequestId: clientReqId ?? undefined,
    userMessageId: resolveUserMessageId(ctx.conversationId, clientReqId),
  })
  if (last_chunk) streaming.markComplete(message_id)
}
