'use client'

import { useParams } from 'next/navigation'
import { SessionRecordView } from '@/components/sessions/session-record-view'

export default function SessionRoute() {
  const params = useParams<{ id: string }>()
  return (
    <SessionRecordView key={params.id} sessionId={params.id} />
  )
}
