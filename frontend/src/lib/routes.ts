const withId = (base: string, id: string) => `${base}/${encodeURIComponent(id)}`

export const routes = {
  overview: '/',
  about: '/about',
  pricing: '/pricing',
  privacy: '/privacy',
  chat: '/chat',
  room: (id: string) => withId('/room', id),
  sessions: '/sessions',
  session: (id: string) => withId('/sessions', id),
  network: '/network',
  networkWith: (query: string) => (query ? `/network?${query}` : '/network'),
  networkAgent: (id: string) => withId('/network/agents', id),
  registerAgent: '/network/agents/new',
  connectMcp: '/connect/mcp',
  connectApi: '/connect/api',
  settings: '/settings',
  settingsModelProvider: '/settings/model-provider',
  settingsConfiguration: '/settings/configuration',
  settingsSystem: '/settings/system',
} as const
