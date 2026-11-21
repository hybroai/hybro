import type { Metadata } from 'next'
import { SetupForm } from '@/components/auth/setup-form'

export const metadata: Metadata = { title: 'Set up – Hybro' }

export default function Page () {
  return <SetupForm />
}
