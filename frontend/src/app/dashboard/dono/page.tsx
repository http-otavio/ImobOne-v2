import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import Link from 'next/link'
import { getLeads, getAlerts } from '@/lib/api'
import { formatCurrencyCompact, formatCurrency, formatLeadName, formatPhone, scoreLabel, scoreTier, scoreTierClasses, formatObjectionCategory } from '@/lib/formatters'
import { formatDistanceToNow, formatDateShort } from '@/lib/dateUtils'
import type { Lead, ObjectionEntry } from '@/types'

// ─── Sub-components (server, no 'use client' needed) ─────────────────────────

function HeroKPI({
  label, value, sub, accent,
}: {
  label: string
  value: string
  sub?: string
  accent: 'gold' | 'red' | 'green' | 'blue' | 'slate'
}) {
  const cls = {
    gold:  { card: 'bg-gradient-to-br from-amber-950/60 to-amber-900/20 border-amber-800/40', val: 'text-amber-300', sub: 'text-amber-500' },
    red:   { card: 'bg-gradient-to-br from-red-950/60 to-red-900/20 border-red-800/40',       val: 'text-red-300',   sub: 'text-red-500'   },
    green: { card: 'bg-gradient-to-br from-green-950/60 to-green-900/20 border-green-800/40', val: 'text-green-300', sub: 'text-green-500' },
    blue:  { card: 'bg-gradient-to-br from-blue-950/60 to-blue-900/20 border-blue-800/40',    val: 'text-blue-300',  sub: 'text-blue-500'  },
    slate: { card: 'bg-slate-900/60 border-slate-800',                                        val: 'text-slate-100', sub: 'text-slate-500' },
  }[accent]

  return (
    <div className={`rounded-2xl border p-6 ${cls.card}`}>
      <p className="text-xs font-semibold uppercase tracking-widest text-slate-500 mb-3">{label}</p>
      <p className={`text-4xl font-light tracking-tight ${cls.val}`}>{value}</p>
      {sub && <p className={`text-xs mt-2 ${cls.sub}`}>{sub}</p>}
    </div>
  )
}

function ScoreBar({ score }: { score: number }) {
  const pct = Math.min(100, (score / 20) * 100)
  const tier = scoreTier(score)
  const cls = scoreTierClasses(tier)
  return (
    <div className="flex items-center gap-2">
      <div className="flex-1 h-1 rounded-full bg-slate-800">
        <div
          className={`h-1 rounded-full ${cls.dot}`}
          style={{ width: `${pct}%` }}
        />
      </div>
      <span className={`text-xs font-semibold tabular-nums ${cls.text}`}>{score}</span>
    </div>
  )
}

function LeadCard({ lead }: { lead: Lead }) {
  const tier    = scoreTier(lead.intention_score, lead.visita_agendada)
  const cls     = scoreTierClasses(tier)
  const name    = formatLeadName(lead.lead_name, lead.lead_phone)
  const phone   = formatPhone(lead.lead_phone)
  const topObj  = lead.objections_detected?.[0]

  return (
    <Link
      href={`/dashboard/leads/${encodeURIComponent(lead.lead_phone)}`}
      className="block group"
    >
      <div className={`
        bg-slate-900 border rounded-xl p-4 transition-all duration-200
        group-hover:border-amber-700/50 group-hover:bg-slate-800/60 group-hover:shadow-lg group-hover:shadow-amber-900/10
        ${lead.human_takeover ? 'border-blue-800/50' : 'border-slate-800/70'}
      `}>

        {/* Header row */}
        <div className="flex items-start justify-between gap-3 mb-3">
          <div className="min-w-0">
            <p className="font-semibold text-slate-100 truncate">{name}</p>
            <p className="text-slate-500 text-xs font-mono mt-0.5">{phone}</p>
          </div>
          <div className="flex flex-col items-end gap-1 flex-shrink-0">
            {lead.visita_agendada && (
              <span className="px-1.5 py-0.5 rounded-md bg-green-900/40 text-green-400 border border-green-800/40 text-xs font-medium">
                Visita ✓
              </span>
            )}
            {lead.human_takeover && (
              <span className="px-1.5 py-0.5 rounded-md bg-blue-900/40 text-blue-400 border border-blue-800/40 text-xs font-medium">
                Corretor
              </span>
            )}
            {!lead.visita_agendada && !lead.human_takeover && (
              <span className={`px-1.5 py-0.5 rounded-md ${cls.bg} ${cls.text} border ${cls.border} text-xs font-medium`}>
                {scoreLabel(lead.intention_score)}
              </span>
            )}
          </div>
        </div>

        {/* Score bar */}
        <ScoreBar score={lead.intention_score} />

        {/* Bottom row */}
        <div className="flex items-center justify-between mt-3 pt-3 border-t border-slate-800/50">
          <div>
            {lead.pipeline_value_brl ? (
              <p className="text-amber-400 text-sm font-semibold">{formatCurrency(lead.pipeline_value_brl)}</p>
            ) : topObj ? (
              <p className="text-slate-500 text-xs">
                Objeção: <span className="text-amber-600">{formatObjectionCategory(topObj.categoria)}</span>
              </p>
            ) : (
              <p className="text-slate-600 text-xs">Sem pipeline estimado</p>
            )}
          </div>
          <p className="text-slate-600 text-xs">{formatDistanceToNow(lead.updated_at)}</p>
        </div>
      </div>
    </Link>
  )
}

