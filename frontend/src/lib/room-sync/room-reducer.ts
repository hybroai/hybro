// RoomReducer — single state entry for snapshot-driven room sync
// (Room Stream Snapshot plan §4, §8).
//
// State = the latest full snapshot plus the ordered deltas after it.
// Snapshots reconcile; deltas carry liveness. The reducer owns:
//   • capability detection off the `connected` handshake (room_seq present),
//   • pre-snapshot delta buffering + bootstrap snapshot recovery,
//   • ordered delta patch with a bounded reorder window and gap self-heal,
//   • snapshot application that folds the snapshot sections into the
//     message / streaming / trace stores (the same fold functions the live
//     handlers use are reused for buffered deltas and replay).
//
// Legacy behavior (no room_seq in `connected`) is preserved unchanged.

import { parseHitlPresentation } from '@/lib/hitl/presentation'
import type {
  AnySSEFrame,
  RoomSnapshotTurn,
  RoomSSEFrameMap,
  RoomSSEType,
  SnapshotData,
} from '@/lib/types/sse'
import { isRoomSSEType } from '@/lib/types/sse'
import {
  hasCanonicalSnapshotCapability,
  isCanonicalHITLRequestData,
  validateCanonicalSnapshotTurns,
} from '@/lib/turn-lifecycle/contract'
import { useMessageStore } from '@/stores/message-store'
import { useTurnStore } from '@/stores/turn-store'
import { useRoomUiStore } from '@/stores/room-ui-store'
import { useStreamingStore } from '@/stores/streaming-store'
import type { ArtifactData, MessageEntity } from '@/stores/message-store/types'
import type { ProcessingLifecycle } from '@/hooks/room/processing-lifecycle'
import { specificPublicAgentName } from '@/lib/agent-display-name'
import {
  buildPendingHitlIncomingMessage,
  hitlQuestionEntityId,
  hitlRequestKey,
} from '@/lib/hitl/hitl-message-projection'

export const REORDER_WINDOW_MS = 500
export const BOOTSTRAP_SNAPSHOT_MS = 500
const PRE_SNAPSHOT_BUFFER_LIMIT = 400
const HEARTBEAT_GAP_SETTLE_MS = 300

export interface RoomReducerDeps {
  conversationId: string
  /** Fold one delta frame through the live handler path (the fold path). */
  onDelta: (frame: AnySSEFrame) => Promise<void>
  /** Force a fresh snapshot: close the stream, reconnect with ?snapshot=1. */
  requestSnapshot: () => void
  /** Real dispatcher-owned HITL request → message index restored by snapshots. */
  hitlRequestIndex?: Map<string, string>
  /** Legacy guard reconciled only when an exact canonical root settles. */
  processingLifecycle?: ProcessingLifecycle
}

type DeltaType = Exclude<RoomSSEType, 'connected' | 'heartbeat' | 'snapshot'>
type DeltaFrame = RoomSSEFrameMap[DeltaType]

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

function frameRoomSeq(frame: AnySSEFrame): number | undefined {
  const data = frame.data as { room_seq?: unknown } | undefined
  return typeof data?.room_seq === 'number' ? data.room_seq : undefined
}

export class RoomReducer {
  private readonly deps: RoomReducerDeps
  private capabilityEnabled = false
  private snapshotApplied = false
  private lastRoomSeq: number | null = null
  /** Highest watermark actually folded into stores. Unlike connection-local
   * ordering state, this survives reconnects so an older bootstrap snapshot
   * cannot replace newer visible state. */
  private appliedRoomSeq: number | null = null
  private highestObservedRoomSeq: number | null = null
  private preSnapshotBuffer: DeltaFrame[] = []
  private reorderBuffer = new Map<number, DeltaFrame>()
  private reorderTimer: ReturnType<typeof setTimeout> | null = null
  private bootstrapTimer: ReturnType<typeof setTimeout> | null = null
  private heartbeatGapTimer: ReturnType<typeof setTimeout> | null = null
  private bootstrapRequested = false
  private disposed = false

