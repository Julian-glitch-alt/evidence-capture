import { useCallback, useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import {
  api,
  formatAge,
  formatDueDate,
  initialsOf,
  type FeedbackView,
  type QueueClaim,
  type QueueView,
} from '../api'
import AdminLayout from '../components/AdminLayout'
import { isNeedsLane, lanePillClass, lanePillLabel } from './claim/shared'

type Tab = 'needs' | 'overdue' | 'waiting' | 'decided' | 'all' | 'test'

const TABS: { key: Tab; label: string }[] = [
  { key: 'needs', label: 'Needs me' },
  { key: 'overdue', label: 'Overdue' },
  { key: 'waiting', label: 'Waiting for claimant' },
  { key: 'decided', label: 'Decided' },
  { key: 'all', label: 'All' },
  { key: 'test', label: 'Test claims' },
]

const US_STATES: { code: string; name: string }[] = [
  { code: 'AL', name: 'Alabama' },
  { code: 'AK', name: 'Alaska' },
  { code: 'AZ', name: 'Arizona' },
  { code: 'AR', name: 'Arkansas' },
  { code: 'CA', name: 'California' },
  { code: 'CO', name: 'Colorado' },
  { code: 'CT', name: 'Connecticut' },
  { code: 'DE', name: 'Delaware' },
  { code: 'DC', name: 'District of Columbia' },
  { code: 'FL', name: 'Florida' },
  { code: 'GA', name: 'Georgia' },
  { code: 'HI', name: 'Hawaii' },
  { code: 'ID', name: 'Idaho' },
  { code: 'IL', name: 'Illinois' },
  { code: 'IN', name: 'Indiana' },
  { code: 'IA', name: 'Iowa' },
  { code: 'KS', name: 'Kansas' },
  { code: 'KY', name: 'Kentucky' },
  { code: 'LA', name: 'Louisiana' },
  { code: 'ME', name: 'Maine' },
  { code: 'MD', name: 'Maryland' },
  { code: 'MA', name: 'Massachusetts' },
  { code: 'MI', name: 'Michigan' },
  { code: 'MN', name: 'Minnesota' },
  { code: 'MS', name: 'Mississippi' },
  { code: 'MO', name: 'Missouri' },
  { code: 'MT', name: 'Montana' },
  { code: 'NE', name: 'Nebraska' },
  { code: 'NV', name: 'Nevada' },
  { code: 'NH', name: 'New Hampshire' },
  { code: 'NJ', name: 'New Jersey' },
  { code: 'NM', name: 'New Mexico' },
  { code: 'NY', name: 'New York' },
  { code: 'NC', name: 'North Carolina' },
  { code: 'ND', name: 'North Dakota' },
  { code: 'OH', name: 'Ohio' },
  { code: 'OK', name: 'Oklahoma' },
  { code: 'OR', name: 'Oregon' },
  { code: 'PA', name: 'Pennsylvania' },
  { code: 'RI', name: 'Rhode Island' },
  { code: 'SC', name: 'South Carolina' },
  { code: 'SD', name: 'South Dakota' },
  { code: 'TN', name: 'Tennessee' },
  { code: 'TX', name: 'Texas' },
  { code: 'UT', name: 'Utah' },
  { code: 'VT', name: 'Vermont' },
  { code: 'VA', name: 'Virginia' },
  { code: 'WA', name: 'Washington' },
  { code: 'WV', name: 'West Virginia' },
  { code: 'WI', name: 'Wisconsin' },
  { code: 'WY', name: 'Wyoming' },
]

function groupOf(queue: QueueView | undefined, id: string): string | null {
  if (!queue) return null
  for (const g of queue.groups) {
    if (g.claims.some((c) => c.id === id)) return g.key
  }
  return null
}

function sortClaims(list: QueueClaim[]) {
  return [...list].sort((a, b) => {
    const ao = a.overdue ? 0 : 1
    const bo = b.overdue ? 0 : 1
    if (ao !== bo) return ao - bo
    const ad = a.review_due ?? ''
    const bd = b.review_due ?? ''
    if (ad !== bd) {
      if (!ad) return 1
      if (!bd) return -1
      return ad < bd ? -1 : 1
    }
    return (b.created_at ?? '') < (a.created_at ?? '') ? -1 : 1
  })
}

function matchesQuery(c: QueueClaim, holder: string, q: string) {
  const hay = [c.id, holder, c.policy_number, c.vehicle ?? '', c.claim_type ?? ''].join(' ').toLowerCase()
  return hay.includes(q)
}

function holderOf(c: QueueClaim) {
  return c.holder ?? '—'
}

function visibleClaims(
  q: string,
  tab: Tab,
  all: QueueClaim[],
  needs: QueueClaim[],
  overdue: QueueClaim[],
  waiting: QueueClaim[],
  decided: QueueClaim[],
  tests: QueueClaim[],
) {
  if (q) return sortClaims([...all, ...tests].filter((c) => matchesQuery(c, holderOf(c), q)))
  if (tab === 'needs') return sortClaims(needs)
  if (tab === 'overdue') return sortClaims(overdue)
  if (tab === 'waiting') return [...waiting].sort((a, b) => (a.created_at ?? '') < (b.created_at ?? '') ? -1 : 1)
  if (tab === 'decided') return decided
  if (tab === 'test') return sortClaims(tests)
  return sortClaims(all)
}

function statusLineText(needs: number, overdue: number, waiting: number) {
  const date = new Date().toLocaleDateString(undefined, { weekday: 'long', month: 'long', day: 'numeric' })
  const bits: string[] = []
  bits.push(`${needs} need${needs === 1 ? 's' : ''} a decision`)
  if (overdue > 0) bits.push(`${overdue} overdue`)
  if (waiting > 0) bits.push(`${waiting} waiting for the claimant`)
  if (bits.length === 0) return `${date}. The queue is clear.`
  return `${date}. ${bits.join(', ')}.`
}

export default function ClaimList() {
  const location = useLocation()
  const navigate = useNavigate()
  const params = new URLSearchParams(location.search)
  const tabParam = params.get('tab')
  const tab: Tab =
    tabParam === 'overdue' || tabParam === 'waiting' || tabParam === 'decided' || tabParam === 'all' || tabParam === 'test'
      ? tabParam
      : 'needs'
  const q = (params.get('q') ?? '').trim().toLowerCase()

  const [queue, setQueue] = useState<QueueView>()
  const [queueFailed, setQueueFailed] = useState(false)
  const [feedback, setFeedback] = useState<FeedbackView>()
  const [feedbackFailed, setFeedbackFailed] = useState(false)
  const [mode, setMode] = useState<'claim' | 'demo'>('claim')
  const [policyNumber, setPolicyNumber] = useState('')
  const [holderName, setHolderName] = useState('')
  const [trialState, setTrialState] = useState('CA')
  const [year, setYear] = useState('')
  const [make, setMake] = useState('')
  const [model, setModel] = useState('')
  const [plate, setPlate] = useState('')
  const [vin, setVin] = useState('')
  const [collisionDeductible, setCollisionDeductible] = useState('500')
  const [comprehensiveDeductible, setComprehensiveDeductible] = useState('250')
  const [trialError, setTrialError] = useState<string>()
  const [opening, setOpening] = useState(false)
  const [showForm, setShowForm] = useState(false)
  const [help, setHelp] = useState(false)
  const [limit, setLimit] = useState(25)
  const [userId, setUserId] = useState<string | null>(null)
  const [guideDismissed, setGuideDismissed] = useState(false)

  const load = useCallback(() => {
    api
      .queue()
      .then((q) => {
        setQueue(q)
        setQueueFailed(false)
      })
      .catch(() => setQueueFailed(true))
  }, [])

  useEffect(() => {
    api
      .feedback()
      .then((f) => {
        setFeedback(f)
        setFeedbackFailed(false)
      })
      .catch(() => setFeedbackFailed(true))
    api
      .me()
      .then((m) => {
        setUserId(m.id)
        let dismissed = false
        try {
          dismissed = window.localStorage.getItem(`ev-try-dismissed:${m.id}`) === '1'
        } catch {
          // private mode: show the guide
        }
        setGuideDismissed(dismissed)
      })
      .catch(() => undefined)
  }, [])

  const dismissGuide = () => {
    if (userId) {
      try {
        window.localStorage.setItem(`ev-try-dismissed:${userId}`, '1')
      } catch {
        // private mode: the guide simply shows again next visit
      }
    }
    setGuideDismissed(true)
  }

  useEffect(() => {
    load()
    const timer = setInterval(load, 5000)
    return () => clearInterval(timer)
  }, [load])

  const setTab = (t: Tab) => {
    setLimit(25)
    const next = new URLSearchParams(location.search)
    next.delete('q')
    if (t === 'needs') next.delete('tab')
    else next.set('tab', t)
    navigate({ pathname: '/', search: next.toString() ? `?${next.toString()}` : '' })
  }

  useEffect(() => {
    setLimit(25)
  }, [tab, q])

  /** T56: numeric fields go over the wire as numbers when they are digits. */
  const deductibleOf = (v: string) => {
    const t = v.trim()
    return /^\d+$/.test(t) ? parseInt(t, 10) : t
  }

  const createTrial = async () => {
    setOpening(true)
    setTrialError(undefined)
    try {
      const trimmedYear = year.trim()
      const trimmedPolicy = policyNumber.trim()
      const c = await api.createTrialClaim({
        ...(trimmedPolicy ? { policy_number: trimmedPolicy } : {}),
        holder_name: holderName.trim(),
        state: trialState,
        vehicle: {
          year: /^\d+$/.test(trimmedYear) ? parseInt(trimmedYear, 10) : trimmedYear,
          make: make.trim(),
          model: model.trim(),
          plate: plate.trim(),
          vin: vin.trim(),
        },
        collision_deductible: deductibleOf(collisionDeductible),
        comprehensive_deductible: deductibleOf(comprehensiveDeductible),
      })
      load()
      navigate(`/claims/${c.id}`)
    } catch (e) {
      setTrialError(e instanceof Error ? e.message : 'Could not open the claim')
    } finally {
      setOpening(false)
    }
  }

  const createDemo = async () => {
    setOpening(true)
    setTrialError(undefined)
    try {
      const c = await api.createClaim('DEMO-AUTO-1001')
      load()
      navigate(`/claims/${c.id}`)
    } catch (e) {
      setTrialError(e instanceof Error ? e.message : 'Could not open the demo claim')
    } finally {
      setOpening(false)
    }
  }

  // Claims from a test API key sit in the server's "test" group: their own tab, never counted as work.
  const all = useMemo(() => queue?.groups.filter((g) => g.key !== 'test').flatMap((g) => g.claims) ?? [], [queue])
  const tests = useMemo(() => queue?.groups.find((g) => g.key === 'test')?.claims ?? [], [queue])
  const needs = useMemo(() => all.filter((c) => isNeedsLane(c.lane) && groupOf(queue, c.id) !== 'decided'), [all, queue])
  const overdue = useMemo(() => needs.filter((c) => c.overdue), [needs])
  const waiting = useMemo(
    () => all.filter((c) => groupOf(queue, c.id) === 'capturing'),
    [all, queue],
  )
  const decided = useMemo(() => all.filter((c) => groupOf(queue, c.id) === 'decided'), [all, queue])

  const counts: Record<Tab, number> = {
    needs: needs.length,
    overdue: overdue.length,
    waiting: waiting.length,
    decided: decided.length,
    all: all.length,
    test: tests.length,
  }

  const visible = visibleClaims(q, tab, all, needs, overdue, waiting, decided, tests)
  const shown = visible.slice(0, limit)
  const remaining = visible.length - shown.length

  const loading = queue === undefined && !queueFailed
  const statusLine =
    queue === undefined
      ? queueFailed
        ? 'Could not load claims. Trying again every 5 seconds.'
        : 'Loading claims…'
      : statusLineText(needs.length, overdue.length, waiting.length)

  const laneCounts = useMemo(() => {
    const lanes = ['siu_referral', 'adjuster_review', 'fast_track', 'standard'] as const
    const names: Record<(typeof lanes)[number], string> = {
      siu_referral: 'SIU',
      adjuster_review: 'review',
      fast_track: 'fast track',
      standard: 'standard',
    }
    return lanes
      .map((l) => ({ lane: l, n: needs.filter((c) => c.lane === l).length, name: names[l] }))
      .filter((x) => x.n > 0)
  }, [needs])

  const oldestWaiting = waiting.length > 0 ? waiting[0] : null
  const firstOverdue = useMemo(() => sortClaims(overdue)[0] ?? null, [overdue])

  const linkAvg = useMemo(() => {
    const a = queue?.averages['link_to_submit']
    const b = queue?.averages['submit_to_report']
    if (a?.mean_s == null || b?.mean_s == null) return null
    return Math.round((a.mean_s + b.mean_s) / 60)
  }, [queue])

  return (
    <AdminLayout>
      <section style={{ display: 'grid', gap: 14 }}>
        <div className="ev-panel ev-pagehead">
          <div>
            <h1>Today</h1>
            <p>{statusLine}</p>
          </div>
          <div className="acts">
            <button type="button" className="ev-pill primary press" onClick={() => setShowForm((v) => !v)} aria-expanded={showForm}>
              New capture link
            </button>
          </div>
        </div>

        {userId && !guideDismissed && (
          <div className="ev-card" aria-label="Trial guide">
            <h2>Try it in 3 steps</h2>
            <ol className="mute" style={{ margin: 0, paddingLeft: 20, display: 'grid', gap: 6, fontSize: 13, listStyle: 'decimal' }}>
              <li>New capture link → Open a claim, with your car&apos;s VIN and plate.</li>
              <li>Scan the QR code with your phone, answer the questions and take the photos live.</li>
              <li>
                Come back here about a minute after sending: open the claim to read the review and the report,
                then decide.
              </li>
            </ol>
            <div style={{ display: 'flex', gap: 10, flexWrap: 'wrap' }}>
              <button type="button" className="ev-pill primary press" onClick={() => setShowForm(true)}>
                New capture link
              </button>
              <button type="button" className="ev-pill press" aria-label="Dismiss the guide" onClick={dismissGuide}>
                Dismiss
              </button>
            </div>
          </div>
        )}

        {showForm && (
          <div className="ev-card">
            <h2>
              Open a claim <small>Policy number → a link and QR code</small>
            </h2>
            <div className="ev-seg" role="group" aria-label="Policy source">
              <button type="button" className="press" aria-pressed={mode === 'claim'} onClick={() => setMode('claim')}>
                Open a claim
              </button>
              <button type="button" className="press" aria-pressed={mode === 'demo'} onClick={() => setMode('demo')}>
                Try the demo
              </button>
            </div>
            {mode === 'demo' ? (
              <form
                style={{ display: 'grid', gap: 10 }}
                onSubmit={(e) => {
                  e.preventDefault()
                  void createDemo()
                }}
              >
                <p className="mute" style={{ fontSize: 13 }}>
                  A sample policy for a 2020 Honda Accord, plate DEMO101, to see how it works. Photos of any other
                  car are flagged as a different vehicle: that is the check working.
                </p>
                {trialError && (
                  <p role="alert" className="ev-err">
                    {trialError}
                  </p>
                )}
                <div>
                  <button type="submit" disabled={opening} className="ev-pill primary press">
                    {opening ? 'Opening…' : 'Open a demo claim'}
                  </button>
                </div>
              </form>
            ) : (
              <form
                style={{ display: 'grid', gap: 10 }}
                onSubmit={(e) => {
                  e.preventDefault()
                  void createTrial()
                }}
              >
                <p className="mute" style={{ fontSize: 13 }}>
                  Enter the policy as it is in your policy system. The photos are checked against this car.
                </p>
                <div className="ev-trialgrid">
                  <div className="span2" style={{ display: 'grid', gap: 6 }}>
                    <label className="ev-field">
                      Policy number
                      <input value={policyNumber} onChange={(e) => setPolicyNumber(e.target.value)} placeholder="e.g. PAS-778812" autoComplete="off" aria-describedby="trial-policy-hint" />
                    </label>
                    <span id="trial-policy-hint" className="mute" style={{ fontSize: 12 }}>
                      Leave empty to make one up for a test.
                    </span>
                  </div>
                  <label className="ev-field span2">
                    Policyholder name
                    <input value={holderName} onChange={(e) => setHolderName(e.target.value)} autoComplete="name" />
                  </label>
                  <label className="ev-field">
                    State
                    <select value={trialState} onChange={(e) => setTrialState(e.target.value)}>
                      {US_STATES.map((s) => (
                        <option key={s.code} value={s.code}>
                          {s.name}
                        </option>
                      ))}
                    </select>
                  </label>
                  <label className="ev-field">
                    Year
                    <input value={year} onChange={(e) => setYear(e.target.value)} inputMode="numeric" placeholder="e.g. 2021" />
                  </label>
                  <label className="ev-field">
                    Make
                    <input value={make} onChange={(e) => setMake(e.target.value)} placeholder="e.g. Honda" />
                  </label>
                  <label className="ev-field">
                    Model
                    <input value={model} onChange={(e) => setModel(e.target.value)} placeholder="e.g. Civic" />
                  </label>
                  <label className="ev-field">
                    Plate
                    <input value={plate} onChange={(e) => setPlate(e.target.value)} placeholder="e.g. 7ABC123" />
                  </label>
                  <div className="span2" style={{ display: 'grid', gap: 6 }}>
                    <label className="ev-field">
                      VIN
                      <input value={vin} onChange={(e) => setVin(e.target.value)} placeholder="17 characters" autoComplete="off" aria-describedby="trial-vin-hint" />
                    </label>
                    <span id="trial-vin-hint" className="mute" style={{ fontSize: 12 }}>
                      On your insurance card, registration, or at the bottom of the windshield.
                    </span>
                  </div>
                  <label className="ev-field">
                    Collision deductible
                    <input type="number" value={collisionDeductible} onChange={(e) => setCollisionDeductible(e.target.value)} inputMode="numeric" min={0} max={10000} />
                  </label>
                  <label className="ev-field">
                    Comprehensive deductible
                    <input type="number" value={comprehensiveDeductible} onChange={(e) => setComprehensiveDeductible(e.target.value)} inputMode="numeric" min={0} max={10000} />
                  </label>
                </div>
                <p className="mute" style={{ fontSize: 12 }}>
                  During the trial, please don&apos;t use customer data.{' '}
                  <a href="/privacy" target="_blank" rel="noopener">
                    Privacy notice
                  </a>
                </p>
                {trialError && (
                  <p role="alert" className="ev-err">
                    {trialError}
                  </p>
                )}
                <div>
                  <button type="submit" disabled={opening} className="ev-pill primary press">
                    {opening ? 'Creating…' : 'Create capture link'}
                  </button>
                </div>
              </form>
            )}
          </div>
        )}

        <div className="ev-kpis">
          <button type="button" className="ev-kpi hero press" data-tab="needs" aria-pressed={!q && tab === 'needs'} onClick={() => setTab('needs')}>
            <span className="k">Waiting for your decision</span>
            {queue === undefined ? (
              <>
                <Sk h={44} w="55%" />
                <Sk h={14} w="85%" />
              </>
            ) : (
              <>
                <span className="v num">{needs.length}</span>
                <span className="n">{laneCounts.length > 0 ? laneCounts.map((x) => `${x.n} ${x.name}`).join(', ') : 'queue empty'}</span>
              </>
            )}
          </button>
          <button type="button" className="ev-kpi press" aria-pressed={!q && tab === 'overdue'} onClick={() => setTab('overdue')}>
            <span className="k">Overdue</span>
            {queue === undefined ? (
              <>
                <Sk h={44} w="40%" />
                <Sk h={14} w="80%" />
              </>
            ) : (
              <>
                <span className="v num">{overdue.length}</span>
                <span className="n">
                  {firstOverdue ? `${holderOf(firstOverdue)}, past the review date` : 'Nothing overdue'}
                </span>
              </>
            )}
          </button>
          <button type="button" className="ev-kpi press" aria-pressed={!q && tab === 'waiting'} onClick={() => setTab('waiting')}>
            <span className="k">Waiting for the claimant</span>
            {queue === undefined ? (
              <>
                <Sk h={44} w="40%" />
                <Sk h={14} w="80%" />
              </>
            ) : (
              <>
                <span className="v num">{waiting.length}</span>
                <span className="n">
                  {oldestWaiting ? `${formatAge(oldestWaiting.created_at)}, ${holderOf(oldestWaiting)}, no photos yet` : 'No one waiting'}
                </span>
              </>
            )}
          </button>
          <button type="button" className="ev-kpi press" onClick={() => navigate('/system')}>
            <span className="k">Link to report</span>
            {queue === undefined ? (
              <>
                <Sk h={36} w="50%" />
                <Sk h={14} w="90%" />
              </>
            ) : (
              <>
                <span className="v sm num">
                  {linkAvg != null ? `${linkAvg} min` : '–'}
                  <span style={{ fontSize: 14, fontWeight: 600, color: 'var(--mute)' }}> average</span>
                </span>
                <span className="n">From link sent to report ready</span>
              </>
            )}
          </button>
        </div>

        <div className="ev-grid-today">
          <div className="ev-tablecard" aria-busy={loading ? true : undefined}>
            <header>
              <h2>{q ? `Results for “${params.get('q')?.trim() ?? ''}”` : 'Claims'}</h2>
              <button type="button" className="ev-linkish press" onClick={() => setHelp((v) => !v)} aria-expanded={help}>
                {help ? 'Hide the label guide' : 'How to read the labels'}
              </button>
              <div className="ev-seg" role="group" aria-label="Filter claims">
                {TABS.filter((t) => t.key !== 'test' || tests.length > 0 || tab === 'test').map((t) => (
                  <button key={t.key} type="button" className="press" aria-pressed={!q && tab === t.key} onClick={() => setTab(t.key)}>
                    {t.label}
                    {queue !== undefined && <em>{counts[t.key]}</em>}
                  </button>
                ))}
              </div>
            </header>
            {help && <LabelHelp />}
            <div className="ev-tablewrap">
              <table className="ev-claims">
                <thead>
                  <tr>
                    <th>Claimant</th>
                    <th>Claim</th>
                    <th>Suggested lane</th>
                    <th>Why it is here</th>
                    <th>Due</th>
                  </tr>
                </thead>
                <tbody>
                  {queue === undefined
                    ? Array.from({ length: 6 }, (_, i) => (
                        <tr key={`sk-${i}`} aria-hidden="true">
                          <td>
                            <Sk h={14} w="70%" />
                          </td>
                          <td>
                            <Sk h={14} w="60%" />
                          </td>
                          <td>
                            <Sk h={14} w="80%" />
                          </td>
                          <td>
                            <Sk h={14} w="90%" />
                          </td>
                          <td>
                            <Sk h={14} w="50%" />
                          </td>
                        </tr>
                      ))
                    : shown.map((c) => (
                        <QueueRow key={c.id} claim={c} group={groupOf(queue, c.id)} onOpen={() => navigate(`/claims/${c.id}`)} />
                      ))}
                </tbody>
              </table>
              {queue !== undefined && visible.length === 0 && (
                <p className="mute" style={{ padding: '28px 20px' }}>
                  No claim matches. Search by claim number, name, policy or vehicle.
                </p>
              )}
            </div>
            {remaining > 0 && (
              <div style={{ padding: '12px 20px 16px' }}>
                <button type="button" className="ev-pill sm press" onClick={() => setLimit((l) => l + 25)}>
                  Show {Math.min(25, remaining)} more of {remaining}
                </button>
              </div>
            )}
          </div>

          <div className="ev-sidecol">
            <div className="ev-card">
              <h2>
                Claims this week{' '}
                {queue !== undefined && (
                  <small>
                    {weekBuckets(all).total} total
                  </small>
                )}
              </h2>
              {queue === undefined ? (
                <span aria-hidden="true" style={{ display: 'grid', gap: 8 }}>
                  <Sk h={14} w="100%" />
                  <Sk h={14} w="100%" />
                  <Sk h={14} w="100%" />
                </span>
              ) : (
                <WeekBars claims={all} />
              )}
              <div className="ev-legend">
                <span><i style={{ background: 'var(--fast)' }} />Fast track</span>
                <span><i style={{ background: 'var(--std)' }} />Standard</span>
                <span><i style={{ background: 'var(--review)' }} />Review</span>
                <span><i style={{ background: 'var(--siu)' }} />SIU</span>
              </div>
            </div>
            <div className="ev-card">
              <h2>
                Flags adjusters confirmed <small>all reviews so far</small>
              </h2>
              <FeedbackPanel feedback={feedback} failed={feedbackFailed} />
            </div>
          </div>
        </div>
      </section>
    </AdminLayout>
  )
}

function QueueRow({ claim, group, onOpen }: { claim: QueueClaim; group: string | null; onOpen: () => void }) {
  const holder = holderOf(claim)
  const lane = group === 'decided' ? 'decided' : (claim.lane ?? null)
  const decidedLabel = group === 'decided' ? claim.headline ?? 'Decided' : null
  const capturingWhy =
    (claim.item_count ?? 0) > 0
      ? `${claim.item_count} photo${claim.item_count === 1 ? '' : 's'} in, not sent yet`
      : `Link sent ${formatAge(claim.created_at)}, no photos yet`
  const why =
    claim.diary_due && claim.diary_reason
      ? `Diary due ${claim.diary_due}: ${claim.diary_reason}`
      : group === 'capturing'
        ? capturingWhy
        : (claim.headline ?? '—')
  return (
    <tr
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' && e.target === e.currentTarget) onOpen()
      }}
      tabIndex={0}
    >
      <td>
        <div className="ev-person">
          <span className="ev-avatar" aria-hidden="true">
            {initialsOf(holder)}
          </span>
          <div>
            <b>
              <Link to={`/claims/${claim.id}`} onClick={(e) => e.stopPropagation()}>
                {holder}
              </Link>
            </b>
            <span>{claim.vehicle ?? '—'}</span>
          </div>
        </div>
      </td>
      <td>
        <span className="ev-cid">{claim.id}</span>
        <div className="mute" style={{ fontSize: 12 }}>
          {claim.claim_type ?? 'Not chosen yet'}
        </div>
      </td>
      <td>
        <span className={`ev-lane ${lanePillClass(lane)}`}>{lanePillLabel(group === 'decided' ? 'decided' : claim.lane, decidedLabel)}</span>
      </td>
      <td className="ev-why">
        {why}
        {claim.flags > 1 && <small>{claim.flags - 1} more finding{claim.flags > 2 ? 's' : ''}</small>}
      </td>
      <td>
        <DueCell claim={claim} group={group} />
        <div className="mute" style={{ fontSize: 12 }}>
          {formatAge(claim.submitted_at ?? claim.created_at)}
        </div>
      </td>
    </tr>
  )
}

