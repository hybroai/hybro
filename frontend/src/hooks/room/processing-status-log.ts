import { useMessageStore } from '@/stores/message-store'
import type { MessageEntity } from '@/stores/message-store/types'

export function findProcessingStatusUserEntity(
  conversationId: string,
  options: {
    messageId?: string | null
    clientRequestId?: string | null
    relatedMessageId?: string | null
    beforeTimestamp?: string | null
    latestUser?: boolean
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

  if (options.latestUser) {
    return store.orderedIds
      .map((id) => store.entities[id])
      .filter((entity): entity is MessageEntity =>
        !!entity &&
        entity.conversationId === conversationId &&
        entity.messageType === 'user'
      )
      .at(-1)
  }

  return undefined
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