  constructor(deps: RoomReducerDeps) {
    this.deps = deps
  }

  get enabled(): boolean {
    return this.capabilityEnabled
  }

  /** Stop the reducer: later frames are ignored, and pending timers and
   * frames still in flight can no longer request a snapshot. */
  dispose(): void {
    this.disposed = true
    this.clearTimers()
  }

  async handle(frame: AnySSEFrame): Promise<void> {
    if (this.disposed || !isRoomSSEType(frame.type)) return
    switch (frame.type) {
      case 'connected':
        await this.onConnected(frame as RoomSSEFrameMap['connected'])
        return
      case 'snapshot':
        await this.onSnapshot(frame as RoomSSEFrameMap['snapshot'])
        return
      case 'heartbeat':
        this.onHeartbeat(frame as RoomSSEFrameMap['heartbeat'])
        return
      default:
        await this.onDelta(frame as DeltaFrame)
    }
  }

  private async onConnected(frame: RoomSSEFrameMap['connected']): Promise<void> {
    // New session: reset per-connection ordering state.
    this.snapshotApplied = false
    this.lastRoomSeq = null
    this.highestObservedRoomSeq = null
    this.preSnapshotBuffer = []
    this.reorderBuffer.clear()
    this.bootstrapRequested = false
    this.clearTimers()

    const roomSeq = frame.data.room_seq
    if (typeof roomSeq === 'number') {
      // Capability negotiation (§4): a room_seq in the handshake enables the
      // new snapshot-driven semantics.
      this.capabilityEnabled = true
      this.lastRoomSeq = roomSeq
      this.highestObservedRoomSeq = roomSeq
      this.scheduleBootstrapCheck()
    } else {
      // A pre-snapshot backend retains the incumbent unsequenced fold. Exact
      // canonical authority is established only by a sequenced handshake and
      // a validated canonical snapshot/root.
      this.capabilityEnabled = false
    }
  }

  private async onSnapshot(frame: RoomSSEFrameMap['snapshot']): Promise<void> {
    if (!this.capabilityEnabled) {
      applySnapshotToStores(
        this.deps.conversationId,
        frame.data,
        this.deps.hitlRequestIndex,
      )
      return
    }
    const watermark = frame.data.room_seq
    if (
      this.appliedRoomSeq !== null
      && watermark < this.appliedRoomSeq
    ) {
      // A replacement snapshot is authoritative only when it covers at least
      // the watermark already folded into the stores.
      this.requestSnapshot()
      return
    }
    if (!applySnapshotToStores(
      this.deps.conversationId,
      frame.data,
      this.deps.hitlRequestIndex,
    )) {
      this.requestSnapshot()
      return
    }
    if (hasCanonicalSnapshotCapability(frame.data) && this.deps.processingLifecycle) {
      reconcileCanonicalSnapshotProcessingGuard(
        this.deps.conversationId,
        this.deps.processingLifecycle,
      )
    }

    this.snapshotApplied = true
    this.lastRoomSeq = watermark
    this.appliedRoomSeq = Math.max(this.appliedRoomSeq ?? watermark, watermark)
    this.highestObservedRoomSeq = Math.max(
      this.highestObservedRoomSeq ?? watermark,
      watermark,
    )
    for (const seq of this.reorderBuffer.keys()) {
      if (seq <= watermark) this.reorderBuffer.delete(seq)
    }
    this.clearReorderTimerIfEmpty()
    if (this.bootstrapTimer) {
      clearTimeout(this.bootstrapTimer)
      this.bootstrapTimer = null
    }

    // Replay pre-snapshot deltas in order (plan rule 2): deltas at or below
    // the snapshot watermark are discarded; the rest re-enter the delta path.
    const buffered = this.preSnapshotBuffer
    this.preSnapshotBuffer = []
    for (const delta of buffered) {
      const seq = frameRoomSeq(delta)
      if (seq !== undefined && seq <= watermark) continue
      await this.onDelta(delta)
    }

    // Drain the reorder window: buffered higher-seq deltas replay in order
    // after the replacement snapshot applies (plan rule 3).
    await this.drainReorderBuffer()
    this.reconcileObservedWatermark()
  }

