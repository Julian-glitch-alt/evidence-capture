import { useMemo, useState } from 'react'
import { api, formatDueDate, formatTime, type ClaimDetail, type Damage } from '../api'
import CarDiagram, { type CarAreaState } from './CarDiagram'
import { normalizeArea } from './DamageMap'
import {
  likelyRange,
  lossCauseOf,
  readableSource,
  REVIEW_STEPS,
  REVIEW_STEP_TITLES,
  scrollToAndFlash,
  slotTitles,
  stepOfFlag,
  usd,
  formatAnswer,
  type OrderedFinding,
  type ReviewStep,
} from '../pages/claim/shared'

/** F{n} is the n-th proposal flag in report order: show that flag's display number. */
function refNumber(ref: string, proposalFlags: { key: string; level: string }[], dismissed: Record<string, { verdict: string } | undefined>, byKey: Map<string, number>): { n: number; level: string } | null {
  const m = /^F(\d+)$/.exec(ref.trim())
  if (!m) return null
  const entry = proposalFlags[Number(m[1]) - 1]
  if (!entry) return null
  if (dismissed[entry.key]?.verdict === 'false_alarm') return null
  const n = byKey.get(entry.key)
  if (n == null) return null
  return { n, level: entry.level }
}

function coverageSummary(claim: ClaimDetail): string {
  const parts: string[] = []
  const coverage = claim.coverage
  const source = claim.policy.source ?? (claim.policy.verified ? 'on_file' : null)
  if (source === 'trial') {
    const by = claim.policy.data?.entered_by?.trim()
    parts.push(by ? `Policy ${claim.policy.policy_no}, details entered in the console by ${by}` : `Policy ${claim.policy.policy_no}, details entered in the console`)
  }
  else if (source === 'insurer') parts.push(`Policy ${claim.policy.policy_no} sent by the insurer with the request`)
  else if (source === 'on_file') parts.push(`Policy ${claim.policy.policy_no} on file and verified`)
  else parts.push(`Policy ${claim.policy.policy_no} was not found in the policy system`)
  if (coverage?.type === 'collision') parts.push('Collision coverage')
  else if (coverage?.type === 'comprehensive') parts.push('Comprehensive coverage')
  else parts.push('No coverage type on file')
  parts.push(coverage?.deductible != null ? `$${coverage.deductible.toLocaleString('en-US')} deductible` : 'No deductible on file')
  parts.push(coverage?.lienholder ? `lienholder ${coverage.lienholder}` : 'no lienholder')
  const vin = claim.checks.find((c) => c.check_id === 'photo.vin_photo')
  const plate = claim.checks.find((c) => c.check_id === 'photo.plate')
  if (vin) parts.push(vin.result === 'pass' ? 'The VIN photo matches the policy' : `VIN photo: ${vin.reason}`)
  if (plate) parts.push(plate.result === 'pass' ? 'The plate photo matches the policy' : `Plate photo: ${plate.reason}`)
  return parts.join('. ') + '.'
}

function liabilitySummary(claim: ClaimDetail): string {
  const cause =
    claim.proposal?.status === 'ready'
      ? (claim.layout.loss_types.find((t) => t.id === claim.layout.loss_type)?.label ?? null)
      : null
  const statement = lossCauseOf(claim, cause)
  const police = formatAnswer(claim.answers['police_contacted'])
  return `The claimant says: ${statement}. Police contacted: ${police}.`
}

function damageSummary(claim: ClaimDetail): string {
  const seen = claim.damage?.areas?.length ? claim.damage.areas.join(', ') : 'nothing priced yet'
  const listed = formatAnswer(claim.answers['damaged_areas'])
  return `Seen in the photos: ${seen}. Listed by the claimant: ${listed}.`
}

