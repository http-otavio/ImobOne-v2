import { NextResponse } from 'next/server'

/** Healthcheck for Docker Swarm. No auth required. */
export async function GET() {
  return NextResponse.json({ status: 'ok', service: 'imob-nextjs' }, { status: 200 })
}
