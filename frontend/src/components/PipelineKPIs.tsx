interface Props {
  totalLeads: number
  leadsQuentes: number
  visitasAgendadas: number
  pipelineEstimadoBrl: number
  emAtendimentoHumano: number
}

function KpiCard({
  label,
  value,
  sub,
  accent,
}: {
  label: string
  value: string
  sub?: string
  accent?: 'gold' | 'red' | 'green' | 'blue'
}) {
  const accentClasses = {
    gold:  'text-amber-400 bg-amber-900/20 border-amber-800/50',
    red:   'text-red-400   bg-red-900/20   border-red-800/50',
    green: 'text-green-400 bg-green-900/20 border-green-800/50',
    blue:  'text-blue-400  bg-blue-900/20  border-blue-800/50',
  }
  const cls = accent ? accentClasses[accent] : 'text-slate-100 bg-slate-900 border-slate-800'

  return (
    <div className={`rounded-xl border p-5 ${cls}`}>
      <p className="text-xs font-medium uppercase tracking-wide opacity-70 mb-1">{label}</p>
      <p className="text-3xl font-semibold">{value}</p>
      {sub && <p className="text-xs opacity-60 mt-1">{sub}</p>}
    </div>
  )
}

export default function PipelineKPIs({
  totalLeads,
  leadsQuentes,
  visitasAgendadas,
  pipelineEstimadoBrl,
  emAtendimentoHumano,
}: Props) {
  const pipelineFormatted = new Intl.NumberFormat('pt-BR', {
    style: 'currency',
    currency: 'BRL',
    maximumFractionDigits: 0,
  }).format(pipelineEstimadoBrl)

  const conversaoRate = totalLeads > 0
    ? ((visitasAgendadas / totalLeads) * 100).toFixed(1)
    : '0.0'

  return (
    <div className="grid grid-cols-2 lg:grid-cols-5 gap-4">
      <KpiCard
        label="Pipeline Estimado"
        value={pipelineFormatted}
        sub="soma dos imóveis de interesse"
        accent="gold"
      />
      <KpiCard
        label="Leads Quentes"
        value={String(leadsQuentes)}
        sub="score ≥ 8"
        accent="red"
      />
      <KpiCard
        label="Visitas Agendadas"
        value={String(visitasAgendadas)}
        sub={`${conversaoRate}% de conversão`}
        accent="green"
      />
      <KpiCard
        label="Total de Leads"
        value={String(totalLeads)}
        sub="na base ativa"
      />
      <KpiCard
        label="Atend. Humano"
        value={String(emAtendimentoHumano)}
        sub="corretor no controle"
        accent="blue"
      />
    </div>
  )
}
