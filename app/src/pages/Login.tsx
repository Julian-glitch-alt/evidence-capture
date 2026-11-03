import { useState, type FormEvent } from 'react'
import { Link } from 'react-router-dom'
import { api } from '../api'

function nextPath(): string {
  const next = new URLSearchParams(window.location.search).get('next') ?? '/'
  // Resolve `next` against this origin and only follow it if it stays here.
  const url = new URL(next, window.location.origin)
  return url.origin === window.location.origin ? url.pathname + url.search : '/'
}

export default function Login() {
  const [email, setEmail] = useState('')
  const [password, setPassword] = useState('')
  const [error, setError] = useState<string>()
  const [busy, setBusy] = useState(false)

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    setBusy(true)
    setError(undefined)
    try {
      const { user } = await api.login(email, password)
      if (user.must_change_password) {
        const next = encodeURIComponent(nextPath())
        window.location.assign(`/change-password?next=${next}`)
      } else {
        window.location.assign(nextPath())
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Login failed')
      setBusy(false)
    }
  }

  return (
    <div className="ev">
      <div className="ev-authwrap">
        <form onSubmit={submit} className="ev-auth" aria-label="Adjuster sign in">
          <span className="ev-brandline" aria-hidden="true">
            <i>
              <svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="#fff" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M12 3l7 3v5c0 4.5-3 8.2-7 10-4-1.8-7-5.5-7-10V6l7-3z" />
                <path d="M8.6 12.2l2.3 2.3 4.6-4.7" />
              </svg>
            </i>
            <span>
              <b>Evidence Desk</b>
            </span>
          </span>
          <h1>Adjuster sign in</h1>
          <p className="sub">Demo Mutual Insurance · claims desk</p>
          <label className="ev-field" htmlFor="email">
            Email
            <input
              id="email"
              type="text"
              autoComplete="username"
              autoFocus
              value={email}
              onChange={(e) => setEmail(e.target.value)}
            />
          </label>
          <label className="ev-field" htmlFor="password">
            Password
            <input
              id="password"
              type="password"
              autoComplete="current-password"
              value={password}
              onChange={(e) => setPassword(e.target.value)}
            />
          </label>
          {error && (
            <p role="alert" className="ev-err" style={{ margin: 0 }}>
              {error}
            </p>
          )}
          <button disabled={busy || !email || !password} className="ev-pill primary press">
            {busy ? 'Signing in…' : 'Sign in'}
          </button>
        </form>
        <p className="mute" style={{ marginTop: 12, fontSize: 12.5 }}>
          <Link to="/privacy">Privacy</Link>
        </p>
      </div>
    </div>
  )
}
