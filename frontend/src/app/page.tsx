import { redirect } from 'next/navigation'
import { getSession } from '@/lib/session'
import { decodeSupabaseJwt } from '@/lib/session'

export default async function HomePage() {
  const session = await getSession()

  if (!session) {
    redirect('/auth/login')
  }

  // Decode role from JWT to send user to the right starting view
  const jwt = decodeSupabaseJwt(session.access_token)
  const role = jwt?.app_metadata?.role ?? 'corretor'

  if (role === 'dono') {
    redirect('/dashboard/dono')
  } else {
    redirect('/dashboard/corretor')
  }
}
