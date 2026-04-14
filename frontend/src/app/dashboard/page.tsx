import { redirect } from 'next/navigation'
import { headers } from 'next/headers'
import { decodeSupabaseJwt } from '@/lib/session'

/** Role-based redirect from /dashboard root. */
export default async function DashboardRootPage() {
  const headersList = await headers()
  const accessToken = headersList.get('x-access-token')

  if (!accessToken) redirect('/auth/login')

  const jwt = decodeSupabaseJwt(accessToken)
  const role = jwt?.app_metadata?.role ?? 'corretor'

  redirect(role === 'dono' ? '/dashboard/dono' : '/dashboard/corretor')
}