function estimateSummary(claim: ClaimDetail): string {
  if (!claim.proposal || claim.proposal.status !== 'ready') return 'The proposal is not ready yet.'
  const likely = claim.proposal.estimate.likely
  const range = likely ? `${usd(likely[0])}–${usd(likely[1])}` : 'no priced range'
  const deductible = claim.proposal.deductible != null ? usd(claim.proposal.deductible) : '—'
  const reserve = claim.proposal.suggested_reserve != null ? usd(claim.proposal.suggested_reserve) : '—'
  return `Likely repair ${range}. Deductible ${deductible}. Suggested starting reserve ${reserve}.`
}

const STEP_SUMMARY: Record<ReviewStep, (c: ClaimDetail) => string> = {
  coverage: coverageSummary,
  liability: liabilitySummary,
  damage: damageSummary,
  estimate: estimateSummary,
}

export default function ReviewMemo({
  claim,
  findings,
  selectedArea,
  onSelectArea,
  onUpdate,
}: {
  claim: ClaimDetail
  findings: OrderedFinding[]
  selectedArea: string | null
  onSelectArea: (a: string | null) => void
  onUpdate: (c: ClaimDetail) => void
}) {
  const titles = useMemo(() => slotTitles(claim), [claim])
  const lane = claim.proposal?.status === 'ready' ? claim.proposal.lane : null
  const laneName =
    lane === 'siu_referral'
      ? 'Refer to SIU'
      : lane === 'adjuster_review'
        ? 'Adjuster review'
        : lane === 'fast_track'
          ? 'Fast track'
          : lane === 'standard'
            ? 'Standard review'
            : '—'
  const laneClass =
    lane === 'siu_referral' ? 'siu' : lane === 'adjuster_review' ? 'review' : lane === 'fast_track' ? 'fast' : 'standard'
  const realN = findings.filter((f) => f.entry.level !== 'note').length
  const ai = claim.ai_summary
  const readyAt = claim.report?.created_at ?? claim.seal?.sealed_at ?? claim.submitted_at
  const numberByKey = new Map(findings.map((f) => [f.entry.key, f.n] as const))
  const proposalFlags = claim.proposal?.status === 'ready' ? claim.proposal.flags : []
  const dismissedFlags = claim.review?.flags ?? {}

  return (
    <article className="ev-memo" id="review-memo" aria-label="Review of the claim">
      <div className="ev-runhead">
        <span>Review of claim {claim.id}</span>
        <span>{readyAt ? `Ready ${formatTime(readyAt)}` : 'Not ready yet'}</span>
      </div>
      <h2 className="ev-mtitle">Review</h2>

      <section className="ev-wsec">
        <h3>In short</h3>
        {ai?.status === 'verified' ? (
          <>
            <p className="ev-overview">
              {ai.sentences.map((s, i) => (
                <span key={i}>
                  {s.text}{' '}
                  {s.refs.map((r, j) => {
                    const hit = refNumber(`${r}`, proposalFlags, dismissedFlags, numberByKey)
                    if (!hit) return null
                    return (
                      <sup
                        key={`${i}-${j}-${r}`}
                        className={`ev-cref${hit.level === 'review' ? ' review' : hit.level === 'note' ? ' note' : ''}`}
                      >
                        {hit.n}
                      </sup>
                    )
                  })}
                  {i < ai.sentences.length - 1 ? ' ' : ''}
                </span>
              ))}
            </p>
            <p className="ev-byline">
              {ai.caption ?? 'Muse Spark · sentences checked against the photos'} · Numbers point to the findings below.
            </p>
          </>
        ) : (
          <>
            <p className="ev-overview">
              <code>{claim.summary || 'Summary not ready yet.'}</code>
            </p>
            <p className="ev-byline">The AI summary is not shown because it did not pass the sentence checks.</p>
          </>
        )}
      </section>

      {REVIEW_STEPS.map((step, si) => {
        const inStep = findings.filter((f) => stepOfFlag(f.entry.step) === step)
        const worst = inStep.some((f) => f.entry.level === 'conflict')
          ? 'critical'
          : inStep.some((f) => f.entry.level === 'review')
            ? 'review'
            : 'pass'
        return (
          <section key={step} className="ev-wsec">
            <h3>
              <span>
                {si + 1}.&ensp;{REVIEW_STEP_TITLES[step]}
              </span>
              <span className={`ev-sev ${worst}`}>{worst === 'critical' ? 'Fail' : worst === 'review' ? 'Check' : 'Pass'}</span>
            </h3>
            <p className="ev-rsum">{STEP_SUMMARY[step](claim)}</p>
            {step === 'damage' && <DamageBlock claim={claim} selectedArea={selectedArea} onSelectArea={onSelectArea} onUpdate={onUpdate} />}
            {inStep.length > 0 && (
              <ol className="ev-wfl">
                {inStep.map((f) => (
                  <FindingItem key={f.entry.key} claim={claim} finding={f} titles={titles} onUpdate={onUpdate} />
                ))}
              </ol>
            )}
          </section>
        )
      })}

      <section className="ev-wsec">
        <h3>Suggested next step</h3>
        <div className={`ev-concl ${lane ? laneClass : 'waiting'}`}>
          <small>Suggested by the checks</small>
          <b>{laneName}</b>
          <span>
            {realN > 0 ? `Based on finding${realN === 1 ? ' 1' : `s 1 to ${realN}`}. You make the decision.` : 'Nothing was flagged. You make the decision.'}
          </span>
          <div className="nums">
            <span>
              Likely repair<strong className="num">{likelyRange(claim)}</strong>
            </span>
            <span>
              Evidence<strong>{claim.proposal?.status === 'ready' ? (claim.proposal.evidence?.band ?? '—') : '—'}</strong>
            </span>
            <span>
              Review due<strong>{claim.review_due ? formatDueDate(claim.review_due) : '—'}</strong>
            </span>
          </div>
        </div>
      </section>
    </article>
  )
}