  private onHeartbeat(frame: RoomSSEFrameMap['heartbeat']): void {
    if (!this.capabilityEnabled || !this.snapshotApplied) return
    const roomSeq = frame.data.room_seq
    if (typeof roomSeq !== 'number') return
    this.highestObservedRoomSeq = Math.max(
      this.highestObservedRoomSeq ?? roomSeq,
      roomSeq,
    )
    this.reconcileObservedWatermark()
  }

  private async onDelta(frame: DeltaFrame): Promise<void> {
    if (!this.capabilityEnabled) {
      await this.deps.onDelta(frame)
      return
    }
    const seq = frameRoomSeq(frame)
    if (seq === undefined) {
      // Unsequenced deltas are never folded into canonical state.
      this.requestBootstrapSnapshotOnce()
      return
    }
    if (!this.snapshotApplied) {
      // Rule 2: deltas arriving before the first snapshot are buffered and
      // replayed in order after the snapshot applies. The bootstrap trigger
      // (rule 7) guarantees the snapshot actually arrives.
      if (this.preSnapshotBuffer.length < PRE_SNAPSHOT_BUFFER_LIMIT) {
        this.preSnapshotBuffer.push(frame)
      }
      this.requestBootstrapSnapshotOnce()
      return
    }
    if (this.lastRoomSeq !== null && seq <= this.lastRoomSeq) {
      // Stale delta already covered by the snapshot: discard (rule 3).
      return
    }
    if (this.lastRoomSeq !== null && seq === this.lastRoomSeq + 1) {
      await this.deps.onDelta(frame)
      this.lastRoomSeq = seq
      this.appliedRoomSeq = Math.max(this.appliedRoomSeq ?? seq, seq)
      this.highestObservedRoomSeq = Math.max(
        this.highestObservedRoomSeq ?? seq,
        seq,
      )
      this.reconcileObservedWatermark()
      await this.drainReorderBuffer()
      return
    }
    // Out-of-order delta: buffer in the bounded reorder window (§4 rule 3).
    if (!this.reorderBuffer.has(seq)) {
      this.reorderBuffer.set(seq, frame)
      if (!this.reorderTimer) {
        this.reorderTimer = setTimeout(() => {
          this.reorderTimer = null
          this.requestSnapshot()
        }, REORDER_WINDOW_MS)
      }
    }
  }

  private async drainReorderBuffer(): Promise<void> {
    if (this.reorderBuffer.size === 0) {
      this.clearReorderTimerIfEmpty()
      return
    }
    const expected = (this.lastRoomSeq ?? 0) + 1
    const next = this.reorderBuffer.get(expected)
    if (next === undefined) return
    this.reorderBuffer.delete(expected)
    this.clearReorderTimerIfEmpty()
    await this.onDelta(next)
  }

  private clearReorderTimerIfEmpty(): void {
    if (this.reorderBuffer.size > 0 || !this.reorderTimer) return
    clearTimeout(this.reorderTimer)
    this.reorderTimer = null
  }

  private reconcileObservedWatermark(): void {
    const observed = this.highestObservedRoomSeq
    const applied = this.lastRoomSeq
    if (observed === null || applied === null || observed <= applied) {
      if (this.heartbeatGapTimer) {
        clearTimeout(this.heartbeatGapTimer)
        this.heartbeatGapTimer = null
      }
      return
    }
    if (this.heartbeatGapTimer) return
    this.heartbeatGapTimer = setTimeout(() => {
      this.heartbeatGapTimer = null
      if (
        this.highestObservedRoomSeq !== null
        && this.lastRoomSeq !== null
        && this.highestObservedRoomSeq > this.lastRoomSeq
      ) {
        this.requestSnapshot()
      }
    }, HEARTBEAT_GAP_SETTLE_MS)
  }

