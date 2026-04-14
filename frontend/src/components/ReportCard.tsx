import { formatDate } from '@/lib/dateUtils'
import type { WeeklyReport } from '@/types'
import { TrendingUp, Users, Calendar, Target, MessageSquareWarning, BarChart3 } from 'lucide-react'

interface Props {
  report: WeeklyReport
  clientId: string
}

function MetricCard({
  icon: Icon,
  label,
  value,
  sub,
  accent,
}: {
  icon: React.ElementType
  label: string
  value: string | number
  sub?: string
  accent?: string
}) {
  return (
    <div className="bg-slate-800/50 border border-slate-700/50 rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <Icon className={`w-4 h-4 ${accent ?? 'text-slate-400'}`} />
        <span className="text-xs text-slate-400 font-medium">{label}</span>
      </div>
      <p className={`text-2xl font-semibold ${accent ?? 'text-slate-200'}`}>{value}</p>
      {sub && <p className="text-xs text-slate-500 mt-1">{sub}</p>}
    </div>
  )
}

export default function ReportCard({ report, clientId }: Props) {
  const pipeline = new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: 'BRL',
    maximumFractionDigits: 0,
  }).format(report.pipeline_estimado_brl)

  const conversao = `${(report.taxa_conversao * 100).toFixed(1)}%`

  const origemEntries = Object.entries(report.leads_por_origem).sort((a, b) => b[1] - a[1])

  return (
    <div className="space-y-6">
      {/* Period header */}
      <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
        <div className="flex items-start justify-between gap-4">
          <div>
            <p className="text-xs text-slate-500 mb-1">Período</p>
            <p className="text-slate-200 font-medium">
              {formatDate(report.period_start)} → {formatDate(report.period_end)}
            </p>
          </div>
          <div className="flex gap-2">
            <a
              href={`/api/proxy/admin/reports/weekly?client_id=${clientId}&format=pdf`}
              className="text-xs border border-slate-700 hover:border-slate-600 text-slate-400 hover:text-slate-200 px-3 py-1.5 rounded-lg transition-colors"
              target="_blank"
              rel="noopener noreferrer"
            >
              PDF
            </a>
            <a
              href={`/api/proxy/admin/reports/export/csv?client_id=${clientId}`}
              className="text-xs border border-slate-700 hover:border-slate-600 text-slate-400 hover:text-slate-200 px-3 py-1.5 rounded-lg transition-colors"
            >
              CSV
            </a>
          </div>
        </div>
      </div>

      {/* KPI grid */}
      <div className="grid grid-cols-2 lg:grid-cols-3 gap-4">
        <MetricCard
          icon={TrendingUp}
          label="Pipeline Estimado"
          value={pipeline}
          accent="text-amber-400"
        />
        <MetricCard
          icon={Users}
          label="Total de Leads"
          value={report.total_leads}
          accent="text-slate-300"
        />
        <MetricCard
          icon={Target}
          label="Leads Quentes"
          value={report.leads_quentes}
          sub="score ≥ 8"
          accent="text-red-400"
        />
        <MetricCard
          icon={Calendar}
          label="Visitas Agendadas"
          value={report.visitas_confirmadas}
          accent="text-green-400"
        />
        <MetricCard
          icon={BarChart3}
          label="Taxa de Conversão"
          value={conversao}
          sub="leads → visita"
          accent="text-blue-400"
        />
        {report.top_objecao && (
          <MetricCard
            icon={MessageSquareWarning}
            label="Top Objeção"
            value={report.top_objecao}
            accent="text-orange-400"
          />
        )}
      </div>

      {/* Origem breakdown */}
      {origemEntries.length > 0 && (
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-5">
          <p className="text-sm font-medium text-slate-300 mb-4">Leads por Origem</p>
          <div className="space-y-3">
            {origemEntries.map(([origem, count]) => {
              const pct = Math.round((count / report.total_leads) * 100)
              return (
                <div key={origem}>
                  <div className="flex justify-between text-xs mb-1">
                    <span className="text-slate-400">{origem}</span>
                    <span className="text-slate-300">{count} ({pct}%)</span>
                  </div>
                  <div className="h-1.5 bg-slate-800 rounded-full overflow-hidden">
                    <div
                      className="h-full bg-gradient-to-r from-amber-500 to-yellow-500 rounded-full"
                      style={{ width: `${pct}%` }}
                    />
                  </div>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}
