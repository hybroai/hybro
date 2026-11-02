import type { MessageEntity } from '@/stores/message-store/types'
import { buildClientRequestUserMessageIndex, routeAgentToTurn } from '@/lib/selectors/route-agent'
import { isSyntheticHitlQuestionEntity } from '@/lib/hitl/hitl-message-projection'
import { isInteractiveState } from '@/lib/types/sse'

export type SummaryOrigin = 'llm' | 'deterministic'

export function parseSummaryOrigin(value: unknown): SummaryOrigin | undefined {
  if (value === 'deterministic' || value === 'llm') return value
  return undefined
}

/** The coordinator's "N agents responded" stub, which carries no answer. */
export function isDeterministicDigestContent(content: string): boolean {
  const text = content.trim()
  if (!text) return false
  return /^\d+ agents? responded\. Expand below to read each answer\.?$/i.test(text)
}

export interface TranscriptTurnSource {
  userMessageId: string
  replyIds: string[]
}

function isTranscriptReply(entity: MessageEntity): boolean {
  if (entity.messageType !== 'agent' || entity.isEphemeral) return false
  // HITL questions belong to the composer questionnaire, not the transcript.
  // An agent reply that asked a question and then finished keeps its HITL
  // metadata, so only question rows and unanswered prompts are left out.
  if (isSyntheticHitlQuestionEntity(entity)) return false
  if (
    entity.hitlRequestId && !entity.hitlResolved && entity.hitlUserAnswer === undefined
    && entity.taskStatus && isInteractiveState(entity.taskStatus)
  ) return false
  if (entity.summaryOrigin === 'deterministic' || isDeterministicDigestContent(entity.content)) return false
  return entity.content.trim().length > 0 || (entity.artifacts?.length ?? 0) > 0
}

/**
 * Group a room's messages into user Turns, each with the agent replies routed
 * to it: by `relatedMessageId`, then `clientRequestId`, then the nearest
 * preceding user message. User shells without content or attachments (created
 * before database hydration fills them) are skipped, and so are their replies
 * until hydration fills them; agent messages before every user message are
 * dropped.
 */
export function buildTranscriptTurnSources(
  conversationId: string,
  entities: Record<string, MessageEntity>,
  orderedIds: readonly string[],
): TranscriptTurnSource[] {
  const roomEntities = orderedIds
    .map((id) => entities[id])
    .filter((entity): entity is MessageEntity => entity?.conversationId === conversationId)
  const allUsers = roomEntities.filter((entity) => entity.messageType === 'user')
  const users = allUsers.filter((entity) => (
    entity.content.trim().length > 0 || (entity.attachments?.length ?? 0) > 0
  ))
  // Shells take part in routing so their replies do not fall back to an
  // earlier message; they are not emitted.
  const userIds = new Set(allUsers.map((entity) => entity.id))
  const clientRequestIndex = buildClientRequestUserMessageIndex(userIds, entities)
  const repliesByUser = new Map<string, string[]>()
  let precedingUserId: string | undefined
  for (const entity of roomEntities) {
    if (userIds.has(entity.id)) {
      precedingUserId = entity.id
      continue
    }
    if (!isTranscriptReply(entity)) continue
    const routed = routeAgentToTurn(entity, userIds, entities, clientRequestIndex)
    const userId = routed === 'unresolved' ? precedingUserId : routed
    if (!userId) continue
    const replies = repliesByUser.get(userId) ?? []
    replies.push(entity.id)
    repliesByUser.set(userId, replies)
  }
  return users.map((entity) => ({
    userMessageId: entity.id,
    replyIds: repliesByUser.get(entity.id) ?? [],
  }))
}
