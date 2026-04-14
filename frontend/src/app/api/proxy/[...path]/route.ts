/**
 * /api/proxy/[...path] — BFF proxy to admin_api for Client Components
 *
 * Client Components cannot call admin_api:8004 directly (Docker internal host
 * unreachable from browser). They call /api/proxy/admin/leads which is
 * transparently proxied here.
 *
 * Security:
 * - Token is read from the HttpOnly session cookie — never exposed to JS.
 * - Request body is forwarded as-is; no mutation.
 * - No caching (cache: 'no-store') — same rationale as lib/api.ts.
 */

import { NextRequest, NextResponse } from 'next/server'
import { getSession, isExpired } from '@/lib/session'

const ADMIN_API = process.env.ADMIN_API_URL ?? 'http://admin_api:8004'

type Params = { path: string[] }

async function handler(
  request: NextRequest,
  { params }: { params: Promise<Params> },
) {
  const session = await getSession()
  if (!session || isExpired(session)) {
    return NextResponse.json({ detail: 'Não autenticado.' }, { status: 401 })
  }

  const { path } = await params
  const upstream = `${ADMIN_API}/${path.join('/')}${request.nextUrl.search}`

  // Forward the request with the user's Bearer token
  const proxyRes = await fetch(upstream, {
    method: request.method,
    headers: {
      'Authorization': `Bearer ${session.access_token}`,
      'Content-Type': 'application/json',
    },
    body: ['GET', 'HEAD'].includes(request.method) ? undefined : await request.text(),
    cache: 'no-store',
  })

  const body = await proxyRes.text()
  return new NextResponse(body, {
    status: proxyRes.status,
    headers: {
      'Content-Type': proxyRes.headers.get('Content-Type') ?? 'application/json',
    },
  })
}

export const GET = handler
export const POST = handler
export const PATCH = handler
export const PUT = handler
export const DELETE = handler