function FindingItem({
  claim,
  finding,
  titles,
  onUpdate,
}: {
  claim: ClaimDetail
  finding: OrderedFinding
  titles: Record<string, string>
  onUpdate: (c: ClaimDetail) => void
}) {
  const { entry, n } = finding
  const [formOpen, setFormOpen] = useState(false)
  const [text, setText] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const record = claim.review?.flags?.[entry.key]
  const locked = claim.decision != null

  const target = (entry.fields ?? []).length > 0 ? `answer-${(entry.fields ?? [])[0]}` : entry.evidence[0] ? `figure-${entry.evidence[0].item_id}` : null

  const save = async (verdict: 'confirmed' | 'false_alarm') => {
    if (verdict === 'false_alarm' && (text.trim().length < 5 || text.trim().length > 500)) {
      setError('A reason of 5–500 characters is required.')
      return
    }
    setSaving(true)
    setError(undefined)
    try {
      const body =
        verdict === 'false_alarm'
          ? { kind: 'flag' as const, key: entry.key, verdict, reason: text.trim() }
          : { kind: 'flag' as const, key: entry.key, verdict }
      onUpdate(await api.review(claim.id, body))
      setFormOpen(false)
      setText('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the review')
    } finally {
      setSaving(false)
    }
  }

  const evidenceLine = () => {
    const bits: string[] = [readableSource(entry.source, titles)]
    for (const e of entry.evidence) {
      const label = titles[e.slot_id] ?? e.slot_id
      bits.push(typeof e.number === 'number' ? `${label} #${e.number}` : label)
    }
    return bits.join(' · ')
  }

  return (
    <li className="ev-wf">
      <span className={`ev-fnum ${entry.level}`}>{n}</span>
      <div>
        <p className="ev-wft">{entry.reason}</p>
        <p className="ev-wfe">{evidenceLine()}</p>
        <div className="ev-wfa">
          {target ? (
            <button type="button" className="ev-linkbtn press" onClick={() => scrollToAndFlash(target)}>
              <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" aria-hidden="true">
                <path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12z" />
                <circle cx="12" cy="12" r="2.6" />
              </svg>
              Show in the form
            </button>
          ) : (
            <span />
          )}
          {entry.level === 'note' ? (
            <span className="ev-sev note">Information only</span>
          ) : (
            <span className="ev-choice" role="group" aria-label={`Your review of finding ${n}`}>
              {!locked && (
                <>
                  {record?.verdict !== 'confirmed' && (
                    <button
                      type="button"
                      className="yes press"
                      aria-pressed={record?.verdict === 'confirmed'}
                      onClick={() => void save('confirmed')}
                      disabled={saving}
                    >
                      Confirm
                    </button>
                  )}
                  {record?.verdict !== 'false_alarm' &&
                    (formOpen ? (
                      <span style={{ display: 'grid', gap: 6 }}>
                        <label style={{ display: 'grid', gap: 4, fontSize: 12 }}>
                          Why is this a false alarm?
                          <input
                            value={text}
                            onChange={(e) => setText(e.target.value)}
                            placeholder="e.g. Odometer read is clear in Close-up #1"
                            style={{ font: 'inherit', border: '1.5px solid var(--line)', borderRadius: 8, padding: '6px 8px' }}
                          />
                        </label>
                        {error && (
                          <span role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                            {error}
                          </span>
                        )}
                        <span style={{ display: 'flex', gap: 6 }}>
                          <button type="button" className="press" disabled={saving} onClick={() => void save('false_alarm')} style={{ fontSize: 12, fontWeight: 700 }}>
                            {saving ? 'Saving…' : 'Save false alarm'}
                          </button>
                          <button type="button" className="press" onClick={() => setFormOpen(false)} style={{ fontSize: 12, fontWeight: 700 }}>
                            Cancel
                          </button>
                        </span>
                      </span>
                    ) : (
                      <button
                        type="button"
                        className="no press"
                        aria-pressed={record?.verdict === 'false_alarm'}
                        onClick={() => setFormOpen(true)}
                      >
                        False alarm
                      </button>
                    ))}
                  {record?.verdict === 'false_alarm' && (
                    <button type="button" className="press" onClick={() => void save('confirmed')} disabled={saving} style={{ fontSize: 12, fontWeight: 700 }}>
                      Undo
                    </button>
                  )}
                </>
              )}
            </span>
          )}
        </div>
        {record && (
          <p className="ev-wfe">
            {record.verdict === 'false_alarm' ? `False alarm: ${record.reason ?? ''} · ${record.by}` : `Confirmed by ${record.by}`}
          </p>
        )}
      </div>
    </li>
  )
}