function DueCell({ claim, group }: { claim: QueueClaim; group: string | null }) {
  if (group === 'decided') return <span className="ev-due">Decided</span>
  if (claim.overdue) return <span className="ev-due late">Overdue</span>
  if (claim.review_due) return <span className="ev-due">Due {formatDueDate(claim.review_due)}</span>
  return <span className="ev-due">Waiting</span>
}

function LabelHelp() {
  const pill = (cls: string, t: string) => <span className={`ev-lane ${cls}`}>{t}</span>
  const badge = (cls: string, t: string) => <span className={`ev-sev ${cls}`}>{t}</span>
  const li = (a: ReactNode, b: string, key: string) => (
    <li key={key}>
      {a}
      <span>{b}</span>
    </li>
  )
  return (
    <div className="ev-labelhelp">
      <div>
        <h3>Suggested lane, one per claim</h3>
        <ul>
          {li(pill('siu', 'SIU'), 'Signs of fraud. Send to the special investigations unit.', 'siu')}
          {li(pill('review', 'Adjuster review'), 'Something to confirm before paying.', 'review')}
          {li(pill('standard', 'Standard'), 'Normal handling. Nothing flagged.', 'standard')}
          {li(pill('fast', 'Fast track'), 'All checks passed. Can be paid quickly.', 'fast')}
        </ul>
      </div>
      <div>
        <h3>Where the claim is</h3>
        <ul>
          {li(pill('waiting', 'Waiting for claimant'), 'Link sent. No photos yet.', 'waiting')}
          {li(pill('waiting', 'Analysing'), 'Photos in. Checks run for about 20 seconds.', 'analysing')}
          {li(pill('decided', 'Approved'), 'A person decided. The insurer was told.', 'decided')}
        </ul>
      </div>
      <div>
        <h3>Check result, one per check or finding</h3>
        <ul>
          {li(badge('critical', 'Fail'), 'Failed, for example the VIN does not match.', 'fail')}
          {li(badge('review', 'Check'), 'A person needs to look.', 'check')}
          {li(badge('pass', 'Pass'), 'Passed.', 'pass')}
          {li(badge('note', 'Note'), 'Information only. Nothing to decide.', 'note')}
        </ul>
      </div>
    </div>
  )
}

