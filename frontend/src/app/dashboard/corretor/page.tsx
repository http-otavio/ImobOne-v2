import { headers } from 'next/headers'
import { redirect } from 'next/navigation'
import { getLeads } from '@/lib/api'
import LeadsTable from '@/components/LeadsTable'

/**
 * Corretor view — lead queue.
 * RLS in the database ensures corretor only sees their assigned leads.
 * No role guard needed here: even if a dono visits this page, they'll
 * see the filtered view correctly (the dono sees all leads via RLS anyway).
 */
export default async function CorretorPage() {
  const headersList = await headers()
  const token = headersList.get('x-access-token')

  if (!token) redirect('/auth/login')

  const { leads } = await getLeads(token, { limit: 100 })

  const ativos   = leads.filter((l) => !l.descartado && !l.human_takeover)
  const humanos  = leads.filter((l) => l.human_takeover)
  const quentes  = ativos.filter((l) => l.intention_score >= 8)

  return (
    <div className="space-y-8">
      <div>
        <h1 className="text-2xl font-semibold text-slate-100">Minha Fila</h1>
        <p className="text-slate-400 text-sm mt-1">
          {ativos.length} leads ativos · {quentes.length} quentes · {humanos.length} em atendimento humano
        </p>
      </div>

      {/* Human takeover section */}
      {humanos.length > 0 && (
        <section>
          <h2 className="text-base font-medium text-amber-400 mb-3 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-amber-400 animate-pulse" />
            Em Atendimento Humano
          </h2>
          <LeadsTable leads={humanos} role="corretor" highlight="takeover" />
        </section>
      )}

      {/* Hot leads */}
      {quentes.length > 0 && (
        <section>
          <h2 className="text-base font-medium text-red-400 mb-3 flex items-center gap-2">
            <span className="w-2 h-2 rounded-full bg-red-400" />
            Leads Quentes (score ≥ 8)
          </h2>
          <LeadsTable leads={quentes} role="corretor" highlight="hot" />
        </section>
      )}

      {/* All active leads */}
      <section>
        <h2 className="text-base font-medium text-slate-300 mb-3">
          Todos os Leads Ativos
        </h2>
        <LeadsTable leads={ativos} role="corretor" />
      </section>
    </div>
  )
}
