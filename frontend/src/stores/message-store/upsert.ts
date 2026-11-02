import { presentationEqual } from '@/lib/hitl/presentation'
import { isTerminalState } from '@/lib/types/sse'
import { patchedPublicAgentName } from '@/lib/agent-display-name'
import { resolveDisplayType } from './resolve-display-type'
import type { MessageEntity, IncomingMessage, MessageSource, ArtifactData } from './types'

/**
 * Core upsert logic, extracted so it can be used by both single and batch writes.
 * Returns null if the update was rejected or is a no-op.
 *
 * Conflict Resolution Rules:
 *   1. Never downgrade a terminal task status.
 *   2. SSE wins over DB for non-terminal states.
 *   3. DB wins for terminal states (handled implicitly — rules 1 & 2 only block, not this).
 *   4. Skip no-op updates.
 *   5. Never overwrite ephemeral messages from DB.
 */
export function applyUpsert(
  entities: Record<string, MessageEntity>,
  orderedIds: string[],
  incoming: IncomingMessage,
  source: MessageSource,
): { entities: Record<string, MessageEntity>; idsChanged: boolean } | null {
  const existing = entities[incoming.id]

  // ── Rule 5: Never overwrite ephemeral from DB ──
  if (existing?.isEphemeral && source === 'db') {
    return null
  }

  if (existing) {
    // ── Rule 0: Skip stale updates when incoming has an older task timestamp ──
    const incomingIsOlder = Boolean(
      existing.taskUpdatedAt &&
      incoming.taskUpdatedAt &&
      new Date(incoming.taskUpdatedAt).getTime() < new Date(existing.taskUpdatedAt).getTime()
    )
    const isAuthoritativeTerminalDbReconciliation = Boolean(
      source === 'db' &&
      incoming.taskStatus != null &&
      isTerminalState(incoming.taskStatus)
    )
    if (incomingIsOlder && !isAuthoritativeTerminalDbReconciliation) {
      return null
    }

    // ── Rule 1: Never downgrade terminal status ──
    const isNewHitlFollowUp = Boolean(
      existing.taskStatus === 'completed' &&
      incoming.taskStatus === 'input-required' &&
      incoming.taskUpdatedAt && existing.taskUpdatedAt &&
      new Date(incoming.taskUpdatedAt).getTime() > new Date(existing.taskUpdatedAt).getTime() &&
      (
        (incoming.hitlRequestId !== undefined && existing.hitlRequestId !== undefined && incoming.hitlRequestId !== existing.hitlRequestId)
        || (incoming.hitlInteractionId !== undefined && existing.hitlInteractionId !== undefined && incoming.hitlInteractionId !== existing.hitlInteractionId)
      )
    )

    if (
      existing.taskStatus &&
      isTerminalState(existing.taskStatus) &&
      incoming.taskStatus &&
      !isTerminalState(incoming.taskStatus) &&
      !isNewHitlFollowUp
    ) {
      return null
    }

    // ── Rule 2: SSE wins over DB for non-terminal ──
    // Block DB from overwriting live SSE state, UNLESS the DB carries a
    // terminal status upgrade (SSE may have missed the terminal event
    // due to a disconnection).
    if (
      existing.source === 'sse' &&
      source === 'db' &&
      existing.taskStatus &&
      !isTerminalState(existing.taskStatus) &&
      !(incoming.taskStatus && isTerminalState(incoming.taskStatus))
    ) {
      return null
    }

    // ── Rule 4: Skip no-op updates ──
    if (isNoOpUpdate(existing, incoming, source)) {
      return null
    }
  }

  // ── Build the new entity ──
  const resolvedEphemeral = incoming.isEphemeral ?? existing?.isEphemeral ?? false
  const merged = mergeIncoming(existing, incoming)

  const displayType = resolveDisplayType({
    messageType: merged.messageType,
  })

  const entity: MessageEntity = {
    ...merged,
    displayType,
    source,
    sourceVersion: (existing?.sourceVersion ?? 0) + 1,
    updatedAt: Date.now(),
    createdAt: existing?.createdAt ?? new Date(incoming.timestamp).getTime(),
    isEphemeral: resolvedEphemeral,
  }

  const newEntities = { ...entities, [entity.id]: entity }
  const idsChanged = !existing // new message added

  return { entities: newEntities, idsChanged }
}

