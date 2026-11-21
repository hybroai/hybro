const LOOPBACK_HOSTS = new Set(['localhost', '[::1]', '::1'])

function isThisMachine(hostname: string): boolean {
  const host = hostname.toLowerCase()
  return LOOPBACK_HOSTS.has(host) || host.endsWith('.localhost') || /^127(\.\d{1,3}){3}$/.test(host)
}

/**
 * True when a password typed on this page would cross the network unencrypted:
 * the page is plain HTTP and its host is not this machine.
 */
export function isPlainHttpFromAnotherMachine(location: Pick<Location, 'protocol' | 'hostname'>): boolean {
  return location.protocol === 'http:' && !isThisMachine(location.hostname)
}
