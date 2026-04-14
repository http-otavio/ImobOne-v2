import { formatDateTime } from '@/lib/dateUtils'
import type { Lead } from '@/types'

interface Props {
  lead: Lead
}

function Row({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex justify-between items-start gap-3">
      <span className="text-slate-500 text-xs flex-shrink-0">{label}</span>
      <span className="text-slate-300 text-xs text-right">{value ?? '—'}</span>
    </div>
  )
}

export default function LeadProfile({ lead }: Props) {
  const pipeline = lead.pipeline_value_brl
    ? new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL', maximumFractionDigits: 0 }).format(lead.pipeline_value_brl)
    : null

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl p-5 space-y-5">
      {/* Header */}
      <div>
        <div className="flex items-start justify-between gap-2 mb-1">
          <h2 className="text-base font-semibold text-slate-100">
            {lead.lead_name ?? 'Lead sem nome'}
          </h2>
          <span className={`
            pill text-xs
            ${lead.intention_score >= 10 ? 'bg-red-900/50 text-red-300 border border-red-800/50' :
              lead.intention_score >= 7  ? 'bg-amber-900/50 text-amber-300 border border-amber-800/50' :
                                           'bg-slate-800 text-slate-400 border border-slate-700'}
          `}>
            score {lead.intention_score}
          </span>
        </div>
        <p className="text-slate-500 text-xs font-mono">{lead.lead_phone}</p>
      </div>

      {/* Status flags */}
      <div className="flex flex-wrap gap-1.5">
        {lead.human_takeover  && <span className="pill bg-blue-900/40 text-blue-300 border border-blue-800/50">atendimento humano</span>}
        {lead.visita_agendada && <span className="pill bg-green-900/40 text-green-300 border border-green-800/50">visita agendada</span>}
        {lead.descartado      && <span className="pill bg-slate-800 text-slate-500 border border-slate-700">descartado</span>}
        {lead.crm_external_id && <span className="pill bg-purple-900/40 text-purple-300 border border-purple-800/50">no CRM</span>}
      </div>

      {/* Data rows */}
      <div className="space-y-2.5 pt-1">
        {pipeline && <Row label="Pipeline estimado" value={<span className="text-amber-400 font-medium">{pipeline}</span>} />}
        <Row label="Primeiro contato" value={formatDateTime(lead.created_at)} />
        <Row label="Última mensagem" value={formatDateTime(lead.updated_at)} />
        {lead.corretor_notified_at && (
          <Row label="Corretor notificado" value={formatDateTime(lead.corretor_notified_at)} />
        )}
        {lead.motivo_descarte && (
          <Row label="Motivo descarte" value={lead.motivo_descarte} />
        )}
      </div>

      {/* Objections */}
      {lead.objections_detected?.length > 0 && (
        <div className="pt-1">
          <p className="text-slate-500 text-xs mb-2">Objeções detectadas</p>
          <div className="space-y-1.5">
            {lead.objections_detected.map((obj, i) => (
              <div key={i} className="bg-slate-800/50 rounded-lg px-3 py-2">
                <p className="text-amber-400 text-xs font-medium">{obj.categoria}</p>
                <p className="text-slate-400 text-xs mt-0.5 line-clamp-2">{obj.mensagem}</p>
              </div>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}
