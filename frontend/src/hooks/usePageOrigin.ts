'use client'

import { useSyncExternalStore } from 'react'

const subscribe = () => () => {}

/** This page's origin; empty during server rendering, where there is no page. */
export function usePageOrigin(): string {
  return useSyncExternalStore(subscribe, () => window.location.origin, () => '')
}
