import { headers } from 'next/headers'
import { redirect, notFound } from 'next/navigation'
import { getLead, getConversation, ApiError } from '@/lib/api'
import ConversationView from '@/components/ConversationView'
import LeadProfile from '@/components/LeadProfile'
import TakeoverPanel from '@/components/TakeoverPanel'

type Props = { params: Promise<{ phone: string }> }

/**
 * Lead detail page — conversation + profile + takeover controls.
 *
 * Server Component: fetches lead and conversation.
 * Client Component (ConversationView): subscribes to Realtime for live messages.
 */
export default async function LeadDetailPage({ params }: Props) {
  const { phone } = await params
  const decodedPhone = decodeURIComponent(phone)

  const headersList = await headers()
  const token = headersList.get('x-access-token')
  if (!token) redirect('/auth/login')

  // Parallel fetch: lead profile + conversation history
  const [leadResult, conversaResult] = await Promise.allSettled([
    getLead(token, decodedPhone),
    getConversation(token, decodedPhone),
  ])

  if (leadResult.status === 'rejected') {
    const err = leadResult.reason
    if (err instanceof ApiError && err.status === 404) notFound()
    throw err
  }

  const lead = leadResult.value
  const conversas = conversaResult.status === 'fulfilled' ? conversaResult.value : []

  return (
    <div className="grid grid-cols-1 lg:grid-cols-3 gap-6 h-full">
      {/* Left column: lead profile + takeover controls */}
      <div className="lg:col-span-1 space-y-4">
        <LeadProfile lead={lead} />
        <TakeoverPanel
          lead={lead}
          token={token}   /* passed down for client-side proxy calls */
        />
      </div>

      {/* Right column: conversation (SSR + Realtime) */}
      <div className="lg:col-span-2">
        <ConversationView
          lead={lead}
          initialMessages={conversas}
          accessToken={token}
        />
      </div>
    </div>
  )
}
