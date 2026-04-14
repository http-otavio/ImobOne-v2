import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import { getLeads, getWeeklyReport, getAlerts, ApiError } from '@/lib/api'
import { decodeSupabaseJwt } from '@/lib/session'
import PipelineKPIs from '@/components/PipelineKPIs'
import LeadsTable from '@/components/LeadsTable'
import AlertsBadge from '@/components/AlertsBadge'

/**
 * Dono dashboard — executive view.
 * Shows: pipeline KPIs, active leads table, pending alert count.
 *
 * All fetches are cache: 'no-store' (enforced in lib/api.ts).
 * This page is server-rendered on every request — no stale data risk.
 */
export default async function DonoPage() {
  const headersList = await headers()
  const token = headersList.get('x-access-token')

  if (!token) redirect('/auth/login')

  // Guard: only dono can access this page
  const jwt = decodeSupabaseJwt(token)
  if (jwt?.app_metadata?.role !== 'dono') {
    redirect('/dashboard/corretor')
  }

  // Parallel fetch — no sequential waterfall
  const [leadsResult, alertsResult] = await Promise.allSettled([
    getLeads(token, { limit: 50 }),
    getAlerts(token),
  ])

  const leads = leadsResult.status === 'fulfilled' ? leadsResult.value.leads : []
  const alerts = alertsResult.status === 'fulfilled' ? alertsResult.value : []
  const pendingAlerts = alerts.filter((a) => !a.resolved_at).length

  // KPI calculations (derived from leads data — zero extra API call)
  const quentes = leads.filter((l) => l.intention_score >= 8 && !l.descartado).length
  const visitasAgendadas = leads.filter((l) => l.visita_agendada).length
  const pipeline = leads.reduce((acc, l) => acc + (l.pipeline_value_brl ?? 0), 0)
  const humano = leads.filter((l) => l.human_takeover).length

  return (
    <div className="space-y-8">
      {/* Header */}
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-slate-100">
            Visão Executiva
          </h1>
          <p className="text-slate-400 text-sm mt-1">
            Dados em tempo real — atualiza a cada carregamento de página
          </p>
        </div>
        {pendingAlerts > 0 && (
          <AlertsBadge count={pendingAlerts} />
        )}
      </div>

      {/* KPI cards */}
      <PipelineKPIs
        totalLeads={leads.length}
        leadsQuentes={quentes}
        visitasAgendadas={visitasAgendadas}
        pipelineEstimadoBrl={pipeline}
        emAtendimentoHumano={humano}
      />

      {/* Leads table */}
      <section>
        <div className="flex items-center justify-between mb-4">
          <h2 className="text-lg font-medium text-slate-200">Leads Ativos</h2>
          <a
            href="/dashboard/alerts"
            className="text-sm text-amber-400 hover:text-amber-300 transition-colors"
          >
            {pendingAlerts > 0 ? `${pendingAlerts} alerta${pendingAlerts > 1 ? 's' : ''} pendente${pendingAlerts > 1 ? 's' : ''}` : 'Ver alertas'}
          </a>
        </div>
        <LeadsTable leads={leads} role="dono" />
      </section>
    </div>
  )
}
