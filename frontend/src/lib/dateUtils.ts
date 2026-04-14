/** Lightweight date formatting utils — no date-fns dependency. */

export function formatDistanceToNow(dateStr: string): string {
  const date = new Date(dateStr)
  const now = new Date()
  const diffMs = now.getTime() - date.getTime()
  const diffSeconds = Math.floor(diffMs / 1000)

  if (diffSeconds < 60)    return 'agora'
  if (diffSeconds < 3600)  return `${Math.floor(diffSeconds / 60)}min atrás`
  if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)}h atrás`
  if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)}d atrás`

  return date.toLocaleDateString('pt-BR', { day: '2-digit', month: 'short' })
}

export function formatDateTime(dateStr: string): string {
  return new Date(dateStr).toLocaleString('pt-BR', {
    day: '2-digit',
    month: 'short',
    hour: '2-digit',
    minute: '2-digit',
  })
}

export function formatDate(dateStr: string): string {
  return new Date(dateStr).toLocaleDateString('pt-BR', {
    day: '2-digit',
    month: 'long',
    year: 'numeric',
  })
}
