// Settings dialog sections that can be opened by URL, so banners can link to them.
export type SettingsSection = 'model-provider'

export const SETTINGS_PARAM = 'settings'

export function settingsHref(pathname: string, section: SettingsSection): string {
  return `${pathname}?${SETTINGS_PARAM}=${section}`
}

export function parseSettingsSection(value: string | null): SettingsSection | null {
  return value === 'model-provider' ? value : null
}

/** The current address with the settings parameter removed, other parameters kept. */
export function withoutSettings(pathname: string, search: string): string {
  const params = new URLSearchParams(search)
  params.delete(SETTINGS_PARAM)
  const query = params.toString()
  return query ? `${pathname}?${query}` : pathname
}
