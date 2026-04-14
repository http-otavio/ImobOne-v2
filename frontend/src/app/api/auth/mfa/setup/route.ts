/**
 * POST /api/auth/mfa/setup
 *
 * Proxy para admin_api POST /admin/auth/mfa/setup.
 * Recebe { access_token } no body (token temporário do login 403 mfa_required).
 * Retorna { factor_id, totp_uri, qr_code, secret }.
 *
 * Não usa cookie — o usuário ainda não está totalmente autenticado.
 */

import { NextRequest, NextResponse } from 'next/server'

const ADMIN_API = process.env.ADMIN_API_URL ?? 'http://admin_api:8004'

export async function POST(request: NextRequest) {
  try {
    const body = await request.json() as { access_token?: string }
    const accessToken = body.access_token

    if (!accessToken) {
      return NextResponse.json({ detail: 'Token ausente.' }, { status: 400 })
    }

    const res = await fetch(`${ADMIN_API}/admin/auth/mfa/setup`, {
      method: 'POST',
      headers: {
        'Authorization': `Bearer ${accessToken}`,
        'Content-Type': 'application/json',
      },
      cache: 'no-store',
    })

    const data = await res.json()

    if (!res.ok) {
      return NextResponse.json(data, { status: res.status })
    }

    return NextResponse.json(data)

  } catch (err) {
    console.error('[mfa/setup] Error:', err)
    return NextResponse.json({ detail: 'Erro interno.' }, { status: 500 })
  }
}
