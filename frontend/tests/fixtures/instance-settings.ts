import type { SettingsField, SettingsView } from '@/lib/api/settings'

// A small slice of the backend registry; values are invented fixtures.
const base = {
  options: null,
  nullable: false,
  minimum: null,
  maximum: null,
  exclusive_minimum: null,
  source: 'default',
  restart_pending: false,
} as const

export const FIELDS: SettingsField[] = [
  { ...base, key: 'backend.approval_deadline_hours', group: 'runs_agents', level: 'basic', apply: 'restart', label: 'Approval deadline (hours)', description: 'How long a Run waits for your approval; empty means no deadline.', kind: 'number', nullable: true, exclusive_minimum: 0, maximum: 87600, default: null, value: null },
  { ...base, key: 'backend.agent_health_check_interval', group: 'runs_agents', level: 'basic', apply: 'restart', label: 'Health check interval (seconds)', description: 'Time between agent health checks.', kind: 'integer', exclusive_minimum: 0, default: 3600, value: 1800, source: 'config' },
  { ...base, key: 'backend.local_agent_discovery_enabled', group: 'discovery', level: 'basic', apply: 'restart', label: 'Discover local agents', description: 'Find A2A agents running on this computer’s ports.', kind: 'boolean', default: false, value: false },
  { ...base, key: 'backend.log_level', group: 'logging', level: 'basic', apply: 'live', label: 'Log level', description: 'Least severe backend log lines written. Applies immediately.', kind: 'enum', options: ['DEBUG', 'INFO', 'WARNING', 'ERROR', 'CRITICAL'], default: 'INFO', value: 'INFO' },
  { ...base, key: 'backend.llm_gateway_max_attempts', group: 'advanced', level: 'advanced', apply: 'restart', label: 'Model call attempts', description: 'Attempts per model request, including the first.', kind: 'integer', minimum: 1, default: 2, value: 2 },
]

export function settingsView(overrides: Partial<SettingsView> = {}, fields: SettingsField[] = FIELDS): SettingsView {
  return {
    revision: 'rev-1',
    config_error: null,
    restart_pending: false,
    restart: { available: true, state: 'idle', error: null, updated_at: null },
    groups: [
      { id: 'runs_agents', label: 'Runs & agents' },
      { id: 'discovery', label: 'Local agent discovery' },
      { id: 'chat_memory', label: 'Chat & memory' },
      { id: 'logging', label: 'Logging' },
      { id: 'advanced', label: 'Advanced' },
    ],
    fields,
    notices: [],
    system: {
      version: '0.4.7',
      deployment: 'image',
      port: 3100,
      listen: 'loopback',
      auth_mode: 'mock',
      mcp_available: true,
      config_location: '~/.hybro/config.json',
    },
    ...overrides,
  }
}
