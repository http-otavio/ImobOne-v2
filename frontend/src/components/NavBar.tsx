'use client'

import { useState } from 'react'
import Link from 'next/link'
import { useRouter, usePathname } from 'next/navigation'
import {
  LayoutDashboard,
  Users,
  AlertTriangle,
  FileBarChart,
  LogOut,
} from 'lucide-react'
import type { UserRole } from '@/types'

interface Props {
  role: UserRole
  nome: string
}

export default function NavBar({ role, nome }: Props) {
  const router = useRouter()
  const pathname = usePathname()
  const [loggingOut, setLoggingOut] = useState(false)

  async function handleLogout() {
    setLoggingOut(true)
    await fetch('/api/auth/logout', { method: 'POST' })
    router.push('/auth/login')
  }

  const isActive = (href: string) =>
    pathname === href || pathname.startsWith(href + '/')

  const navItems =
    role === 'dono'
      ? [
          { href: '/dashboard/dono',    label: 'Visão Geral',  icon: LayoutDashboard },
          { href: '/dashboard/leads',    label: 'Leads',        icon: Users },
          { href: '/dashboard/alerts',   label: 'Alertas',      icon: AlertTriangle },
          { href: '/dashboard/reports',  label: 'Relatórios',   icon: FileBarChart },
        ]
      : [
          { href: '/dashboard/corretor', label: 'Minha Fila',    icon: Users },
          { href: '/dashboard/leads',    label: 'Todos os Leads', icon: LayoutDashboard },
        ]

  const displayName = nome || (role === 'dono' ? 'Gestor' : 'Corretor')

  return (
    <nav className="sticky top-0 z-50 bg-slate-900/95 backdrop-blur border-b border-slate-800/70">
      <div className="max-w-7xl mx-auto px-4 sm:px-6">
        <div className="flex items-center justify-between h-14">

          {/* Logo */}
          <Link href="/dashboard" className="flex items-center gap-2.5 flex-shrink-0">
            <div className="w-7 h-7 rounded-md bg-gradient-to-br from-yellow-500 to-amber-600 flex items-center justify-center shadow-lg shadow-amber-900/30">
              <span className="text-slate-900 font-bold text-xs">I</span>
            </div>
            <span className="font-semibold text-slate-100 tracking-tight">ImobOne</span>
          </Link>

          {/* Nav items */}
          <div className="hidden sm:flex items-center gap-0.5">
            {navItems.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className={`
                  flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-all
                  ${isActive(href)
                    ? 'bg-amber-500/15 text-amber-400 shadow-sm'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800/60'
                  }
                `}
              >
                <Icon className="w-3.5 h-3.5" />
                {label}
              </Link>
            ))}
          </div>

          {/* User identity */}
          <div className="flex items-center gap-2">
            <div className="hidden sm:flex items-center gap-2 pr-2 border-r border-slate-800">
              <span className={`
                px-2 py-0.5 rounded-md text-xs font-semibold tracking-wide
                ${role === 'dono'
                  ? 'bg-amber-500/15 text-amber-400 border border-amber-500/20'
                  : 'bg-blue-500/15 text-blue-400 border border-blue-500/20'
                }
              `}>
                {role === 'dono' ? 'Gestor' : 'Corretor'}
              </span>
              <span className="text-slate-400 text-xs truncate max-w-28">{displayName}</span>
            </div>

            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg text-sm text-slate-500 hover:text-red-400 hover:bg-red-900/20 transition-all disabled:opacity-40"
              title="Sair"
            >
              <LogOut className="w-3.5 h-3.5" />
              <span className="hidden sm:inline text-xs">Sair</span>
            </button>
          </div>
        </div>
      </div>
    </nav>
  )
}
