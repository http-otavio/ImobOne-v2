/**
 * POST /api/auth/login
 *
 * Recebe { email, password } e chama admin_api /admin/auth/session.
 *
 * Casos de resposta:
 *  - 200  → login completo, seta cookie HttpOnly, retorna { ok, user, expires_at }
 *  - 403 + mfa_required → passa os tokens temporários para o browser redirecionar
 *                          ao fluxo de enrollment em /auth/mfa (NÃO seta cookie)
 *  - qualquer outro erro → repassa status + detail ao browser
 */

import { NextRequest, NextResponse } from 'next/server'
import { encryptSession, setSessionCookie } from '@/lib/session'

const ADMIN_API = process.env.ADMIN_API_URL ?? 'http://admin_api:8004'

export async function POST(request: NextRequest) {
  try {
    const { email, password } = await request.json()

    if (!email || !password) {
      return NextResponse.json(
        { detail: 'Email e senha são obrigatórios.' },
        { status: 400 },
      )
    }

    // Call admin_api directly — gives us full control over the 403 mfa_required case
    const apiRes = await fetch(`${ADMIN_API}/admin/auth/session`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password }),
      cache: 'no-store',
    })

    const data = await apiRes.json().catch(() => ({})) as Record<string, unknown>

    // ── MFA required: pass tokens through — DO NOT set cookie ────────────────
    if (apiRes.status === 403 && data.mfa_required) {
      return NextResponse.json(
        {
          mfa_required:  true,
          access_token:  data.access_token  ?? '',
          refresh_token: data.refresh_token ?? '',
        },
        { status: 403 },
      )
    }

    // ── Any other non-OK response → relay error ───────────────────────────────
    if (!apiRes.ok) {
      return NextResponse.json(
        { detail: (data.detail as string) ?? 'Credenciais inválidas.' },
        { status: apiRes.status },
      )
    }

    // ── Success → encrypt tokens, set HttpOnly cookie ─────────────────────────
    const { access_token, refresh_token, expires_at, user } = data as {
      access_token: string
      refresh_token: string
      expires_at: number
      user: { role: string; nome?: string; client_id: string; corretor_phone: string }
    }

    const encrypted = await encryptSession({
      access_token,
      refresh_token,
      expires_at,
      role: user?.role ?? 'corretor',
      nome: user?.nome ?? '',
    })

    const response = NextResponse.json({ ok: true, user, expires_at })
    setSessionCookie(response, encrypted)
    return response

  } catch (err) {
    console.error('[auth/login] Unexpected error:', err)
    return NextResponse.json({ detail: 'Erro interno.' }, { status: 500 })
  }
}
