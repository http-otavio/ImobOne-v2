import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import { getLeads } from '@/lib/api'
import { formatLeadName, formatPhone, scoreLabel, scoreTier, scoreTierClasses, formatCurrency, formatObjectionCategory } from '@/lib/formatters'
import { formatDistanceToNow, formatDateShort } from '@/lib/dateUtils'
import type { Lead } from '@/types'

// ─── Corretor Lead Card ───────────────────────────────────────────────────────

function CorretorCard({
  lead,
  variant,
}: {
  lead: Lead
  variant: 'takeover' | 'hot' | 'visit' | 'default'
}) {
  const name  = formatLeadName(lead.lead_name, lead.lead_phone)
  const phone = formatPhone(lead.lead_phone)
  const tier  = scoreTier(lead.intention_score, lead.visita_agendada)
  const cls   = scoreTierClasses(tier)
  const topObj = lead.objections_detected?.[0]

  const borderColor = {
    takeover: 'border-blue-700/50 hover:border-blue-600/60',
    hot:      'border-red-800/40 hover:border-red-700/60',
    visit:    'border-green-800/40 hover:border-green-700/60',
    default:  'border-slate-800/70 hover:border-amber-700/30',
  }[variant]

  const accentStrip = {
    takeover: 'bg-blue-500',
    hot:      'bg-red-500',
    visit:    'bg-green-500',
    default:  'bg-slate-700',
  }[variant]

  return (
    <Link
      href={`/dashboard/leads/${encodeURIComponent(lead.lead_phone)}`}
      className="block group"
    >
      <div className={`
        relative bg-slate-900 border rounded-xl overflow-hidden
        transition-all duration-200 hover:bg-slate-800/60 hover:shadow-lg
        ${borderColor}
      `}>
        {/* Accent strip */}
        <div className={`absolute left-0 top-0 bottom-0 w-0.5 ${accentStrip}`} />

        <div className="p-4 pl-5">
          {/* Header */}
          <div className="flex items-start justify-between gap-3 mb-3">
            <div className="min-w-0">
              <p className="font-semibold text-slate-100 truncate group-hover:text-amber-300 transition-colors">
                {name}
              </p>
              <p className="text-slate-500 text-xs font-mono mt-0.5">{phone}</p>
            </div>
            <div className="flex flex-col items-end gap-1 flex-shrink-0 mt-0.5">
              {variant === 'takeover' && (
                <span className="px-2 py-0.5 rounded-full bg-blue-900/40 text-blue-300 border border-blue-700/40 text-xs font-medium">
                  ● Você está no controle
                </span>
              )}
              {variant === 'visit' && (
                <span className="px-2 py-0.5 rounded-full bg-green-900/40 text-green-300 border border-green-700/40 text-xs font-medium">
                  Visita confirmada
                </span>
              )}
              {variant === 'hot' && (
                <span className={`px-2 py-0.5 rounded-full ${cls.bg} ${cls.text} border ${cls.border} text-xs font-medium`}>
                  {scoreLabel(lead.intention_score)}
                </span>
              )}
              {variant === 'default' && (
                <span className="px-2 py-0.5 rounded-full bg-slate-800 text-slate-500 border border-slate-700 text-xs">
                  score {lead.intention_score}
                </span>
              )}
            </div>
          </div>

          {/* Body: contextual info */}
          <div className="space-y-1.5">
            {lead.pipeline_value_brl && (
              <div className="flex items-center gap-2">
                <span className="text-slate-600 text-xs">Pipeline</span>
                <span className="text-amber-400 text-sm font-semibold">{formatCurrency(lead.pipeline_value_brl)}</span>
              </div>
            )}
            {topObj && (
              <div className="flex items-center gap-2">
                <span className="text-slate-600 text-xs">Objeção</span>
                <span className="text-amber-700 text-xs">{formatObjectionCategory(topObj.categoria)}</span>
              </div>
            )}
          </div>

          {/* Footer */}
          <div className="flex items-center justify-between mt-3 pt-3 border-t border-slate-800/50">
            <p className="text-slate-600 text-xs">
              Último contato: {formatDistanceToNow(lead.updated_at)}
            </p>
            <span className="text-amber-500 text-xs font-medium group-hover:text-amber-400">
              Ver briefing →
            </span>
          </div>
        </div>
      </div>
    </Link>
  )
}

