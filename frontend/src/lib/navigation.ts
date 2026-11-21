/** A full page load, so every provider starts again from the new sign-in state. */
export function goTo(url: string): void {
  window.location.assign(url)
}

// Any fixed origin works: only whether the target stays on it matters.
const SAME_SITE = 'http://hybro.invalid'

/**
 * A same-site path to return to after sign-in; anything else becomes `/`.
 * The URL parser decides, because browsers drop tabs and line breaks and read
 * a backslash as a slash, so `/\t/evil.example` leaves the site.
 */
export function safeRedirect(target: string | null | undefined): string {
  if (!target || !target.startsWith('/')) return '/'
  let url: URL
  try {
    url = new URL(target, SAME_SITE)
  } catch {
    return '/'
  }
  if (url.origin !== SAME_SITE) return '/'
  return `${url.pathname}${url.search}${url.hash}`
}

/** The first `redirect_url` value; a repeated parameter arrives as a list. */
export function redirectParam(value: string | string[] | undefined): string {
  const first = Array.isArray(value) ? value[0] : value
  return first || '/'
}
