import type { MessageEntity } from '@/stores/message-store/types'
import type { HitlLifecycleState, PendingHitl } from './conversation-types'

const GENERIC_PROMPT = /^the agent needs additional information\.?$/i

// Lifecycle of one pending question from its durable statuses. Shared by the
// chat store selector and the session page, which reads the pending API.
export function hitlLifecycleState({ interactionStatus, applicationStatus, canceled = false, question }: {
  interactionStatus?: string | null
  applicationStatus?: string | null
  canceled?: boolean
  question: string
}): HitlLifecycleState {
  const interaction = interactionStatus
  const application = applicationStatus
  if (canceled || interaction === 'canceled') return 'canceled'
  if (interaction === 'expired') return 'expired'
  if (
    interaction === 'delivery_uncertain'
    || application === 'delivery_uncertain'
  ) return 'delivery_uncertain'
  if (
    interaction === 'applying' ||
    interaction === 'answers_recorded' ||
    application === 'applying'
  ) return 'applying'
  if (
    interaction === 'failed' ||
    interaction === 'error' ||
    !question.trim() ||
    GENERIC_PROMPT.test(question.trim())
  ) return 'routing_failed'
  return 'open'
}

function deriveLifecycleState(entity: MessageEntity, question: string): HitlLifecycleState {
  return hitlLifecycleState({
    interactionStatus: entity.hitlInteractionStatus,
    applicationStatus: entity.hitlApplicationStatus,
    canceled: entity.taskStatus === 'canceled',
    question,
  })
}

export function selectPendingHitls(
  conversationId: string,
  entities: Record<string, MessageEntity>,
  orderedIds: string[],
): PendingHitl[] {
  const hitlEntities: MessageEntity[] = []
  for (const id of orderedIds) {
    const e = entities[id]
    if (e && e.conversationId === conversationId && e.hitlRequestId) hitlEntities.push(e)
  }

  const activeGroupIds = new Set<string>()
  for (const e of hitlEntities) {
    if (e.hitlGroupId && !e.hitlResolved) {
      activeGroupIds.add(e.hitlGroupId)
    }
  }

  return hitlEntities
    .filter(e => {
      if (!e.hitlGroupId) return !e.hitlResolved
      return activeGroupIds.has(e.hitlGroupId)
    })
    .map(e => {
      const question = e.hitlPrompt ?? e.content ?? e.taskStatusMessage ?? ''
      return {
        hitlId: e.hitlRequestId!,
        source: e.hitlSource ?? 'agent',
        agentName: e.senderName,
        question,
        promptType: e.hitlPromptType ?? 'text',
        choices: e.hitlChoices ?? undefined,
        presentation: e.hitlPresentation,
        messageId: e.hitlMessageId ?? e.id,
        interactionId: e.hitlInteractionId ?? e.hitlGroupId ?? e.hitlRequestId!,
        interactionVersion: e.hitlInteractionVersion,
        interactionStatus: e.hitlInteractionStatus,
        applicationStatus: e.hitlApplicationStatus,
        lifecycleState: deriveLifecycleState(e, question),
        errorMessage: e.taskError ?? undefined,
        expiresAt: e.hitlExpiresAt,
        clientRequestId: e.clientRequestId,
        groupId: e.hitlGroupId ?? undefined,
        groupTotal: e.hitlGroupTotal ?? undefined,
        groupIndex: e.hitlGroupIndex ?? undefined,
        isAnswered: e.hitlResolved === true || !!e.hitlUserAnswer,
        answer: e.hitlUserAnswer || undefined,
      }
    })
    .sort((a, b) => {
      if (a.interactionId !== b.interactionId) return a.messageId.localeCompare(b.messageId)
      return (a.groupIndex ?? 0) - (b.groupIndex ?? 0) || a.hitlId.localeCompare(b.hitlId)
    })
}
