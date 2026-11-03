import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { ApiError, api, todayIso, type Me } from '../api'

function initials(name: string) {
  const parts = name.trim().split(/\s+/).filter(Boolean)
  if (parts.length === 0) return '–'
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase()
  return (parts[0][0] + parts[parts.length - 1][0]).toUpperCase()
}

export default function AdminLayout({ children }: { children: ReactNode }) {
  const { pathname, search } = useLocation()
  const navigate = useNavigate()
  const [me, setMe] = useState<Me | null>(null)
  const [checked, setChecked] = useState(false)
  const [contactEmail, setContactEmail] = useState<string | null>(null)
  const [needs, setNeeds] = useState(0)
  const [attention, setAttention] = useState(0)
  const [q, setQ] = useState('')
  const searchRef = useRef<HTMLInputElement>(null)
  const [sharedDismissed, setSharedDismissed] = useState(false)

  const params = new URLSearchParams(search)
  const tab = params.get('tab')

  useEffect(() => {
    let cancelled = false
    api
      .me()
      .then((user) => {
        if (cancelled) return
        setMe(user)
        setChecked(true)
        let dismissed = false
        try {
          dismissed = window.localStorage.getItem(`ev-shared-dismissed:${user.id}`) === '1'
        } catch {
          // private mode: show the notice
        }
        setSharedDismissed(dismissed)
        if (user.must_change_password && pathname !== '/change-password') {
          const next = encodeURIComponent(pathname)
          window.location.assign(`/change-password?next=${next}`)
        }
      })
      .catch((e) => {
        // A 401 already sends the browser to /login?next=… inside api.request.
        if (e instanceof ApiError && e.status === 401) return
        if (!cancelled) setChecked(true)
      })
    return () => {
      cancelled = true
    }
  }, [pathname])

  useEffect(() => {
    api
      .contact()
      .then((c) => setContactEmail(c.email))
      .catch(() => setContactEmail(null))
  }, [])

  useEffect(() => {
    if (!me) return
    api
      .queue()
      .then((queue) => {
        // Same as Today's "Needs me": test-key claims are not work, and a decided claim keeps its lane but is done.
        const all = queue.groups.filter((g) => g.key !== 'test').flatMap((g) => g.claims)
        const open = queue.groups.filter((g) => g.key !== 'test' && g.key !== 'decided').flatMap((g) => g.claims)
        const needsCount = open.filter((c) => ['siu_referral', 'adjuster_review', 'fast_track', 'standard'].includes(c.lane ?? '')).length
        setNeeds(needsCount)
        const today = todayIso()
        const overdue = open.filter((c) => c.overdue).length
        const diariesToday = all.filter((c) => c.diary_due != null && c.diary_due <= today && !c.overdue).length
        setAttention(overdue + diariesToday)
      })
      .catch(() => undefined)
  }, [pathname, me])

  useEffect(() => {
    if (pathname === '/') setQ(params.get('q') ?? '')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pathname, search])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement | null
      if (e.key === '/' && el !== searchRef.current && el?.tagName !== 'INPUT' && el?.tagName !== 'TEXTAREA') {
        e.preventDefault()
        searchRef.current?.focus()
      }
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [])

  const queueOn = pathname === '/' || pathname.startsWith('/claims')
  const todayOn = pathname === '/' && (tab === null || tab === 'needs')

  const dismissShared = () => {
    if (me) {
      try {
        window.localStorage.setItem(`ev-shared-dismissed:${me.id}`, '1')
      } catch {
        // private mode: the notice simply shows again next visit
      }
    }
    setSharedDismissed(true)
  }

  const submitSearch = (value: string) => {
    setQ(value)
    const next = new URLSearchParams(search)
    if (value.trim()) next.set('q', value.trim())
    else next.delete('q')
    navigate({ pathname: '/', search: next.toString() ? `?${next.toString()}` : '' })
  }

  // T54: nothing of the console (rail, top bar, children and their data
  // requests) mounts until the sign-in check has answered.
  if (!me && !checked) return <div className="ev" style={{ minHeight: '100%' }} />

  return (
    <div className="ev" style={{ paddingBlock: 16, paddingInline: 16 }}>
      <div className="ev-frame">
        <aside className="ev-rail" aria-label="Main navigation">
          <div className="ev-logo">
            <i aria-hidden="true">
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6l7-3z" />
                <path d="M8.6 12.2l2.3 2.3 4.6-4.7" />
              </svg>
            </i>
            <span>
              <b>Evidence Desk</b>
              <small>Demo Mutual Insurance</small>
            </span>
          </div>
          <span className="ev-navlabel">Menu</span>
          <nav className="ev-nav" aria-label="Menu">
            <Link to="/" className={todayOn ? 'on' : ''} aria-current={todayOn ? 'page' : undefined}>
              <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <rect x="4" y="4" width="7" height="7" rx="2" />
                <rect x="13" y="4" width="7" height="7" rx="2" />
                <rect x="4" y="13" width="7" height="7" rx="2" />
                <rect x="13" y="13" width="7" height="7" rx="2" />
              </svg>
              Today
              {needs > 0 && <span className="count">{needs}</span>}
            </Link>
          </nav>
          <span className="ev-navlabel">Admin</span>
          <nav className="ev-nav" aria-label="Admin">
            <Link to="/settings" className={pathname === '/settings' ? 'on' : ''}>
              <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M4 7h10M18 7h2M4 17h4M12 17h8" />
                <circle cx="16" cy="7" r="2" />
                <circle cx="10" cy="17" r="2" />
              </svg>
              Settings
            </Link>
            <Link to="/system" className={pathname === '/system' ? 'on' : ''}>
              <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M3 12h4l2-6 4 12 2-6h6" />
              </svg>
              System health
            </Link>
            <Link to="/developers" className={pathname === '/developers' ? 'on' : ''}>
              <svg viewBox="0 0 24 24" width="19" height="19" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M8 9l-4 3 4 3M16 9l4 3-4 3M13.5 6l-3 12" />
              </svg>
              Developers
            </Link>
          </nav>
          <div className="ev-promo">
            <b>See it in your claims system</b>
            <span>Demo: how the report lands in the insurer&apos;s own claim file.</span>
            <Link to="/demo-insurer">Open the demo</Link>
          </div>
          <div className="ev-me">
            <span className="ev-avatar" aria-hidden="true">
              {me ? initials(me.name) : '–'}
            </span>
            <span>
              <b>{me ? me.name : '…'}</b>
              <br />
              <span className="mute">{me ? (me.role === 'supervisor' ? 'Supervisor' : 'Adjuster') : ''}</span>
            </span>
          </div>
        </aside>

        <main className="ev-main">
          <div className="ev-topbar">
            <label className="ev-search">
              <svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
                <circle cx="11" cy="11" r="7" />
                <path d="M20 20l-3.5-3.5" />
              </svg>
              <input
                ref={searchRef}
                type="search"
                placeholder="Search claim, claimant, policy or vehicle"
                aria-label="Search claims"
                value={q}
                onChange={(e) => submitSearch(e.target.value)}
              />
              <kbd>/</kbd>
            </label>
            <span className="ev-spacer" />
            {contactEmail && (
              <a
                className="ev-signout press"
                href={`mailto:${contactEmail}?subject=${encodeURIComponent('Evidence Desk feedback')}`}
                title={contactEmail}
              >
                Send feedback
              </a>
            )}
            <Link
              to="/?tab=overdue"
              className={`ev-iconbtn press${attention > 0 ? ' dot' : ''}`}
              aria-label={attention === 1 ? '1 overdue review' : `${attention} overdue reviews`}
            >
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" aria-hidden="true">
                <path d="M6 16V11a6 6 0 0 1 12 0v5l1.5 2h-15z" />
                <path d="M10 20a2 2 0 0 0 4 0" />
              </svg>
            </Link>
            <span className="ev-who">
              <span className="ev-avatar" aria-hidden="true">
                {me ? initials(me.name) : '–'}
              </span>
              <span>
                <b>{me ? me.name : '…'}</b>
                <span className="mute">
                  {me ? `${me.email} · ${me.role === 'supervisor' ? 'Supervisor' : 'Adjuster'}` : ''}
                </span>
              </span>
            </span>
            <button type="button" className="ev-signout press" onClick={() => api.logout().finally(() => window.location.assign('/login'))}>
              Sign out
            </button>
          </div>
          {me && !sharedDismissed && (
            <div role="note" aria-label="Shared workspace notice" className="ev-sharednote">
              <span>
                Shared trial workspace: use only your own car and made-up details. Other trial users can see
                these claims. <Link to="/privacy">Privacy</Link>
              </span>
              <button type="button" aria-label="Dismiss the workspace notice" onClick={dismissShared}>
                ×
              </button>
            </div>
          )}
          <div data-queue-on={queueOn || undefined}>{children}</div>
        </main>
      </div>
    </div>
  )
}

const STATUS_LABEL: Record<string, string> = {
  opened: 'Link sent',
  capturing: 'Customer capturing',
  submitted: 'Submitted',
  verifying: 'Verifying',
  assessed: 'Assessed',
  in_review: 'In review',
  approved: 'Approved',
  closed: 'Closed',
}

export function StatusBadge({ status }: { status: string }) {
  const done = !['opened', 'capturing'].includes(status)
  return (
    <span
      className={`rounded-full px-2.5 py-0.5 text-xs font-medium ${
        done ? 'bg-emerald-100 text-emerald-800' : 'bg-slate-100 text-slate-600'
      }`}
    >
      {STATUS_LABEL[status] ?? status.replace(/_/g, ' ')}
    </span>
  )
}

export function PolicyBadge({ verified }: { verified: boolean }) {
  return verified ? (
    <span className="rounded-full bg-emerald-100 px-2.5 py-0.5 text-xs font-medium text-emerald-800">Policy verified</span>
  ) : (
    <span className="rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">Policy not verified</span>
  )
}