// ─── Top Objections Radar ─────────────────────────────────────────────────────

function ObjectionRadar({ leads }: { leads: Lead[] }) {
  // Aggregate objection counts across all leads
  const counts: Record<string, number> = {}
  for (const lead of leads) {
    for (const obj of (lead.objections_detected ?? [])) {
      counts[obj.categoria] = (counts[obj.categoria] ?? 0) + 1
    }
  }

  const sorted = Object.entries(counts)
    .sort((a, b) => b[1] - a[1])
    .slice(0, 5)

  const maxCount = sorted[0]?.[1] ?? 1

  if (sorted.length === 0) {
    return (
      <p className="text-slate-600 text-sm italic">Nenhuma objeção registrada no período.</p>
    )
  }

  return (
    <div className="space-y-3">
      {sorted.map(([cat, count]) => (
        <div key={cat}>
          <div className="flex justify-between items-center mb-1">
            <span className="text-slate-300 text-sm">{formatObjectionCategory(cat)}</span>
            <span className="text-slate-500 text-xs tabular-nums">{count}×</span>
          </div>
          <div className="h-1.5 rounded-full bg-slate-800">
            <div
              className="h-1.5 rounded-full bg-gradient-to-r from-amber-600 to-amber-400"
              style={{ width: `${(count / maxCount) * 100}%` }}
            />
          </div>
        </div>
      ))}
    </div>
  )
}

// ─── VIP Leads Radar ──────────────────────────────────────────────────────────

function VIPLeads({ leads }: { leads: Lead[] }) {
  const vips = leads
    .filter((l) => l.intention_score >= 10 && !l.descartado && !l.human_takeover)
    .sort((a, b) => b.intention_score - a.intention_score)
    .slice(0, 4)

  if (vips.length === 0) {
    return <p className="text-slate-600 text-sm italic">Nenhum lead VIP no momento.</p>
  }

  return (
    <div className="space-y-2.5">
      {vips.map((lead) => (
        <Link
          key={lead.lead_phone}
          href={`/dashboard/leads/${encodeURIComponent(lead.lead_phone)}`}
          className="flex items-center justify-between p-2.5 rounded-lg bg-slate-800/40 hover:bg-slate-800 border border-slate-800/50 hover:border-amber-700/40 transition-all group"
        >
          <div className="min-w-0">
            <p className="text-slate-200 text-sm font-medium truncate group-hover:text-amber-300 transition-colors">
              {formatLeadName(lead.lead_name, lead.lead_phone)}
            </p>
            {lead.pipeline_value_brl && (
              <p className="text-amber-600 text-xs mt-0.5">{formatCurrency(lead.pipeline_value_brl)}</p>
            )}
          </div>
          <span className="text-red-400 text-xs font-bold ml-2 flex-shrink-0">
            {lead.intention_score} pts
          </span>
        </Link>
      ))}
    </div>
  )
}

// ─── Page ─────────────────────────────────────────────────────────────────────

