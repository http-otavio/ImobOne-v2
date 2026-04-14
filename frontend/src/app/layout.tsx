import type { Metadata } from 'next'
import './globals.css'

export const metadata: Metadata = {
  title: 'ImobOne — Painel Administrativo',
  description: 'Painel de gestão da plataforma ImobOne',
  robots: 'noindex, nofollow',  // admin panel — never indexed
}

export default function RootLayout({
  children,
}: {
  children: React.ReactNode
}) {
  return (
    <html lang="pt-BR">
      <body className="min-h-screen bg-slate-950 text-slate-100 antialiased">
        {children}
      </body>
    </html>
  )
}
