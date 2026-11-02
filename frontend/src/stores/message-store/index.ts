import { create } from 'zustand'
import { subscribeWithSelector } from 'zustand/middleware'
import { isTerminalState } from '@/lib/types/sse'
import type { TaskState } from '@/lib/types/sse'
import type { MessageEntity, IncomingMessage, MessageSource } from './types'
import { applyUpsert, buildSortedIds } from './upsert'
import { resolveDisplayType } from './resolve-display-type'

export type { MessageEntity, IncomingMessage, MessageSource } from './types'
export type { DisplayType } from './types'
export { resolveDisplayType } from './resolve-display-type'
export { detectAndMarkStaleTasks } from './stale-detection'
export { filterHydrationMessages } from './hydration-filter'
export { convertApiMessageToIncoming } from './convert-api-message'
export type { ConvertApiMessageOptions } from './convert-api-message'

interface MessageStoreState {
  // ── Per-room entity storage ───────────────────────────────
  entities: Record<string, MessageEntity>
  orderedIds: string[]
  conversationId: string | null

  // ── Sync metadata ────────────────────────────────────────
  hydratedFromDb: boolean
  lastDbSyncAt: number | null
  version: number

  // ── Write operations ─────────────────────────────────────
  upsertMessage: (msg: IncomingMessage, source: MessageSource) => void
  upsertMany: (msgs: IncomingMessage[], source: MessageSource) => ReadonlySet<string>
  replaceMessageId: (oldId: string, newId: string) => void
  replaceAndPatchMessageId: (
    oldId: string,
    newId: string,
    patch: Partial<Pick<MessageEntity, 'content' | 'attachments' | 'clientRequestId' | 'userId' | 'timestamp' | 'quoteId'>>
  ) => void
  removeMessage: (id: string) => void
  cancelAllNonTerminal: (conversationId: string) => void
  nudgeSyncBridge: () => void
  setRoom: (conversationId: string) => void
  clearRoom: () => void
  markDbSynced: () => void
}

const INITIAL_STATE = {
  entities: {} as Record<string, MessageEntity>,
  orderedIds: [] as string[],
  conversationId: null as string | null,
  hydratedFromDb: false,
  lastDbSyncAt: null as number | null,
  version: 0,
}

