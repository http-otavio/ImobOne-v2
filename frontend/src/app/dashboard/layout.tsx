import { redirect } from 'next/navigation'
import { headers } from 'next/headers'
import NavBar from '@/components/NavBar'
import SessionGuard from '@/components/SessionGuard'

/**
 * Dashboard layout — Server Component.
 *
 * Reads role and nome from headers injected by middleware.
 * Role comes from the encrypted session cookie (set at login / mfa-challenge),
 * NOT from JWT app_metadata (which Supabase doesn't populate for custom roles).
 *
 * Authorization: RLS at database level — role here is display-only.
 */
export default async function DashboardLayout({
  children,
}: {
  children: React.ReactNode
}) {
  const headersList = await headers()
  const accessToken = headersList.get('x-access-token')
  const expiresAt   = Number(headersList.get('x-expires-at') ?? '0')
  const role        = (headersList.get('x-user-role') ?? 'corretor') as 'dono' | 'corretor'
  const nome        = headersList.get('x-user-nome') ?? ''

  if (!accessToken) {
    redirect('/auth/login')
  }

  return (
    <div className="min-h-screen flex flex-col bg-slate-950">
      <NavBar role={role} nome={nome} />
      <SessionGuard expiresAt={expiresAt} />
      <main className="flex-1 max-w-7xl mx-auto w-full px-4 sm:px-6 py-8">
        {children}
      </main>
    </div>
  )
}
