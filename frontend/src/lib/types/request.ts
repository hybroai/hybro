/**
 * Request types for the backend API.
 */

export interface AgentRegisterRequest {
  agent_url: string;
}
export interface AgentDeleteRequest {
  agent_id: string;
}
export interface InspectionCenterRequest {
  agent_id?: string | null;
  agent_url: string;
}
export interface CreateRoomRequest {
  room_name: string;
  room_owner_name: string;
  extend_info?: {
    [k: string]: unknown;
  } | null;
  membership_seed_input?: "manual" | "saved_group" | "all_current_agents";
  room_agent_ids?: string[];
  seed_group_id?: string;
  seed_all_current_agents?: true;
}

export type ExecutionMode = 'direct' | 'supervisor'

export type AgentScopeInput =
  | { source: 'mention'; agent_ids: [string, ...string[]] }
  | { source: 'room_default' }
  | { source: 'all_agents' }
  | { source: 'saved_group'; group_id: string }

export type SendMessagePayload = {
  message: unknown
  client_request_id: string
  mode: ExecutionMode
  agent_scope: AgentScopeInput
}
