// A per-browser dismissal: one value in localStorage under its own key, with
// listeners so every banner that reads it updates at once. Storage can be
// unavailable (private mode); a banner then stays visible.

export interface DismissalStore {
  subscribe: (listener: () => void) => () => void
  read: () => string | null
  write: (value: string | null) => void
}

export function createDismissalStore(key: string): DismissalStore {
  const listeners = new Set<() => void>()
  return {
    subscribe(listener) {
      listeners.add(listener)
      window.addEventListener('storage', listener)
      return () => {
        listeners.delete(listener)
        window.removeEventListener('storage', listener)
      }
    },
    read() {
      try {
        return window.localStorage.getItem(key)
      } catch {
        return null
      }
    },
    write(value) {
      try {
        if (value === null) window.localStorage.removeItem(key)
        else window.localStorage.setItem(key, value)
      } catch {
        // Unavailable storage: the banner stays visible.
      }
      listeners.forEach(listener => listener())
    },
  }
}
