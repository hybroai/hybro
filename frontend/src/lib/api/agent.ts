// Agent-related API functions
import type { 
  AgentRegisterRequest,
  AgentDeleteRequest,
  AgentRouteResponse,
  InspectionCenterRequest,
} from '@/lib/types'

import { getApiUrl } from '../utils'
import { ApiError, apiGet, apiPost } from '../api-client'

const API_BASE_URL = getApiUrl('agent')
const LOCAL_AGENTS_BASE_URL = getApiUrl('local-agents')

export interface LocalAgentDiscoveryResult {
  trigger: 'startup' | 'scheduled' | 'manual'
  open_ports: number
  agents_found: number
  agents_added: number
  agents_reactivated: number
  agents_deactivated: number
  duration_ms: number
  reused_running_discovery: boolean
}

// ============= PROTECTED ENDPOINTS (Auth Required) =============

// Discover A2A agents running on the Docker host
export async function discoverLocalAgents(
  getToken?: () => Promise<string | null>
): Promise<LocalAgentDiscoveryResult> {
  return apiPost<LocalAgentDiscoveryResult>(
    `${LOCAL_AGENTS_BASE_URL}/discovery`,
    {},
    getToken
  )
}

// Register agent
export async function registerAgent(
  request: AgentRegisterRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentRouteResponse> {
  return apiPost<AgentRouteResponse>(
    `${API_BASE_URL}/registerAgent`,
    request,
    getToken
  )
}

/**
 * The reason a registration was refused with HTTP 409, from the backend's
 * `{detail: {code, message}}` conflict body; undefined for any other error.
 */
export function registrationConflictMessage(error: unknown): string | undefined {
  if (!(error instanceof ApiError) || error.status !== 409) return undefined
  const details = error.details
  if (typeof details !== 'object' || details === null || !('detail' in details)) return undefined
  const detail = details.detail
  if (typeof detail !== 'object' || detail === null || !('message' in detail)) return undefined
  return typeof detail.message === 'string' && detail.message ? detail.message : undefined
}

// Get agents from provider_id
export async function getAgentsByProviderId(
  getToken?: () => Promise<string | null>
): Promise<AgentRouteResponse> {
  return apiGet<AgentRouteResponse>(
    `${API_BASE_URL}/getAgent/me`,
    getToken
  )
}

// Delete agent
export async function deleteAgent(
  request: AgentDeleteRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentRouteResponse> {
  return apiPost<AgentRouteResponse>(
    `${API_BASE_URL}/deleteAgent`,
    request,
    getToken
  )
}

// ============= PUBLIC ENDPOINTS (No Auth Required) =============

// Get agent card from URL - PUBLIC
export async function getAgentCardFromUrl(
  request: InspectionCenterRequest
): Promise<AgentRouteResponse> {
  return apiPost<AgentRouteResponse>(
    `${API_BASE_URL}/getAgentCardFromUrl`,
    request
  )
}

// Get agent by ID - PUBLIC
export async function getAgent(
  agentId: string,
  signal?: AbortSignal,
  getToken?: () => Promise<string | null>
): Promise<AgentRouteResponse> {
  return apiGet<AgentRouteResponse>(
    `${API_BASE_URL}/getAgent/${agentId}`,
    getToken,
    signal
  )
}

export interface GetAllAgentsOptions {
  activeOnly?: boolean
  signal?: AbortSignal
  timeoutMs?: number
  getToken?: () => Promise<string | null>
}

// Get visible agents, optionally filtering to active agents.
export async function getAllAgents(
  options: GetAllAgentsOptions = {}
): Promise<AgentRouteResponse> {
  // A relative same-origin base cannot go through `new URL`.
  const query = options.activeOnly ? '?active_only=true' : ''
  return apiGet<AgentRouteResponse>(
    `${API_BASE_URL}/getAllAgents${query}`,
    options.getToken,
    options.signal,
    options.timeoutMs
  )
}