  private scheduleBootstrapCheck(): void {
    if (this.bootstrapTimer) clearTimeout(this.bootstrapTimer)
    this.bootstrapTimer = setTimeout(() => {
      this.bootstrapTimer = null
      if (!this.snapshotApplied) {
        this.requestSnapshot()
      }
    }, BOOTSTRAP_SNAPSHOT_MS)
  }

  private requestBootstrapSnapshotOnce(): void {
    if (this.bootstrapRequested || this.snapshotApplied) return
    this.bootstrapRequested = true
    // Rule 7: the first delta without a prior snapshot triggers recovery.
    this.requestSnapshot()
  }

  private requestSnapshot(): void {
    if (!this.disposed) this.deps.requestSnapshot()
  }

  private clearTimers(): void {
    for (const timer of [this.bootstrapTimer, this.reorderTimer, this.heartbeatGapTimer]) {
      if (timer) clearTimeout(timer)
    }
    this.bootstrapTimer = null
    this.reorderTimer = null
    this.heartbeatGapTimer = null
  }
}

// ── Snapshot application (fold snapshot sections into stores) ───────────────

function reconcileCanonicalSnapshotProcessingGuard(
  conversationId: string,
  lifecycle: ProcessingLifecycle,
): void {
  // HITL pauses clear the send guard but retain the exact owning root.
  const userMessageId = lifecycle.getMessageId()
  const clientRequestId = lifecycle.getClientRequestId()
  if (!userMessageId || !clientRequestId) return
  const room = useTurnStore.getState().rooms[conversationId]
  const guardedTurn = room && Object.values(room.turns).find((turn) => (
    turn.userMessageId === userMessageId
    && turn.clientRequestId === clientRequestId
  ))
  if (guardedTurn && ['completed', 'failed', 'canceled'].includes(guardedTurn.state)) {
    lifecycle.markProcessingResolved()
    lifecycle.stopProcessing()
    lifecycle.disarmCancelTimeout()
    useRoomUiStore.getState().setCancelling(conversationId, false)
    useMessageStore.getState().removeMessage(lifecycle.placeholderId(conversationId))
    lifecycle.dismissPlaceholder()
  }
}

function mapSnapshotStatusToTerminal(
  status: string | null,
): 'completed' | 'failed' | 'canceled' | undefined {
  if (status === 'completed') return 'completed'
  if (status === 'canceled') return 'canceled'
  if (status === 'failed' || status === 'rejected' || status === 'error' || status === 'rate_limited') {
    return 'failed'
  }
  return undefined
}

