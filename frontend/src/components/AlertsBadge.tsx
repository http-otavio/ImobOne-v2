import Link from 'next/link'
import { AlertTriangle } from 'lucide-react'

export default function AlertsBadge({ count }: { count: number }) {
  return (
    <Link
      href="/dashboard/alerts"
      className="flex items-center gap-2 bg-red-900/30 border border-red-800/50 hover:bg-red-900/50 rounded-xl px-4 py-2.5 transition-colors"
    >
      <AlertTriangle className="w-4 h-4 text-red-400" />
      <span className="text-red-300 text-sm font-medium">
        {count} alerta{count > 1 ? 's' : ''} pendente{count > 1 ? 's' : ''}
      </span>
    </Link>
  )
}
