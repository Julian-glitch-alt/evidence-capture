import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

export default function DemoLayout({ children }: { children: ReactNode }) {
  return (
    <div className="min-h-full bg-slate-200 text-[13px] text-slate-800">
      <header className="border-b border-slate-600 bg-slate-700 text-slate-100">
        <div className="mx-auto flex max-w-6xl flex-wrap items-center gap-x-4 gap-y-1 px-4 py-2">
          <Link to="/demo-insurer" className="text-sm font-semibold tracking-tight">
            Demo Mutual · Claims System (mock)
          </Link>
          <span className="rounded-sm bg-slate-600 px-1.5 py-0.5 text-[11px] text-slate-200">
            Mock insurer system for the demo
          </span>
          <nav className="ml-auto flex gap-4 text-[12px]">
            <Link to="/demo-insurer" className="text-slate-200 hover:underline">
              Desktop
            </Link>
            <Link to="/demo-insurer/new" className="text-slate-200 hover:underline">
              New claim
            </Link>
            <Link to="/" className="text-slate-300 hover:underline">
              Evidence console →
            </Link>
          </nav>
        </div>
      </header>
      <main className="mx-auto max-w-6xl px-4 py-4">{children}</main>
    </div>
  )
}
