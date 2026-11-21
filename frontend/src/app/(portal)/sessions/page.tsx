import { Suspense } from 'react'
import type { Metadata } from 'next'
import { SessionsTablePage } from '@/components/sessions/session-table'

export const metadata: Metadata = { title: 'Inbox – Hybro' }

export default function SessionsRoute() {
  return (
    <Suspense fallback={null}>
      <SessionsTablePage />
    </Suspense>
  )
}
