/**
 * lib/api.ts — Server-side fetchers for admin_api
 *
 * ALL fetches use cache: 'no-store'. No exceptions for authenticated routes.
 * Rationale: Next.js Data Cache is a shared server cache keyed by URL,
 * NOT by Authorization header. Caching authenticated responses would serve
 * Corretor A's data to Corretor B. This is a cross-tenant data leak.
 *
 * The SSR round-trip to admin_api via Docker internal network takes ~10–30ms.
 * That is the correct trade-off for a B2B panel with RLS-enforced data.
 */

import type {
  Lead,
  Conversa,
  AnomalyAlert,
  WeeklyReport,
  UserProfile,
  PaginatedLeads,
} from '@/types'

const ADMIN_API = process.env.ADMIN_API_URL ?? 'http://admin_api:8004'

// ─── Fetch helper ─────────────────────────────────────────────────────────────

interface FetchOptions {
  method?: string
  body?: unknown
}

async function adminFetch<T>(
  path: string,
  token: string,
  options: FetchOptions = {},
): Promise<T> {
  const res = await fetch(`${ADMIN_API}${path}`, {
    method: options.method ?? 'GET',
    headers: {
      'Authorization': `Bearer ${token}`,
      'Content-Type': 'application/json',
    },
    body: options.body ? JSON.stringify(options.body) : undefined,
    // ── CRITICAL: no Data Cache on any authenticated route ──────────────────
    cache: 'no-store',
  })

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: res.statusText }))
    throw new ApiError(err.detail ?? 'Erro desconhecido', res.status)
  }

  return res.json() as Promise<T>
}

export class ApiError extends Error {
  constructor(message: string, public status: number) {
    super(message)
    this.name = 'ApiError'
  }
}

// ─── Auth ─────────────────────────────────────────────────────────────────────

export async function loginWithCredentials(
  email: string,
  password: string,
): Promise<{
  access_token: string
  refresh_token: string
  expires_at: number
  user: UserProfile
}> {
  const res = await fetch(`${ADMIN_API}/admin/auth/session`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, password }),
    cache: 'no-store',
  })

  if (!res.ok) {
    const err = await res.json().catch(() => ({ detail: 'Credenciais inválidas' }))
    throw new ApiError(err.detail ?? 'Erro de autenticação', res.status)
  }

  return res.json()
}

// ─── Leads ────────────────────────────────────────────────────────────────────

export async function getLeads(
  token: string,
  params?: { status?: string; page?: number; limit?: number },
): Promise<PaginatedLeads> {
  const qs = new URLSearchParams()
  if (params?.status) qs.set('status', params.status)
  if (params?.page)   qs.set('page',   String(params.page))
  if (params?.limit)  qs.set('limit',  String(params.limit))
  const query = qs.toString() ? `?${qs}` : ''
  return adminFetch<PaginatedLeads>(`/admin/leads${query}`, token)
}

export async function getLead(token: string, phone: string): Promise<Lead> {
  return adminFetch<Lead>(`/admin/leads/${encodeURIComponent(phone)}`, token)
}

export async function getConversation(
  token: string,
  phone: string,
): Promise<Conversa[]> {
  return adminFetch<Conversa[]>(
    `/admin/leads/${encodeURIComponent(phone)}/conversation`,
    token,
  )
}

// ─── Takeover ─────────────────────────────────────────────────────────────────

export async function startTakeover(
  token: string,
  phone: string,
): Promise<{ ok: boolean }> {
  return adminFetch<{ ok: boolean }>(
    `/admin/leads/${encodeURIComponent(phone)}/takeover`,
    token,
    { method: 'POST' },
  )
}

export async function returnToSofia(
  token: string,
  phone: string,
): Promise<{ ok: boolean }> {
  return adminFetch<{ ok: boolean }>(
    `/admin/leads/${encodeURIComponent(phone)}/takeover/return`,
    token,
    { method: 'POST' },
  )
}

export async function sendMessageAsTakeover(
  token: string,
  phone: string,
  message: string,
): Promise<{ ok: boolean }> {
  return adminFetch<{ ok: boolean }>(
    `/admin/leads/${encodeURIComponent(phone)}/messages`,
    token,
    { method: 'POST', body: { message } },
  )
}

// ─── Alerts ───────────────────────────────────────────────────────────────────

export async function getAlerts(token: string): Promise<AnomalyAlert[]> {
  return adminFetch<AnomalyAlert[]>('/admin/alerts', token)
}

export async function resolveAlert(
  token: string,
  alertId: string,
): Promise<{ ok: boolean }> {
  return adminFetch<{ ok: boolean }>(
    `/admin/alerts/${alertId}/resolve`,
    token,
    { method: 'PATCH' },
  )
}

// ─── Reports ──────────────────────────────────────────────────────────────────

export async function getWeeklyReport(
  token: string,
  clientId: string,
): Promise<WeeklyReport> {
  return adminFetch<WeeklyReport>(
    `/admin/reports/weekly?client_id=${encodeURIComponent(clientId)}`,
    token,
  )
}
