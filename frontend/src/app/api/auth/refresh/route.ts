import { NextRequest, NextResponse } from 'next/server'
import { createClient } from '@supabase/supabase-js'
import { getSession, encryptSession, setSessionCookie } from '@/lib/session'

const SUPABASE_URL = process.env.NEXT_PUBLIC_SUPABASE_URL!
const SUPABASE_ANON_KEY = process.env.NEXT_PUBLIC_SUPABASE_ANON_KEY!

export async function POST(_request: NextRequest) {
  try {
    const session = await getSession()
    if (!session) {
      return NextResponse.json({ detail: 'Sessão não encontrada.' }, { status: 401 })
    }

    // Refresh directly with Supabase Auth (avoids an unnecessary FastAPI hop)
    const supabase = createClient(SUPABASE_URL, SUPABASE_ANON_KEY)
    const { data, error } = await supabase.auth.setSession({
      access_token: session.access_token,
      refresh_token: session.refresh_token,
    })

    if (error || !data.session) {
      return NextResponse.json(
        { detail: 'Sessão expirada. Faça login novamente.' },
        { status: 401 },
      )
    }

    const newSession = data.session

    const encrypted = await encryptSession({
      access_token: newSession.access_token,
      refresh_token: newSession.refresh_token,
      expires_at: newSession.expires_at ?? Math.floor(Date.now() / 1000) + 3600,
    })

    const response = NextResponse.json({
      ok: true,
      expires_at: newSession.expires_at,
    })

    setSessionCookie(response, encrypted)
    return response

  } catch (err) {
    console.error('[auth/refresh] Unexpected error:', err)
    return NextResponse.json({ detail: 'Erro interno.' }, { status: 500 })
  }
}
