/** Lightweight date formatting utils — null-safe, no date-fns dependency. */

function parseDate(dateStr: string | null | undefined): Date | null {
  if (!dateStr) return null
  const d = new Date(dateStr)
  return isNaN(d.getTime()) ? null : d
}

export function formatDistanceToNow(dateStr: string | null | undefined): string {
  const date = parseDate(dateStr)
  if (!date) return '—'
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSeconds = Math.floor(diffMs / 1000)

  if (diffSeconds < 60)    return 'agora'
  if (diffSeconds < 3600)  return `${Math.floor(diffSeconds / 60)}min atrás`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h atrás`
  if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)}d atrás`

  return date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' })
}

export function formatDateTime(dateStr: string | null | undefined): string {
  const date = parseDate(dateStr)
  if (!date) return '—'
  return date.toLocaleString('pt-BR', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDate(dateStr: string | null | undefined): string {
  const date = parseDate(dateStr)
  if (!date) return '—'
  return date.toLocaleDateString('pt-BR', {
    day: '2-digit',
    month: 'long',
    year: 'numeric',
  })
}

export function formatDateShort(dateStr: string | null | undefined): string {
  const date = parseDate(dateStr)
  if (!date) return '—'
  return date.toLocaleDateString('pt-BR', {
    day: '2-digit',
    month: '2-digit',
    year: '2-digit',
  })
}