function weekBuckets(claims: QueueClaim[]) {
  const days: { key: string; label: string }[] = []
  const today = new Date()
  for (let i = 6; i >= 0; i--) {
    const d = new Date(today.getFullYear(), today.getMonth(), today.getDate() - i)
    const key = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`
    days.push({ key, label: d.toLocaleDateString(undefined, { weekday: 'short' }) })
  }
  const lanes = ['fast_track', 'standard', 'adjuster_review', 'siu_referral'] as const
  const data = days.map((d) => ({ ...d, counts: [0, 0, 0, 0] as number[] }))
  for (const c of claims) {
    const at = c.submitted_at ?? c.created_at
    if (!at) continue
    const dt = new Date(at)
    const key = `${dt.getFullYear()}-${String(dt.getMonth() + 1).padStart(2, '0')}-${String(dt.getDate()).padStart(2, '0')}`
    const idx = days.findIndex((d) => d.key === key)
    if (idx < 0) continue
    const laneIdx = lanes.indexOf((c.lane ?? '') as (typeof lanes)[number])
    if (laneIdx < 0) continue
    data[idx].counts[laneIdx] += 1
  }
  const total = data.reduce((n, d) => n + d.counts.reduce((a, b) => a + b, 0), 0)
  return { days: data, total }
}

function WeekBars({ claims }: { claims: QueueClaim[] }) {
  const { days } = weekBuckets(claims)
  const colors = ['var(--fast)', 'var(--std)', 'var(--review)', 'var(--siu)']
  const W = 300
  const H = 160
  const B = 20
  const T = 6
  const max = Math.max(1, ...days.map((d) => d.counts.reduce((a, b) => a + b, 0)))
  const bw = 26
  const step = W / 7
  const y = (v: number) => T + (H - T - B) * (1 - v / Math.max(max, 1))
  let s = ''
  days.forEach((d, i) => {
    const x = step * i + (step - bw) / 2
    const total = d.counts.reduce((a, b) => a + b, 0)
    s += `<rect x="${x}" y="${y(Math.max(max, 1))}" width="${bw}" height="${y(0) - y(Math.max(max, 1))}" rx="${bw / 2}" fill="var(--sunk)"/>`
    s += `<clipPath id="wcp${i}"><rect x="${x}" y="${y(total)}" width="${bw}" height="${y(0) - y(total)}" rx="${bw / 2}"/></clipPath><g clip-path="url(#wcp${i})">`
    let acc = 0
    d.counts.forEach((v, k) => {
      if (!v) return
      s += `<rect x="${x}" y="${y(acc + v)}" width="${bw}" height="${y(acc) - y(acc + v)}" fill="${colors[k]}"/>`
      acc += v
    })
    s += `</g><text x="${x + bw / 2}" y="${y(total) - 5}" text-anchor="middle" style="fill:var(--ink-2)">${total}</text>`
    s += `<text x="${x + bw / 2}" y="${H - 4}" text-anchor="middle">${d.label}</text>`
  })
  return (
    <svg
      className="ev-bars"
      viewBox={`0 0 ${W} ${H}`}
      role="img"
      aria-label="Claims per day this week, split by lane"
      dangerouslySetInnerHTML={{ __html: s }}
    />
  )
}

function Sk({ h, w }: { h: number; w: string }) {
  return <span className="ev-sk" aria-hidden="true" style={{ height: h, width: w }} />
}

function FeedbackPanel({ feedback, failed }: { feedback: FeedbackView | undefined; failed: boolean }) {
  if (failed) return <p className="mute">Could not load the flag numbers.</p>
  if (!feedback)
    return (
      <span aria-hidden="true" style={{ display: 'grid', gap: 8 }}>
        <Sk h={14} w="100%" />
        <Sk h={14} w="85%" />
        <Sk h={14} w="90%" />
      </span>
    )
  const rows = (feedback.rows ?? []).filter((r) => r.total > 0)
  const reviewed = rows.filter((r) => r.confirmed + r.false_alarm > 0)
  if (reviewed.length === 0) return <p className="mute">No flags have been reviewed yet.</p>
  return (
    <div className="ev-confirm">
      {reviewed.slice(0, 5).map((r) => (
        <div key={r.reason_code}>
          <span className="t">
            <b>{r.reason_code}</b>
            <span className="num">
              {r.confirmed} of {r.total}
            </span>
          </span>
          <span className="ev-meter">
            <span style={{ width: `${Math.round((100 * r.confirmed) / Math.max(1, r.total))}%` }} />
          </span>
        </div>
      ))}
    </div>
  )
}
