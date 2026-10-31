import type { AgentGroup } from '@/lib/types/agent-group'
import type { GraphDisplayPrefs } from '@/lib/graph-labels'
import type { AgentInactiveReason } from '@/lib/types/agent'

export interface NetworkAgent {
  id: string
  name: string
  description: string
  skills: string[]
  tags: string[]
  status: 'active' | 'inactive' | 'deleted' | 'unavailable' | 'unknown'
  inactiveReason?: AgentInactiveReason
  iconUrl?: string
  provider?: string
  version?: string
  source?: 'cloud' | 'local' | 'hub' | 'bundled'
  roomCalls: number
}

export interface NetworkFormValues {
  name: string
  description: string
  agentIds: string[]
}

export type NetworkSelection = { type: 'network'; id: string } | { type: 'agent'; id: string }

export interface NetworkCanvasHandle {
  fitAll: () => void
  focusNetwork: (id: string) => void
  zoom: (factor: number) => void
  resetLayout: () => void
}

export interface NetworkCanvasProps {
  networks: AgentGroup[]
  agents: NetworkAgent[]
  selection: NetworkSelection | null
  /** Node keys (`agent:<id>`, `network:<id>`) matching the search; null when no search is active. */
  search: ReadonlySet<string> | null
  display: GraphDisplayPrefs
  onSelect: (selection: NetworkSelection | null) => void
  onZoomChange: (percent: number) => void
}


export interface AgentPickerProps {
  agents: NetworkAgent[]
  value: string[]
  onChange: (ids: string[]) => void
  existingIds?: readonly string[]
  loading: boolean
  error: string | null
  disabled?: boolean
  onRetry: () => void
}

export interface SubnetDialogProps {
  open: boolean
  network?: AgentGroup
  /** Preselected members when creating a subnet from an agent. */
  initialAgentIds?: string[]
  agents: NetworkAgent[]
  agentsLoading: boolean
  agentsError: string | null
  pending: boolean
  onOpenChange: (open: boolean) => void
  onRetryAgents: () => void
  onSave: (values: NetworkFormValues) => Promise<void>
  onDelete?: () => void
}

export interface NetworkDetailsPanelProps {
  selection: { type: 'network'; network: AgentGroup } | { type: 'agent'; agent: NetworkAgent }
  networks: readonly AgentGroup[]
  agents: NetworkAgent[]
  pending: boolean
  className?: string
  onClose: () => void
  onManage: () => void
  onDelete: () => void
  onSetMembers: (networkId: string, agentIds: string[]) => Promise<unknown>
  onCreateWithAgent: (agentId: string) => void
}
