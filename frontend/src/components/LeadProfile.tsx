import { formatDateTime } from '@/lib/dateUtils'
import { formatLeadName, formatPhone, formatCurrency, scoreLabel, scoreTier, scoreTierClasses, formatObjectionCategory } from '@/lib/formatters'
import type { Lead } from '@/types'

interface Props {
  lead: Lead
}

function ScoreBar({ score, visita }: { score: number; visita: boolean }) {
  const tier = scoreTier(score, visita)
  const cls  = scoreTierClasses(tier)
  const pct  = Math.min(100, (score / 20) * 100)
  const label = scoreLabel(score, visita)

  return (
    <div className="space-y-1.5">
      <div className="flex justify-between items-center">
        <span className={`text-xs font-semibold ${cls.text}`}>{label}</span>
        <span className={`text-xs tabular-nums ${cls.text}`}>{score}<span className="text-slate-600">/20</span></span>
      </div>
      <div className="h-1.5 rounded-full bg-slate-800">
        <div
          className={`h-1.5 rounded-full transition-all ${cls.dot}`}
          style={{ width: `${pct}%` }}
        />
      </div>
    </div>
  )
}

function DataRow({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between items-start gap-4">
      <span className="text-slate-500 text-xs flex-shrink-0 pt-0.5">{label}</span>
      <span className="text-slate-300 text-xs text-right">{value ?? '—'}</span>
    </div>
  )
}

export default function LeadProfile({ lead }: Props) {
  const name     = formatLeadName(lead.lead_name, lead.lead_phone)
  const phone    = formatPhone(lead.lead_phone)
  const tier     = scoreTier(lead.intention_score, lead.visita_agendada)
  const cls      = scoreTierClasses(tier)
  const pipeline = formatCurrency(lead.pipeline_value_brl)

  return (
    <div className="bg-slate-900 border border-slate-800/70 rounded-xl overflow-hidden">

      {/* Executive header */}
      <div className="px-5 py-5 border-b border-slate-800/50">
        <div className="flex items-start justify-between gap-3 mb-4">
          <div className="min-w-0">
            <h2 className="text-lg font-semibold text-slate-100 leading-tight">{name}</h2>
            <p className="text-slate-500 text-xs font-mono mt-0.5">{phone}</p>
          </div>
          {lead.pipeline_value_brl && (
            <div className="text-right flex-shrink-0">
              <p className="text-amber-400 font-semibold text-base">{pipeline}</p>
              <p className="text-slate-600 text-xs">pipeline estimado</p>
            </div>
          )}
        </div>

        {/* Score bar */}
        <ScoreBar score={lead.intention_score} visita={lead.visita_agendada} />
      </div>

      {/* Status badges */}
      <div className="px-5 py-3 border-b border-slate-800/50 flex flex-wrap gap-1.5">
        {lead.human_takeover  && (
          <span className="pill bg-blue-900/40 text-blue-300 border border-blue-700/40 text-xs">
            ● Corretor no controle
          </span>
        )}
        {lead.visita_agendada && (
          <span className="pill bg-green-900/40 text-green-300 border border-green-700/40 text-xs">
            ✓ Visita confirmada
          </span>
        )}
        {lead.descartado && (
          <span className="pill bg-slate-800 text-slate-500 border border-slate-700 text-xs">
            Descartado{lead.motivo_descarte ? ` · ${lead.motivo_descarte}` : ''}
          </span>
        )}
        {lead.crm_external_id && (
          <span className="pill bg-purple-900/40 text-purple-300 border border-purple-700/40 text-xs">
            CRM sincronizado
          </span>
        )}
        {!lead.human_takeover && !lead.visita_agendada && !lead.descartado && (
          <span className={`pill ${cls.bg} ${cls.text} border ${cls.border} text-xs`}>
            {scoreLabel(lead.intention_score)}
          </span>
        )}
      </div>

      {/* Data rows */}
      <div className="px-5 py-4 space-y-2.5 border-b border-slate-800/50">
        <DataRow label="Primeiro contato"     value={formatDateTime(lead.created_at)} />
        <DataRow label="Última mensagem"      value={formatDateTime(lead.updated_at)} />
        {lead.corretor_notified_at && (
          <DataRow label="Corretor notificado" value={formatDateTime(lead.corretor_notified_at)} />
        )}
      </div>

      {/* Objections */}
      {lead.objections_detected?.length > 0 && (
        <div className="px-5 py-4">
          <p className="text-xs font-semibold text-slate-500 uppercase tracking-wide mb-3">
            Objeções Detectadas
          </p>
          <div className="space-y-2">
            {lead.objections_detected.map((obj, i) => (
              <div
                key={i}
                className="bg-slate-800/60 border border-slate-700/50 rounded-lg px-3 py-2.5"
              >
                <div className="flex items-center justify-between gap-2 mb-1">
                  <p className="text-amber-500 text-xs font-semibold">
                    {formatObjectionCategory(obj.categoria)}
                  </p>
                  <p className="text-slate-600 text-xs flex-shrink-0">
                    {formatDateTime(obj.detectado_em)}
                  </p>
                </div>
                <p className="text-slate-400 text-xs leading-relaxed line-clamp-2">{obj.mensagem}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
