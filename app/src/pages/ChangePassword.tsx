import { useEffect, useState, type FormEvent } from 'react'
import { api } from '../api'

function nextPath(): string {
  const next = new URLSearchParams(window.location.search).get('next') ?? '/'
  const url = new URL(next, window.location.origin)
  return url.origin === window.location.origin ? url.pathname + url.search : '/'
}

/** First sign-in with a one-time password: choose a personal password before anything else. */
export default function ChangePassword() {
  const [current, setCurrent] = useState('')
  const [next, setNext] = useState('')
  const [confirm, setConfirm] = useState('')
  const [error, setError] = useState<string>()
  const [busy, setBusy] = useState(false)

  useEffect(() => {
    api
      .me()
      .then((me) => {
        if (!me.must_change_password) window.location.assign(nextPath())
      })
      .catch(() => undefined)
  }, [])

  const submit = async (e: FormEvent) => {
    e.preventDefault()
    if (next !== confirm) {
      setError('The new passwords do not match')
      return
    }
    setBusy(true)
    setError(undefined)
    try {
      const { user } = await api.changePassword(current, next)
      if (!user.must_change_password) window.location.assign(nextPath())
      else setBusy(false)
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Could not save the password')
      setBusy(false)
    }
  }

  return (
    <div className="ev">
      <div className="ev-authwrap">
        <form onSubmit={submit} className="ev-auth" aria-label="Choose a new password">
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
          <h1>Choose a new password</h1>
          <p className="sub">Sign in with the one-time password you were given, then pick your own.</p>
          <label className="ev-field" htmlFor="current">
            One-time password
            <input
              id="current"
              type="password"
              autoComplete="current-password"
              autoFocus
              value={current}
              onChange={(e) => setCurrent(e.target.value)}
            />
          </label>
          <label className="ev-field" htmlFor="new">
            New password
            <input
              id="new"
              type="password"
              autoComplete="new-password"
              value={next}
              onChange={(e) => setNext(e.target.value)}
            />
          </label>
          <label className="ev-field" htmlFor="confirm">
            New password again
            <input
              id="confirm"
              type="password"
              autoComplete="new-password"
              value={confirm}
              onChange={(e) => setConfirm(e.target.value)}
            />
          </label>
          {error && (
            <p role="alert" className="ev-err" style={{ margin: 0 }}>
              {error}
            </p>
          )}
          <button disabled={busy || !current || !next || !confirm} className="ev-pill primary press">
            {busy ? 'Saving…' : 'Save'}
          </button>
        </form>
      </div>
    </div>
  )
}
