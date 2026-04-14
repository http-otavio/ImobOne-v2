import { headers } from 'next/headers'
import { redirect, notFound } from 'next/navigation'
import Link from 'next/link'
import { ChevronLeft } from 'lucide-react'
import { getLead, getConversation, ApiError } from '@/lib/api'
import { formatLeadName, formatPhone } from '@/lib/formatters'
import ConversationView from '@/components/ConversationView'
import LeadProfile from '@/components/LeadProfile'
import TakeoverPanel from '@/components/TakeoverPanel'

type Props = { params: Promise<{ phone: string }> }

export default async function LeadDetailPage({ params }: Props) {
  const { phone } = await params
  const decodedPhone = decodeURIComponent(phone)

  const headersList = await headers()
  const token = headersList.get('x-access-token')
  if (!token) redirect('/auth/login')

  const [leadResult, conversaResult] = await Promise.allSettled([
    getLead(token, decodedPhone),
    getConversation(token, decodedPhone),
  ])

  if (leadResult.status === 'rejected') {
    const err = leadResult.reason
    if (err instanceof ApiError && err.status === 404) notFound()
    throw err
  }

  const lead     = leadResult.value
  const conversas = conversaResult.status === 'fulfilled' ? conversaResult.value : []

  const displayName = formatLeadName(lead.lead_name, lead.lead_phone)
  const displayPhone = formatPhone(lead.lead_phone)

  return (
    <div className="space-y-6">

      {/* ── Breadcrumb header ── */}
      <div className="flex items-center gap-3">
        <Link
          href="/dashboard"
          className="flex items-center gap-1 text-slate-500 hover:text-slate-300 transition-colors text-sm"
        >
          <ChevronLeft className="w-4 h-4" />
          Leads
        </Link>
        <span className="text-slate-700">/</span>
        <div>
          <span className="text-slate-200 font-medium">{displayName}</span>
          {displayName !== displayPhone && (
            <span className="text-slate-500 text-sm ml-2 font-mono">{displayPhone}</span>
          )}
        </div>
        {lead.human_takeover && (
          <span className="ml-auto px-2.5 py-1 rounded-lg bg-blue-900/30 border border-blue-700/40 text-blue-400 text-xs font-medium animate-pulse">
            ● Atendimento Humano Ativo
          </span>
        )}
      </div>

      {/* ── Main layout: sidebar + conversation ── */}
      <div className="grid grid-cols-1 lg:grid-cols-[320px_1fr] gap-6 items-start">

        {/* Left sidebar: profile + takeover */}
        <div className="space-y-4">
          <LeadProfile lead={lead} />
          <TakeoverPanel lead={lead} token={token} />
        </div>

        {/* Right: conversation — full height */}
        <div>
          <ConversationView
            lead={lead}
            initialMessages={conversas}
            accessToken={token}
          />
        </div>
      </div>

    </div>
  )
}