/**
 * Merge incoming fields onto existing entity, handling undefined vs null correctly.
 * - undefined in incoming → preserve existing value
 * - null in incoming → explicitly set to null
 * - value in incoming → overwrite
 */
function mergeIncoming(
  existing: MessageEntity | undefined,
  incoming: IncomingMessage,
): Omit<MessageEntity, 'displayType' | 'source' | 'sourceVersion' | 'updatedAt' | 'createdAt' | 'isEphemeral'> {
  if (!existing) {
    return {
      id: incoming.id,
      conversationId: incoming.conversationId,
      messageType: incoming.messageType,
      content: incoming.content,
      senderName: incoming.senderName,
      timestamp: incoming.timestamp,
      agentId: incoming.agentId,
      agentSource: incoming.agentSource,
      userId: incoming.userId,
      taskStatus: incoming.taskStatus ?? undefined,
      taskError: incoming.taskError,
      taskStatusMessage: incoming.taskStatusMessage,
      taskRequiresInput: incoming.taskRequiresInput,
      taskRequiresAuth: incoming.taskRequiresAuth,
      taskContent: incoming.taskContent,
      dispatchText: incoming.dispatchText,
      taskCreatedAt: incoming.taskCreatedAt,
      taskUpdatedAt: incoming.taskUpdatedAt,
      stepNumber: incoming.stepNumber,
      totalSteps: incoming.totalSteps,
      relatedMessageId: incoming.relatedMessageId,
      hitlRequestId: incoming.hitlRequestId,
      hitlMessageId: incoming.hitlMessageId,
      hitlSource: incoming.hitlSource,
      hitlPrompt: incoming.hitlPrompt,
      hitlPromptType: incoming.hitlPromptType,
      hitlChoices: incoming.hitlChoices,
      hitlPresentation: incoming.hitlPresentation,
      hitlExpiresAt: incoming.hitlExpiresAt,
      hitlResolved: incoming.hitlResolved,
      hitlInteractionId: incoming.hitlInteractionId,
      hitlInteractionStatus: incoming.hitlInteractionStatus,
      hitlInteractionVersion: incoming.hitlInteractionVersion,
      hitlApplicationStatus: incoming.hitlApplicationStatus,
      hitlGroupId: incoming.hitlGroupId,
      hitlGroupTotal: incoming.hitlGroupTotal,
      hitlGroupIndex: incoming.hitlGroupIndex,
      hitlUserAnswer: incoming.hitlUserAnswer,
      clientRequestId: incoming.clientRequestId,
      artifacts: incoming.artifacts,
      attachments: incoming.attachments,
      turnTerminalStatus: incoming.turnTerminalStatus,
      summaryOrigin: incoming.summaryOrigin,
      quotedText: incoming.quotedText,
      quotedSenderName: incoming.quotedSenderName,
      quoteId: incoming.quoteId,
    }
  }

  const hasTimestamps = Boolean(incoming.taskUpdatedAt && existing.taskUpdatedAt)
  const incomingIsNewerByTime = Boolean(
    hasTimestamps &&
    new Date(incoming.taskUpdatedAt!).getTime() > new Date(existing.taskUpdatedAt!).getTime()
  )
  const incomingIsOlderByTime = Boolean(
    hasTimestamps &&
    new Date(incoming.taskUpdatedAt!).getTime() < new Date(existing.taskUpdatedAt!).getTime()
  )

  const sameHitlIdentity = Boolean(
    incoming.hitlRequestId !== undefined
    && existing.hitlRequestId === incoming.hitlRequestId
    && incoming.hitlInteractionId !== undefined
    && existing.hitlInteractionId === incoming.hitlInteractionId
  )
  const incomingHitlIsNewer = Boolean(
    incoming.hitlInteractionVersion !== undefined
    && existing.hitlInteractionVersion !== undefined
    && incoming.hitlInteractionVersion > existing.hitlInteractionVersion
  )
  const isStaleHitlVersion = Boolean(
    incoming.hitlInteractionId !== undefined
    && existing.hitlInteractionId !== undefined
    && incoming.hitlInteractionId === existing.hitlInteractionId
    && incoming.hitlInteractionVersion !== undefined
    && existing.hitlInteractionVersion !== undefined
    && incoming.hitlInteractionVersion < existing.hitlInteractionVersion
  )
  const isDifferentHitlRound = Boolean(
    (incoming.hitlRequestId !== undefined && existing.hitlRequestId !== undefined && incoming.hitlRequestId !== existing.hitlRequestId)
    || (incoming.hitlInteractionId !== undefined && existing.hitlInteractionId !== undefined && incoming.hitlInteractionId !== existing.hitlInteractionId)
  )
  const isNewHitlFollowUp = Boolean(
    isDifferentHitlRound
    && !incomingIsOlderByTime
    && (incomingIsNewerByTime || (!hasTimestamps && existing.hitlInteractionVersion === undefined))
  )
  const isStaleHitl = Boolean(
    incomingIsOlderByTime
    || isStaleHitlVersion
    || (isDifferentHitlRound && !incomingHitlIsNewer && incomingIsOlderByTime)
  )
  const incomingHitlIsTerminal = Boolean(
    incoming.hitlResolved === true
    || ['applied', 'responded', 'canceled', 'expired'].includes(
      incoming.hitlInteractionStatus ?? '',
    )
    || incoming.hitlApplicationStatus === 'applied'
  )
  const existingHitlIsNonRegressible = Boolean(
    sameHitlIdentity
    && !incomingHitlIsTerminal
    && (
      existing.hitlResolved === true
      || existing.hitlUserAnswer
      || ['answers_recorded', 'applying', 'applied', 'responded'].includes(
        existing.hitlInteractionStatus ?? '',
      )
      || ['applying', 'applied'].includes(existing.hitlApplicationStatus ?? '')
    )
  )
  const acceptsHitlUpdate = !isStaleHitl && !existingHitlIsNonRegressible
  const finalizesExistingHitl = Boolean(
    existing.hitlRequestId
    && existing.hitlUserAnswer
    && incoming.taskStatus
    && isTerminalState(incoming.taskStatus)
    && incoming.hitlRequestId === undefined
    && incoming.hitlPrompt === undefined
    && incoming.hitlInteractionStatus === undefined
    && incoming.hitlApplicationStatus === undefined
    && incoming.hitlResolved === undefined
  )

  return {
    id: incoming.id,
    conversationId: incoming.conversationId,
    messageType: incoming.messageType,
    content: incoming.content,
    senderName: incoming.messageType === 'agent'
      ? (patchedPublicAgentName(existing.senderName, incoming.senderName) ?? incoming.senderName)
      : incoming.senderName,
    timestamp: incoming.timestamp,
    agentId: incoming.agentId !== undefined ? incoming.agentId : existing.agentId,
    agentSource: incoming.agentSource !== undefined ? incoming.agentSource : existing.agentSource,
    userId: incoming.userId !== undefined ? incoming.userId : existing.userId,
    taskStatus: incomingIsOlderByTime
      ? existing.taskStatus
      : incoming.taskStatus !== undefined
        ? (incoming.taskStatus ?? undefined) // null → undefined (clear the field)
        : existing.taskStatus,
    taskError: incomingIsOlderByTime
      ? existing.taskError
      : incoming.taskError !== undefined
        ? incoming.taskError
        : existing.taskError,
    taskStatusMessage: incomingIsOlderByTime
      ? existing.taskStatusMessage
      : incoming.taskStatusMessage !== undefined
        ? incoming.taskStatusMessage
        : existing.taskStatusMessage,
    taskRequiresInput: incomingIsOlderByTime
      ? existing.taskRequiresInput
      : incoming.taskRequiresInput !== undefined
        ? incoming.taskRequiresInput
        : existing.taskRequiresInput,
    taskRequiresAuth: incomingIsOlderByTime
      ? existing.taskRequiresAuth
      : incoming.taskRequiresAuth !== undefined
        ? incoming.taskRequiresAuth
        : existing.taskRequiresAuth,
    taskContent: incomingIsOlderByTime
      ? existing.taskContent
      : incoming.taskContent !== undefined
        ? incoming.taskContent
        : existing.taskContent,
    dispatchText: incomingIsOlderByTime
      ? existing.dispatchText
      : incoming.dispatchText !== undefined
        ? incoming.dispatchText
        : existing.dispatchText,
    taskCreatedAt: incoming.taskCreatedAt !== undefined ? incoming.taskCreatedAt : existing.taskCreatedAt,
    taskUpdatedAt: incomingIsOlderByTime
      ? existing.taskUpdatedAt
      : incoming.taskUpdatedAt !== undefined
        ? incoming.taskUpdatedAt
        : existing.taskUpdatedAt,
    stepNumber: incoming.stepNumber !== undefined ? incoming.stepNumber : existing.stepNumber,
    totalSteps: incoming.totalSteps !== undefined ? incoming.totalSteps : existing.totalSteps,
    relatedMessageId: incoming.relatedMessageId !== undefined ? incoming.relatedMessageId : existing.relatedMessageId,
    hitlRequestId: acceptsHitlUpdate && incoming.hitlRequestId !== undefined ? incoming.hitlRequestId : existing.hitlRequestId,
    hitlMessageId: acceptsHitlUpdate && incoming.hitlMessageId !== undefined ? incoming.hitlMessageId : existing.hitlMessageId,
    hitlSource: acceptsHitlUpdate && incoming.hitlSource !== undefined ? incoming.hitlSource : existing.hitlSource,
    hitlPrompt: acceptsHitlUpdate && incoming.hitlPrompt !== undefined ? incoming.hitlPrompt : existing.hitlPrompt,
    hitlPromptType: acceptsHitlUpdate && incoming.hitlPromptType !== undefined ? incoming.hitlPromptType : existing.hitlPromptType,
    hitlChoices: acceptsHitlUpdate && incoming.hitlChoices !== undefined ? incoming.hitlChoices : existing.hitlChoices,
    hitlPresentation: acceptsHitlUpdate && incoming.hitlPresentation !== undefined ? incoming.hitlPresentation : existing.hitlPresentation,
    hitlExpiresAt: acceptsHitlUpdate && incoming.hitlExpiresAt !== undefined ? incoming.hitlExpiresAt : existing.hitlExpiresAt,
    hitlResolved: finalizesExistingHitl
      ? true
      : isNewHitlFollowUp
        ? (incoming.hitlResolved !== undefined ? incoming.hitlResolved : false)
        : acceptsHitlUpdate && incoming.hitlResolved !== undefined
          ? incoming.hitlResolved
          : existing.hitlResolved,
    hitlInteractionId: acceptsHitlUpdate && incoming.hitlInteractionId !== undefined ? incoming.hitlInteractionId : existing.hitlInteractionId,
    hitlInteractionStatus: finalizesExistingHitl
      ? 'responded'
      : acceptsHitlUpdate && incoming.hitlInteractionStatus !== undefined
        ? incoming.hitlInteractionStatus
        : isNewHitlFollowUp
          ? 'open'
          : existing.hitlInteractionStatus,
    hitlInteractionVersion: acceptsHitlUpdate && incoming.hitlInteractionVersion !== undefined ? incoming.hitlInteractionVersion : existing.hitlInteractionVersion,
    hitlApplicationStatus: finalizesExistingHitl
      ? 'applied'
      : acceptsHitlUpdate && incoming.hitlApplicationStatus !== undefined
        ? incoming.hitlApplicationStatus
        : isNewHitlFollowUp
          ? undefined
          : existing.hitlApplicationStatus,
    hitlGroupId: acceptsHitlUpdate && incoming.hitlGroupId !== undefined ? incoming.hitlGroupId : existing.hitlGroupId,
    hitlGroupTotal: acceptsHitlUpdate && incoming.hitlGroupTotal !== undefined ? incoming.hitlGroupTotal : existing.hitlGroupTotal,
    hitlGroupIndex: acceptsHitlUpdate && incoming.hitlGroupIndex !== undefined ? incoming.hitlGroupIndex : existing.hitlGroupIndex,
    hitlUserAnswer: isNewHitlFollowUp
      ? (incoming.hitlUserAnswer !== undefined ? incoming.hitlUserAnswer : undefined)
      : acceptsHitlUpdate && incoming.hitlUserAnswer !== undefined
        ? incoming.hitlUserAnswer
        : existing.hitlUserAnswer,
    clientRequestId: incoming.clientRequestId !== undefined ? incoming.clientRequestId : existing.clientRequestId,
    artifacts: incoming.artifacts !== undefined ? incoming.artifacts : existing.artifacts,
    attachments: incoming.attachments !== undefined ? incoming.attachments : existing.attachments,
    turnTerminalStatus: incoming.turnTerminalStatus !== undefined ? incoming.turnTerminalStatus : existing.turnTerminalStatus,
    summaryOrigin: incoming.summaryOrigin !== undefined ? incoming.summaryOrigin : existing.summaryOrigin,
    quotedText: incoming.quotedText !== undefined ? incoming.quotedText : existing.quotedText,
    quotedSenderName: incoming.quotedSenderName !== undefined ? incoming.quotedSenderName : existing.quotedSenderName,
    quoteId: incoming.quoteId !== undefined ? incoming.quoteId : existing.quoteId,
  }
}

