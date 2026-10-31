import { describe, expect, it } from 'vitest'
import { parseSettingsSection, settingsHref, withoutSettings } from '@/lib/settings-url'

describe('settings URLs', () => {
  it('links a section on the current page', () => {
    expect(settingsHref('/room/r%201', 'model-provider')).toBe('/room/r%201?settings=model-provider')
  })

  it('accepts only known sections', () => {
    expect(parseSettingsSection('model-provider')).toBe('model-provider')
    expect(parseSettingsSection('profile')).toBeNull()
    expect(parseSettingsSection(null)).toBeNull()
  })

  it('drops only the settings parameter when the dialog closes', () => {
    expect(withoutSettings('/chat', '?settings=model-provider')).toBe('/chat')
    expect(withoutSettings('/network', '?view=map&settings=model-provider')).toBe('/network?view=map')
  })
})