// ─── Section header ───────────────────────────────────────────────────────────

function SectionHeader({
  title, count, color, pulsing,
}: {
  title: string
  count: number
  color: string
  pulsing?: boolean
}) {
  return (
    <div className="flex items-center justify-between mb-4">
      <div className="flex items-center gap-2.5">
        <span className={`w-2 h-2 rounded-full ${color} ${pulsing ? 'animate-pulse' : ''}`} />
        <h2 className="text-sm font-semibold text-slate-200 uppercase tracking-wide">{title}</h2>
      </div>
      <span className="text-slate-600 text-xs">{count} lead{count !== 1 ? 's' : ''}</span>
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function CorretorPage() {
  const headersList = await headers()
  const token = headersList.get('x-access-token')

  if (!token) redirect('/auth/login')

  const { leads } = await getLeads(token, { limit: 100 })

  const humanos   = leads.filter((l) => l.human_takeover)
  const visitas   = leads.filter((l) => l.visita_agendada && !l.human_takeover && !l.descartado)
  const quentes   = leads.filter((l) => l.intention_score >= 8 && !l.human_takeover && !l.visita_agendada && !l.descartado)
  const demais    = leads.filter((l) => l.intention_score < 8 && !l.human_takeover && !l.visita_agendada && !l.descartado)

  const totalAtivos = leads.filter((l) => !l.descartado).length

  return (
    <div className="space-y-10">

      {/* ── Header ── */}
      <div>
        <p className="text-amber-600 text-xs font-semibold uppercase tracking-widest mb-1">
          Central de Atendimento
        </p>
        <h1 className="text-2xl font-light text-slate-100 tracking-tight">Minha Fila</h1>
        <p className="text-slate-500 text-sm mt-1">
          {totalAtivos} leads ativos
          {humanos.length > 0 && <> · <span className="text-blue-400 font-medium">{humanos.length} em atendimento humano</span></>}
          {quentes.length > 0 && <> · <span className="text-red-400 font-medium">{quentes.length} quentes</span></>}
        </p>
      </div>

      {/* ── Human Takeover (highest urgency) ── */}
      {humanos.length > 0 && (
        <section>
          <SectionHeader
            title="Você está no controle"
            count={humanos.length}
            color="bg-blue-400"
            pulsing
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {humanos.map((l) => (
              <CorretorCard key={l.lead_phone} lead={l} variant="takeover" />
            ))}
          </div>
        </section>
      )}

      {/* ── Visitas Confirmadas ── */}
      {visitas.length > 0 && (
        <section>
          <SectionHeader
            title="Visitas Confirmadas"
            count={visitas.length}
            color="bg-green-400"
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {visitas.map((l) => (
              <CorretorCard key={l.lead_phone} lead={l} variant="visit" />
            ))}
          </div>
        </section>
      )}

      {/* ── Leads Quentes ── */}
      {quentes.length > 0 && (
        <section>
          <SectionHeader
            title="Leads Quentes — Abordar Agora"
            count={quentes.length}
            color="bg-red-400"
          />
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {quentes.map((l) => (
              <CorretorCard key={l.lead_phone} lead={l} variant="hot" />
            ))}
          </div>
        </section>
      )}

      {/* ── Fila Geral ── */}
      {demais.length > 0 && (
        <section>
          <SectionHeader
            title="Em Qualificação"
            count={demais.length}
            color="bg-slate-500"
          />
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {demais.map((l) => (
              <CorretorCard key={l.lead_phone} lead={l} variant="default" />
            ))}
          </div>
        </section>
      )}

      {/* ── Empty state ── */}
      {totalAtivos === 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-2xl p-16 text-center">
          <p className="text-slate-400 mb-1">Fila vazia</p>
          <p className="text-slate-600 text-sm">A Sofia está atendendo. Novos leads aparecerão aqui quando qualificados.</p>
        </div>
      )}

    </div>
  )
}
