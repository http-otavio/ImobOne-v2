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
  ChevronDown,
} from 'lucide-react'
import type { UserRole } from '@/types'

interface Props {
  role: UserRole
  email: string
}

export default function NavBar({ role, email }: Props) {
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
          { href: '/dashboard/dono',      label: 'Visão Geral',  icon: LayoutDashboard },
          { href: '/dashboard/leads',      label: 'Leads',        icon: Users },
          { href: '/dashboard/alerts',     label: 'Alertas',      icon: AlertTriangle },
          { href: '/dashboard/reports',    label: 'Relatórios',   icon: FileBarChart },
        ]
      : [
          { href: '/dashboard/corretor',   label: 'Minha Fila',   icon: Users },
          { href: '/dashboard/leads',      label: 'Todos os Leads', icon: LayoutDashboard },
        ]

  return (
    <nav className="sticky top-0 z-50 bg-slate-900/95 backdrop-blur border-b border-slate-800">
      <div className="max-w-7xl mx-auto px-4 sm:px-6">
        <div className="flex items-center justify-between h-14">
          {/* Logo */}
          <Link href="/dashboard" className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-md bg-gradient-to-br from-yellow-500 to-amber-600 flex items-center justify-center">
              <span className="text-slate-900 font-bold text-xs">I</span>
            </div>
            <span className="font-semibold text-slate-100 text-sm">ImobOne</span>
          </Link>

          {/* Nav items */}
          <div className="hidden sm:flex items-center gap-1">
            {navItems.map(({ href, label, icon: Icon }) => (
              <Link
                key={href}
                href={href}
                className={`
                  flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium transition-colors
                  ${isActive(href)
                    ? 'bg-amber-500/15 text-amber-400'
                    : 'text-slate-400 hover:text-slate-200 hover:bg-slate-800'
                  }
                `}
              >
                <Icon className="w-4 h-4" />
                {label}
              </Link>
            ))}
          </div>

          {/* User menu */}
          <div className="flex items-center gap-3">
            <div className="hidden sm:flex items-center gap-1.5 text-slate-400 text-xs">
              <span className={`
                px-1.5 py-0.5 rounded text-xs font-medium
                ${role === 'dono' ? 'bg-amber-900/40 text-amber-400' : 'bg-blue-900/40 text-blue-400'}
              `}>
                {role === 'dono' ? 'Dono' : 'Corretor'}
              </span>
              <span className="truncate max-w-32">{email}</span>
            </div>

            <button
              onClick={handleLogout}
              disabled={loggingOut}
              className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm text-slate-400 hover:text-red-400 hover:bg-slate-800 transition-colors disabled:opacity-50"
              title="Sair"
            >
              <LogOut className="w-4 h-4" />
              <span className="hidden sm:inline">Sair</span>
            </button>
          </div>
        </div>
      </div>
    </nav>
  )
}
