import { useEffect, useMemo, useState } from 'react'
import {
  api,
  formatDueDate,
  formatTime,
  packageUrl,
  reportDraftUrl,
  todayIso,
  type ClaimDetail,
} from '../api'
import Timeline from './Timeline'
import { scrollToAndFlash, usd, type OrderedFinding } from '../pages/claim/shared'

type Action = 'approve' | 'edit' | 'sendback' | 'siu' | 'field' | null

function fullLaneLabel(lane: string | null) {
  if (lane === 'siu_referral') return 'Refer to SIU'
  if (lane === 'adjuster_review') return 'Adjuster review'
  if (lane === 'fast_track') return 'Fast track'
  return 'Standard'
}

export default function DecisionCard({
  claim,
  findings,
  onUpdate,
}: {
  claim: ClaimDetail
  findings: OrderedFinding[]
  onUpdate: (c: ClaimDetail) => void
}) {
  const proposal = claim.proposal
  const [action, setAction] = useState<Action>(null)
  const [guard, setGuard] = useState(false)
  const [reason, setReason] = useState('')
  const [amount, setAmount] = useState('')
  const [siuReason, setSiuReason] = useState('')
  const [fieldReason, setFieldReason] = useState('')
  const [message, setMessage] = useState('')
  const [slots, setSlots] = useState<string[]>([])
  const [decideError, setDecideError] = useState<string>()
  const [sending, setSending] = useState(false)

  const reviewable = useMemo(() => findings.filter((f) => f.entry.level !== 'note'), [findings])
  const reviewed = useMemo(() => {
    const r = claim.review?.flags ?? {}
    return reviewable.filter((f) => r[f.entry.key]?.verdict === 'confirmed' || r[f.entry.key]?.verdict === 'false_alarm').length
  }, [reviewable, claim.review?.flags])
  const unreviewed = reviewable.length - reviewed

  const photoSlots = useMemo(
    () => claim.layout.sections.flatMap((s) => s.slots).filter((s) => s.kind === 'photo'),
    [claim.layout.sections],
  )

  if (!proposal || proposal.status !== 'ready') return null

  if (claim.decision) {
    const d = claim.decision
    return (
      <div className="ev-card">
        <h2>Your decision</h2>
        <p>
          <strong>Decided: {d.label}</strong> by {d.decided_by}, {formatTime(d.decided_at)}. Locked.
        </p>
        <p className="mute" style={{ fontSize: 12.5 }}>
          Lane: {fullLaneLabel(d.lane_before)} → {fullLaneLabel(d.lane_after)}
          {' · '}Suggested initial reserve: {d.reserve_before !== null ? usd(d.reserve_before) : '—'}
          {' → '}
          {d.reserve_after !== null ? usd(d.reserve_after) : '—'}
        </p>
        {d.reason && <p style={{ fontSize: 12.5 }}>Reason: {d.reason}</p>}
        {d.slots.length > 0 && (
          <p className="mute" style={{ fontSize: 12.5 }}>
            Photos requested: {d.slots.map((s) => photoSlots.find((p) => p.id === s)?.title ?? s).join(', ')}
          </p>
        )}
        <DiaryCard claim={claim} onUpdate={onUpdate} />
      </div>
    )
  }

  const phone = claim.contact?.phone ?? '—'
  const due = claim.review_due ? formatDueDate(claim.review_due) : '—'

  const open = (key: Exclude<Action, null>) => {
    setAction(action === key ? null : key)
    setDecideError(undefined)
    if (key === 'sendback') {
      const named = new Set<string>()
      for (const f of findings) {
        for (const e of f.entry.evidence) {
          if (photoSlots.some((s) => s.id === e.slot_id)) named.add(e.slot_id)
        }
      }
      setSlots(photoSlots.filter((s) => named.has(s.id)).map((s) => s.id))
      setMessage('')
    }
    if (key === 'siu') {
      const critical = findings.filter((f) => f.entry.level === 'conflict').map((f) => f.entry.reason)
      const rest = findings.filter((f) => f.entry.level === 'review').map((f) => f.entry.reason)
      setSiuReason([...critical, ...rest].join('. ') || '')
    }
    if (key === 'edit') {
      setAmount(proposal.suggested_reserve != null ? String(proposal.suggested_reserve) : '')
      setReason('')
    }
    if (key === 'approve') setReason('')
    if (key === 'field') setFieldReason('')
  }

  const confirm = async () => {
    if (!action) return
    setSending(true)
    setDecideError(undefined)
    try {
      if (action === 'approve') {
        const trimmed = reason.trim()
        if (proposal.flags.length > 0 && trimmed.length < 5) {
          setDecideError('A reason of at least 5 characters is required.')
          return
        }
        if (trimmed.length > 500) {
          setDecideError('The reason is too long.')
          return
        }
        const body: { action: string; reason?: string } = { action: 'approve' }
        if (trimmed) body.reason = trimmed
        onUpdate(await api.decide(claim.id, body))
      } else if (action === 'edit') {
        const trimmed = reason.trim()
        if (trimmed.length < 5) {
          setDecideError('A reason of at least 5 characters is required.')
          return
        }
        if (trimmed.length > 500) {
          setDecideError('The reason is too long.')
          return
        }
        const reserve = amount.trim() === '' ? NaN : Number(amount.trim())
        if (!Number.isInteger(reserve) || reserve < 0) {
          setDecideError('The approved amount must be a whole number of dollars.')
          return
        }
        onUpdate(
          await api.decide(claim.id, { action: 'edit_approve', lane: proposal.lane, suggested_reserve: reserve, reason: trimmed }),
        )
      } else if (action === 'sendback') {
        const trimmed = message.trim()
        if (slots.length === 0) {
          setDecideError('Pick the photos to retake.')
          return
        }
        if (trimmed.length < 5 || trimmed.length > 500) {
          setDecideError('A message of 5–500 characters is required.')
          return
        }
        onUpdate(await api.decide(claim.id, { action: 'send_back', slots, reason: trimmed }))
      } else if (action === 'siu') {
        const trimmed = siuReason.trim()
        if (trimmed.length < 5) {
          setDecideError('A reason of at least 5 characters is required.')
          return
        }
        if (trimmed.length > 500) {
          setDecideError('The reason is too long.')
          return
        }
        onUpdate(await api.decide(claim.id, { action: 'refer_siu', reason: trimmed }))
      } else if (action === 'field') {
        const trimmed = fieldReason.trim()
        if (trimmed.length < 5) {
          setDecideError('A reason of at least 5 characters is required.')
          return
        }
        if (trimmed.length > 500) {
          setDecideError('The reason is too long.')
          return
        }
        onUpdate(await api.decide(claim.id, { action: 'field_inspection', reason: trimmed }))
      }
      setAction(null)
      setGuard(false)
    } catch (e) {
      setDecideError(e instanceof Error ? e.message : 'Could not record the decision')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="ev-card">
      <h2>
        Your decision <small>{reviewed} of {reviewable.length} reviewed</small>
      </h2>
      <p className="ev-activity">
        Activity: review verified evidence, due {due}. Claimant {phone}.
      </p>
      {guard && unreviewed > 0 && (
        <div className="ev-guard" role="status">
          <span>
            <b>{unreviewed} finding{unreviewed === 1 ? ' is' : 's are'} not reviewed yet.</b>
          </span>
          <span className="acts">
            <button type="button" className="ev-pill sm press" onClick={() => scrollToAndFlash('review-memo')}>
              Review findings
            </button>
            <button type="button" className="ev-pill sm primary press" onClick={() => open('approve')}>
              Approve anyway
            </button>
          </span>
        </div>
      )}
      <div className="ev-decidebox">
        <button
          type="button"
          className="ev-pill primary press"
          onClick={() => {
            if (unreviewed > 0 && !guard) {
              setGuard(true)
              return
            }
            open('approve')
          }}
        >
          Approve
        </button>
        <div className="two">
          <button type="button" className="ev-pill press" aria-expanded={action === 'edit'} onClick={() => open('edit')}>
            Edit and approve
          </button>
          <button type="button" className="ev-pill press" aria-expanded={action === 'sendback'} onClick={() => open('sendback')}>
            Send back
          </button>
        </div>
        {action === 'edit' && (
          <div className="ev-expand">
            <label>
              Approved amount
              <input
                type="text"
                value={amount}
                onChange={(e) => setAmount(e.target.value)}
                inputMode="numeric"
                aria-label="Approved amount"
              />
            </label>
            <label>
              Reason
              <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} aria-label="Reason" placeholder="What did you change and why?" />
            </label>
            {decideError && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {decideError}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className="ev-pill sm primary press" disabled={sending} onClick={() => void confirm()}>
                {sending ? 'Confirming…' : 'Approve at this amount'}
              </button>
              <button type="button" className="ev-pill sm press" onClick={() => setAction(null)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {action === 'sendback' && (
          <div className="ev-expand">
            <span style={{ fontSize: 12.5, fontWeight: 600 }}>Photos to take again</span>
            <div className="ticks">
              {photoSlots.map((slot) => (
                <label key={slot.id}>
                  <input
                    type="checkbox"
                    checked={slots.includes(slot.id)}
                    onChange={(e) => setSlots(e.target.checked ? [...slots, slot.id] : slots.filter((s) => s !== slot.id))}
                  />
                  {slot.title}
                </label>
              ))}
            </div>
            <label>
              Message to the claimant
              <textarea value={message} onChange={(e) => setMessage(e.target.value)} rows={2} aria-label="Message to the claimant" />
            </label>
            {decideError && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {decideError}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className="ev-pill sm primary press" disabled={sending} onClick={() => void confirm()}>
                {sending ? 'Sending…' : 'Send back to the claimant'}
              </button>
              <button type="button" className="ev-pill sm press" onClick={() => setAction(null)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        <div className="two">
          <button type="button" className="ev-pill danger press" aria-expanded={action === 'siu'} onClick={() => open('siu')}>
            Refer to SIU
          </button>
          <button type="button" className="ev-pill press" aria-expanded={action === 'field'} onClick={() => open('field')}>
            Field inspection
          </button>
        </div>
        {action === 'siu' && (
          <div className="ev-expand">
            <label>
              Reason for the referral
              <textarea value={siuReason} onChange={(e) => setSiuReason(e.target.value)} rows={3} aria-label="Reason for the referral" />
            </label>
            {decideError && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {decideError}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button
                type="button"
                className="ev-pill sm primary press"
                disabled={sending || siuReason.trim().length < 5}
                onClick={() => void confirm()}
              >
                {sending ? 'Referring…' : 'Refer to SIU with this reason'}
              </button>
              <button type="button" className="ev-pill sm press" onClick={() => setAction(null)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {action === 'field' && (
          <div className="ev-expand">
            <label>
              Reason for the inspection
              <textarea value={fieldReason} onChange={(e) => setFieldReason(e.target.value)} rows={2} aria-label="Reason for the inspection" />
            </label>
            {decideError && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {decideError}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className="ev-pill sm primary press" disabled={sending} onClick={() => void confirm()}>
                {sending ? 'Requesting…' : 'Request field inspection'}
              </button>
              <button type="button" className="ev-pill sm press" onClick={() => setAction(null)}>
                Cancel
              </button>
            </div>
          </div>
        )}
        {action === 'approve' && (
          <div className="ev-expand">
            {proposal.flags.length > 0 && (
              <label>
                Reason
                <textarea value={reason} onChange={(e) => setReason(e.target.value)} rows={2} aria-label="Reason" placeholder="This claim has findings — what did you check?" />
              </label>
            )}
            {decideError && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {decideError}
              </p>
            )}
            <div style={{ display: 'flex', gap: 8 }}>
              <button type="button" className="ev-pill sm primary press" disabled={sending} onClick={() => void confirm()}>
                {sending ? 'Confirming…' : 'Confirm approval'}
              </button>
              <button type="button" className="ev-pill sm press" onClick={() => setAction(null)}>
                Cancel
              </button>
            </div>
          </div>
        )}
      </div>
      <DiaryCard claim={claim} onUpdate={onUpdate} />
    </div>
  )
}

export function DiaryCard({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [dueOn, setDueOn] = useState(todayIso())
  const [reason, setReason] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const diaries = claim.diaries ?? []
  const open = diaries.filter((d) => d.done_at == null)

  const save = async () => {
    setSaving(true)
    setError(undefined)
    try {
      await api.createDiary(claim.id, { due_on: dueOn, reason: reason.trim() })
      onUpdate(await api.claim(claim.id))
      setReason('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not set the diary')
    } finally {
      setSaving(false)
    }
  }

  const done = async (id: string) => {
    setError(undefined)
    try {
      await api.completeDiary(id)
      onUpdate(await api.claim(claim.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not mark the diary done')
    }
  }

  return (
    <div className="ev-diary">
      <label htmlFor={`diary-date-${claim.id}`}>Follow-up date</label>
      <div className="ev-drow">
        <input
          id={`diary-date-${claim.id}`}
          type="date"
          value={dueOn}
          min={todayIso()}
          onChange={(e) => setDueOn(e.target.value)}
          aria-label="Follow-up date"
        />
        <input
          className="reason"
          type="text"
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          placeholder="Call about the police report"
          aria-label="Reason"
        />
        <button type="button" className="ev-pill sm press" disabled={saving} onClick={() => void save()}>
          {saving ? 'Setting…' : 'Set diary'}
        </button>
      </div>
      {error && (
        <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
          {error}
        </p>
      )}
      {open.length > 0 && (
        <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 6, fontSize: 12.5 }}>
          {open.map((d) => (
            <li key={d.id} style={{ display: 'flex', gap: 8, alignItems: 'baseline', justifyContent: 'space-between' }}>
              <span>
                {d.due_on}: {d.reason}
              </span>
              <button type="button" className="ev-linkbtn press" onClick={() => void done(d.id)}>
                Mark done
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

export function NotesCard({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [text, setText] = useState('')
  const [error, setError] = useState<string>()
  const [saving, setSaving] = useState(false)
  const notes = claim.review?.notes ?? []

  const save = async () => {
    const v = text.trim()
    if (v.length < 5 || v.length > 1000) {
      setError('Notes are 5–1000 characters.')
      return
    }
    setSaving(true)
    setError(undefined)
    try {
      onUpdate(await api.review(claim.id, { kind: 'note', text: v }))
      setText('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not add the note')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="ev-card">
      <h2>Notes</h2>
      <div className="ev-notes">
        {notes.length > 0 && (
          <ul style={{ listStyle: 'none', margin: 0, padding: 0, display: 'grid', gap: 8 }}>
            {notes.map((n, i) => (
              <li key={i} style={{ background: 'var(--sunk)', borderRadius: 10, padding: '8px 12px', fontSize: 12.5 }}>
                <p style={{ margin: 0 }}>{n.text}</p>
                <p className="mute" style={{ margin: '4px 0 0', fontSize: 11.5 }}>
                  {n.by} · {formatTime(n.at)}
                </p>
              </li>
            ))}
          </ul>
        )}
        {!claim.decision && (
          <>
            <label htmlFor={`note-${claim.id}`} style={{ fontSize: 12.5, fontWeight: 700 }}>
              Add a note
            </label>
            <textarea
              id={`note-${claim.id}`}
              value={text}
              onChange={(e) => setText(e.target.value)}
              rows={2}
              placeholder="What should the next adjuster know?"
              aria-label="Add a note"
            />
            {error && (
              <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
                {error}
              </p>
            )}
            <div>
              <button type="button" className="ev-pill sm press" disabled={saving} onClick={() => void save()}>
                {saving ? 'Saving…' : 'Save note'}
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  )
}

export function ReportFilesCard({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [sending, setSending] = useState(false)
  const [error, setError] = useState<string>()
  const report = claim.report
  const message = [...(report?.outbox ?? [])].reverse().find((m) => m.event === 'decision.recorded' || m.event === 'evidence.ready')

  const resend = async () => {
    setSending(true)
    setError(undefined)
    try {
      await api.deliver(claim.id)
      onUpdate(await api.claim(claim.id))
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Resend failed')
    } finally {
      setSending(false)
    }
  }

  return (
    <div className="ev-card">
      <h2>Report files</h2>
      {!report ? (
        <p className="mute" style={{ fontSize: 12.5 }}>
          The report is prepared when the analysis finishes.
        </p>
      ) : (
        <div className="ev-filerow">
          <a className="ev-file press" href={reportDraftUrl(report.url)} target="_blank" rel="noreferrer">
            <span><b>Original report</b><span className="mute">Made {formatTime(report.created_at)}, sent to the insurer</span></span>
          </a>
          {report.reviewed_sha256 ? (
            <a className="ev-file press" href={report.url} target="_blank" rel="noreferrer">
              <span><b>Reviewed report</b><span className="mute">With the review and decision</span></span>
            </a>
          ) : (
            <button type="button" className="ev-file muted" disabled aria-label="Reviewed report, made when you decide">
              <span><b>Reviewed report</b><span className="mute">Made when you decide</span></span>
            </button>
          )}
          <a className="ev-file press" href={packageUrl(claim.id)}>
            <span><b>Sealed package</b><span className="mute">Anyone can check it on Verify</span></span>
          </a>
          <a className="ev-file press" href="/verify" target="_blank" rel="noreferrer">
            <span><b>Verify this package</b><span className="mute">Check the seal in your browser</span></span>
          </a>
          <p className="mute" style={{ fontSize: 12 }}>
            {message
              ? message.delivered_at
                ? `Delivered ${formatTime(message.delivered_at)}`
                : `Not delivered: ${message.last_error ?? 'pending'} (${message.attempts} attempts)`
              : 'Not delivered yet'}
          </p>
          <div style={{ display: 'flex', gap: 8, alignItems: 'center' }}>
            <button type="button" onClick={() => void resend()} disabled={sending} className="ev-pill sm press">
              {sending ? 'Resending…' : 'Resend'}
            </button>
            {error && <span style={{ fontSize: 12, color: 'var(--siu)' }}>{error}</span>}
          </div>
        </div>
      )}
    </div>
  )
}

export function MoreActionsCard({ claim, onUpdate }: { claim: ClaimDetail; onUpdate: (c: ClaimDetail) => void }) {
  const [meRole, setMeRole] = useState<string | null>(null)
  const [timelineOpen, setTimelineOpen] = useState(false)
  const [delOpen, setDelOpen] = useState(false)
  const [delReason, setDelReason] = useState('')
  const [delError, setDelError] = useState<string>()
  const [delBusy, setDelBusy] = useState(false)
  const [resendBusy, setResendBusy] = useState(false)
  const [resendError, setResendError] = useState<string>()

  useEffect(() => {
    api.me().then((u) => setMeRole(u.role)).catch(() => undefined)
  }, [])

  const resend = async () => {
    setResendBusy(true)
    setResendError(undefined)
    try {
      await api.deliver(claim.id)
      onUpdate(await api.claim(claim.id))
    } catch (e) {
      setResendError(e instanceof Error ? e.message : 'Resend failed')
    } finally {
      setResendBusy(false)
    }
  }

  const confirmDelete = async () => {
    const trimmed = delReason.trim()
    if (trimmed.length < 5 || trimmed.length > 500) {
      setDelError('A reason of 5–500 characters is required.')
      return
    }
    setDelBusy(true)
    setDelError(undefined)
    try {
      await api.deleteMedia(claim.id, trimmed)
      onUpdate(await api.claim(claim.id))
      setDelOpen(false)
      setDelReason('')
    } catch (e) {
      setDelError(e instanceof Error ? e.message : 'Could not delete the media')
    } finally {
      setDelBusy(false)
    }
  }

  return (
    <div className="ev-card">
      <h2>More actions</h2>
      <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
        {claim.report && (
          <button type="button" className="ev-pill sm press" disabled={resendBusy} onClick={() => void resend()}>
            {resendBusy ? 'Resending…' : 'Resend the report'}
          </button>
        )}
        {!claim.removal && claim.submitted_at && meRole === 'supervisor' && (
          <button type="button" className="ev-pill sm press" aria-expanded={delOpen} onClick={() => setDelOpen((v) => !v)}>
            Delete media
          </button>
        )}
        <button type="button" className="ev-pill sm press" aria-expanded={timelineOpen} onClick={() => setTimelineOpen((v) => !v)}>
          {timelineOpen ? 'Hide the timeline' : 'Show the timeline'}
        </button>
      </div>
      {resendError && (
        <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
          {resendError}
        </p>
      )}
      {delOpen && (
        <form
          style={{ display: 'grid', gap: 8 }}
          onSubmit={(e) => {
            e.preventDefault()
            void confirmDelete()
          }}
        >
          <label style={{ display: 'grid', gap: 4, fontSize: 12.5, fontWeight: 700 }}>
            Why is the media being deleted?
            <input
              value={delReason}
              onChange={(e) => setDelReason(e.target.value)}
              placeholder="e.g. The claimant asked us to delete their photos."
              aria-label="Why is the media being deleted?"
              style={{ font: 'inherit', fontWeight: 400, border: '1.5px solid var(--line-strong)', borderRadius: 10, padding: '8px 10px' }}
            />
          </label>
          {delError && (
            <p role="alert" style={{ fontSize: 12, color: 'var(--siu)' }}>
              {delError}
            </p>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            <button type="submit" disabled={delBusy} className="ev-pill sm primary press">
              {delBusy ? 'Deleting…' : 'Confirm delete'}
            </button>
            <button type="button" className="ev-pill sm press" onClick={() => setDelOpen(false)}>
              Cancel
            </button>
          </div>
        </form>
      )}
      {timelineOpen && <Timeline events={claim.events} />}
    </div>
  )
}