function applySnapshotMessages(
  conversationId: string,
  snapshot: SnapshotData,
  canonicalUserMessageIds: ReadonlySet<string>,
): void {
  const store = useMessageStore.getState()
  if (store.conversationId && store.conversationId !== conversationId) return

  for (const message of snapshot.messages) {
    if (message.content !== null || message.agent_id || message.task_status !== null) {
      // Agent-side record: upsert the committed message content.
      store.upsertMessage(
        {
          id: message.message_id,
          conversationId,
          messageType: 'agent',
          content: message.content ?? '',
          senderName: specificPublicAgentName(message.agent_name) ?? 'Unknown agent',
          agentId: message.agent_id ?? undefined,
          clientRequestId: message.client_request_id ?? undefined,
          relatedMessageId: message.related_message_id ?? undefined,
          timestamp: message.ts ?? new Date().toISOString(),
          taskStatus: (message.task_status as never) ?? undefined,
          taskError: message.task_error ?? undefined,
          taskContent: message.task_content ?? undefined,
          taskRequiresInput: message.requires_input,
          taskRequiresAuth: message.requires_auth,
          stepNumber: message.step_number ?? undefined,
          totalSteps: message.total_steps ?? undefined,
          taskCreatedAt: message.created_at ?? undefined,
          isEphemeral: false,
        },
        'sse',
      )
    }
    if (!canonicalUserMessageIds.has(message.message_id) && message.status) {
      // Turn-level processing state belongs to the USER entity. Snapshot may
      // arrive before DB hydration; create a correlation-preserving shell so
      // the durable status is never discarded because of arrival order.
      // DB hydration later fills content/sender fields and preserves the SSE
      // overlay through the normal message-store merge rules.
      let userEntity = useMessageStore.getState().entities[message.message_id]
      if (!userEntity || userEntity.messageType !== 'user') {
        store.upsertMessage(
          {
            id: message.message_id,
            conversationId,
            messageType: 'user',
            content: '',
            senderName: 'You',
            timestamp: message.ts || new Date().toISOString(),
            clientRequestId: message.client_request_id ?? undefined,
            isEphemeral: false,
          },
          'sse',
        )
        userEntity = useMessageStore.getState().entities[message.message_id]
      }
      if (!userEntity || userEntity.messageType !== 'user') continue

      const turnTerminalStatus = mapSnapshotStatusToTerminal(message.status)
      if (turnTerminalStatus) {
        store.upsertMessage(
          {
            id: message.message_id,
            conversationId,
            messageType: 'user',
            content: userEntity.content,
            senderName: userEntity.senderName,
            timestamp: userEntity.timestamp,
            clientRequestId: message.client_request_id ?? userEntity.clientRequestId,
            turnTerminalStatus,
          },
          'sse',
        )
      }
    }
  }
}

function applySnapshotStreaming(conversationId: string, snapshot: SnapshotData): void {
  const streaming = useStreamingStore.getState()
  for (const [messageId, record] of Object.entries(snapshot.streaming)) {
    if (record.text) {
      streaming.append(
        messageId,
        conversationId,
        textPartialToArtifact(messageId, record.text),
        false,
        {
          clientRequestId: record.client_request_id ?? undefined,
        },
      )
    }
    for (let index = 0; index < record.artifacts.length; index++) {
      streaming.append(
        messageId,
        conversationId,
        record.artifacts[index] as unknown as ArtifactData,
        index > 0,
        {
          clientRequestId: record.client_request_id ?? undefined,
        },
      )
    }
    if (record.is_complete) {
      streaming.markComplete(messageId)
    }
  }
}

function stringArraysEqual(left: readonly string[], right: readonly string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index])
}

