// Agent-related API functions
import type { 
  AgentCenterRequest, 
  AgentCenterResponse,
  InspectionCenterRequest,
} from '@/lib/types'

import { getApiUrl } from '../utils'
import { apiGet, apiPost } from '../api-client'

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
  request: AgentCenterRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentCenterResponse> {
  return apiPost<AgentCenterResponse>(
    `${API_BASE_URL}/registerAgent`,
    request,
    getToken
  )
}

// Get agents from provider_id
export async function getAgentsByProviderId(
  getToken?: () => Promise<string | null>
): Promise<AgentCenterResponse> {
  return apiGet<AgentCenterResponse>(
    `${API_BASE_URL}/getAgent/me`,
    getToken
  )
}

// Delete agent
export async function deleteAgent(
  request: AgentCenterRequest,
  getToken?: () => Promise<string | null>
): Promise<AgentCenterResponse> {
  return apiPost<AgentCenterResponse>(
    `${API_BASE_URL}/deleteAgent`,
    request,
    getToken
  )
}

// ============= PUBLIC ENDPOINTS (No Auth Required) =============

// Get agent card from URL - PUBLIC
export async function getAgentCardFromUrl(
  request: InspectionCenterRequest
): Promise<AgentCenterResponse> {
  return apiPost<AgentCenterResponse>(
    `${API_BASE_URL}/getAgentCardFromUrl`,
    request
  )
}

// Get agent by ID - PUBLIC
export async function getAgent(
  agentId: string,
  signal?: AbortSignal,
  getToken?: () => Promise<string | null>
): Promise<AgentCenterResponse> {
  return apiGet<AgentCenterResponse>(
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
): Promise<AgentCenterResponse> {
  const url = new URL(`${API_BASE_URL}/getAllAgents`)
  if (options.activeOnly) {
    url.searchParams.set('active_only', 'true')
  }
  return apiGet<AgentCenterResponse>(
    url.toString(),
    options.getToken,
    options.signal,
    options.timeoutMs
  )
}