/**
 * Helper: if incoming field is undefined, treat as "not changing" → use existing.
 * If incoming field is explicitly null or a value, it IS a change candidate.
 * This distinguishes "field not provided" from "field explicitly set to null".
 */
function coalesce<T>(incomingVal: T | undefined, existingVal: T): T {
  return incomingVal === undefined ? existingVal : incomingVal
}

function arraysShallowEqual(a: unknown, b: unknown): boolean {
  if (a === b) return true
  if (a == null && b == null) return true
  if (!Array.isArray(a) || !Array.isArray(b)) return a === b
  if (a.length !== b.length) return false
  for (let i = 0; i < a.length; i++) {
    if (a[i] !== b[i]) return false
  }
  return true
}

function artifactsEqual(
  a: ArtifactData[] | undefined,
  b: ArtifactData[] | undefined
): boolean {
  if (a === b) return true
  if (!a || !b) return a === b
  if (a.length !== b.length) return false
  return JSON.stringify(a) === JSON.stringify(b)
}


/**
 * Detect whether an incoming update changes any rendering-visible fields.
 * Returns true if nothing visible changed — the store should skip this update.
 */
export function isNoOpUpdate(
  existing: MessageEntity,
  incoming: IncomingMessage,
  // eslint-disable-next-line @typescript-eslint/no-unused-vars
  _source: MessageSource,
): boolean {
  return (
    existing.content           === coalesce(incoming.content, existing.content) &&
    existing.taskStatus        === coalesce(incoming.taskStatus, existing.taskStatus) &&
    existing.taskError         === coalesce(incoming.taskError, existing.taskError) &&
    existing.taskStatusMessage === coalesce(incoming.taskStatusMessage, existing.taskStatusMessage) &&
    existing.senderName        === coalesce(incoming.senderName, existing.senderName) &&
    existing.agentSource       === coalesce(incoming.agentSource, existing.agentSource) &&
    existing.stepNumber        === coalesce(incoming.stepNumber, existing.stepNumber) &&
    existing.totalSteps        === coalesce(incoming.totalSteps, existing.totalSteps) &&
    existing.taskContent       === coalesce(incoming.taskContent, existing.taskContent) &&
    existing.dispatchText      === coalesce(incoming.dispatchText, existing.dispatchText) &&
    existing.taskRequiresInput === coalesce(incoming.taskRequiresInput, existing.taskRequiresInput) &&
    existing.taskRequiresAuth  === coalesce(incoming.taskRequiresAuth, existing.taskRequiresAuth) &&
    existing.taskUpdatedAt       === coalesce(incoming.taskUpdatedAt, existing.taskUpdatedAt) &&
    existing.hitlResolved      === coalesce(incoming.hitlResolved, existing.hitlResolved) &&
    existing.hitlSource        === coalesce(incoming.hitlSource, existing.hitlSource) &&
    existing.hitlInteractionId === coalesce(incoming.hitlInteractionId, existing.hitlInteractionId) &&
    existing.hitlInteractionStatus === coalesce(incoming.hitlInteractionStatus, existing.hitlInteractionStatus) &&
    existing.hitlInteractionVersion === coalesce(incoming.hitlInteractionVersion, existing.hitlInteractionVersion) &&
    existing.hitlApplicationStatus === coalesce(incoming.hitlApplicationStatus, existing.hitlApplicationStatus) &&
    existing.hitlPrompt        === coalesce(incoming.hitlPrompt, existing.hitlPrompt) &&
    existing.hitlRequestId     === coalesce(incoming.hitlRequestId, existing.hitlRequestId) &&
    existing.hitlMessageId     === coalesce(incoming.hitlMessageId, existing.hitlMessageId) &&
    existing.hitlPromptType    === coalesce(incoming.hitlPromptType, existing.hitlPromptType) &&
    existing.hitlExpiresAt     === coalesce(incoming.hitlExpiresAt, existing.hitlExpiresAt) &&
    arraysShallowEqual(existing.hitlChoices, coalesce(incoming.hitlChoices, existing.hitlChoices)) &&
    presentationEqual(existing.hitlPresentation, coalesce(incoming.hitlPresentation, existing.hitlPresentation)) &&
    existing.hitlGroupId       === coalesce(incoming.hitlGroupId, existing.hitlGroupId) &&
    existing.hitlGroupTotal    === coalesce(incoming.hitlGroupTotal, existing.hitlGroupTotal) &&
    existing.hitlGroupIndex    === coalesce(incoming.hitlGroupIndex, existing.hitlGroupIndex) &&
    existing.hitlUserAnswer    === coalesce(incoming.hitlUserAnswer, existing.hitlUserAnswer) &&
    existing.clientRequestId   === coalesce(incoming.clientRequestId, existing.clientRequestId) &&
    existing.isEphemeral       === (incoming.isEphemeral ?? existing.isEphemeral) &&
    artifactsEqual(existing.artifacts, coalesce(incoming.artifacts, existing.artifacts)) &&
    existing.attachments       === coalesce(incoming.attachments, existing.attachments) &&
    existing.turnTerminalStatus === coalesce(incoming.turnTerminalStatus, existing.turnTerminalStatus) &&
    existing.summaryOrigin === coalesce(incoming.summaryOrigin, existing.summaryOrigin) &&
    existing.quoteId === coalesce(incoming.quoteId, existing.quoteId)
  )
}

