/**
 * Response types for the backend API.
 *
 * A2A protocol types (AgentCard, Task, Message, Part, etc.) are imported from
 * the canonical @a2a-js/sdk package.  Only backend-specific response wrappers
 * and orchestration types are defined here.
 */

import type {
  AgentCard,
  Task,
} from '@a2a-js/sdk'

// ── Types NOT in SDK: kept here as they originate from the backend ────────

export type { Agent, AgentRouteResponse } from './agent';

export interface InsepectionCenterConnectionValidationResponse {
  agent_url: string;
  agent_card?: AgentCard | null;
  is_valid: boolean;
  result?: string[] | null;
  status_code?: number;
}
export interface InspectionCenterResponse {
  agent_url: string;
  agent_card?: AgentCard | null;
  result: string[];
  status_code?: number;
}
export interface MessageContent {
  message_text?: string | null;
  message_task?: Task | null;
  [k: string]: unknown;
}
export interface Room {
  conversation_id?: string;
  room_name: string;
  room_owner_id: string;
  room_owner_name: string;
  room_agent_set?: {
    [k: string]: string;
  };
  room_created_at?: string;
  extend_info?: unknown;

  // ── Canonical provenance fields ────────────────────────────────────────
  membership_origin?: "manual" | "saved_group" | "all_current_agents";
  membership_origin_status?: "seeded_never_edited" | "seeded_edited" | "manual";
  source_group_id?: string | null;
  source_group_name?: string | null;
}
export interface RoomCenterRoomMessageResponse {
  conversation_id?: string | null;
  message_list?: RoomMessage[] | null;
  has_more?: boolean | null;
  next_cursor?: string | null;
  success: boolean;
  error?: string | null;
  status_code?: number;
}
/**
 * Unified room message format for both user and agent messages
 */
export interface RoomMessage {
  conversation_id: string;
  message_id: string;
  client_request_id?: string | null;
  message_created_at?: string;
  message_type: string;
  user_id?: string | null;
  agent_id?: string | null;
  related_message_id?: string | null;
  message_content: MessageContent;
  // Step tracking from task decomposition (1-indexed) - for agent messages
  step_number?: number | null;
  total_steps?: number | null;
  // Task timestamp for staleness detection (only set for agent messages with tasks)
  task_updated_at?: string | null;
  // Task description being processed (only set for agent messages with tasks)
  task_content?: string | null;
  /** Persisted quote snapshot id on user messages (QUOTE_REPLY). */
  quote_id?: string | null;
  extend_info?: unknown;
}
export interface RoomCenterRoomSettingResponse {
  conversation_id?: string | null;
  room_agent_set?: string[] | null;
  resolved_agents?: RoomAgentRefWire[] | null;
  room_default_status?: "ok" | "degraded" | "empty" | "all_unavailable" | null;
  active_runs?: ActiveRunRefWire[] | null;
  room?: Room | null;
  room_list?: Room[] | null;
  success: boolean;
  error?: string | null;
  status_code?: number;
}
export interface ActiveRunRefWire {
  state: string;
  trigger_message_id?: string | null;
  agent_id?: string | null;
  updated_at?: string | null;
}
export interface RoomCenterActiveRunsResponse {
  conversation_id?: string | null;
  active_runs?: ActiveRunRefWire[] | null;
  success: boolean;
  error?: string | null;
  status_code?: number;
}
export interface RoomAgentRefWire {
  id: string;
  name?: string | null;
  availability: "available" | "inaccessible" | "inactive" | "deleted";
}
export interface ScopeResolutionErrorWire {
  code: "invalid_target" | "group_not_usable" | "unauthorized_mention" | "empty_scope";
  message: string;
}
export interface RoomCenterUserMessageResponse {
  conversation_id?: string | null;
  message_id?: string | null;
  dispatch_root_message_id?: string | null;
  user_id?: string | null;
  user_name?: string | null;
  message?: RoomUserMessage | null;
  message_list?: RoomUserMessage[] | null;
  scope_resolution_error?: ScopeResolutionErrorWire | null;
  success: boolean;
  error?: string | null;
  status_code?: number;
}
export interface RoomUserMessage {
  conversation_id: string;
  message_id: string;
  client_request_id?: string | null;
  message_created_at?: string;
  message_type?: string;
  user_id?: string | null;
  agent_id?: string | null;
  related_message_id?: string | null;
  /** Persisted quote snapshot id (QUOTE_REPLY). */
  quote_id?: string | null;
  message_content: MessageContent;
  extend_info?: unknown;
}