function validateCanonicalSnapshotHitl(
  conversationId: string,
  snapshot: SnapshotData,
  turns: RoomSnapshotTurn[],
): boolean {
  const store = useMessageStore.getState()
  const turnsByRunId = new Map(turns.map((turn) => [turn.run_id, turn]))
  const canonicalRequests = new Map<string, NonNullable<ReturnType<typeof canonicalRequestData>>>()
  const seenRequestKeys = new Set<string>()

  for (const turn of turns) {
    for (const interaction of turn.hitl_interactions) {
      const projectedIds = interaction.requests.map((request) => request.request_id)
      if (!stringArraysEqual(interaction.request_ids, projectedIds)) return false
      if (interaction.requests.some((request, index) => (
        request.question_index !== index
        || request.question_count !== interaction.request_ids.length
      ))) return false
    }
  }

  for (const request of snapshot.hitl.requests) {
    const requestId = typeof request.request_id === 'string' ? request.request_id : undefined
    const messageId = typeof request.message_id === 'string' ? request.message_id : undefined
    if (!requestId || !messageId) return false
    const requestKey = hitlRequestKey(
      typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      requestId,
    )
    if (seenRequestKeys.has(requestKey)) return false
    seenRequestKeys.add(requestKey)
    const requestClientId = typeof request.client_request_id === 'string'
      ? request.client_request_id
      : undefined
    const requestUserMessageId = typeof request.related_user_message_id === 'string'
      ? request.related_user_message_id
      : typeof request.related_message_id === 'string'
        ? request.related_message_id
        : undefined
    const questionCount = typeof request.question_count === 'number'
      ? request.question_count
      : undefined
    const projectionId = hitlQuestionEntityId(
      messageId,
      typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      requestId,
      questionCount,
    )
    const canonical = canonicalRequestData(request)
    const declaresCanonical = Object.prototype.hasOwnProperty.call(request, 'run_id')
    if (declaresCanonical && canonical === null) return false
    // Rolling-deploy rows without run_id predate the canonical request
    // contract, even when their message_id happens to use an orchestrator
    // prefix. Accept them through legacy overlay before strict correlation;
    // rows declaring run_id remain exact and fail closed.
    if (!declaresCanonical) continue
    const claimsCanonicalRoot = canonical !== null
    if (!claimsCanonicalRoot) return false
    const existingEntitiesMatch = [store.entities[messageId], store.entities[projectionId]]
      .filter((entity): entity is MessageEntity => entity !== undefined)
      .every((entity) => (
        entity.conversationId === conversationId
        && entity.clientRequestId === requestClientId
        && entity.relatedMessageId === requestUserMessageId
      ))
    if (!existingEntitiesMatch) return false
    const owner = canonical
      ? turnsByRunId.get(canonical.run_id)
      : turns.find((turn) => (
          turn.client_request_id === requestClientId
          && turn.user_message_id === requestUserMessageId
        ))
    if (
      !owner
      || owner.client_request_id !== requestClientId
      || owner.user_message_id !== requestUserMessageId
    ) return false
    if (!canonical) continue

    const interaction = owner.hitl_interactions.find((item) => (
      item.interaction_id === canonical.interaction_id
    ))
    const question = interaction?.requests.find((item) => (
      item.request_id === canonical.request_id
    ))
    if (
      !interaction
      || !question
      || !interaction.request_ids.includes(canonical.request_id)
      || question.message_id !== canonical.message_id
      || question.question_index !== canonical.question_index
      || question.question_count !== canonical.question_count
      || question.prompt !== canonical.prompt
      || question.prompt_type !== canonical.prompt_type
      || question.source !== canonical.source
      || (question.agent_label ?? null) !== (canonical.agent_label ?? null)
      || !stringArraysEqual(question.choices, canonical.choices ?? [])
    ) return false
    canonicalRequests.set(
      hitlRequestKey(canonical.interaction_id, canonical.request_id),
      canonical,
    )
  }

  for (const turn of turns) {
    if (turn.state !== 'awaiting_input' || !turn.active_interaction_id) continue
    const interaction = turn.hitl_interactions.find((item) => (
      item.interaction_id === turn.active_interaction_id
    ))
    if (!interaction || interaction.state !== 'awaiting_input') return false
    for (const requestId of interaction.request_ids) {
      const question = interaction.requests.find((item) => item.request_id === requestId)
      if (!question || question.status !== 'requested') return false
      const request = canonicalRequests.get(hitlRequestKey(
        interaction.interaction_id,
        requestId,
      ))
      if (!request || request.run_id !== turn.run_id) return false
    }
  }
  return true
}

const CANONICAL_HITL_REQUEST_KEYS = [
  'run_id',
  'request_id',
  'message_id',
  'interaction_id',
  'related_user_message_id',
  'client_request_id',
  'question_index',
  'question_count',
  'prompt',
  'prompt_type',
  'source',
  'room_seq',
  'choices',
  'agent_label',
  'room_event_id',
  'parent_event_id',
  'delivery_id',
  'trace_id',
] as const

