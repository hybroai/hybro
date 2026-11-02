import { useMessageStore } from '@/stores/message-store'
import type {
  MessageEntity,
  MessageSource,
  ProcessingStatusLogEntry,
  TurnPhaseLog,
} from '@/stores/message-store/types'
import type { ProcessingLifecycle } from './processing-lifecycle'

export const INITIAL_PROCESSING_STATUS_MESSAGE = 'Thinking...'

export function createInitialProcessingStatusLog(timestamp: string): ProcessingStatusLogEntry {
  return {
    id: `processing-log-${timestamp}-0`,
    message: INITIAL_PROCESSING_STATUS_MESSAGE,
    timestamp,
  }
}

/** Real work-log entries, without the optimistic 'Thinking...' placeholder. */
export function progressStatusLogs(entries: ProcessingStatusLogEntry[]): ProcessingStatusLogEntry[] {
  return entries.filter((entry) => entry.message !== INITIAL_PROCESSING_STATUS_MESSAGE)
}

export function ensureInitialProcessingStatusLog(
  conversationId: string,
  userEntity: MessageEntity | undefined,
  timestamp = new Date().toISOString(),
): void {
  if (!userEntity) return

  appendProcessingStatusLog(conversationId, userEntity, INITIAL_PROCESSING_STATUS_MESSAGE, timestamp)
}

export function findProcessingStatusUserEntity(
  conversationId: string,
  options: {
    messageId?: string | null
    clientRequestId?: string | null
    relatedMessageId?: string | null
    beforeTimestamp?: string | null
    latestWithLogs?: boolean
    preferClientRequestId?: boolean
  },
): MessageEntity | undefined {
  const store = useMessageStore.getState()
  const findByClientRequestId = () => {
    if (!options.clientRequestId) return undefined
    return store.orderedIds
      .map((id) => store.entities[id])
      .find((entity) =>
        entity?.conversationId === conversationId &&
        entity.messageType === 'user' &&
        entity.clientRequestId === options.clientRequestId
      )
  }

  if (options.preferClientRequestId) {
    const correlated = findByClientRequestId()
    if (correlated) return correlated
  }

  if (options.messageId) {
    const direct = store.entities[options.messageId]
    if (direct?.conversationId === conversationId && direct.messageType === 'user') return direct
  }

  if (options.relatedMessageId) {
    const related = store.entities[options.relatedMessageId]
    if (related?.conversationId === conversationId && related.messageType === 'user') return related
  }

  if (!options.preferClientRequestId) {
    const correlated = findByClientRequestId()
    if (correlated) return correlated
  }

  if (options.beforeTimestamp) {
    const beforeTime = new Date(options.beforeTimestamp).getTime()
    const candidates = store.orderedIds
      .map((id) => store.entities[id])
      .filter((entity): entity is MessageEntity =>
        !!entity &&
        entity.conversationId === conversationId &&
        entity.messageType === 'user' &&
        new Date(entity.timestamp).getTime() <= beforeTime
      )
    const latest = candidates.at(-1)
    if (latest) return latest
  }

  if (options.latestWithLogs) {
    return store.orderedIds
      .map((id) => store.entities[id])
      .filter((entity): entity is MessageEntity =>
        !!entity &&
        entity.conversationId === conversationId &&
        entity.messageType === 'user' &&
        (entity.processingStatusLogs?.length ?? 0) > 0
      )
      .at(-1)
  }

  return undefined
}

export function normalizeProcessingDetails(details: unknown): string | undefined {
  if (typeof details === 'string') return details.trim() || undefined
  if (!details || typeof details !== 'object') return undefined

  const message = (details as { message?: unknown }).message
  return typeof message === 'string' ? message.trim() || undefined : undefined
}

export function processingDetailsToLogMessage(
  details: Record<string, unknown> | null | undefined,
): string | undefined {
  if (!details) return undefined
  const message = details.message ?? details.status_message ?? details.stage ?? details.description
  if (typeof message === 'string' && message.trim()) return message.trim()
  const json = JSON.stringify(details)
  return json === '{}' ? undefined : json
}

export function parseTurnPhaseFromDetails(
  details: unknown,
): TurnPhaseLog | undefined {
  if (!details || typeof details !== 'object') return undefined
  const phase = (details as { turn_phase?: unknown }).turn_phase
  if (phase === 'collecting' || phase === 'synthesizing' || phase === 'terminal') {
    return phase
  }
  return undefined
}

export function appendProcessingStatusLog(
  conversationId: string,
  userEntity: MessageEntity | undefined,
  message: unknown,
  timestamp = new Date().toISOString(),
  source: MessageSource = 'optimistic',
  options?: { turnPhase?: ProcessingStatusLogEntry['turnPhase'] },
): void {
  const trimmed = normalizeProcessingDetails(message)
  if (!trimmed || !userEntity) return

  const latestUserEntity = useMessageStore.getState().entities[userEntity.id] ?? userEntity
  const existing = latestUserEntity.processingStatusLogs ?? []
  if (existing.some((entry) => entry.message === trimmed)) return

  // The optimistic 'Thinking...' placeholder is replaced by the first real
  // work-log entry so the UI never shows both a live log and a stale
  // placeholder for the same turn.
  const entries = existing.length === 1
    && existing[0].message === INITIAL_PROCESSING_STATUS_MESSAGE
    ? []
    : existing

  useMessageStore.getState().upsertMessage({
    id: latestUserEntity.id,
    conversationId,
    messageType: 'user',
    content: latestUserEntity.content,
    senderName: latestUserEntity.senderName,
    timestamp: latestUserEntity.timestamp,
    processingStatusLogs: [
      ...entries,
      {
        id: `processing-log-${timestamp}-${entries.length}`,
        message: trimmed,
        timestamp,
        ...(options?.turnPhase ? { turnPhase: options.turnPhase } : {}),
      },
    ],
  }, source)
}

export function clearProcessingStatusLogs(
  conversationId: string,
  userEntity: MessageEntity | undefined,
): void {
  if (!userEntity || (userEntity.processingStatusLogs?.length ?? 0) === 0) return

  useMessageStore.getState().upsertMessage({
    id: userEntity.id,
    conversationId,
    messageType: 'user',
    content: userEntity.content,
    senderName: userEntity.senderName,
    timestamp: userEntity.timestamp,
    processingStatusLogs: [],
  }, 'optimistic')
}

export function clearCurrentProcessingStatusLogs(
  conversationId: string,
  lifecycle: ProcessingLifecycle,
): void {
  const userEntity = findProcessingStatusUserEntity(conversationId, {
    messageId: lifecycle.getMessageId(),
    clientRequestId: lifecycle.getPendingRunEventAck(),
    latestWithLogs: true,
  })
  clearProcessingStatusLogs(conversationId, userEntity)
}
