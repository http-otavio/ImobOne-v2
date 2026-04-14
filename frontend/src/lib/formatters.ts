/**
 * lib/formatters.ts — Domain-aware formatters for ImobOne
 *
 * All formatters are null-safe and return "—" for missing data.
 * Currency uses pt-BR locale. Phone formats Brazilian numbers.
 */

// ─── Currency ────────────────────────────────────────────────────────────────

const BRL = new Intl.NumberFormat('pt-BR', {
  style: 'currency',
  currency: 'BRL',
  maximumFractionDigits: 0,
})

export function formatCurrency(value: number | null | undefined): string {
  if (value == null || value === 0) return '—'
  return BRL.format(value)
}

export function formatCurrencyCompact(value: number | null | undefined): string {
  if (value == null || value === 0) return '—'
  if (value >= 1_000_000) return `R$ ${(value / 1_000_000).toFixed(1)}M`
  if (value >= 1_000)     return `R$ ${(value / 1_000).toFixed(0)}k`
  return BRL.format(value)
}

// ─── Phone ───────────────────────────────────────────────────────────────────

/** Format Brazilian WhatsApp number. Returns "—" for placeholder values. */
export function formatPhone(phone: string | null | undefined): string {
  if (!phone) return '—'
  // Guard against '$phone' template literal bug in DB
  if (phone.startsWith('$') || phone.length < 8) return '—'
  const digits = phone.replace(/\D/g, '')
  // +55 (XX) XXXXX-XXXX (13 digits: 55 + DDD + 9-digit mobile)
  if (digits.length === 13 && digits.startsWith('55')) {
    return `+${digits.slice(0, 2)} (${digits.slice(2, 4)}) ${digits.slice(4, 9)}-${digits.slice(9)}`
  }
  // +55 (XX) XXXX-XXXX (12 digits: 55 + DDD + 8-digit landline)
  if (digits.length === 12 && digits.startsWith('55')) {
    return `+${digits.slice(0, 2)} (${digits.slice(2, 4)}) ${digits.slice(4, 8)}-${digits.slice(8)}`
  }
  return phone
}

/** Returns a safe display name, falling back to phone number. */
export function formatLeadName(
  name: string | null | undefined,
  phone: string | null | undefined,
): string {
  if (name && name.trim()) return name.trim()
  return formatPhone(phone)
}

// ─── Score / Intent ───────────────────────────────────────────────────────────

/** Business-context label for a lead's intention score. */
export function scoreLabel(score: number, visitaAgendada?: boolean): string {
  if (visitaAgendada) return 'Visita Confirmada'
  if (score >= 14) return 'Lead Altamente Qualificado'
  if (score >= 10) return 'Lead Quente — Visita Provável'
  if (score >= 7)  return 'Lead Morno — Em Qualificação'
  if (score >= 4)  return 'Lead em Prospecção'
  return 'Primeiro Contato'
}

export type ScoreTier = 'hot' | 'warm' | 'cold' | 'confirmed'

export function scoreTier(score: number, visitaAgendada?: boolean): ScoreTier {
  if (visitaAgendada) return 'confirmed'
  if (score >= 10)    return 'hot'
  if (score >= 7)     return 'warm'
  return 'cold'
}

/** Tailwind classes for score tier */
export function scoreTierClasses(tier: ScoreTier): {
  bg: string; text: string; border: string; dot: string
} {
  switch (tier) {
    case 'confirmed': return { bg: 'bg-green-900/30',  text: 'text-green-300',  border: 'border-green-800/50',  dot: 'bg-green-400' }
    case 'hot':       return { bg: 'bg-red-900/30',    text: 'text-red-300',    border: 'border-red-800/50',    dot: 'bg-red-400'   }
    case 'warm':      return { bg: 'bg-amber-900/30',  text: 'text-amber-300',  border: 'border-amber-800/50',  dot: 'bg-amber-400' }
    case 'cold':      return { bg: 'bg-slate-800/60',  text: 'text-slate-400',  border: 'border-slate-700/50',  dot: 'bg-slate-500' }
  }
}

// ─── Objections ───────────────────────────────────────────────────────────────

const OBJECTION_LABELS: Record<string, string> = {
  preco:              'Preço',
  prazo_entrega:      'Prazo de Entrega',
  localizacao:        'Localização',
  financiamento:      'Financiamento',
  nao_e_momento:      'Não é o momento',
  concorrencia:       'Concorrência',
  condicoes_pagamento: 'Cond. de Pagamento',
}

export function formatObjectionCategory(cat: string): string {
  return OBJECTION_LABELS[cat] ?? cat.replace(/_/g, ' ')
}
