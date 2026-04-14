'use client'

import { useState } from 'react'
import { useRouter } from 'next/navigation'
import { Shield, ShieldOff, Loader2 } from 'lucide-react'
import { formatDateTime } from '@/lib/dateUtils'
import type { AnomalyAlert } from '@/types'

interface Props {
  alerts: AnomalyAlert[]
  dimmed?: boolean
}

const ALERT_LABELS: Record<string, string> = {
  bulk_read:       'Leitura em massa',
  export_attempt:  'Tentativa de exportação',
}

export default function AlertsList({ alerts, dimmed }: Props) {
  const router = useRouter()
  const [resolving, setResolving] = useState<string | null>(null)

  async function handleResolve(alertId: string) {
    setResolving(alertId)
    await fetch(`/api/proxy/admin/alerts/${alertId}/resolve`, { method: 'PATCH' })
    setResolving(null)
    router.refresh()
  }

  return (
    <div className={`space-y-3 ${dimmed ? 'opacity-50' : ''}`}>
      {alerts.map((alert) => (
        <div
          key={alert.id}
          className={`
            bg-slate-900 border rounded-xl p-5
            ${alert.resolved_at ? 'border-slate-800' : 'border-red-800/50'}
          `}
        >
          <div className="flex items-start justify-between gap-4">
            <div className="flex items-start gap-3">
              <div className={`mt-0.5 p-1.5 rounded-lg ${alert.resolved_at ? 'bg-slate-800' : 'bg-red-900/30'}`}>
                {alert.resolved_at
                  ? <Shield className="w-4 h-4 text-slate-500" />
                  : <ShieldOff className="w-4 h-4 text-red-400" />
                }
              </div>
              <div>
                <p className="text-sm font-medium text-slate-200">
                  {ALERT_LABELS[alert.alert_type] ?? alert.alert_type}
                </p>
                <p className="text-xs text-slate-500 mt-0.5">
                  {alert.user_email ?? alert.user_id} · {formatDateTime(alert.created_at)}
                </p>
                {alert.session_revoked && (
                  <p className="text-xs text-amber-500 mt-1">
                    Sessão revogada automaticamente
                  </p>
                )}
                <pre className="text-xs text-slate-600 mt-2 bg-slate-800/50 rounded px-2 py-1.5 overflow-x-auto">
                  {JSON.stringify(alert.detail, null, 2)}
                </pre>
              </div>
            </div>

            {!alert.resolved_at && (
              <button
                onClick={() => handleResolve(alert.id)}
                disabled={resolving === alert.id}
                className="
                  flex-shrink-0 flex items-center gap-1.5 text-xs
                  border border-slate-700 hover:border-slate-600
                  text-slate-400 hover:text-slate-200
                  px-3 py-1.5 rounded-lg transition-colors
                  disabled:opacity-50
                "
              >
                {resolving === alert.id ? <Loader2 className="w-3 h-3 animate-spin" /> : null}
                Resolver
              </button>
            )}
          </div>
        </div>
      ))}
    </div>
  )
}
