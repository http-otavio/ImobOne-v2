import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import { getAlerts } from '@/lib/api'
import { decodeSupabaseJwt } from '@/lib/session'
import AlertsList from '@/components/AlertsList'

/** Anomaly alerts page — dono only. */
export default async function AlertsPage() {
  const headersList = await headers()
  const token = headersList.get('x-access-token')
  if (!token) redirect('/auth/login')

  // Only dono can see alerts
  const jwt = decodeSupabaseJwt(token)
  if (jwt?.app_metadata?.role !== 'dono') {
    redirect('/dashboard/corretor')
  }

  const alerts = await getAlerts(token)

  const pending  = alerts.filter((a) => !a.resolved_at)
  const resolved = alerts.filter((a) => a.resolved_at)

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Alertas de Segurança</h1>
        <p className="text-slate-400 text-sm mt-1">
          Detecção automática de anomalias de acesso. Sessões suspeitas são revogadas automaticamente.
        </p>
      </div>

      {pending.length === 0 ? (
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-8 text-center">
          <div className="text-4xl mb-3">✅</div>
          <p className="text-slate-300 font-medium">Nenhum alerta pendente</p>
          <p className="text-slate-500 text-sm mt-1">Todos os acessos estão dentro do padrão normal.</p>
        </div>
      ) : (
        <section>
          <h2 className="text-base font-medium text-red-400 mb-4 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-red-400 animate-pulse" />
            Alertas Pendentes ({pending.length})
          </h2>
          <AlertsList alerts={pending} />
        </section>
      )}

      {resolved.length > 0 && (
        <section>
          <h2 className="text-base font-medium text-slate-500 mb-4">
            Histórico ({resolved.length})
          </h2>
          <AlertsList alerts={resolved} dimmed />
        </section>
      )}
    </div>
  )
}
