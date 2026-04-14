import { redirect } from 'next/navigation'
import { headers } from 'next/headers'
import { decodeSupabaseJwt } from '@/lib/session'
import NavBar from '@/components/NavBar'
import SessionGuard from '@/components/SessionGuard'

/**
 * Dashboard layout — Server Component.
 *
 * Reads the access_token injected by middleware (x-access-token header),
 * decodes role from the Supabase JWT, and passes it down to the NavBar
 * and SessionGuard (client component that mounts useRefreshSession).
 *
 * Does NOT fetch the profile from admin_api here — each page SSR fetches
 * what it needs. This layout is intentionally lean.
 */
export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const headersList = await headers()
  const accessToken = headersList.get('x-access-token')
  const expiresAt = Number(headersList.get('x-expires-at') ?? '0')

  if (!accessToken) {
    redirect('/auth/login')
  }

  const jwt = decodeSupabaseJwt(accessToken)
  const role = jwt?.app_metadata?.role ?? 'corretor'
  const email = jwt?.email ?? ''

  return (
    <div className="min-h-screen flex flex-col bg-slate-950">
      {/* Top navigation bar */}
      <NavBar role={role as 'dono' | 'corretor'} email={email} />

      {/* Session refresh guard — proactively refreshes 2min before expiry */}
      <SessionGuard expiresAt={expiresAt} />

      {/* Page content */}
      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 py-8">
        {children}
      </main>
    </div>
  )
}
