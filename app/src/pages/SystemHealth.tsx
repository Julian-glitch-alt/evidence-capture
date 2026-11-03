import { useEffect, useState } from 'react'
import { Link } from 'react-router-dom'
import AdminLayout from '../components/AdminLayout'
import { api, formatAge, type FeedbackView, type ModelAccuracy, type SystemHealth as SystemHealthData } from '../api'

export default function SystemHealth() {
  const [health, setHealth] = useState<SystemHealthData>()
  const [feedback, setFeedback] = useState<FeedbackView>()
  const [error, setError] = useState<string>()

  useEffect(() => {
    let live = true
    const load = () =>
      Promise.all([api.health(), api.feedback()])
        .then(([h, f]) => {
          if (live) {
            setHealth(h)
            setFeedback(f)
            setError(undefined)
          }
        })
        .catch((e) => {
          if (live) setError(e instanceof Error ? e.message : 'Could not load system health')
        })
    void load()
    const timer = setInterval(load, 30000)
    return () => {
      live = false
      clearInterval(timer)
    }
  }, [])

  const undelivered = (health?.outbox.dead ?? 0) + (health?.outbox.retrying ?? 0)
  const sweepAt = health?.last_sweep?.at ?? null

  return (
    <AdminLayout>
      <section style={{ display: 'grid', gap: 14 }}>
        <div className="ev-panel ev-pagehead">
          <div>
            <h1>System health</h1>
            <p>Failures in the last 24 hours, and what the adjusters confirmed.</p>
          </div>
        </div>
        {error && (
          <p role="alert" className="ev-err" style={{ padding: '0 6px' }}>
            {error}
          </p>
        )}
        {!health && !error ? (
          <p className="mute" style={{ padding: '0 6px' }}>
            Loading…
          </p>
        ) : (
          health && (
            <>
              <div className="ev-kpis" role="group" aria-label="Failures in the last 24 hours">
                <Tile
                  label="Background checks that failed (24 h)"
                  value={String(health.analyses_failed)}
                  linked={health.claims.length > 0}
                />
                <Tile
                  label="Photo checks that failed (24 h)"
                  value={String(health.photo_checks_failed)}
                  linked={health.claims.length > 0}
                />
                <Tile
                  label="Messages to the claims system that could not be delivered"
                  value={String(undelivered)}
                  linked={health.claims.length > 0}
                  note={`${health.outbox.delivered} delivered`}
                />
                <Tile
                  label="Model calls that failed today"
                  value={`${health.model_calls.errors} of ${health.model_calls.total}`}
                  linked={health.claims.length > 0}
                  note={health.model_calls.error_rate == null ? 'no calls yet' : `${(health.model_calls.error_rate * 100).toFixed(1)}% failed`}
                />
              </div>
              <p className="mute" style={{ padding: '0 6px' }}>
                Last automatic sweep: {sweepAt ? formatAge(sweepAt) : 'never'}
                {health.last_sweep?.error && <span className="ev-err"> ({health.last_sweep.error})</span>}
              </p>
              {health.model && (
                <p className="mute" style={{ padding: '0 6px' }}>
                  Model: {health.model.model} on {health.model.host} · {health.model.retention}
                </p>
              )}
              {health.accuracy && <AccuracyCard accuracy={health.accuracy} />}
              <section className="ev-card" id="claims" aria-label="Claims involved">
                <h2>Claims involved</h2>
                {health.claims.length === 0 ? (
                  <p className="mute">Nothing failing in the last 24 hours.</p>
                ) : (
                  <ul style={{ margin: 0, padding: 0, listStyle: 'none', display: 'grid', gap: 6 }}>
                    {health.claims.map((id) => (
                      <li key={id}>
                        <Link to={`/claims/${id}`} className="ev-linkish mono">
                          {id}
                        </Link>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
              <section className="ev-card" aria-label="Adjuster feedback on flags">
                <h2>Adjuster feedback on flags</h2>
                {!feedback || feedback.rows.length === 0 ? (
                  <p className="mute">No flag has been reviewed yet.</p>
                ) : (
                  <>
                    <div className="ev-tablewrap">
                      <table>
                        <thead>
                          <tr>
                            <th>Reason</th>
                            <th>Confirmed</th>
                            <th>False alarm</th>
                            <th>Confirm rate</th>
                          </tr>
                        </thead>
                        <tbody>
                          {feedback.rows.map((r) => (
                            <tr key={r.reason_code}>
                              <td>{r.reason_code}</td>
                              <td className="num">{r.confirmed}</td>
                              <td className="num">{r.false_alarm}</td>
                              <td className="num">{formatRate(r.confirm_rate, r.ci)}</td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                    <p className="mute" style={{ fontSize: 12.5 }}>
                      Each flag the adjusters confirm or dismiss is counted here. Wide intervals mean too few reviews
                      to tune the weights yet.
                    </p>
                  </>
                )}
              </section>
            </>
          )
        )}
      </section>
    </AdminLayout>
  )
}

function formatRate(rate: number, ci: [number, number] | null) {
  const pct = `${Math.round(rate * 100)}%`
  if (!ci) return pct
  return `${pct} (${Math.round(ci[0] * 100)}–${Math.round(ci[1] * 100)}%)`
}

function Tile({ label, value, linked, note }: { label: string; value: string; linked: boolean; note?: string }) {
  return (
    <div className="ev-kpi static">
      <span className="k">{label}</span>
      {linked ? (
        <a href="#claims" className="v num">
          {value}
        </a>
      ) : (
        <span className="v num">{value}</span>
      )}
      {note && <span className="n">{note}</span>}
    </div>
  )
}


const pct = (v: number) => `${Math.round(v * 100)}%`

function AccuracyCard({ accuracy }: { accuracy: ModelAccuracy }) {
  const a = accuracy
  return (
    <section className="ev-card" aria-label="Model accuracy">
      <h2>
        How well the AI reads photos{' '}
        <small>
          measured {a.measured_on} · {a.model} on {a.host}
        </small>
      </h2>
      <p className="mute" style={{ margin: 0 }}>
        {a.set.photos} photos and {a.set.claims} claims with known answers. {a.set.about}
      </p>
      <div className="ev-tablewrap">
        <table>
          <thead>
            <tr>
              <th>What we checked</th>
              <th>Result</th>
              <th>95% range</th>
            </tr>
          </thead>
          <tbody>
            {a.rows.map((r) => (
              <tr key={r.id}>
                <td>
                  {r.label}
                  {r.lower_is_better && <span className="mute"> (lower is better)</span>}
                </td>
                <td className="num">
                  <b>{r.n ? pct(r.hits / r.n) : 'n/a'}</b> <span className="mute">({r.hits} of {r.n})</span>
                </td>
                <td className="num mute">{r.ci ? `${pct(r.ci[0])} to ${pct(r.ci[1])}` : 'n/a'}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="mute" style={{ margin: 0, fontSize: 12 }}>
        Small sets give wide ranges: read the range, not only the result. {a.cost.calls} model calls, about $
        {a.cost.usd.toFixed(2)} for the whole set; half the calls answered within {a.cost.p50_s.toFixed(0)} s.
      </p>
    </section>
  )
}