export default async function DonoPage() {
  const headersList = await headers()
  const token = headersList.get('x-access-token')
  const role  = headersList.get('x-user-role')

  if (!token) redirect('/auth/login')
  // Role guard — if not dono, route to corretor view
  if (role && role !== 'dono') redirect('/dashboard/corretor')

  const [leadsResult, alertsResult] = await Promise.allSettled([
    getLeads(token, { limit: 100 }),
    getAlerts(token),
  ])

  const leads       = leadsResult.status === 'fulfilled' ? leadsResult.value.leads : []
  const alerts      = alertsResult.status === 'fulfilled' ? alertsResult.value : []
  const pendingAlerts = alerts.filter((a) => !a.resolved_at).length

  // ── KPI calculations ───────────────────────────────────────────────────────
  const ativos         = leads.filter((l) => !l.descartado)
  const quentes        = ativos.filter((l) => l.intention_score >= 8 && !l.human_takeover)
  const visitasConf    = ativos.filter((l) => l.visita_agendada)
  const pipeline       = ativos.reduce((acc, l) => acc + (l.pipeline_value_brl ?? 0), 0)
  const humano         = leads.filter((l) => l.human_takeover)
  const taxaConversao  = ativos.length > 0
    ? ((visitasConf.length / ativos.length) * 100).toFixed(1)
    : '0.0'

  // ── Top active leads for card grid (sort: takeover > hot score > visita) ──
  const leadCards = [...ativos]
    .sort((a, b) => {
      if (a.human_takeover !== b.human_takeover) return a.human_takeover ? -1 : 1
      if (a.visita_agendada !== b.visita_agendada) return a.visita_agendada ? -1 : 1
      return b.intention_score - a.intention_score
    })
    .slice(0, 12)

  // ── Today string ──────────────────────────────────────────────────────────
  const today = new Date().toLocaleDateString('pt-BR', {
    weekday: 'long', day: 'numeric', month: 'long',
  })

  return (
    <div className="space-y-10">

      {/* ── Page header ── */}
      <div className="flex items-start justify-between">
        <div>
          <p className="text-amber-600 text-xs font-semibold uppercase tracking-widest mb-1">
            Sala de Comando Comercial
          </p>
          <h1 className="text-2xl font-light text-slate-100 tracking-tight">
            Visão Executiva
          </h1>
          <p className="text-slate-500 text-sm mt-1 capitalize">{today}</p>
        </div>
        {pendingAlerts > 0 && (
          <Link
            href="/dashboard/alerts"
            className="flex items-center gap-2 px-3 py-2 rounded-xl bg-red-900/20 border border-red-800/40 text-red-400 text-sm hover:bg-red-900/30 transition-colors"
          >
            <span className="w-2 h-2 rounded-full bg-red-500 animate-pulse" />
            {pendingAlerts} alerta{pendingAlerts > 1 ? 's' : ''}
          </Link>
        )}
      </div>

      {/* ── Hero KPIs ── */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <HeroKPI
          label="Pipeline Ativo"
          value={pipeline > 0 ? formatCurrencyCompact(pipeline) : '—'}
          sub={pipeline > 0 ? `${ativos.length} leads · valor estimado` : 'aguardando imóveis de interesse'}
          accent="gold"
        />
        <HeroKPI
          label="Leads Quentes"
          value={String(quentes.length)}
          sub={quentes.length > 0 ? 'score ≥ 8 · prontos para abordar' : 'nenhum no momento'}
          accent="red"
        />
        <HeroKPI
          label="Visitas Confirmadas"
          value={String(visitasConf.length)}
          sub={`${taxaConversao}% de conversão da base ativa`}
          accent="green"
        />
        <HeroKPI
          label="Taxa de Conversão"
          value={`${taxaConversao}%`}
          sub={`${visitasConf.length} visitas / ${ativos.length} leads ativos`}
          accent="blue"
        />
      </div>

      {/* ── Radar Comercial ── */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-6">

        {/* Top Objections */}
        <div className="bg-slate-900 border border-slate-800/70 rounded-2xl p-6">
          <div className="mb-5">
            <p className="text-xs font-semibold uppercase tracking-widest text-slate-500 mb-1">
              Radar Comercial
            </p>
            <h2 className="text-base font-semibold text-slate-100">Objeções Recorrentes</h2>
            <p className="text-slate-500 text-xs mt-1">
              Padrões identificados nas conversas desta semana
            </p>
          </div>
          <ObjectionRadar leads={ativos} />
        </div>

        {/* VIP Leads */}
        <div className="bg-slate-900 border border-slate-800/70 rounded-2xl p-6">
          <div className="mb-5">
            <p className="text-xs font-semibold uppercase tracking-widest text-slate-500 mb-1">
              Prioridade Máxima
            </p>
            <h2 className="text-base font-semibold text-slate-100">Leads VIP para Abordagem</h2>
            <p className="text-slate-500 text-xs mt-1">
              Score ≥ 10 · aguardando ação do corretor
            </p>
          </div>
          <VIPLeads leads={leads} />
          {humano.length > 0 && (
            <div className="mt-4 pt-4 border-t border-slate-800/50">
              <p className="text-slate-500 text-xs">
                <span className="text-blue-400 font-medium">{humano.length}</span> em atendimento humano ativo
              </p>
            </div>
          )}
        </div>
      </div>

      {/* ── Lead Cards ── */}
      <section>
        <div className="flex items-center justify-between mb-5">
          <div>
            <h2 className="text-base font-semibold text-slate-100">Base de Leads Ativa</h2>
            <p className="text-slate-500 text-xs mt-0.5">
              {ativos.length} leads · ordenados por prioridade comercial
            </p>
          </div>
          <Link
            href="/dashboard/leads"
            className="text-xs text-amber-500 hover:text-amber-400 transition-colors"
          >
            Ver todos →
          </Link>
        </div>

        {leadCards.length === 0 ? (
          <div className="bg-slate-900 border border-slate-800 rounded-2xl p-12 text-center">
            <p className="text-slate-500">Nenhum lead ativo. A Sofia está pronta para atender.</p>
          </div>
        ) : (
          <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-4">
            {leadCards.map((lead) => (
              <LeadCard key={lead.lead_phone} lead={lead} />
            ))}
          </div>
        )}
      </section>

    </div>
  )
}
