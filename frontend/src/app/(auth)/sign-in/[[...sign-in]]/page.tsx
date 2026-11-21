import { SignInForm } from '@/components/auth/sign-in-form'
import { redirectParam } from '@/lib/navigation'

export default async function Page ({
  searchParams,
}: {
  searchParams: Promise<{ redirect_url?: string | string[] }>
}) {
  const { redirect_url } = await searchParams
  return <SignInForm redirectUrl={redirectParam(redirect_url)} />
}