export const useMessageStore = create<MessageStoreState>()(
  subscribeWithSelector((set, get) => ({
    ...INITIAL_STATE,

    upsertMessage: (incoming, source) => {
      set((state) => {
        const result = applyUpsert(state.entities, state.orderedIds, incoming, source)
        if (!result) return state

        const newOrderedIds = result.idsChanged
          ? buildSortedIds(result.entities)
          : state.orderedIds

        return {
          entities: result.entities,
          orderedIds: newOrderedIds,
          version: state.version + 1,
        }
      })
    },

    upsertMany: (msgs, source) => {
      // Determine which IDs will actually be written before calling set(),
      // so callers can safely use the returned set for downstream operations
      // (e.g. clearing streaming buffers only for truly-applied messages).
      //
      // INVARIANT: there must be NO await between get() and set() below.
      // Both calls are synchronous and JavaScript is single-threaded, so no
      // other Zustand action can mutate state in between. Any future refactor
      // that adds an await here would break the guarantee that appliedIds
      // matches what set() actually applies.
      const appliedIds = new Set<string>()
      const currentState = get()
      let entities = currentState.entities
      for (const incoming of msgs) {
        const result = applyUpsert(entities, currentState.orderedIds, incoming, source)
        if (result) {
          appliedIds.add(incoming.id)
          entities = result.entities
        }
      }

      if (appliedIds.size > 0) {
        set((state) => {
          let newEntities = { ...state.entities }
          let idsChanged = false

          for (const incoming of msgs) {
            const result = applyUpsert(newEntities, state.orderedIds, incoming, source)
            if (result) {
              newEntities = result.entities
              idsChanged = idsChanged || result.idsChanged
            }
          }

          const newOrderedIds = idsChanged
            ? buildSortedIds(newEntities)
            : state.orderedIds

          return {
            entities: newEntities,
            orderedIds: newOrderedIds,
            version: state.version + 1,
          }
        })
      }

      return appliedIds
    },

    replaceMessageId: (oldId, newId) => set((state) => {
      if (!oldId || !newId || oldId === newId) return state
      const oldEntity = state.entities[oldId]
      if (!oldEntity) return state

      const newEntity = state.entities[newId]
      const mergedEntity = newEntity
        ? {
            ...oldEntity,
            ...newEntity,
            id: newId,
            // Preserve optimistic correlation metadata if the newer entity
            // was created from SSE and omitted it.
            clientRequestId: newEntity.clientRequestId ?? oldEntity.clientRequestId,
            turnTerminalStatus: newEntity.turnTerminalStatus ?? oldEntity.turnTerminalStatus,
            sourceVersion: Math.max(newEntity.sourceVersion, oldEntity.sourceVersion) + 1,
            updatedAt: Date.now(),
          }
        : {
            ...oldEntity,
            id: newId,
            sourceVersion: oldEntity.sourceVersion + 1,
            updatedAt: Date.now(),
          }

      const entities = { ...state.entities }
      delete entities[oldId]
      entities[newId] = mergedEntity

      // Rewire related-message links pointing at the optimistic id.
      for (const [id, entity] of Object.entries(entities)) {
        if (entity.relatedMessageId === oldId) {
          entities[id] = {
            ...entity,
            relatedMessageId: newId,
            sourceVersion: entity.sourceVersion + 1,
            updatedAt: Date.now(),
          }
        }
      }

      const orderedIds = state.orderedIds
        .map(id => (id === oldId ? newId : id))
        .filter((id, idx, arr) => arr.indexOf(id) === idx)

      return {
        entities,
        orderedIds,
        version: state.version + 1,
      }
    }),

    /**
     * Atomically replaces an optimistic message ID with the real server ID and
     * applies a patch (e.g. server-resolved attachment URLs) in a single state
     * update, avoiding the two-render flash caused by separate upsert+replace
     * calls.
     *
     * If an SSE event has already created an entity for `newId` (race
     * condition), the SSE entity wins and is merged with the optimistic one so
     * that SSE-provided data is preserved.
     */
    replaceAndPatchMessageId: (oldId, newId, patch) => set((state) => {
      if (!oldId || !newId || oldId === newId) return state
      const oldEntity = state.entities[oldId]
      if (!oldEntity) return state

      const existingAtNewId = state.entities[newId]
      const merged: MessageEntity = existingAtNewId
        // SSE already wrote the real entity — merge optimistic into SSE (SSE wins).
        ? {
            ...oldEntity,
            ...existingAtNewId,
            id: newId,
            clientRequestId: existingAtNewId.clientRequestId ?? oldEntity.clientRequestId,
            turnTerminalStatus: existingAtNewId.turnTerminalStatus ?? oldEntity.turnTerminalStatus,
            sourceVersion: Math.max(existingAtNewId.sourceVersion, oldEntity.sourceVersion) + 1,
            updatedAt: Date.now(),
          }
        // Normal case — apply patch from the HTTP response in one shot.
        : {
            ...oldEntity,
            ...patch,
            id: newId,
            sourceVersion: oldEntity.sourceVersion + 1,
            updatedAt: Date.now(),
          }

      const entities = { ...state.entities }
      delete entities[oldId]
      entities[newId] = merged

      // Rewire related-message links pointing at the optimistic id.
      for (const [id, entity] of Object.entries(entities)) {
        if (entity.relatedMessageId === oldId) {
          entities[id] = {
            ...entity,
            relatedMessageId: newId,
            sourceVersion: entity.sourceVersion + 1,
            updatedAt: Date.now(),
          }
        }
      }

      const orderedIds = state.orderedIds
        .map(id => (id === oldId ? newId : id))
        .filter((id, idx, arr) => arr.indexOf(id) === idx)

      return { entities, orderedIds, version: state.version + 1 }
    }),

    removeMessage: (id) => set((state) => {
      if (!state.entities[id]) return state
      // eslint-disable-next-line @typescript-eslint/no-unused-vars
      const { [id]: _removed, ...rest } = state.entities
      return {
        entities: rest,
        orderedIds: state.orderedIds.filter(oid => oid !== id),
        version: state.version + 1,
      }
    }),

    /**
     * Batch-cancel all non-terminal tasks in a room.
     * Uses resolveDisplayType instead of hardcoding displayType (Gap 19).
     */
    cancelAllNonTerminal: (conversationId) => set((state) => {
      let changed = false
      const newEntities = { ...state.entities }

      for (const [id, entity] of Object.entries(newEntities)) {
        if (
          entity.conversationId === conversationId &&
          entity.taskStatus &&
          !isTerminalState(entity.taskStatus) &&
          !entity.isEphemeral
        ) {
          newEntities[id] = {
            ...entity,
            taskStatus: 'canceled' as TaskState,
            displayType: resolveDisplayType({
              messageType: entity.messageType,
            }),
            sourceVersion: entity.sourceVersion + 1,
            updatedAt: Date.now(),
          }
          changed = true
        }
      }

      return changed
        ? { entities: newEntities, version: state.version + 1 }
        : state
    }),

    nudgeSyncBridge: () => set((state) => ({
      version: state.version + 1,
    })),

    setRoom: (conversationId) => set({
      ...INITIAL_STATE,
      conversationId,
    }),

    clearRoom: () => set({
      ...INITIAL_STATE,
    }),

    markDbSynced: () => set({
      hydratedFromDb: true,
      lastDbSyncAt: Date.now(),
    }),
  }))
)