/**
 * Build a sorted array of message IDs from the entities map.
 * Sort order: createdAt (primary, server-assigned creation time, immutable
 * once set), stepNumber within the same workflow (same relatedMessageId and
 * createdAt within 60s), then message ID for stability.
 *
 * Using `createdAt` (derived from the server's message_created_at) instead
 * of the mutable `timestamp` field ensures consistent ordering across
 * sessions — parallel agent bubbles assigned close server timestamps sort
 * identically whether first seen via SSE or loaded from DB after a refresh.
 */
export function buildSortedIds(entities: Record<string, MessageEntity>): string[] {
  return Object.values(entities)
    .sort((a, b) => {
      const timeDiff = a.createdAt - b.createdAt

      // Step-number sorting only applies within the SAME workflow
      // (same relatedMessageId) and only when createdAt is close.
      if (
        a.stepNumber != null && b.stepNumber != null &&
        a.relatedMessageId && b.relatedMessageId &&
        a.relatedMessageId === b.relatedMessageId &&
        Math.abs(timeDiff) < 60_000
      ) {
        const stepDiff = a.stepNumber - b.stepNumber
        if (stepDiff !== 0) return stepDiff
      }

      if (timeDiff !== 0) return timeDiff

      // Tiebreakers: step number within same workflow, then ID
      if (
        a.relatedMessageId && b.relatedMessageId &&
        a.relatedMessageId === b.relatedMessageId
      ) {
        const stepA = a.stepNumber ?? Infinity
        const stepB = b.stepNumber ?? Infinity
        if (stepA !== stepB) return stepA - stepB
      }

      return a.id.localeCompare(b.id)
    })
    .map(e => e.id)
}

