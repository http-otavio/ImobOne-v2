// ─── Session ─────────────────────────────────────────────────────────────────

export interface SessionPayload {
  access_token: string
  refresh_token: string
  expires_at: number    // unix timestamp (seconds)
  role?: string         // 'dono' | 'corretor' — display hint, NOT a security decision
  nome?: string         // display name from profiles table
}

// ─── User / Profile ───────────────────────────────────────────────────────────

export type UserRole = 'dono' | 'corretor'

export interface UserProfile {
  id: string
  email: string
  role: UserRole
  nome: string
  client_id: string
  mfa_enrolled: boolean
  corretor_phone?: string   // used for assigned_corretor_id resolution
}

// ─── Lead ─────────────────────────────────────────────────────────────────────

export type LeadStatus = 'ativo' | 'quente' | 'visita_agendada' | 'descartado' | 'humano'

export interface Lead {
  id: string
  lead_phone: string
  lead_name: string | null
  intention_score: number
  human_takeover: boolean
  visita_agendada: boolean
  descartado: boolean
  motivo_descarte: string | null
  crm_external_id: string | null
  pipeline_value_brl: number | null
  corretor_notified_at: string | null
  created_at: string
  updated_at: string
  client_id: string
  assigned_corretor_id: string | null
  objections_detected: ObjectionEntry[]
}

export interface ObjectionEntry {
  categoria: string
  mensagem: string
  detectado_em: string
}

// ─── Conversa ─────────────────────────────────────────────────────────────────

export interface Conversa {
  id: string
  lead_phone: string
  role: 'user' | 'assistant'
  content: string
  created_at: string
}

// ─── Alerts ───────────────────────────────────────────────────────────────────

export type AlertType = 'bulk_read' | 'export_attempt'

export interface AnomalyAlert {
  id: string
  user_id: string
  alert_type: AlertType
  detail: Record<string, unknown>
  session_revoked: boolean
  resolved_at: string | null
  created_at: string
  // joined from profiles:
  user_email?: string
  user_nome?: string
}

// ─── Reports ──────────────────────────────────────────────────────────────────

export interface WeeklyReport {
  client_id: string
  period_start: string
  period_end: string
  total_leads: number
  visitas_confirmadas: number
  leads_quentes: number
  pipeline_estimado_brl: number
  top_objecao: string | null
  taxa_conversao: number
  leads_por_origem: Record<string, number>
  generated_at: string
}

// ─── API responses ────────────────────────────────────────────────────────────

export interface ApiError {
  detail: string
}

export interface PaginatedLeads {
  leads: Lead[]
  total: number
  page: number
}