function DamageBlock({
  claim,
  selectedArea,
  onSelectArea,
  onUpdate,
}: {
  claim: ClaimDetail
  selectedArea: string | null
  onSelectArea: (a: string | null) => void
  onUpdate: (c: ClaimDetail) => void
}) {
  const damages = useMemo(() => claim.damage?.damages ?? [], [claim])
  const excluded = useMemo(() => {
    const out = new Set<string>()
    for (const [key, entry] of Object.entries(claim.review?.damages ?? {})) {
      if (entry.verdict === 'excluded') out.add(key)
    }
    return out
  }, [claim.review?.damages])
  const declared = useMemo(() => {
    const v = claim.answers['damaged_areas']
    return Array.isArray(v) ? v.filter((x): x is string => typeof x === 'string') : []
  }, [claim.answers])

  const states = useMemo(() => {
    const out: Record<string, CarAreaState> = {}
    const seenCount = new Map<string, number>()
    for (const d of damages) {
      const key = normalizeArea(d.area)
      seenCount.set(key, (seenCount.get(key) ?? 0) + 1)
    }
    for (const key of new Set([...seenCount.keys(), ...declared.map(normalizeArea)])) {
      out[key] = { seen: seenCount.get(key) ?? 0, declared: declared.some((a) => normalizeArea(a) === key) }
    }
    return out
  }, [damages, declared])

  const markers = useMemo(() => {
    const out: Record<string, string> = {}
    damages.forEach((d, i) => {
      const key = normalizeArea(d.area)
      const label = String(d.number > 0 ? d.number : i + 1)
      out[key] = out[key] ? `${out[key]}, ${label}` : label
    })
    return out
  }, [damages])

  if (damages.length === 0) return null
  return (
    <div style={{ display: 'grid', gap: 10, marginTop: 8, fontFamily: 'var(--sans)' }}>
      <CarDiagram
        states={states}
        markers={markers}
        active={selectedArea}
        onArea={(area) => onSelectArea(selectedArea != null && normalizeArea(selectedArea) === normalizeArea(area) ? null : area)}
      />
      <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6, fontSize: 12.5 }}>
        {damages.map((d, i) => (
          <DamageLine key={d.key} damage={d} index={i} claim={claim} excluded={excluded.has(d.key)} onUpdate={onUpdate} />
        ))}
      </ul>
    </div>
  )
}

