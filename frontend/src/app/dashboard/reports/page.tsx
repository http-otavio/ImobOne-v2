import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import { getWeeklyReport } from '@/lib/api'
import { decodeSupabaseJwt } from '@/lib/session'
import ReportCard from '@/components/ReportCard'

/** Weekly reports page — dono only. */
export default async function ReportsPage({
  searchParams,
}: {
  searchParams: Promise<{ client_id?: string }>
}) {
  const headersList = await headers()
  const token = headersList.get('x-access-token')
  if (!token) redirect('/auth/login')

  const jwt = decodeSupabaseJwt(token)
  if (jwt?.app_metadata?.role !== 'dono') {
    redirect('/dashboard/corretor')
  }

  const { client_id } = await searchParams
  // client_id falls back to the one embedded in the JWT app_metadata
  const clientId = client_id ?? (jwt?.app_metadata as Record<string, string>)?.client_id ?? 'demo_imobiliaria_vendas'

  let report = null
  let fetchError: string | null = null

  try {
    report = await getWeeklyReport(token, clientId)
  } catch (err: unknown) {
    fetchError = err instanceof Error ? err.message : 'Erro ao carregar relatório.'
  }

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Relatório Semanal</h1>
        <p className="text-slate-400 text-sm mt-1">
          Resumo executivo gerado automaticamente todo domingo às 18:00 BRT.
        </p>
      </div>

      {fetchError ? (
        <div className="bg-red-900/20 border border-red-800/50 rounded-xl p-6 text-red-300">
          {fetchError}
        </div>
      ) : report ? (
        <ReportCard report={report} clientId={clientId} />
      ) : (
        <div className="bg-slate-900 border border-slate-800 rounded-xl p-8 text-center">
          <p className="text-slate-400">Nenhum relatório disponível para este cliente.</p>
        </div>
      )}
    </div>
  )
}
