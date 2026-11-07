import { describe, expect, it } from 'vitest'
import { http, HttpResponse } from 'msw'
import { server } from '../../setup/msw-server'
import { getApiUrl } from '@/lib/utils'
import { getSettings, restartServices, saveSettings, settingsError, type SettingsField } from '@/lib/api/settings'
import { formatValue, parseDraft, pendingChanges, toDraft } from '@/lib/instance-settings'

function field(overrides: Partial<SettingsField>): SettingsField {
  return {
    key: 'backend.agent_health_check_interval',
    group: 'runs_agents',
    level: 'basic',
    apply: 'restart',
    label: 'Health check interval (seconds)',
    description: 'Time between agent health checks.',
    kind: 'integer',
    options: null,
    nullable: false,
    minimum: null,
    maximum: null,
    exclusive_minimum: 0,
    default: 3600,
    value: 3600,
    source: 'default',
    restart_pending: false,
    ...overrides,
  }
}

describe('settings API client', () => {
  it('reads the settings view', async () => {
    server.use(http.get(getApiUrl('settings'), () => HttpResponse.json({ revision: 'r1' })))
    await expect(getSettings()).resolves.toEqual({ revision: 'r1' })
  })

  it('saves values with the revision it read', async () => {
    let body: unknown
    server.use(http.put(getApiUrl('settings'), async ({ request }) => {
      body = await request.json()
      return HttpResponse.json({ changed: ['backend.log_level'], restart_required: false, settings: {} })
    }))
    await saveSettings('r1', { 'backend.log_level': 'DEBUG' })
    expect(body).toEqual({ revision: 'r1', values: { 'backend.log_level': 'DEBUG' } })
  })

  it('asks for a restart with a JSON body', async () => {
    let contentType: string | null = null
    let body: unknown
    server.use(http.post(getApiUrl('settings/restart'), async ({ request }) => {
      contentType = request.headers.get('content-type')
      body = await request.json()
      return HttpResponse.json({ restart: { available: true, state: 'requested' } }, { status: 202 })
    }))
    await expect(restartServices()).resolves.toEqual({ restart: { available: true, state: 'requested' } })
    expect(contentType).toMatch(/^application\/json/)
    expect(body).toEqual({})
  })

  it('reads the error body with its field errors', async () => {
    server.use(http.put(getApiUrl('settings'), () => HttpResponse.json({
      detail: 'Some settings are invalid.',
      code: 'invalid_settings',
      errors: [{ field: 'backend.local_agent_discovery_port_end', code: 'value_error', message: 'Must not be below the first port.' }],
    }, { status: 422 })))
    const failure = await saveSettings('r1', {}).catch((error: unknown) => error)
    expect(settingsError(failure)).toEqual({
      status: 422,
      detail: 'Some settings are invalid.',
      code: 'invalid_settings',
      errors: [{ field: 'backend.local_agent_discovery_port_end', code: 'value_error', message: 'Must not be below the first port.' }],
    })
    expect(settingsError(new Error('network'))).toBeNull()
  })
})

describe('settings drafts', () => {
  it('shows numbers and empty values as input text', () => {
    expect(toDraft(field({ value: 1800 }))).toBe('1800')
    expect(toDraft(field({ kind: 'number', nullable: true, value: null }))).toBe('')
    expect(toDraft(field({ kind: 'boolean', value: true }))).toBe(true)
  })

  it('parses numbers and checks the bounds the backend reports', () => {
    const interval = field({})
    expect(parseDraft(interval, ' 600 ')).toEqual({ value: 600 })
    expect(parseDraft(interval, '0')).toEqual({ error: 'Must be greater than 0.' })
    expect(parseDraft(interval, '1.5')).toEqual({ error: 'Enter a whole number.' })
    expect(parseDraft(interval, 'soon')).toEqual({ error: 'Enter a number.' })
    expect(parseDraft(field({ minimum: 1, maximum: 65535, exclusive_minimum: null }), '70000')).toEqual({ error: 'Must be at most 65535.' })
    expect(parseDraft(field({ minimum: 1, exclusive_minimum: null }), '0')).toEqual({ error: 'Must be at least 1.' })
    expect(parseDraft(field({ kind: 'number' }), '2.5')).toEqual({ value: 2.5 })
  })

  it('turns an empty number or a reset into null, which returns the field to its default', () => {
    expect(parseDraft(field({}), '')).toEqual({ value: null })
    expect(parseDraft(field({ kind: 'boolean' }), null)).toEqual({ value: null })
    expect(pendingChanges([field({ value: 1800, source: 'config' })], { 'backend.agent_health_check_interval': null }).values)
      .toEqual({ 'backend.agent_health_check_interval': null })
    expect(pendingChanges([field({})], { 'backend.agent_health_check_interval': null }).keys).toEqual([])
  })

  it('keeps text, options and switches as they are', () => {
    expect(parseDraft(field({ kind: 'url', default: '', value: '' }), ' https://hybro.example.com ')).toEqual({ value: 'https://hybro.example.com' })
    expect(parseDraft(field({ kind: 'enum', options: ['DEBUG', 'INFO'] }), 'DEBUG')).toEqual({ value: 'DEBUG' })
    expect(parseDraft(field({ kind: 'boolean', default: true, value: true }), false)).toEqual({ value: false })
  })

  it('collects only real changes, with errors and the fields that need a restart', () => {
    const interval = field({ value: 1800, source: 'config' })
    const level = field({ key: 'backend.log_level', kind: 'enum', apply: 'live', options: ['DEBUG', 'INFO'], default: 'INFO', value: 'INFO' })
    const port = field({ key: 'backend.local_agent_discovery_port_start', minimum: 1, exclusive_minimum: null, default: 9000, value: 9000 })
    const changes = pendingChanges([interval, level, port], {
      [interval.key]: '1800.0',
      [level.key]: 'DEBUG',
      [port.key]: '0',
    })
    expect(changes.values).toEqual({ 'backend.log_level': 'DEBUG' })
    expect(changes.errors).toEqual({ 'backend.local_agent_discovery_port_start': 'Must be at least 1.' })
    expect(changes.keys).toEqual(['backend.log_level', 'backend.local_agent_discovery_port_start'])
    expect(changes.restartKeys).toEqual(['backend.local_agent_discovery_port_start'])
  })

  it('formats values for people', () => {
    expect(formatValue(field({ kind: 'boolean' }), true)).toBe('On')
    expect(formatValue(field({ kind: 'boolean' }), false)).toBe('Off')
    expect(formatValue(field({ kind: 'number', nullable: true }), null)).toBe('none')
    expect(formatValue(field({ kind: 'url' }), '')).toBe('empty')
    expect(formatValue(field({}), 3600)).toBe('3600')
  })
})
