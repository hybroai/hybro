export interface PublicConfig {
  /** Empty means same origin: the browser calls the API on its own address. */
  api_base_url: string
  api_prefix: string
  server_url: string
  clerk_publishable_key: string
  clerk_sign_in_url: string
  clerk_sign_up_url: string
  clerk_sign_in_fallback_redirect_url: string
  clerk_sign_up_fallback_redirect_url: string
  enable_waitlist: boolean
  max_message_length: number
  inspection_timeout_ms: number
  /** True when the listener is public and MCP is not served. */
  mcp_disabled?: boolean
}

const stringKeys = [
  'api_base_url', 'api_prefix', 'server_url', 'clerk_publishable_key',
  'clerk_sign_in_url', 'clerk_sign_up_url',
  'clerk_sign_in_fallback_redirect_url', 'clerk_sign_up_fallback_redirect_url',
] as const
const numberKeys = ['max_message_length', 'inspection_timeout_ms'] as const
const allowed = new Set<string>([...stringKeys, ...numberKeys, 'enable_waitlist', 'mcp_disabled'])

/** Only the CLI's public projection is accepted, never the full config/auth file. */
export function parsePublicConfig(serialized: string | undefined): Readonly<PublicConfig> {
  const failure = () => new Error('Invalid or missing frontend configuration; start/build through the Hybro CLI.')
  if (!serialized) throw failure()
  let value: unknown
  try { value = JSON.parse(serialized) } catch { throw failure() }
  if (!value || typeof value !== 'object' || Array.isArray(value)) throw failure()
  const record = value as Record<string, unknown>
  if (Object.keys(record).some(key => !allowed.has(key))) throw failure()
  if (stringKeys.some(key => typeof record[key] !== 'string')) throw failure()
  if (numberKeys.some(key => typeof record[key] !== 'number' || !Number.isSafeInteger(record[key]) || (record[key] as number) <= 0)) throw failure()
  if (typeof record.enable_waitlist !== 'boolean') throw failure()
  if (record.mcp_disabled !== undefined && typeof record.mcp_disabled !== 'boolean') throw failure()
  for (const key of ['api_base_url', 'server_url'] as const) {
    if (key === 'api_base_url' && record[key] === '') continue
    try {
      const url = new URL(record[key] as string)
      if (!['http:', 'https:'].includes(url.protocol) || url.username || url.password) throw failure()
    } catch { throw failure() }
  }
  if (!/^\/[A-Za-z0-9/_-]+$/.test(record.api_prefix as string) || (record.api_prefix as string).endsWith('/')) throw failure()
  // Every field was checked above; no unvalidated JSON escapes this boundary.
  return Object.freeze(record) as unknown as Readonly<PublicConfig>
}