function DamageLine({
  damage,
  index,
  claim,
  excluded,
  onUpdate,
}: {
  damage: Damage
  index: number
  claim: ClaimDetail
  excluded: boolean
  onUpdate: (c: ClaimDetail) => void
}) {
  const [open, setOpen] = useState(false)
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const n = damage.number > 0 ? damage.number : index + 1
  const entry = claim.review?.damages?.[damage.key]

  const save = async (verdict: 'excluded' | 'included') => {
    if (verdict === 'excluded' && (reason.trim().length < 5 || reason.trim().length > 500)) {
      setError('A reason of 5–500 characters is required to take damage out of this loss.')
      return
    }
    setSaving(true)
    setError(undefined)
    try {
      const body =
        verdict === 'excluded'
          ? { kind: 'damage' as const, key: damage.key, verdict, reason: reason.trim() }
          : { kind: 'damage' as const, key: damage.key, verdict }
      onUpdate(await api.review(claim.id, body))
      setOpen(false)
      setReason('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save the review')
    } finally {
      setSaving(false)
    }
  }

  return (
    <li style={{ display: 'flex', gap: 8, flexWrap: 'wrap', alignItems: 'baseline', justifyContent: 'space-between' }}>
      <span style={excluded ? { textDecoration: 'line-through', color: 'var(--mute)' } : undefined}>
        #{n} {damage.area} · {damage.damage_type} · {damage.severity} · {damage.age === 'old' ? 'Possibly old' : damage.age === 'new' ? 'New' : 'Unsure'}
      </span>
      {!claim.decision &&
        (excluded && entry ? (
          <span>
            Excluded{entry.reason ? `: ${entry.reason}` : ''} · {entry.by}{' '}
            <button type="button" className="ev-linkbtn press" onClick={() => void save('included')}>
              Include again
            </button>
          </span>
        ) : open ? (
          <span style={{ display: 'grid', gap: 4 }}>
            <label style={{ display: 'grid', gap: 4, fontSize: 12 }}>
              Why is this not part of this loss?
              <input
                value={reason}
                onChange={(e) => setReason(e.target.value)}
                placeholder="e.g. Hood scratches are old wear"
                style={{ font: 'inherit', border: '1.5px solid var(--line)', borderRadius: 8, padding: '6px 8px' }}
              />
            </label>
            {error && (
              <span role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {error}
              </span>
            )}
            <span style={{ display: 'flex', gap: 6 }}>
              <button type="button" className="ev-linkbtn press" disabled={saving} onClick={() => void save('excluded')}>
                {saving ? 'Saving…' : 'Exclude from estimate'}
              </button>
              <button type="button" className="ev-linkbtn press" onClick={() => setOpen(false)}>
                Cancel
              </button>
            </span>
          </span>
        ) : (
          <button type="button" className="ev-linkbtn press" onClick={() => setOpen(true)}>
            Exclude from estimate
          </button>
        ))}
    </li>
  )
}
