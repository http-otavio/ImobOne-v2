/**
 * POST /api/auth/mfa/challenge
 *
 * Proxy para admin_api POST /admin/auth/mfa/challenge.
 * Recebe { access_token, factor_id, code } no body.
 * Em sucesso: define o cookie de sessão (igual ao fluxo de login normal)
 * e retorna { ok: true, user, expires_at }.
 */

import { NextRequest, NextResponse } from 'next/server'
import { encryptSession, setSessionCookie } from '@/lib/session'

const ADMIN_API = process.env.ADMIN_API_URL ?? 'http://admin_api:8004'

export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as {
      access_token?: string
      factor_id?: string
      code?: string
    }

    const { access_token, factor_id, code } = body

    if (!access_token || !factor_id || !code) {
      return NextResponse.json(
        { detail: 'access_token, factor_id e code são obrigatórios.' },
        { status: 400 },
      )
    }

    const res = await fetch(`${ADMIN_API}/admin/auth/mfa/challenge`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${access_token}`,
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ factor_id, code }),
      cache: 'no-store',
    })

    const data = await res.json()

    if (!res.ok) {
      return NextResponse.json(data, { status: res.status })
    }

    // Verificação bem-sucedida — encripta tokens e seta o cookie de sessão
    const { access_token: newToken, refresh_token, expires_at, user } = data as {
      access_token: string
      refresh_token: string
      expires_at: number
      user: { role: string; client_id: string; corretor_phone: string }
    }

    const encrypted = await encryptSession({
      access_token:  newToken,
      refresh_token: refresh_token,
      expires_at:    expires_at,
    })

    const response = NextResponse.json({
      ok:         true,
      user,
      expires_at,
    })

    setSessionCookie(response, encrypted)

    return response

  } catch (err) {
    console.error('[mfa/challenge] Error:', err)
    return NextResponse.json({ detail: 'Erro interno.' }, { status: 500 })
  }
}
