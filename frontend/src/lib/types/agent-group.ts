/**
 * Agent Group Types
 *
 * An agent group is a set of agent IDs with two scopes:
 *   - Saved group: reusable across chats and rooms (persisted via CRUD).
 *   - Room group: room-scoped snapshot that lives with the room.
 *
 * Applying a saved group to a room copies its members into the room snapshot.
 * Later edits to the saved group do not change existing rooms.
 */

// ── Built-in group selection IDs ─────────────────────────────────────────

export const BUILTIN_GROUP_ALL_AGENTS = "all_agents"
/** UI selection id for room membership / room_default dispatch. */
export const BUILTIN_GROUP_ROOM_TEAM = "room_team"

// ── Canonical enums ──────────────────────────────────────────────────────

export type AgentAvailability =
  | "available"
  | "inaccessible"
  | "inactive"
  | "deleted"

// ── Canonical read models ────────────────────────────────────────────────

export interface RoomAgentRef {
  id: string
  name?: string
  availability: AgentAvailability
}

/**
 * Stale agent references: room/group members not found in the current
 * availableAgents catalog.  Rendered as disabled placeholders and
 * merged back on save so they are never silently pruned.
 */
export type StaleAgentRef = RoomAgentRef

// ── Canonical write inputs ───────────────────────────────────────────────

export type RoomMembershipWriteInput =
  | { membership_seed_input: "manual"; room_agent_ids: string[] }
  | { membership_seed_input: "saved_group"; seed_group_id: string }
  | { membership_seed_input: "all_current_agents"; seed_all_current_agents: true }

export type TargetModeDispatchInput =
  | {
      message_target_mode: "room_default"
      target_group_id?: never
      mentioned_agent_ids?: never
    }
  | {
      message_target_mode: "all_agents"
      target_group_id?: never
      mentioned_agent_ids?: never
    }
  | {
      message_target_mode: "saved_group"
      target_group_id: string
      mentioned_agent_ids?: never
    }

export type MentionDispatchInput = {
  mentioned_agent_ids: [string, ...string[]]
  message_target_mode?: never
  target_group_id?: never
}

export type MessageDispatchInput =
  | MentionDispatchInput
  | TargetModeDispatchInput

export type AgentScopeInput =
  import('@/lib/types/request').AgentScopeInput

export function dispatchToAgentScope(dispatch: MessageDispatchInput): AgentScopeInput {
  if (isMentionDispatchInput(dispatch)) {
    return { source: 'mention', agent_ids: dispatch.mentioned_agent_ids }
  }
  if (dispatch.message_target_mode === 'saved_group') {
    return { source: 'saved_group', group_id: dispatch.target_group_id }
  }
  return { source: dispatch.message_target_mode }
}

export function isMentionDispatchInput(dispatch: MessageDispatchInput): dispatch is MentionDispatchInput {
  return Array.isArray(dispatch.mentioned_agent_ids) && dispatch.mentioned_agent_ids.length > 0
}

export function resolveSelectedGroupDispatch(selectedGroup: string): TargetModeDispatchInput {
  switch (selectedGroup) {
    case BUILTIN_GROUP_ROOM_TEAM:
      return { message_target_mode: "room_default" }
    case BUILTIN_GROUP_ALL_AGENTS:
      return { message_target_mode: "all_agents" }
    default:
      return { message_target_mode: "saved_group", target_group_id: selectedGroup }
  }
}

// ── Persisted saved-group model (CRUD) ───────────────────────────────────

export interface AgentGroup {
  group_id: string
  name: string
  description?: string | null
  type: "builtin" | "user"
  owner_id: string | null
  agents: string[]
  created_at?: string
  updated_at?: string
}

export interface AgentGroupCreateRequest {
  name: string
  description?: string
  owner_id: string
  agents: string[]
  /** Stable owner-scoped key for idempotent preset creation. */
  preset_key?: string
}

export interface AgentGroupUpdateRequest {
  group_id: string
  name?: string
  description?: string
  agents?: string[]
}

export interface AgentGroupResponse {
  success: boolean
  group?: AgentGroup
  error?: string
  status_code?: number
}

export interface AgentGroupListResponse {
  success: boolean
  groups?: AgentGroup[]
  error?: string
  status_code?: number
}

// ── Helpers ──────────────────────────────────────────────────────────────

export function isBuiltinGroup(groupId: string): boolean {
  return groupId === BUILTIN_GROUP_ALL_AGENTS || groupId === BUILTIN_GROUP_ROOM_TEAM
}
