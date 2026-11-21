import { Suspense } from 'react'
import type { Metadata } from 'next'
import { ConnectPage } from '@/components/connect/connect-page'

export const metadata: Metadata = { title: 'Access – Hybro' }

export default function ConnectSourcesPage() {
  return <Suspense fallback={null}><ConnectPage key="sources" kind="sources" /></Suspense>
}
