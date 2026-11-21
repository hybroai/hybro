import type { SessionAgent, SessionClient } from '@/lib/api/session'

/** Who sent the session: its client, which the token proves. */
export function sessionClientName(session: { client: SessionClient }): string {
  return session.client.name
}

/**
 * The name the client gave itself, shown beside the client and never instead
 * of it; null when there is none or it only repeats the client's name.
 */
export function sessionClientLabel(session: { client_label: string | null, client: SessionClient }): string | null {
  const label = session.client_label?.trim()
  return label && label !== session.client.name ? label : null
}

// Session times are always shown in English, whatever the browser locale.
const LOCALE = 'en-US'

export function formatSessionTime(value: string | null | undefined, timeZone?: string): string {
  if (!value) return ''
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return ''
  return new Intl.DateTimeFormat(LOCALE, {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
    timeZone,
  })
    .format(date)
    .replace(/ /g, ' ')
}

export function formatSessionDuration(ms: number | null | undefined): string {
  if (ms == null || !Number.isFinite(ms) || ms < 0) return ''
  const seconds = Math.round(ms / 1000)
  if (seconds < 60) return `${seconds}s`
  const minutes = Math.floor(seconds / 60)
  const rest = seconds % 60
  if (minutes < 60) return rest ? `${minutes}m ${rest}s` : `${minutes}m`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}


export function sessionAgentNames(agents: readonly SessionAgent[]): string {
  return agents.map(agent => agent.name || agent.agent_id).join(', ')
}