function canonicalRequestData(
  request: SnapshotData['hitl']['requests'][number],
) {
  const wireRequest: Record<string, unknown> = {}
  for (const key of CANONICAL_HITL_REQUEST_KEYS) {
    if (Object.prototype.hasOwnProperty.call(request, key)) {
      wireRequest[key] = request[key]
    }
  }
  return isCanonicalHITLRequestData(wireRequest) ? wireRequest : null
}

function applySnapshotHitl(
  conversationId: string,
  snapshot: SnapshotData,
  hitlRequestIndex: Map<string, string>,
): void {
  const store = useMessageStore.getState()
  for (const request of snapshot.hitl.requests) {
    const requestId =
      typeof request.request_id === 'string' ? request.request_id : undefined
    const messageId =
      typeof request.message_id === 'string' ? request.message_id : undefined
    if (!requestId || !messageId) continue
    const questionCount = typeof request.question_count === 'number'
      ? request.question_count
      : undefined
    const projectionId = hitlQuestionEntityId(
      messageId,
      typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      requestId,
      questionCount,
    )
    let sourceEntity = store.entities[messageId]
    const projectionEntity = store.entities[projectionId]
    // Snapshot folds add the record timestamp as display metadata. The live
    // canonical validator intentionally accepts only wire fields, so normalize
    // this snapshot-only key before applying the same strict contract.
    const snapshotTs = request.ts
    const canonical = canonicalRequestData(request)
    const requestClientId = typeof request.client_request_id === 'string'
      ? request.client_request_id
      : undefined
    const requestUserMessageId = typeof request.related_user_message_id === 'string'
      ? request.related_user_message_id
      : typeof request.related_message_id === 'string'
        ? request.related_message_id
        : undefined
    const roomTurns = useTurnStore.getState().rooms[conversationId]?.turns
    const owner = canonical
      ? roomTurns?.[canonical.run_id]
      : requestClientId && requestUserMessageId
        ? Object.values(roomTurns ?? {}).find((turn) => (
            turn.clientRequestId === requestClientId
            && turn.userMessageId === requestUserMessageId
          ))
        : undefined
    const ownerMatches = Boolean(owner
      && owner.clientRequestId === requestClientId
      && owner.userMessageId === requestUserMessageId)
    const declaresCanonical = Object.prototype.hasOwnProperty.call(request, 'run_id')
    const claimsCanonicalRoot = canonical !== null
    const existingEntitiesMatch = [sourceEntity, projectionEntity]
      .filter((entity): entity is MessageEntity => entity !== undefined)
      .every((entity) => (
        entity.clientRequestId === requestClientId
        && entity.relatedMessageId === requestUserMessageId
      ))
    if (
      hasCanonicalSnapshotCapability(snapshot)
      && declaresCanonical
      && claimsCanonicalRoot
      && (!ownerMatches || !existingEntitiesMatch)
    ) {
      continue
    }
    if (!sourceEntity && owner && ownerMatches) {
      // Rolling-deploy compatibility: canonical snapshots written before the
      // A2A HITL producer upgrade contain an exact Turn root but legacy field
      // names. Restore the Agent card identity independently from any
      // request-scoped questionnaire projections.
      const waitingAgentLabels = owner.activity.flatMap((item) => (
        item.kind === 'tool'
        && item.executionKind === 'agent'
        && item.status === 'suspended'
        && item.targetName
          ? [item.targetName]
          : []
      ))
      const compatibleAgentLabel = waitingAgentLabels.length === 1
        ? waitingAgentLabels[0]
        : undefined
      store.upsertMessage({
        id: messageId,
        conversationId,
        messageType: 'agent',
        content: '',
        senderName: canonical?.agent_label ?? compatibleAgentLabel ?? 'Agent',
        timestamp: typeof snapshotTs === 'string' ? snapshotTs : new Date().toISOString(),
        clientRequestId: requestClientId,
        relatedMessageId: requestUserMessageId,
        isEphemeral: false,
      }, 'sse')
      sourceEntity = useMessageStore.getState().entities[messageId]
    }
    if (!sourceEntity) continue
    const terminal = ['responded', 'resolved', 'expired', 'canceled', 'error']
      .includes(String(request.status ?? ''))
    const incoming = buildPendingHitlIncomingMessage({
      conversationId,
      messageId,
      requestId,
      source: typeof request.source === 'string' ? request.source : undefined,
      prompt: typeof request.prompt === 'string' ? request.prompt : undefined,
      promptType: typeof request.prompt_type === 'string' ? request.prompt_type : undefined,
      choices: Array.isArray(request.choices)
        ? request.choices.filter((choice): choice is string => typeof choice === 'string')
        : undefined,
      presentation: parseHitlPresentation(request),
      timestamp: typeof snapshotTs === 'string' ? snapshotTs : sourceEntity.timestamp,
      agentId: sourceEntity.agentId,
      agentName: canonical?.agent_label ?? sourceEntity.senderName,
      agentSource: sourceEntity.agentSource,
      expiresAt: typeof request.expires_at === 'string' ? request.expires_at : undefined,
      interactionId: typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      interactionStatus: typeof request.interaction_status === 'string' ? request.interaction_status : undefined,
      interactionVersion: typeof request.interaction_version === 'number' ? request.interaction_version : undefined,
      applicationStatus: typeof request.application_status === 'string' ? request.application_status : undefined,
      groupId: typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      groupTotal: questionCount,
      groupIndex: typeof request.question_index === 'number' ? request.question_index : undefined,
      stepNumber: undefined,
      totalSteps: undefined,
      relatedMessageId: requestUserMessageId,
      clientRequestId: requestClientId,
    })
    store.upsertMessage({ ...incoming, hitlResolved: terminal }, 'sse')
    const requestKey = hitlRequestKey(
      typeof request.interaction_id === 'string' ? request.interaction_id : undefined,
      requestId,
    )
    if (terminal) {
      hitlRequestIndex.delete(requestKey)
      hitlRequestIndex.delete(requestId)
    } else {
      hitlRequestIndex.set(requestKey, incoming.id)
    }
  }
}