function isTextOnlyArtifact(a: ArtifactData): boolean {
  return a.parts.length > 0 && a.parts.every(p => p.kind === 'text')
}

/**
 * Merge an incoming artifact into an existing artifact list.
 * - If append=true and artifact already exists, append new parts.
 * - Otherwise replace the existing artifact with same ID.
 * - New artifact IDs are appended to the list.
 * - Same-name text-only artifacts with different IDs are deduplicated
 *   (keeps only the latest) to contain misbehaving agents that emit a
 *   new artifact ID per streaming token instead of using append semantics.
 */
export function mergeArtifacts(
  existing: ArtifactData[] | undefined,
  incoming: ArtifactData,
  append: boolean = false,
): ArtifactData[] {
  const list = existing ? [...existing] : []
  const idx = list.findIndex(a => a.artifactId === incoming.artifactId)

  if (idx >= 0) {
    if (append) {
      const merged = mergeTextParts([...list[idx].parts], incoming.parts)
      list[idx] = {
        ...list[idx],
        parts: merged,
        isStreaming: incoming.isStreaming ?? list[idx].isStreaming,
      }
    } else {
      list[idx] = incoming
    }
  } else {
    // Dedup: if incoming is a text-only artifact with a name that matches
    // an existing text-only artifact, replace it instead of pushing a new entry.
    const sameNameIdx = incoming.name && isTextOnlyArtifact(incoming)
      ? list.findIndex(a => a.name === incoming.name && isTextOnlyArtifact(a))
      : -1

    if (sameNameIdx >= 0) {
      list[sameNameIdx] = incoming
    } else {
      list.push(incoming)
    }
  }

  return list
}

