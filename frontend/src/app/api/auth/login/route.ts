import { NextRequest, NextResponse } from 'next/server'
import { loginWithCredentials, ApiError } from '@/lib/api'
import { encryptSession, setSessionCookie } from '@/lib/session'

export async function POST(request: NextRequest) {
  try {
    const { email, password } = await request.json()

    if (!email || !password) {
      return NextResponse.json(
        { detail: 'Email e senha são obrigatórios.' },
        { status: 400 },
      )
    }

    // Call admin_api (internal Docker network — never exposed via Traefik)
    const authData = await loginWithCredentials(email, password)

    // Encrypt ONLY the tokens into the HttpOnly cookie
    const encrypted = await encryptSession({
      access_token: authData.access_token,
      refresh_token: authData.refresh_token,
      expires_at: authData.expires_at,
    })

    // Return user profile to the client for display (not sensitive, not stored in cookie)
    const response = NextResponse.json({
      ok: true,
      user: authData.user,
      expires_at: authData.expires_at,
    })

    setSessionCookie(response, encrypted)
    return response

  } catch (err) {
    if (err instanceof ApiError) {
      return NextResponse.json({ detail: err.message }, { status: err.status })
    }
    console.error('[auth/login] Unexpected error:', err)
    return NextResponse.json({ detail: 'Erro interno.' }, { status: 500 })
  }
}
