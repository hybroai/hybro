import { Suspense } from 'react'
import type { Metadata } from 'next'
import { ConnectPage } from '@/components/connect/connect-page'

export const metadata: Metadata = { title: 'Access – Hybro' }

export default function ConnectClientsPage() {
  return <Suspense fallback={null}><ConnectPage key="clients" kind="clients" /></Suspense>
}