/**
 * Extract display text from text-only artifacts for persisted entity state.
 * Returns the last text-only artifact so thinking + answer agents surface
 * only the most recent stream (the answer artifact).
 */
export function extractTextFromArtifacts(artifacts: ArtifactData[]): string {
  let last = ''
  for (const a of artifacts) {
    if (!isTextOnlyArtifact(a)) continue
    last = a.parts.map(p => p.text || '').join('')
  }
  return last
}

/**
 * Extract live streaming display text by concatenating all text-only artifacts
 * in emission order. Matches backend extract_parts_from_artifacts ("" join).
 */
export function extractStreamTextFromArtifacts(artifacts: ArtifactData[]): string {
  let combined = ''
  for (const a of artifacts) {
    if (!isTextOnlyArtifact(a)) continue
    combined += a.parts.map(p => p.text || '').join('')
  }
  return combined
}

/**
 * When appending artifact parts during streaming, concatenate consecutive
 * text parts into the trailing text part instead of creating separate
 * `<p>` elements per token (which causes the one-word-per-line glitch).
 */
function mergeTextParts(
  existingParts: ArtifactData['parts'],
  newParts: ArtifactData['parts'],
): ArtifactData['parts'] {
  const result = [...existingParts]
  for (const part of newParts) {
    const last = result[result.length - 1]
    if (part.kind === 'text' && last?.kind === 'text') {
      result[result.length - 1] = { ...last, text: (last.text || '') + (part.text || '') }
    } else {
      result.push(part)
    }
  }
  return result
}