/** Snapshot replace: fold every snapshot section into the stores. */
export function applySnapshotToStores(
  conversationId: string,
  snapshot: SnapshotData,
  hitlRequestIndex?: Map<string, string>,
): boolean {
  const advertisesCanonical = (snapshot as { turn_lifecycle_schema?: unknown }).turn_lifecycle_schema === 1
    || Object.prototype.hasOwnProperty.call(snapshot, 'turns')
  if (advertisesCanonical && !hasCanonicalSnapshotCapability(snapshot)) return false
  if (!hasCanonicalSnapshotCapability(snapshot)) {
    applySnapshotMessages(conversationId, snapshot, new Set())
    applySnapshotStreaming(conversationId, snapshot)
    if (hitlRequestIndex) applySnapshotHitl(conversationId, snapshot, hitlRequestIndex)
    return true
  }
  const turns = validateCanonicalSnapshotTurns(snapshot)
  if (!turns || !validateCanonicalSnapshotHitl(conversationId, snapshot, turns)) return false
  const replacement = useTurnStore.getState().replaceSnapshot(conversationId, turns)
  if (!replacement.ok) return false

  const canonicalUserMessageIds = new Set(
    (snapshot.turns ?? []).map((turn) => turn.user_message_id),
  )
  applySnapshotMessages(conversationId, snapshot, canonicalUserMessageIds)
  applySnapshotStreaming(conversationId, snapshot)
  if (hitlRequestIndex) {
    // Canonical awaiting_input restores from durable correlated requests; no
    // recency inference is used by the composer selector.
    applySnapshotHitl(conversationId, snapshot, hitlRequestIndex)
  }
  return true
}
