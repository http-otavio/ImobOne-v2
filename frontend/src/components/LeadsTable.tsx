import Link from 'next/link'
import { formatDistanceToNow } from '@/lib/dateUtils'
import type { Lead, UserRole } from '@/types'

interface Props {
  leads: Lead[]
  role: UserRole
  highlight?: 'hot' | 'takeover'
}

function ScoreBadge({ score }: { score: number }) {
  if (score >= 10) return <span className="pill bg-red-900/50 text-red-300 border border-red-800/50">{score} 🔥</span>
  if (score >= 7)  return <span className="pill bg-amber-900/50 text-amber-300 border border-amber-800/50">{score}</span>
  return <span className="pill bg-slate-800 text-slate-400 border border-slate-700">{score}</span>
}

function StatusBadge({ lead }: { lead: Lead }) {
  if (lead.human_takeover)  return <span className="pill bg-blue-900/40 text-blue-300 border border-blue-800/50">humano</span>
  if (lead.visita_agendada) return <span className="pill bg-green-900/40 text-green-300 border border-green-800/50">visita</span>
  if (lead.descartado)      return <span className="pill bg-slate-800 text-slate-500 border border-slate-700">descartado</span>
  return <span className="pill bg-slate-800 text-slate-400 border border-slate-700">ativo</span>
}

export default function LeadsTable({ leads, role, highlight }: Props) {
  if (leads.length === 0) {
    return (
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-8 text-center text-slate-500">
        Nenhum lead encontrado.
      </div>
    )
  }

  return (
    <div className="bg-slate-900 border border-slate-800 rounded-xl overflow-hidden">
      <table className="w-full text-sm">
        <thead>
          <tr className="border-b border-slate-800">
            <th className="text-left text-slate-400 font-medium px-4 py-3">Lead</th>
            <th className="text-left text-slate-400 font-medium px-4 py-3">Telefone</th>
            <th className="text-left text-slate-400 font-medium px-4 py-3">Score</th>
            {role === 'dono' && (
              <th className="text-right text-slate-400 font-medium px-4 py-3">Pipeline</th>
            )}
            <th className="text-left text-slate-400 font-medium px-4 py-3">Status</th>
            <th className="text-left text-slate-400 font-medium px-4 py-3 hidden md:table-cell">Último contato</th>
            <th className="px-4 py-3" />
          </tr>
        </thead>
        <tbody className="divide-y divide-slate-800/50">
          {leads.map((lead) => (
            <tr
              key={lead.lead_phone}
              className={`
                hover:bg-slate-800/50 transition-colors
                ${highlight === 'hot'     ? 'bg-red-900/5' : ''}
                ${highlight === 'takeover' ? 'bg-blue-900/5' : ''}
              `}
            >
              <td className="px-4 py-3">
                <span className="font-medium text-slate-200">
                  {lead.lead_name ?? '—'}
                </span>
              </td>
              <td className="px-4 py-3 text-slate-400 font-mono text-xs">
                {lead.lead_phone}
              </td>
              <td className="px-4 py-3">
                <ScoreBadge score={lead.intention_score} />
              </td>
              {role === 'dono' && (
                <td className="px-4 py-3 text-right text-slate-300 font-medium">
                  {lead.pipeline_value_brl
                    ? new Intl.NumberFormat('pt-BR', { style: 'currency', currency: 'BRL', maximumFractionDigits: 0 }).format(lead.pipeline_value_brl)
                    : '—'
                  }
                </td>
              )}
              <td className="px-4 py-3">
                <StatusBadge lead={lead} />
              </td>
              <td className="px-4 py-3 text-slate-500 hidden md:table-cell">
                {formatDistanceToNow(lead.updated_at)}
              </td>
              <td className="px-4 py-3 text-right">
                <Link
                  href={`/dashboard/leads/${encodeURIComponent(lead.lead_phone)}`}
                  className="text-amber-400 hover:text-amber-300 text-xs font-medium transition-colors"
                >
                  Ver →
                </Link>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}
